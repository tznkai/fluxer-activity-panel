# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every incoming message passes through here before `admin.handle`. This is a security boundary: it is the only path by
which a non-admin's DM reaches any command code.

The order is fixed. A guild message goes to `admin.handle`, which ignores what is not a command and refuses the rest. A
bot author or a non-snowflake author goes there too, and is refused and logged as before. Then a `!session` command
takes the rate-limited non-admin path. An admin head (`!admin`, `!card`, `!request`) goes to `admin.handle`, which
authorizes it. Anything else goes to the session flow, when that user has a draft, and is rate limited the same way."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Protocol

from fluxer_client.events import MessageCreate
from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerREST
from fluxer_panel import admin
from fluxer_panel.card_commands import CardContext
from fluxer_panel.config import Config
from fluxer_panel.logs import log_event

log = logging.getLogger(__name__)

NON_ADMIN_LIMIT = 5      # DM commands, replies and DM reactions per window, per non-admin user
NON_ADMIN_WINDOW = 60.0  # seconds

SessionHandler = Callable[[str, MessageCreate], Awaitable[None]]   # (argument string, event)


class DraftReplies(Protocol):
    """The part of the session flow the router needs: whether a user has an open draft, and the reply to it."""

    def has_draft(self, user_id: str) -> bool: ...

    async def on_reply(self, event: MessageCreate) -> None: ...


class NonAdminLimiter:
    """5 DM commands, replies or DM reactions per 60 s per non-admin user. Admins are exempt. A refusal is logged with no
    user id, and the caller drops the event silently."""

    def __init__(self, config: Config, *, clock: Callable[[], float] = time.time) -> None:
        self._config = config
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def admit(self, user_id: str) -> bool:
        if user_id in self._config.admin_user_ids:
            return True
        now = self._clock()
        hits = self._hits.setdefault(user_id, deque())
        cutoff = now - NON_ADMIN_WINDOW
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= NON_ADMIN_LIMIT:
            log_event(log, "dm_rate_limited")
            return False
        hits.append(now)
        return True


def _session_head(content: str, prefix: str) -> str | None:
    """The argument string after '<prefix>session' when the message is that command, else None. The same rule as
    `admin.parse_head`: the command word must be the whole message or be followed by a space."""
    text = content.strip()
    full = f"{prefix}session"
    if text == full:
        return ""
    if text.startswith(full + " "):
        return text[len(full):].strip()
    return None


class DmRouter:
    def __init__(self, *, config: Config, rest: FluxerREST, card_ctx: CardContext | None, limiter: NonAdminLimiter,
                 session_flow: DraftReplies | None = None, session_commands: SessionHandler | None = None) -> None:
        self._config = config
        self._rest = rest
        self._card_ctx = card_ctx
        self._limiter = limiter
        self._session_flow = session_flow
        self._session_commands = session_commands

    async def route(self, event: MessageCreate) -> None:
        if event.guild_id is not None:
            await admin.handle(event, self._config, self._rest, self._card_ctx)   # unchanged guild behaviour
            return
        if event.author_is_bot or not is_snowflake(event.author_id):
            await admin.handle(event, self._config, self._rest, self._card_ctx)   # refused and logged, as before
            return
        head = _session_head(event.content, self._config.prefix)
        if head is not None:
            if not self._limiter.admit(event.author_id):
                return
            if self._session_commands is not None:
                await self._session_commands(head, event)
            return
        if admin.parse_head(event.content, self._config.prefix) is not None:
            await admin.handle(event, self._config, self._rest, self._card_ctx)   # admin heads: unchanged
            return
        flow = self._session_flow
        if flow is not None and flow.has_draft(event.author_id):
            if not self._limiter.admit(event.author_id):
                return
            await flow.on_reply(event)
