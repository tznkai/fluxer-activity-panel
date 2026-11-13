# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`!poll` in DMs. Reached only through dm_router, never from a guild channel.

A poll is named by its number or its message link. `vote` is a ballot reply, so it needs no starter. The four changes
(sort, shown, extend, close) need the poll's starter or an admin. Each is one compare-and-set transaction under the
card's lock, and the poll is read again inside that transaction, so a change to a poll that is no longer open is
refused. The poll message and ballots are edited after the lock is released. `close` goes through PollCloser, which
owns the close path."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fluxer_client.events import MessageCreate
from fluxer_client.rest import FluxerREST
from fluxer_panel import admin, cards, poll_count, polls, render, text
from fluxer_panel.card_commands import CardLocks, chunks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.logs import log_event
from fluxer_panel.poll_close import PollCloser
from fluxer_panel.poll_flow import PollFlow
from fluxer_panel.poll_post import PollPost

log = logging.getLogger(__name__)

_NUMBER = re.compile(r"[0-9]{1,10}")
_MESSAGE_LINK = re.compile(r"https?://[^/\s]+/channels/([0-9]{1,25})/([0-9]{1,25})/([0-9]{1,25})/?")
_SHOWN = re.compile(r"[0-9]{1,2}")
_DURATION = re.compile(r"([1-9][0-9]{0,3})([mhd])", re.IGNORECASE)
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}
SHOWN_MIN = 1
SHOWN_MAX = 20
_CHANGES = frozenset({"sort", "shown", "extend", "close"})

# A reply and the outcome that is logged with it: "done", or a refusal key that names no user input.
_Reply = tuple[str, str]


def _sort_tx(conn: Any, poll_id: int, now: int) -> bool:
    """Ranks the options by approval, then soonest, and re-letters them, in one transaction with the read. False when
    the poll is not open."""
    poll = polls.get(conn, poll_id)
    if poll is None or poll.state != "open":
        return False
    options = polls.options(conn, poll_id)
    votes = polls.votes(conn, poll_id)
    order = [option_id for option_id, _count in
             poll_count.approval(options, [(v.user_id, v.option_id) for v in votes])]
    return polls.sort_if_open(conn, poll_id, order, now=now)


def _extend_tx(conn: Any, poll_id: int, seconds: int, cap: int) -> tuple[str, int | None]:
    """Moves closes_at by `seconds`, read in this transaction. The cap is measured from creation. Returns the outcome
    ("done", "cap" or "closed") and the new closes_at when it is done."""
    poll = polls.get(conn, poll_id)
    if poll is None or poll.state != "open":
        return "closed", None
    new = poll.closes_at + seconds
    if new - poll.created_at > cap:
        return "cap", None
    if not polls.extend_if_open(conn, poll_id, closes_at=new):
        return "closed", None
    return "done", new


def _listing(conn: Any, *, user_id: str, everyone: bool) -> list[tuple[polls.Poll, str]]:
    """The polls a user may list, newest first (by poll id, which is assigned in creation order), with each card's
    title. A member sees the polls they started or were sent (open and closed). An admin also sees every open poll."""
    found = {poll.id: poll for poll in polls.for_user(conn, user_id)}
    if everyone:
        found.update({poll.id: poll for poll in polls.open_polls(conn)})
    rows: list[tuple[polls.Poll, str]] = []
    for poll in sorted(found.values(), key=lambda p: p.id, reverse=True):
        card = cards.get_by_id(conn, poll.card_id)
        rows.append((poll, card.title if card is not None else ""))
    return rows


class PollCommands:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, locks: CardLocks, poll_post: PollPost,
                 poll_flow: PollFlow, closer: PollCloser, clock: Callable[[], float] = time.time,
                 kick: Callable[[], None] = lambda: None) -> None:
        self._db = db
        self._rest = rest
        self._config = config
        self._locks = locks
        self._poll_post = poll_post
        self._flow = poll_flow
        self._closer = closer
        self._clock = clock
        self._kick = kick
        self._handlers: dict[str, Callable[[polls.Poll, str], Awaitable[_Reply]]] = {
            "sort": self._sort, "shown": self._shown, "extend": self._extend, "close": self._close,
        }

    def _now(self) -> int:
        return int(self._clock())

    async def _say(self, event: MessageCreate, content: str) -> None:
        await self._rest.send_message(event.channel_id, content=content, allowed_mentions={"parse": []})

    def _log(self, sub: str, outcome: str, poll_id: int | None = None) -> None:
        if poll_id is None:
            log_event(log, "poll_command", command=f"poll_{sub}", outcome=outcome)
        else:
            log_event(log, "poll_command", command=f"poll_{sub}", outcome=outcome, poll_id=poll_id)

    async def handle(self, args: str, event: MessageCreate) -> None:
        if event.guild_id is not None:   # never answered in a guild channel
            return
        parts = args.split(maxsplit=2)
        sub = parts[0].lower() if parts else ""
        if sub in ("", "help"):
            await self._say(event, text.POLL_USAGE)
            return
        if sub == "list":
            if len(parts) > 1:
                await self._say(event, text.POLL_USAGE)
                return
            await self._list(event)
            return
        if sub != "vote" and sub not in _CHANGES:
            await self._say(event, text.POLL_USAGE)
            return
        ref = parts[1] if len(parts) > 1 else ""
        value = parts[2].strip() if len(parts) > 2 else ""
        if not ref or (sub == "vote" and not value):
            await self._say(event, text.POLL_USAGE)
            return
        poll = await self._resolve(ref)
        if poll is None:
            await self._say(event, text.POLL_UNKNOWN)
            self._log(sub, "unknown")
            return
        if sub == "vote":
            # The flow answers in this DM and does its own logging. A closed poll or a non-recipient is refused there.
            await self._flow.vote(event.author_id, poll.id, value, reply_channel_id=event.channel_id)
            return
        if not self._may_change(poll, event):
            await self._say(event, text.POLL_NOT_ALLOWED)
            self._log(sub, "not_allowed", poll.id)
            return
        if poll.state != "open":
            await self._say(event, text.POLL_NOT_OPEN)
            self._log(sub, "not_open", poll.id)
            return
        reply, outcome = await self._handlers[sub](poll, value)
        await self._say(event, reply)
        self._log(sub, outcome, poll.id)

    def _may_change(self, poll: polls.Poll, event: MessageCreate) -> bool:
        return event.author_id == poll.starter_id or admin.authorize(event, self._config)

    async def _resolve(self, ref: str) -> polls.Poll | None:
        """A poll number, or a message link whose message id is the poll's. The ids are digits only, from the regex,
        before any lookup."""
        if _NUMBER.fullmatch(ref):
            poll_id = int(ref)
            return await self._db.run(lambda conn: polls.get(conn, poll_id))
        link = _MESSAGE_LINK.fullmatch(ref)
        if link is not None:
            message_id = link.group(3)
            return await self._db.run(lambda conn: polls.get_by_message(conn, message_id))
        return None

    async def _list(self, event: MessageCreate) -> None:
        everyone = admin.authorize(event, self._config)
        rows = await self._db.run(lambda conn: _listing(conn, user_id=event.author_id, everyone=everyone))
        lines = [self._line(poll, title) for poll, title in rows]
        if not lines:
            await self._say(event, text.POLL_LIST_EMPTY)
            self._log("list", "empty")
            return
        for chunk in chunks(lines):
            await self._say(event, chunk)
        self._log("list", "done")

    def _line(self, poll: polls.Poll, title: str) -> str:
        if poll.state == "open":
            when = text.POLL_LIST_OPEN_AT.format(ts=poll.closes_at)
        else:
            when = text.POLL_LIST_CLOSED_AT.format(ts=poll.closed_at or poll.closes_at)
        link = ""
        if self._config.instance_url:
            link = text.POLL_LIST_LINK.format(link=render.jump_link(
                text.POLL_LINK_LABEL, self._config.instance_url, poll.guild_id, poll.channel_id, poll.message_id))
        return text.POLL_LIST_LINE.format(n=poll.id, card=render.escape(title), state=text.POLL_STATE[poll.state],
                                          when=when, link=link)

    # Changes. Each returns the reply and its logged outcome. A change the transaction refuses (the poll is no longer
    # open) returns the not-open reply, so a change that lost a race is told as a refusal.

    async def _sort(self, poll: polls.Poll, _value: str) -> _Reply:
        now = self._now()
        async with self._locks.get(poll.card_id):
            done = await self._db.run(lambda conn: _sort_tx(conn, poll.id, now))
        if not done:
            return text.POLL_NOT_OPEN, "not_open"
        self._poll_post.changed(poll.id)
        return text.POLL_SORTED, "done"

    async def _shown(self, poll: polls.Poll, value: str) -> _Reply:
        if not value:
            return text.POLL_USAGE, "usage"
        if not _SHOWN.fullmatch(value) or not SHOWN_MIN <= int(value) <= SHOWN_MAX:
            return text.POLL_SHOWN_RANGE, "range"
        size = int(value)
        async with self._locks.get(poll.card_id):
            done = await self._db.run(lambda conn: polls.set_pending_ballot_size_if_open(conn, poll.id, size))
        if not done:
            return text.POLL_NOT_OPEN, "not_open"
        return text.POLL_SHOWN_PENDING.format(n=size), "done"

    async def _extend(self, poll: polls.Poll, value: str) -> _Reply:
        if value:
            match = _DURATION.fullmatch(value)
            if match is None:
                return text.POLL_USAGE, "usage"
            seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]
        else:
            seconds = self._config.poll_length_seconds
        cap = self._config.poll_max_length_seconds
        async with self._locks.get(poll.card_id):
            outcome, new = await self._db.run(lambda conn: _extend_tx(conn, poll.id, seconds, cap))
        if outcome == "closed":
            return text.POLL_NOT_OPEN, "not_open"
        if outcome == "cap" or new is None:
            return text.POLL_EXTEND_CAP, "cap"
        self._poll_post.changed(poll.id)
        self._kick()
        return text.POLL_EXTENDED.format(ts=new), "done"

    async def _close(self, poll: polls.Poll, _value: str) -> _Reply:
        closed = await self._closer.close(poll.id)
        if closed is None:
            return text.POLL_NOT_OPEN, "not_open"
        return text.POLL_CLOSED_DONE, "done"
