# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The timed session work, run on every poller tick: a session ends at its end time, a weekly series posts its next
occurrence one day after the current start, and an asleep heads-up goes to the admins in the 30 minutes before a session
with people going.

Each job lists what is due without a lock, then acts on each item under the card's lock. Every state change is one
transaction that reads the session again, so a move, a cancel or a repeat change made since the listing is respected, and
a write refuses when the row has changed. No DM is sent while the lock is held, and each message edit runs after it.
`due` is the soonest time anything is still to happen, so the poller's wait ends there and never passes it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, notify, render, rsvp, session_time, sessions, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import Card
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.logs import log_event
from fluxer_panel.resolver import CardStatus, StatusKind
from fluxer_panel.session_post import SessionPost
from fluxer_panel.sessions import Session, SessionCapReached

if TYPE_CHECKING:
    from fluxer_panel.session_flow import SessionFlow

log = logging.getLogger(__name__)


class _AlreadyPosted(Exception):
    """The occurrence's successor was already marked posted, so a second writer got there first: the store is refused."""


@dataclass(frozen=True)
class _Plan:
    """A due occurrence as read under the card's lock. `stop` means the card is full: the series ends, and the proposer
    is told once after the lock is released."""
    session: Session
    card: Card
    starts: int
    ends: int
    stop: bool


@dataclass(frozen=True)
class _Posted:
    """A successor occurrence that is stored, with the ids the after-lock steps need."""
    session_id: int
    card_id: int
    channel_id: str
    message_id: str


@dataclass(frozen=True)
class _Alert:
    """A heads-up that the transaction has marked sent, with the values its text needs."""
    title: str
    starts_at: int
    going: int
    session_id: int
    card_id: int


def _end_tx(conn: Any, session_id: int, now: int) -> bool:
    """Ends the session when it is still upcoming and its end has passed, as read in this transaction: a move made since
    the listing may have pushed the end into the future."""
    session = sessions.get(conn, session_id)
    if session is None or session.state != "upcoming" or session.ends_at > now:
        return False
    return sessions.end_if(conn, session_id, now)


def _plan_tx(conn: Any, session_id: int, now: int, zone: ZoneInfo) -> _Plan | None:
    """Reads the occurrence again and works out its successor. A series whose card is no longer live is cleared and
    skipped. A full card clears the whole series and returns a stop plan, so the proposer gets one note."""
    session = sessions.get(conn, session_id)
    if session is None or session.recurrence != "weekly" or session.next_posted:
        return None
    if session.starts_at + sessions.RECURRENCE_LEAD > now:
        return None   # moved since it was listed, so no longer due
    card = cards.get_by_id(conn, session.card_id)
    if card is None or card.state != "live":
        sessions.set_series_recurrence(conn, session.series_id, None)
        return None
    start = session_time.next_weekly(datetime.fromtimestamp(session.starts_at, zone), tz=zone,
                                     now=datetime.fromtimestamp(now, zone))
    starts = int(start.timestamp())
    ends = starts + (session.ends_at - session.starts_at)
    try:
        sessions.check_caps(conn, session.card_id, None)
    except SessionCapReached:
        sessions.set_series_recurrence(conn, session.series_id, None)
        return _Plan(session, card, starts, ends, stop=True)
    return _Plan(session, card, starts, ends, stop=False)


def _store_tx(conn: Any, plan: _Plan, message_id: str, now: int) -> int:
    """Marks the occurrence's successor posted and stores the successor, in one transaction. A refused mark or a failed
    insert rolls both back, and the caller deletes the message it posted."""
    session = plan.session
    if not sessions.mark_next_posted_if(conn, session.id):
        raise _AlreadyPosted
    return sessions.add(conn, card_id=session.card_id, guild_id=session.guild_id, channel_id=session.channel_id,
                        message_id=message_id, starts_at=plan.starts, ends_at=plan.ends,
                        proposer_id=session.proposer_id, now=now, location_text=session.location_text,
                        seats=session.seats, recurrence="weekly", series_id=session.series_id, proposer_cap=False)


def _headsup_tx(conn: Any, session_id: int, now: int) -> _Alert | None:
    """Checks the heads-up conditions as they are now and marks the heads-up sent in the same transaction. The card needs
    a manager, and someone must be going. A move clears the mark, so a moved session gets its own heads-up."""
    session = sessions.get(conn, session_id)
    if session is None or session.state != "upcoming" or session.headsup_sent:
        return None
    if not session.starts_at - sessions.HEADSUP_WINDOW <= now < session.starts_at:
        return None
    card = cards.get_by_id(conn, session.card_id)
    if card is None or card.manager is None:
        return None
    going = sessions.counts_of(conn, session.id).going
    if going == 0:
        return None
    if not sessions.mark_headsup_if(conn, session.id):
        return None
    return _Alert(card.title, session.starts_at, going, session.id, session.card_id)


class SessionJobs:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, locks: CardLocks, session_post: SessionPost,
                 session_flow: SessionFlow, status_of: Callable[[int], CardStatus | None],
                 clock: Callable[[], float] = time.time, kick: Callable[[], None] = lambda: None,
                 backoff: DmBackoff | None = None) -> None:
        self._db = db
        self._rest = rest
        self._config = config
        self._locks = locks
        self._session_post = session_post
        self._flow = session_flow
        self._status_of = status_of
        self._clock = clock
        self._kick = kick
        self._backoff = backoff
        self.due: float | None = None   # the soonest time anything is still to happen: the poller's wait ends there

    async def run(self) -> None:
        """One pass of every job. A failing job is logged and never stops the others or the poller. Then `due` is read."""
        for step in (self._expire_drafts, self._end, self._recur, self._headsup):
            try:
                await step()
            except Exception as exc:  # noqa: BLE001 - one failed job never stops the others or the poller
                log_event(log, "session_job_failed", level=logging.WARNING, kind=step.__name__.lstrip("_"),
                          reason=type(exc).__name__)
        try:
            self.due = await self._next_due()
        except Exception as exc:  # noqa: BLE001 - no wake-up is kept when none can be read: the poll interval still runs
            self.due = None
            log_event(log, "session_job_failed", level=logging.WARNING, kind="due", reason=type(exc).__name__)

    async def _expire_drafts(self) -> None:
        await self._flow.expire_drafts()

    async def _end(self) -> None:
        now = int(self._clock())
        candidates = await self._db.run(lambda c: sessions.due_to_end(c, now))
        ended: list[Session] = []
        for candidate in candidates:
            async with self._locks.get(candidate.card_id):
                if await self._db.run(partial(_end_tx, session_id=candidate.id, now=now)):
                    ended.append(candidate)
        for session in ended:
            try:
                await self._session_post.edit_session(session.id)
            except Exception as exc:  # noqa: BLE001 - the end is stored; the message keeps its last good form
                log_event(log, "session_edit_failed", level=logging.WARNING, session_id=session.id,
                          reason=type(exc).__name__)
            log_event(log, "session_ended", session_id=session.id, card_id=session.card_id)
        if ended:
            self._kick()

    async def _recur(self) -> None:
        now = int(self._clock())
        candidates = await self._db.run(lambda c: sessions.recurrence_due(c, now))
        for candidate in candidates:
            await self._recur_one(candidate.id, candidate.card_id)

    async def _recur_one(self, session_id: int, card_id: int) -> None:
        async with self._locks.get(card_id):
            outcome = await self._occurrence_locked(session_id)
        if isinstance(outcome, _Posted):
            await self._after_post(outcome)
        elif isinstance(outcome, _Plan):
            await self._dm_stopped(outcome)

    async def _occurrence_locked(self, session_id: int) -> _Posted | _Plan | None:
        """Under the card's lock: posts the successor and stores it, or stops the series when the card is full. Nothing
        here is decided from a read taken before the lock."""
        now = int(self._clock())
        plan = await self._db.run(partial(_plan_tx, session_id=session_id, now=now, zone=self._config.session_time_zone))
        if plan is None:
            return None
        if plan.stop:
            log_event(log, "session_series_stopped", session_id=session_id, card_id=plan.session.card_id)
            return plan
        message_id = await self._post_occurrence(plan, now)
        if message_id is None:
            return None
        try:
            new_id = await self._db.run(partial(_store_tx, plan=plan, message_id=message_id, now=now))
        except Exception as exc:  # noqa: BLE001 - with no row the message would never end: it is deleted, and retried later
            await self._session_post.delete_unstored(plan.session.channel_id, message_id)
            log_event(log, "session_recur_failed", level=logging.WARNING, card_id=plan.session.card_id,
                      reason=type(exc).__name__)
            return None
        return _Posted(new_id, plan.session.card_id, plan.session.channel_id, message_id)

    async def _post_occurrence(self, plan: _Plan, now: int) -> str | None:
        """Sends the successor to the session's stored channel. Returns its message id, or None when the send fails or
        the id is not a snowflake. Nothing is stored then, so the next tick tries again: next_posted is still 0."""
        session = plan.session
        provisional = sessions.provisional(card_id=session.card_id, guild_id=session.guild_id,
                                           channel_id=session.channel_id, starts_at=plan.starts, ends_at=plan.ends,
                                           proposer_id=session.proposer_id, now=now,
                                           location_text=session.location_text, seats=session.seats)
        payload = render.session_message(
            plan.card, provisional, rsvp.Counts(going=1, maybe=0, waiting=0),
            effective_location=sessions.effective_location(provisional, plan.card),
            effective_seats=sessions.effective_seats(provisional, plan.card),
            instance_url=self._config.instance_url, provisional=True)
        try:
            sent = await self._session_post.send_session(session.channel_id, payload)
        except Exception as exc:  # noqa: BLE001 - the next tick retries the same occurrence
            log_event(log, "session_recur_failed", level=logging.WARNING, card_id=session.card_id,
                      reason=type(exc).__name__)
            return None
        message_id = sent.get("id") if isinstance(sent, dict) else None
        if not is_snowflake(message_id):
            log_event(log, "session_recur_failed", level=logging.WARNING, card_id=session.card_id,
                      reason="bad_message_id")
            return None
        return str(message_id)

    async def _after_post(self, posted: _Posted) -> None:
        """After the lock: the three RSVP reactions, the footer edit on the new occurrence, then a poller kick."""
        try:
            await self._session_post.seed_session(posted.channel_id, posted.message_id)
        except Exception as exc:  # noqa: BLE001 - a missing seed is logged; users can still react
            log_event(log, "session_seed_failed", level=logging.WARNING, session_id=posted.session_id,
                      reason=type(exc).__name__)
        try:
            await self._session_post.edit_session(posted.session_id)
        except Exception as exc:  # noqa: BLE001 - the message keeps the provisional form until a later edit
            log_event(log, "session_edit_failed", level=logging.WARNING, session_id=posted.session_id,
                      reason=type(exc).__name__)
        self._kick()
        log_event(log, "session_recurred", session_id=posted.session_id, card_id=posted.card_id)

    async def _dm_stopped(self, plan: _Plan) -> None:
        """After the lock: the proposer's one note that the series has stopped."""
        content = text.SESSION_SERIES_STOPPED.format(card=render.escape(plan.card.title))
        await notify.dm_user(self._rest, plan.session.proposer_id, content, backoff=self._backoff, bypass=True)

    async def _headsup(self) -> None:
        now = int(self._clock())
        candidates = await self._db.run(lambda c: sessions.headsup_candidates(c, now))
        for candidate in candidates:
            alert = await self._headsup_one(candidate.id, candidate.card_id, now)
            if alert is None:
                continue
            content = text.SESSION_HEADSUP.format(card=render.escape(alert.title), ts=alert.starts_at, n=alert.going)
            await notify.dm_admins(self._rest, self._config, content, failed_event="session_headsup_failed")
            log_event(log, "session_headsup", session_id=alert.session_id, card_id=alert.card_id, count=alert.going)

    async def _headsup_one(self, session_id: int, card_id: int, now: int) -> _Alert | None:
        """Under the card's lock: the server's status is read, then the heads-up is marked sent only when every condition
        holds. The DM goes out after the lock, in the caller."""
        async with self._locks.get(card_id):
            status = self._status_of(card_id)
            if status is None or status.kind is not StatusKind.ASLEEP:
                return None
            return await self._db.run(partial(_headsup_tx, session_id=session_id, now=now))

    async def _next_due(self) -> float | None:
        """The soonest time still to happen: a session end, a recurrence, a heads-up or a draft expiry. Only times after
        the clock now count, so work that is due but still pending gives no wake-up, and the poller cannot spin on it."""
        clock_now = self._clock()
        now = int(clock_now)
        times = await self._db.run(lambda c: sessions.next_times(c, now))
        expiry = self._flow.next_expiry()
        pending = [when for when in (*times, expiry) if when is not None and when > clock_now]
        return min(pending) if pending else None
