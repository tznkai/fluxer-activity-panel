# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Closing a poll. One transaction ranks the usable options and closes the poll compare-and-set; the poll message and
ballots are then edited at once (no debounce), and 📅 (and 🔁 for an approval poll) is seeded when there is a winner.

Every way of closing a poll goes through here: `!poll close` now, the timed job, `!card archive` and a deleted poll
message later. The ranking and the close share one transaction, so a vote or a command can never change a poll
between the ranking and its close."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from fluxer_panel import poll_count, polls
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.db import Database
from fluxer_panel.logs import log_event
from fluxer_panel.poll_post import PollPost

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Closed:
    poll_id: int
    card_id: int
    outcome: str                  # "winner" | "none" | "removed"


def _ranked_ballots(votes: list[polls.Vote]) -> list[list[int]]:
    """Each user's picks in rank order. `polls.votes` is already ordered by user, then rank."""
    ballots: dict[str, list[int]] = {}
    for vote in votes:
        ballots.setdefault(vote.user_id, []).append(vote.option_id)
    return list(ballots.values())


def _close_tx(conn: Any, *, poll_id: int, now: int, outcome_override: str | None) -> Closed | None:
    """Ranks and closes in the caller's transaction. None when the poll is missing or not open, or when the
    compare-and-set close does not apply."""
    poll = polls.get(conn, poll_id)
    if poll is None or poll.state != "open":
        return None
    winner: int | None = None
    if outcome_override is None:
        usable = poll_count.usable(polls.options(conn, poll_id), now=now)
        votes = polls.votes(conn, poll_id)
        if poll.is_runoff:
            winner = poll_count.instant_runoff(usable, _ranked_ballots(votes)).winner
        else:
            ranked = poll_count.ranked_with_votes(usable, [(v.user_id, v.option_id) for v in votes])
            winner = ranked[0][0] if ranked else None
    outcome = outcome_override or ("winner" if winner is not None else "none")
    if not polls.close_if_open(conn, poll_id, now=now, outcome=outcome, winner_option_id=winner):
        return None
    return Closed(poll_id, poll.card_id, outcome)


class PollCloser:
    def __init__(self, *, db: Database, locks: CardLocks, poll_post: PollPost,
                 clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._locks = locks
        self._poll_post = poll_post
        self._clock = clock

    async def close(self, poll_id: int, *, outcome_override: str | None = None) -> Closed | None:
        """Take the card lock, close, release, then edit and seed. None when the poll was not open."""
        poll = await self._db.run(lambda c: polls.get(c, poll_id))
        if poll is None:
            return None
        async with self._locks.get(poll.card_id):
            closed = await self._db.run(partial(_close_tx, poll_id=poll_id, now=int(self._clock()),
                                                outcome_override=outcome_override))
        if closed is not None:
            await self._after(closed)
        return closed

    async def close_locked(self, poll_id: int, *, outcome_override: str | None = None) -> Closed | None:
        """As close(), for a caller that already holds this poll's card lock (it edits under that lock)."""
        closed = await self._db.run(partial(_close_tx, poll_id=poll_id, now=int(self._clock()),
                                            outcome_override=outcome_override))
        if closed is not None:
            await self._after(closed)
        return closed

    async def on_message_gone(self, poll_id: int) -> None:
        """The poll message answered 404 to an edit. An open poll closes as removed; a closed one is left. The read is
        made without the card lock: when the 404 comes from an archive's own flush, the archive holds the lock and has
        already closed the poll, so this returns before it would take the lock again."""
        poll = await self._db.run(lambda c: polls.get(c, poll_id))
        if poll is None or poll.state != "open":
            return
        await self.close(poll_id, outcome_override="removed")

    async def _after(self, closed: Closed) -> None:
        """The edits and seeding after a committed close. The poll and its ballots are flushed at once, so the poll
        shows as closed without waiting for the debounce; the seeding runs only for a winner."""
        log_event(log, "poll_closed", poll_id=closed.poll_id, card_id=closed.card_id, outcome=closed.outcome)
        self._poll_post.changed(closed.poll_id)
        await self._poll_post.flush(closed.poll_id)
        if closed.outcome == "winner":
            await self._poll_post.seed_closed(closed.poll_id)
