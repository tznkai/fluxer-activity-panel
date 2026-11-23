# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every REST call that touches a session message. The games channel is refused before any REST call."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_panel import cards, mirrors, notify, render, rsvp, sessions, subscriptions, text
from fluxer_panel.cards import Card
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.games_guard import GamesChannelRefused
from fluxer_panel.logs import log_event
from fluxer_panel.mirror_post import MirrorPost
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.rsvp import Counts
from fluxer_panel.sessions import Session

log = logging.getLogger(__name__)


def session_link_for(conn: sqlite3.Connection, session: Session, user_id: str) -> tuple[str, str, str] | None:
    """The (guild id, channel id, message id) a session DM to this user links to (decision 3). A home user, or anyone
    with no far origin, gets the home message. A far user gets the mirror in the subscription they came through, in that
    subscription's community. None when that mirror or subscription is gone, so the DM carries no link."""
    via = sessions.presence_via(conn, session.id, user_id)
    if via is None:
        return session.guild_id, session.channel_id, session.message_id
    mirror = mirrors.get(conn, "session", session.id, via)
    sub = subscriptions.get_by_id(conn, via)
    if mirror is None or sub is None:
        return None
    return sub.guild_id, mirror.channel_id, mirror.message_id


class SessionPost:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, pacer: ChannelPacer,
                 backoff: DmBackoff | None = None, mirrors: MirrorPost | None = None) -> None:
        self._db = db
        self._rest = rest
        self._config = config
        self._pacer = pacer
        self._backoff = backoff   # shared with the press replies; promotion DMs bypass it (AC7.2)
        self._mirrors = mirrors   # the both-subscription copies of a home session; None leaves them unmirrored
        self._edit_locks: dict[int, asyncio.Lock] = {}
        self._on_gone: Callable[[int], Awaitable[None]] | None = None

    async def send_session(self, channel_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if channel_id == self._config.games_channel_id:
            raise GamesChannelRefused
        return await self._rest.send_message(channel_id, embeds=payload["embeds"],
                                             allowed_mentions=payload["allowed_mentions"])

    def edit_lock(self, session_id: int) -> asyncio.Lock:
        """One lock per session. Every edit of a session message, from any path, renders and sends under it."""
        return self._edit_locks.setdefault(session_id, asyncio.Lock())

    async def _load(self, session_id: int) -> tuple[Session, Card, Counts] | None:
        """Re-reads the session, its card and its counts in one db.run. Never trust a caller's snapshot. None when the
        session or its card is gone."""
        def read(conn: sqlite3.Connection) -> tuple[Session, Card, Counts] | None:
            session = sessions.get(conn, session_id)
            if session is None:
                return None
            card = cards.get_by_id(conn, session.card_id)
            if card is None:
                return None
            return session, card, sessions.counts_of(conn, session_id)

        return await self._db.run(read)

    async def edit_session(self, session_id: int, *, scrubbed: bool = False, mirror_new: bool = False) -> None:
        """Edits the stored message in the session's own stored channel, rendering the session's state as it is in
        the DB at send time. Then the mirrors (`sync_mirrors`), which edit the stored ones and post none: a mirror is
        posted only when its session is published (`sync_mirrors`) or its copy enters both (`back_fill_mirror`), so a
        mirror the copy lost on a 404 stays gone (decision 5). `mirror_new=True` posts the missing ones too. Raises
        FluxerHTTPError to the caller."""
        async with self.edit_lock(session_id):
            loaded = await self._load(session_id)
            if loaded is None:
                return
            session, card, counts = loaded
            if session.channel_id == self._config.games_channel_id:
                raise GamesChannelRefused
            payload = self._render(card, session, counts, scrubbed=scrubbed)
            try:
                await self._rest.edit_message(session.channel_id, session.message_id, embeds=payload["embeds"],
                                              allowed_mentions=payload["allowed_mentions"])
            except FluxerHTTPError as exc:
                if exc.status == 404:
                    await self._note_gone(session_id)
                raise
            await self.sync_mirrors(session_id, scrubbed=scrubbed, mirror_new=mirror_new)

    def _render(self, card: Card, session: Session, counts: Counts, *, scrubbed: bool = False,
                far: bool = False) -> dict[str, Any]:
        """The session message for the home post (`far=False`) or for a both copy's mirror (`far=True`)."""
        return render.session_message(
            card, session, counts,
            effective_location=sessions.effective_location(session, card),
            effective_seats=sessions.effective_seats(session, card),
            instance_url=self._config.instance_url,
            scrubbed=scrubbed, far=far,
        )

    async def sync_mirrors(self, session_id: int, *, scrubbed: bool = False, mirror_new: bool = True) -> None:
        """Pushes the home session's current state into the mirror of every active both copy of its card (AC3.3). A
        no-op for a session that a both scope started (AC3.7) and when no MirrorPost is wired. A failure is logged and
        never raised: the home action that calls this has already happened."""
        if self._mirrors is None:
            return
        try:
            await self._sync_mirrors(session_id, scrubbed=scrubbed, mirror_new=mirror_new)
        except Exception as exc:  # noqa: BLE001 - the mirrors are best effort; the home object is already right
            log_event(log, "mirror_sync_failed", level=logging.WARNING, session_id=session_id,
                      reason=type(exc).__name__)

    async def _sync_mirrors(self, session_id: int, *, scrubbed: bool, mirror_new: bool) -> None:
        assert self._mirrors is not None
        loaded = await self._load(session_id)
        if loaded is None:
            return
        session, card, counts = loaded
        if session.scope_sub_id is not None:
            return
        targets = await self._db.run(lambda c: self._mirrors.targets(c, card))
        stored = await self._db.run(lambda c: {m.sub_id for m in mirrors.for_object(c, "session", session.id)})
        payload = self._render(card, session, counts, scrubbed=scrubbed, far=True)
        for sub, channel_id in targets:
            if sub.id not in stored and not mirror_new:
                continue
            message_id = await self._mirrors.sync(sub=sub, channel_id=channel_id, kind="session",
                                                  object_id=session.id, payload=payload)
            if message_id is not None and sub.id not in stored:
                await self.seed_session(channel_id, message_id)

    async def back_fill_mirror(self, session_id: int, sub: subscriptions.Subscription, channel_id: str
                               ) -> tuple[str, str] | None:
        """Brings one both copy's mirror of an upcoming home session up to date, for the back-fill that runs under the
        card's lock when the copy enters both. It edits a stored mirror and posts none without one, so the reply's
        reactions are never sent under the lock. Returns (channel, message id) when this call posted the mirror, so the
        caller seeds it after the lock is released, and None otherwise."""
        if self._mirrors is None:
            return None
        loaded = await self._load(session_id)
        if loaded is None:
            return None
        session, card, counts = loaded
        if session.scope_sub_id is not None:
            return None
        stored = await self._db.run(lambda c: mirrors.get(c, "session", session.id, sub.id))
        payload = self._render(card, session, counts, far=True)
        message_id = await self._mirrors.sync(sub=sub, channel_id=channel_id, kind="session", object_id=session.id,
                                              payload=payload)
        if stored is None and message_id is not None:
            return channel_id, message_id
        return None

    def set_on_gone(self, callback: Callable[[int], Awaitable[None]]) -> None:
        """Registers the hook for a session whose message was deleted (a 404 on its edit). Called with the session id,
        only while the session is still upcoming. The hook must never take a card lock."""
        self._on_gone = callback

    async def _note_gone(self, session_id: int) -> None:
        """Runs the deleted-message hook for an upcoming session. The state is read again here, after the failed edit, so
        a session that ended or was cancelled meanwhile is not reported. A failing hook is logged and does not replace the
        edit's own failure, which the caller still gets."""
        if self._on_gone is None:
            return
        loaded = await self._load(session_id)
        if loaded is None or loaded[0].state != "upcoming":
            return
        try:
            await self._on_gone(session_id)
        except Exception as exc:  # noqa: BLE001 - the edit's failure is what the caller must see
            log_event(log, "session_gone_hook_failed", level=logging.WARNING, session_id=session_id,
                      reason=type(exc).__name__)

    async def dm_attendees(self, session: Session, content: str, *, exclude: str | None) -> int:
        """One DM to each going and waiting user, except `exclude`. Bypasses the DM backoff (AC7.2). Returns how many
        DMs were attempted."""
        attendees = await self._db.run(lambda conn: sessions.attendees(conn, session.id))
        targets = [user_id for user_id in attendees if user_id != exclude]
        for user_id in targets:
            await notify.dm_user(self._rest, user_id, content, backoff=self._backoff, bypass=True)
        return len(targets)

    async def seed_session(self, channel_id: str, message_id: str) -> None:
        """Adds ✅ ❔ ❌ in that order. Not gated by the kill switch: RSVP reactions are state, not presses."""
        if channel_id == self._config.games_channel_id:
            raise GamesChannelRefused
        for emoji in rsvp.RSVP_EMOJI:
            await self._pacer.wait(channel_id)
            try:
                await self._rest.add_reaction(channel_id, message_id, emoji)
            except Exception as exc:  # noqa: BLE001 - a missing seed is logged; users can still react
                log_event(log, "session_seed_failed", level=logging.WARNING, channel_id=channel_id,
                          reason=type(exc).__name__)
                return

    async def delete_unstored(self, channel_id: str, message_id: str) -> None:
        """Deletes a posted message whose store failed. Never raises (as interactions._discard_unstored). The games
        channel is refused before any REST call, and that refusal is logged, not raised."""
        if channel_id == self._config.games_channel_id:
            log_event(log, "session_discard_failed", level=logging.WARNING, channel_id=channel_id,
                      reason="GamesChannelRefused")
            return
        try:
            await self._rest.delete_message(channel_id, message_id)
        except Exception as exc:  # noqa: BLE001 - a failed delete is logged; the caller is not told
            log_event(log, "session_discard_failed", level=logging.WARNING, channel_id=channel_id,
                      reason=type(exc).__name__)

    async def scrub(self, session: Session) -> None:
        """The purge's scrub: edits every stored mirror to its closed form with no location, then the home message. The
        mirrors go first, so no copy outlives the rows with the location in it. Raises on a home failure only."""
        await self._scrub_mirrors(session.id)
        await self.edit_session(session.id, scrubbed=True)

    async def _scrub_mirrors(self, session_id: int) -> None:
        if self._mirrors is None:
            return
        loaded = await self._load(session_id)
        if loaded is None:
            return
        session, card, counts = loaded
        rows = await self._db.run(lambda c: mirrors.for_object(c, "session", session_id))
        payload = self._render(card, session, counts, scrubbed=True, far=True)
        for row in rows:
            try:
                await self._mirrors.edit_stored(row, payload)
            except Exception as exc:  # noqa: BLE001 - a copy that cannot be scrubbed is logged; the rest still run
                log_event(log, "mirror_scrub_failed", level=logging.WARNING, session_id=session_id, mirror_id=row.id,
                          reason=type(exc).__name__)

    async def link_for(self, session: Session, user_id: str) -> str | None:
        """The masked jump link a session DM to this user carries: the home message, or for a far user their mirror (see
        `session_link_for`). None when there is no instance URL or no link to give."""
        if not self._config.instance_url:
            return None
        jump = await self._db.run(lambda c: session_link_for(c, session, user_id))
        if jump is None:
            return None
        return render.jump_link(text.SESSION_LINK_LABEL, self._config.instance_url, *jump)

    async def dm_attendees_linked(self, session: Session, *, exclude: str | None,
                                  content_for: Callable[[str | None], str]) -> int:
        """Like `dm_attendees`, but each attendee's DM is built from the link it carries: `content_for` gets the user's
        `link_for` (None when there is none). Bypasses the DM backoff (AC7.2). Returns how many DMs were attempted."""
        attendees = await self._db.run(lambda c: sessions.attendees(c, session.id))
        targets = [user_id for user_id in attendees if user_id != exclude]
        for user_id in targets:
            link = await self.link_for(session, user_id)
            await notify.dm_user(self._rest, user_id, content_for(link), backoff=self._backoff, bypass=True)
        return len(targets)

    async def dm_promoted(self, session: Session, card: Card, user_ids: list[str]) -> None:
        """One promotion DM per user, in order, each with the jump link its user is given (`link_for`): the home message,
        or the mirror for a far user. Shared by the RSVP path, the reconcile and the seats command. A promotion DM never
        consults the DM backoff (AC7.2)."""
        title = render.escape(card.title)
        for user_id in user_ids:
            link = await self.link_for(session, user_id)
            if link is None:
                content = text.SESSION_PROMOTED_NO_LINK.format(card=title)
            else:
                content = text.SESSION_PROMOTED.format(card=title, link=link)
            await notify.dm_user(self._rest, user_id, content, backoff=self._backoff, bypass=True)
