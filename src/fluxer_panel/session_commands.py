# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`!session` in DMs, for proposers and admins. Reached only through dm_router, never from a guild channel.

A change is one compare-and-set write under the card's lock, and the session is read again inside that same
transaction, so nothing decided before the lock is trusted as current. The message edit and the DMs run after the lock
is released. The two exceptions are `cancel_card_sessions_locked`, which the archive calls with the lock already held,
and `on_message_gone`, which takes no card lock at all (asyncio locks are not re-entrant)."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from fluxer_client.events import MessageCreate
from fluxer_client.rest import FluxerREST
from fluxer_panel import admin, card_fields, cards, render, rsvp, session_time, sessions, text
from fluxer_panel.card_commands import CardLocks, chunks
from fluxer_panel.cards import Card, CardFieldError
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.logs import log_event
from fluxer_panel.session_post import SessionPost
from fluxer_panel.sessions import Session

log = logging.getLogger(__name__)

_NUMBER = re.compile(r"[0-9]{1,10}")
_MESSAGE_LINK = re.compile(r"https?://[^/\s]+/channels/([0-9]{1,25})/([0-9]{1,25})/([0-9]{1,25})/?")
_CHANGES = frozenset({"cancel", "move", "repeat", "location", "seats"})

# A reply and the outcome that is logged with it: "done", or a refusal key that names no user input.
_Reply = tuple[str, str]


def _local(now: float, zone: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(now, zone)


def _counts_text(counts: rsvp.Counts) -> str:
    line = text.SESSION_COUNTS.format(going=counts.going, maybe=counts.maybe)
    if counts.waiting > 0:
        line += text.SESSION_COUNTS_WAITING.format(waiting=counts.waiting)
    return line


def _read(conn, session_id: int) -> tuple[Session, Card] | None:
    session = sessions.get(conn, session_id)
    if session is None:
        return None
    card = cards.get_by_id(conn, session.card_id)
    if card is None:
        return None
    return session, card


def _cancel_tx(conn, session_id: int, now: int) -> tuple[Session, Card] | None:
    if not sessions.cancel_if(conn, session_id, now):
        return None
    return _read(conn, session_id)


def _cancelled_text(session: Session, card: Card) -> str:
    return text.SESSION_CANCELLED_DM.format(card=render.escape(card.title), ts=session.starts_at)


class SessionCommands:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, locks: CardLocks,
                 session_post: SessionPost, clock: Callable[[], float] = time.time,
                 kick: Callable[[], None] = lambda: None) -> None:
        self._db = db
        self._rest = rest
        self._config = config
        self._locks = locks
        self._session_post = session_post
        self._clock = clock
        self._kick = kick

    def _now(self) -> int:
        return int(self._clock())

    async def _say(self, event: MessageCreate, content: str) -> None:
        await self._rest.send_message(event.channel_id, content=content, allowed_mentions={"parse": []})

    def _log(self, sub: str, outcome: str, session_id: int | None = None) -> None:
        if session_id is None:
            log_event(log, "session_command", command=f"session_{sub}", outcome=outcome)
        else:
            log_event(log, "session_command", command=f"session_{sub}", outcome=outcome, session_id=session_id)

    async def handle(self, args: str, event: MessageCreate) -> None:
        if event.guild_id is not None:   # never answered in a guild channel (AC6.5)
            return
        parts = args.split(maxsplit=2)
        sub = parts[0].lower() if parts else ""
        if sub in ("", "help"):
            await self._say(event, text.SESSION_USAGE)
            return
        if sub == "list":
            if len(parts) > 1:
                await self._say(event, text.SESSION_USAGE)
                return
            await self._list(event)
            return
        if sub not in _CHANGES:
            await self._say(event, text.SESSION_USAGE)
            return
        ref = parts[1] if len(parts) > 1 else ""
        value = parts[2].strip() if len(parts) > 2 else ""
        if not ref:
            await self._say(event, text.SESSION_USAGE)
            return
        session = await self._resolve(ref)
        if session is None:
            await self._say(event, text.SESSION_UNKNOWN)
            self._log(sub, "unknown")
            return
        if not self._may_change(session, event):
            await self._say(event, text.SESSION_NOT_ALLOWED)
            self._log(sub, "not_allowed", session.id)
            return
        if session.state != "upcoming":
            await self._say(event, text.SESSION_NOT_UPCOMING)
            self._log(sub, "not_upcoming", session.id)
            return
        handlers = {"cancel": self._cancel, "move": self._move, "repeat": self._repeat,
                    "location": self._location, "seats": self._seats}
        reply, outcome = await handlers[sub](event, session, value)
        await self._say(event, reply)
        self._log(sub, outcome, session.id)

    def _may_change(self, session: Session, event: MessageCreate) -> bool:
        return event.author_id == session.proposer_id or admin.authorize(event, self._config)

    async def _resolve(self, ref: str) -> Session | None:
        """A session number, or a message link whose message id is the session's. The ids are digits only, from the
        regex, before any lookup."""
        if _NUMBER.fullmatch(ref):
            session_id = int(ref)
            return await self._db.run(lambda conn: sessions.get(conn, session_id))
        link = _MESSAGE_LINK.fullmatch(ref)
        if link is not None:
            message_id = link.group(3)
            return await self._db.run(lambda conn: sessions.get_by_message(conn, message_id))
        return None

    def _link(self, session: Session) -> str | None:
        if not self._config.instance_url:
            return None
        return render.jump_link(text.SESSION_LINK_LABEL, self._config.instance_url, session.guild_id,
                                session.channel_id, session.message_id)

    async def _edit(self, session_id: int) -> None:
        """The message edit after a change. The change is already stored, so a failed edit is logged by type only."""
        try:
            await self._session_post.edit_session(session_id)
        except Exception as exc:  # noqa: BLE001 - the stored change stands; the message keeps its last good form
            log_event(log, "session_edit_failed", level=logging.WARNING, session_id=session_id,
                      reason=type(exc).__name__)

    async def _list(self, event: MessageCreate) -> None:
        if admin.authorize(event, self._config):
            rows = await self._db.run(lambda conn: [
                (session, title, sessions.counts_of(conn, session.id))
                for session, title in sessions.upcoming_with_titles(conn)])
            lines = [text.SESSION_LIST_ALL.format(n=session.id, card=render.escape(title), ts=session.starts_at,
                                                  counts=_counts_text(counts))
                     for session, title, counts in rows]
        else:
            mine = await self._db.run(lambda conn: sessions.upcoming_for_user(conn, event.author_id))
            lines = [text.SESSION_LIST_MINE.format(
                        n=session.id, card=render.escape(title), ts=session.starts_at,
                        state=text.SESSION_STATE[state].format(pos=pos))
                     for session, title, state, pos in mine]
        if not lines:
            await self._say(event, text.SESSION_LIST_EMPTY)
            self._log("list", "empty")
            return
        for chunk in chunks(lines):
            await self._say(event, chunk)
        self._log("list", "done")

    # Changes. Each returns the reply and its logged outcome. A change that the transaction refuses (the session is no
    # longer upcoming) returns the not-upcoming reply, so a change that lost a race is told as a refusal.

    async def _cancel(self, event: MessageCreate, session: Session, _value: str) -> _Reply:
        now = self._now()
        async with self._locks.get(session.card_id):
            cancelled = await self._db.run(lambda conn: _cancel_tx(conn, session.id, now))
        if cancelled is None:
            return text.SESSION_NOT_UPCOMING, "not_upcoming"
        fresh, card = cancelled
        await self._edit(session.id)
        await self._session_post.dm_attendees(fresh, _cancelled_text(fresh, card), exclude=event.author_id)
        self._kick()
        return text.SESSION_CANCEL_DONE.format(n=session.id), "done"

    async def _move(self, event: MessageCreate, session: Session, value: str) -> _Reply:
        zone = self._config.session_time_zone
        now = self._clock()
        try:
            when = session_time.parse(value, now=_local(now, zone), tz=zone)
        except session_time.TimeRefused as exc:
            return text.SESSION_TIME_REFUSED[exc.reason] + " " + text.SESSION_TIME_EXAMPLE, exc.reason
        starts = int(when.timestamp())
        async with self._locks.get(session.card_id):
            moved = await self._db.run(lambda conn: self._move_tx(conn, session.id, starts))
        if moved is None:
            return text.SESSION_NOT_UPCOMING, "not_upcoming"
        fresh, card = moved
        await self._edit(session.id)
        link = self._link(fresh)
        title = render.escape(card.title)
        if link is not None:
            content = text.SESSION_MOVED_DM.format(card=title, ts=fresh.starts_at, link=link)
        else:
            content = text.SESSION_MOVED_DM_NO_LINK.format(card=title, ts=fresh.starts_at)
        await self._session_post.dm_attendees(fresh, content, exclude=event.author_id)
        self._kick()
        return text.SESSION_MOVE_DONE.format(n=session.id, ts=starts), "done"

    @staticmethod
    def _move_tx(conn, session_id: int, starts: int) -> tuple[Session, Card] | None:
        """Keeps the length of the session as it is now, read in this transaction, not as it was when the command came."""
        current = _read(conn, session_id)
        if current is None or current[0].state != "upcoming":
            return None
        session = current[0]
        length = session.ends_at - session.starts_at
        if not sessions.move_if(conn, session_id, starts_at=starts, ends_at=starts + length):
            return None
        return _read(conn, session_id)

    async def _repeat(self, event: MessageCreate, session: Session, value: str) -> _Reply:
        if value == "weekly":
            recurrence, reply = "weekly", text.SESSION_REPEAT_ON
        elif value == "off":
            recurrence, reply = None, text.SESSION_REPEAT_OFF
        else:
            return text.SESSION_USAGE, "usage"
        async with self._locks.get(session.card_id):
            done = await self._db.run(lambda conn: self._repeat_tx(conn, session.id, recurrence))
        if not done:
            return text.SESSION_NOT_UPCOMING, "not_upcoming"
        return reply.format(n=session.id), "done"

    @staticmethod
    def _repeat_tx(conn, session_id: int, recurrence: str | None) -> bool:
        current = sessions.get(conn, session_id)
        if current is None or current.state != "upcoming":
            return False
        sessions.set_series_recurrence(conn, current.series_id, recurrence)
        return True

    async def _location(self, event: MessageCreate, session: Session, value: str) -> _Reply:
        if value == "clear":
            location: str | None = None
        else:
            try:
                location = card_fields.text_value("location", value)
            except CardFieldError as err:
                return card_fields.refusal_reply(err), err.rule
        async with self._locks.get(session.card_id):
            changed = await self._db.run(lambda conn: sessions.set_location_if(conn, session.id, location))
        if not changed:
            return text.SESSION_NOT_UPCOMING, "not_upcoming"
        await self._edit(session.id)
        return text.SESSION_LOCATION_DONE.format(n=session.id), "done"

    async def _seats(self, event: MessageCreate, session: Session, value: str) -> _Reply:
        if value == "clear":
            seats: int | None = None
        else:
            try:
                seats = card_fields.int_range("seats", value, *card_fields.SEATS_RANGE)
            except CardFieldError as err:
                return card_fields.refusal_reply(err), err.rule
        now = self._now()
        async with self._locks.get(session.card_id):
            result = await self._db.run(lambda conn: self._seats_tx(conn, session.id, seats, now))
        if result is None:
            return text.SESSION_NOT_UPCOMING, "not_upcoming"
        fresh, card, outcome = result
        await self._edit(session.id)
        # The edit can find the message gone, and that cancels the session. Promotions go out only while it stands.
        after = await self._db.run(lambda conn: sessions.get(conn, session.id))
        if after is not None and after.state == "upcoming" and outcome.promoted:
            await self._session_post.dm_promoted(fresh, card, outcome.promoted)
        self._kick()
        return text.SESSION_SEATS_DONE.format(n=session.id), "done"

    @staticmethod
    def _seats_tx(conn, session_id: int, seats: int | None, now: int) -> tuple[Session, Card, rsvp.Outcome] | None:
        current = _read(conn, session_id)
        if current is None or current[0].state != "upcoming":
            return None
        if not sessions.set_seats_if(conn, session_id, seats):
            return None
        fresh, card = _read(conn, session_id)   # re-read after the write, in the same transaction
        outcome = sessions.recompute(conn, fresh, sessions.effective_seats(fresh, card), now)
        return fresh, card, outcome

    # The hooks ---------------------------------------------------------------------------------------------

    async def on_message_gone(self, session_id: int) -> None:
        """The SessionPost hook for a 404 on a session's message. Takes no card lock: the cancel is one compare-and-set
        transaction, and the DMs and the poller kick come after it. The message is gone, so it is never edited."""
        now = self._now()
        cancelled = await self._db.run(lambda conn: _cancel_tx(conn, session_id, now))
        if cancelled is None:
            return
        session, card = cancelled
        await self._session_post.dm_attendees(session, _cancelled_text(session, card), exclude=None)
        self._kick()
        log_event(log, "session_message_gone", session_id=session_id)

    async def cancel_card_sessions_locked(self, card_id: int, actor: str) -> int:
        """Cancels the card's upcoming sessions and stops their series, then edits each message and DMs its attendees
        except `actor`. The caller holds the card's lock. This method takes none, and nothing it calls may take one."""
        now = self._now()

        def cancel_all(conn) -> list[tuple[Session, Card]]:
            card = cards.get_by_id(conn, card_id)
            if card is None:
                return []
            done: list[tuple[Session, Card]] = []
            for session in sessions.upcoming_for_card(conn, card_id):
                if sessions.cancel_if(conn, session.id, now):
                    sessions.set_series_recurrence(conn, session.series_id, None)
                    done.append((session, card))
            return done

        cancelled = await self._db.run(cancel_all)
        for session, card in cancelled:
            await self._edit(session.id)
            await self._session_post.dm_attendees(session, _cancelled_text(session, card), exclude=actor)
        log_event(log, "session_card_cancelled", card_id=card_id, count=len(cancelled))
        return len(cancelled)
