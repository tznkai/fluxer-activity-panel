# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every incoming message passes through here before `admin.handle`. This is a security boundary: it is the only path by
which a non-admin's DM reaches any command code.

The order is fixed. A guild message goes to `admin.handle`, which ignores what is not a command and refuses the rest. A
bot author or a non-snowflake author goes there too, and is refused and logged as before. Then `help` or `!help` gets
the list of command heads, and a `!session` or a `!poll` command takes the rate-limited non-admin path. An admin head (`!admin`, `!card`, `!request`) goes to `admin.handle`,
which authorizes it. Anything else goes to the session flow, when that user has a draft; failing that, to the poll flow,
when that user has an open poll ballot. Both are rate limited the same way."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from fluxer_client.events import MessageCreate
from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerREST
from fluxer_panel import admin, text
from fluxer_panel.card_commands import CardContext
from fluxer_panel.config import Config
from fluxer_panel.logs import log_event
from fluxer_panel.polls import Poll

log = logging.getLogger(__name__)

NON_ADMIN_LIMIT = 5      # DM commands, replies and DM reactions per window, per non-admin user
NON_ADMIN_WINDOW = 60.0  # seconds

CommandHandler = Callable[[str, MessageCreate], Awaitable[None]]   # (argument string, event)


class DraftReplies(Protocol):
    """The part of the session flow the router needs: whether a user has an open draft, and the reply to it."""

    def has_draft(self, user_id: str) -> bool: ...

    async def on_reply(self, event: MessageCreate) -> None: ...


class PollReplies(Protocol):
    """The part of the poll flow the router needs: the open polls where a user has a ballot (one lookup per DM), and
    the reply, given those polls."""

    async def open_ballots(self, user_id: str) -> Sequence[Poll]: ...

    async def on_reply(self, event: MessageCreate, open_polls: Sequence[Poll]) -> None: ...


class NonAdminLimiter:
    """5 DM commands, replies or DM reactions per 60 s per non-admin user. Admins are exempt. A refusal is logged with no
    user id, and the caller drops the event silently."""

    def __init__(self, config: Config, *, clock: Callable[[], float] = time.time) -> None:
        self._config = config
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep: float | None = None

    def admit(self, user_id: str) -> bool:
        if user_id in self._config.admin_user_ids:
            return True
        now = self._clock()
        self._sweep(now)
        hits = self._hits.get(user_id)
        if hits is None:
            hits = self._hits[user_id] = deque()
        cutoff = now - NON_ADMIN_WINDOW
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= NON_ADMIN_LIMIT:
            log_event(log, "dm_rate_limited")
            return False
        hits.append(now)
        return True

    def _sweep(self, now: float) -> None:
        """Forgets every user whose newest hit is outside the window, at most once per window. Without it each user who
        ever sent a command would keep an entry for the life of the process. A forgotten user has no hits left in the
        window, so the next admit behaves exactly as it would have."""
        if self._last_sweep is not None and now - self._last_sweep < NON_ADMIN_WINDOW:
            return
        self._last_sweep = now
        cutoff = now - NON_ADMIN_WINDOW
        for user_id in [uid for uid, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[user_id]


def _head(content: str, prefix: str, word: str) -> str | None:
    """The argument string after '<prefix><word>' when the message is that command, else None. The same rule as
    `admin.parse_head`: the command word must be the whole message or be followed by a space."""
    text = content.strip()
    full = f"{prefix}{word}"
    if text == full:
        return ""
    if text.startswith(full + " "):
        return text[len(full):].strip()
    return None


def _is_help(content: str, prefix: str) -> bool:
    """`help` or `<prefix>help` as the whole message, in any case. `help me` or `!help session` is not help."""
    word = content.strip().lower()
    return word in ("help", f"{prefix}help".lower())


class DmRouter:
    def __init__(self, *, config: Config, rest: FluxerREST, card_ctx: CardContext | None, limiter: NonAdminLimiter,
                 session_flow: DraftReplies | None = None, session_commands: CommandHandler | None = None,
                 poll_flow: PollReplies | None = None, poll_commands: CommandHandler | None = None) -> None:
        self._config = config
        self._rest = rest
        self._card_ctx = card_ctx
        self._limiter = limiter
        self._session_flow = session_flow
        self._session_commands = session_commands
        self._poll_flow = poll_flow
        self._poll_commands = poll_commands

    async def _help(self, event: MessageCreate) -> None:
        """The command heads, with the admin heads only for an admin. Never sent to a guild channel: `route` hands a
        guild message to `admin.handle` before it gets here."""
        content = text.HELP_TOP
        if admin.authorize(event, self._config):
            content = f"{content}\n{text.HELP_ADMIN}"
        try:
            await self._rest.send_message(event.channel_id, content=content, allowed_mentions={"parse": []})
        except Exception as exc:  # noqa: BLE001 - a failed help reply must not escape the gateway dispatch
            log_event(log, "help_failed", level=logging.WARNING, reason=type(exc).__name__)
            return
        log_event(log, "help_sent")

    async def route(self, event: MessageCreate) -> None:
        if event.guild_id is not None:
            await admin.handle(event, self._config, self._rest, self._card_ctx)   # unchanged guild behaviour
            return
        if event.author_is_bot or not is_snowflake(event.author_id):
            await admin.handle(event, self._config, self._rest, self._card_ctx)   # refused and logged, as before
            return
        if _is_help(event.content, self._config.prefix):
            if not self._limiter.admit(event.author_id):
                return
            await self._help(event)
            return
        head = _head(event.content, self._config.prefix, "session")
        if head is not None:
            if not self._limiter.admit(event.author_id):
                return
            if self._session_commands is not None:
                await self._session_commands(head, event)
            return
        poll_head = _head(event.content, self._config.prefix, "poll")
        if poll_head is not None:
            if not self._limiter.admit(event.author_id):
                return
            if self._poll_commands is not None:
                await self._poll_commands(poll_head, event)
            return
        if admin.parse_head(event.content, self._config.prefix) is not None:
            await admin.handle(event, self._config, self._rest, self._card_ctx)   # admin heads: unchanged
            return
        flow = self._session_flow
        if flow is not None and flow.has_draft(event.author_id):
            if not self._limiter.admit(event.author_id):
                return
            await flow.on_reply(event)
            return
        polls = self._poll_flow
        if polls is not None:
            open_polls = await polls.open_ballots(event.author_id)
            if open_polls:
                if not self._limiter.admit(event.author_id):
                    return
                await polls.on_reply(event, open_polls)
