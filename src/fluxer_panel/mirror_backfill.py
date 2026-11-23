# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The back-fill of a both copy's mirrors, run when the copy enters both (decision 5). The poller never back-fills, so a
mirror that is lost on a 404 stays gone until the copy enters both again.

The caller holds the card's lock. The back-fill posts and stores the mirrors under it, and returns the session mirrors
it posted; the caller then seeds their reactions with `seed` after the lock is released, since a reaction call is never
made under the lock. The poll mirror is posted without ballots: ballots go out only when a poll opens."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from fluxer_panel import cards, polls, sessions, subscriptions
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.logs import log_event
from fluxer_panel.mirror_post import MirrorPost
from fluxer_panel.poll_post import mirror_payload, session_jump
from fluxer_panel.session_post import SessionPost
from fluxer_panel.subscriptions import Subscription

log = logging.getLogger(__name__)


class MirrorBackfill:
    def __init__(self, *, db: Database, config: Config, session_post: SessionPost, mirror_post: MirrorPost) -> None:
        self._db = db
        self._config = config
        self._session_post = session_post
        self._mirror_post = mirror_post

    async def backfill_locked(self, card: cards.Card, sub_id: int) -> list[tuple[str, str]]:
        """Mirrors the card's upcoming home sessions, then its open home poll, into one both copy. The copy is read
        again here, so its local channel is the one stored in this command. Returns (channel id, message id) of each
        session mirror this call posted, for `seed`. A failure of one object is logged by type; the rest still run.
        """
        sub = await self._db.run(lambda c: subscriptions.get_by_id(c, sub_id))
        if sub is None or sub.mode != "both" or sub.paused:
            return []
        channel_id = await self._db.run(lambda c: self._mirror_post.target(c, card, sub))
        if channel_id is None:
            return []
        upcoming = await self._db.run(lambda c: sessions.upcoming_for_scope(c, card.id, None))
        seeds: list[tuple[str, str]] = []
        for session in upcoming:
            try:
                posted = await self._session_post.back_fill_mirror(session.id, sub, channel_id)
            except Exception as exc:  # noqa: BLE001 - one session's mirror must not stop the others or the command
                self._log_failed(sub, "session", session.id, exc)
                continue
            if posted is not None:
                seeds.append(posted)
        try:
            await self._poll_mirror(card, sub, channel_id)
        except Exception as exc:  # noqa: BLE001 - the poll mirror is best effort; the command's own change stands
            self._log_failed(sub, "poll", None, exc)
        return seeds

    async def seed(self, seeds: Sequence[tuple[str, str]]) -> None:
        """Seeds the RSVP reactions of each session mirror posted by `backfill_locked`. Runs after the card lock."""
        for channel_id, message_id in seeds:
            try:
                await self._session_post.seed_session(channel_id, message_id)
            except Exception as exc:  # noqa: BLE001 - a missing seed is logged; users can still react
                log_event(log, "mirror_seed_failed", level=logging.WARNING, channel_id=channel_id,
                          message_id=message_id, reason=type(exc).__name__)

    async def _poll_mirror(self, card: cards.Card, sub: Subscription, channel_id: str) -> None:
        """Posts or edits the open home poll's mirror in one copy, with its session link when the poll is scheduled."""
        def read(conn) -> tuple[int, dict] | None:
            poll = polls.open_for_scope(conn, card.id, None)
            if poll is None:
                return None
            view = polls.view(conn, poll.id)
            if view is None:
                return None
            jump = None if poll.session_id is None else session_jump(conn, poll.session_id, sub.id)
            return poll.id, mirror_payload(view, instance_url=self._config.instance_url, session_jump=jump)

        found = await self._db.run(read)
        if found is None:
            return
        poll_id, payload = found
        await self._mirror_post.sync(sub=sub, channel_id=channel_id, kind="poll", object_id=poll_id, payload=payload)

    @staticmethod
    def _log_failed(sub: Subscription, kind: str, object_id: int | None, exc: Exception) -> None:
        log_event(log, "mirror_backfill_failed", level=logging.WARNING, sub_id=sub.id, kind=kind,
                  object_id=object_id, reason=type(exc).__name__)
