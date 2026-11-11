# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every REST call that touches a session message. The games channel is refused before any REST call."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, notify, render, rsvp, sessions, text
from fluxer_panel.cards import Card
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.interactions import GamesChannelRefused
from fluxer_panel.logs import log_event
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.rsvp import Counts
from fluxer_panel.sessions import Session

log = logging.getLogger(__name__)


class SessionPost:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, pacer: ChannelPacer,
                 backoff: DmBackoff | None = None) -> None:
        self._db = db
        self._rest = rest
        self._config = config
        self._pacer = pacer
        self._backoff = backoff   # shared with the press replies; promotion DMs bypass it (AC7.2)
        self._edit_locks: dict[int, asyncio.Lock] = {}

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

    async def edit_session(self, session_id: int, *, scrubbed: bool = False) -> None:
        """Edits the stored message in the session's own stored channel, rendering the session's state as it is in
        the DB at send time. Raises FluxerHTTPError to the caller."""
        async with self.edit_lock(session_id):
            loaded = await self._load(session_id)
            if loaded is None:
                return
            session, card, counts = loaded
            if session.channel_id == self._config.games_channel_id:
                raise GamesChannelRefused
            payload = render.session_message(
                card, session, counts,
                effective_location=sessions.effective_location(session, card),
                effective_seats=sessions.effective_seats(session, card),
                instance_url=self._config.instance_url,
                scrubbed=scrubbed,
            )
            await self._rest.edit_message(session.channel_id, session.message_id, embeds=payload["embeds"],
                                          allowed_mentions=payload["allowed_mentions"])

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
        """The purge's scrub: edits the message to its closed form with no location. Raises on failure."""
        await self.edit_session(session.id, scrubbed=True)

    async def dm_promoted(self, session: Session, card: Card, user_ids: list[str]) -> None:
        """One promotion DM per user, in order, each with a jump link to the session's stored message. Shared by the RSVP
        path, the reconcile and the seats command. A promotion DM never consults the DM backoff (AC7.2)."""
        title = render.escape(card.title)
        if self._config.instance_url:
            link = render.jump_link(text.SESSION_LINK_LABEL, self._config.instance_url, session.guild_id,
                                    session.channel_id, session.message_id)
            content = text.SESSION_PROMOTED.format(card=title, link=link)
        else:
            content = text.SESSION_PROMOTED_NO_LINK.format(card=title)
        for user_id in user_ids:
            await notify.dm_user(self._rest, user_id, content, backoff=self._backoff, bypass=True)
