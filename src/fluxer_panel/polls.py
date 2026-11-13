# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Polls: the rows behind a time poll (approval polls and ranked runoffs). Synchronous SQL over one sqlite3 connection,
run through `await db.run(fn)` like the session store.

Each call runs inside one transaction, so a read and the write it guards are atomic. Every state change is
compare-and-set on `state = 'open'`, so a late vote, a second close or a stale sort cannot undo a change that
already happened.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

MAX_OPTIONS = 702   # letters a..z, then aa..zz


class TooManyOptions(Exception):
    """A poll already has MAX_OPTIONS options."""


@dataclass(frozen=True)
class Poll:
    id: int
    card_id: int
    guild_id: str
    channel_id: str
    message_id: str
    starter_id: str
    kind: str                     # "approval" | "runoff"
    parent_poll_id: int | None
    state: str                    # "open" | "closed"
    created_at: int
    closes_at: int
    closed_at: int | None
    ballot_size: int
    pending_ballot_size: int | None
    sorted_at: int | None         # last `!poll sort`, None if never sorted
    outcome: str | None           # None while open; "winner" | "none" | "removed"
    winner_option_id: int | None
    session_id: int | None

    @property
    def is_runoff(self) -> bool:
        return self.kind == "runoff"


@dataclass(frozen=True)
class PollOption:
    id: int
    poll_id: int
    position: int                 # 0-based; letter a is position 0
    starts_at: int
    origin: str                   # "offered" | "typed"


@dataclass(frozen=True)
class Ballot:
    poll_id: int
    user_id: str
    dm_channel_id: str | None
    dm_message_id: str | None
    dm_failed: bool
    answered: bool
    reminded: bool


@dataclass(frozen=True)
class Vote:
    user_id: str
    option_id: int
    rank: int | None              # None for approval polls; 1 = favourite in a runoff


_POLL_COLUMNS = ("id, card_id, guild_id, channel_id, message_id, starter_id, kind, parent_poll_id, state, created_at, "
                 "closes_at, closed_at, ballot_size, pending_ballot_size, sorted_at, outcome, winner_option_id, "
                 "session_id")
_OPTION_COLUMNS = "id, poll_id, position, starts_at, origin"
_BALLOT_COLUMNS = "poll_id, user_id, dm_channel_id, dm_message_id, dm_failed, answered, reminded"


def _load_poll(row: tuple) -> Poll:
    return Poll(*row)


def _load_ballot(row: tuple) -> Ballot:
    (poll_id, user_id, dm_channel_id, dm_message_id, dm_failed, answered, reminded) = row
    return Ballot(poll_id, user_id, dm_channel_id, dm_message_id, bool(dm_failed), bool(answered), bool(reminded))


def _polls(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[Poll]:
    return [_load_poll(row) for row in conn.execute(f"SELECT {_POLL_COLUMNS} FROM polls {sql}", params).fetchall()]


def _ballots(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[Ballot]:
    return [_load_ballot(row) for row in
            conn.execute(f"SELECT {_BALLOT_COLUMNS} FROM poll_ballots {sql}", params).fetchall()]


def _options(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[PollOption]:
    return [PollOption(*row) for row in
            conn.execute(f"SELECT {_OPTION_COLUMNS} FROM poll_options {sql}", params).fetchall()]


def create(conn: sqlite3.Connection, *, card_id: int, guild_id: str, channel_id: str, message_id: str,
           starter_id: str, kind: str, parent_poll_id: int | None, now: int, closes_at: int, ballot_size: int,
           options: Sequence[tuple[int, str]], recipients: Sequence[str]) -> int | None:
    """Open a poll with its options and one ballot per recipient. None when the card already has an open poll."""
    if (kind == "runoff") != (parent_poll_id is not None):
        raise ValueError("a runoff needs a parent poll, and only a runoff has one")
    if open_for_card(conn, card_id) is not None:
        return None
    cur = conn.execute(
        "INSERT INTO polls (card_id, guild_id, channel_id, message_id, starter_id, kind, parent_poll_id, state, "
        "created_at, closes_at, ballot_size) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
        (card_id, guild_id, channel_id, message_id, starter_id, kind, parent_poll_id, now, closes_at, ballot_size))
    poll_id = cur.lastrowid
    for position, (starts_at, origin) in enumerate(options):
        conn.execute("INSERT INTO poll_options (poll_id, position, starts_at, origin) VALUES (?, ?, ?, ?)",
                     (poll_id, position, starts_at, origin))
    for user_id in dict.fromkeys(recipients):
        conn.execute("INSERT INTO poll_ballots (poll_id, user_id) VALUES (?, ?)", (poll_id, user_id))
    return poll_id


def get(conn: sqlite3.Connection, poll_id: int) -> Poll | None:
    rows = _polls(conn, "WHERE id = ?", (poll_id,))
    return rows[0] if rows else None


def get_by_message(conn: sqlite3.Connection, message_id: str) -> Poll | None:
    rows = _polls(conn, "WHERE message_id = ?", (message_id,))
    return rows[0] if rows else None


def open_for_card(conn: sqlite3.Connection, card_id: int) -> Poll | None:
    rows = _polls(conn, "WHERE card_id = ? AND state = 'open'", (card_id,))
    return rows[0] if rows else None


def open_polls(conn: sqlite3.Connection) -> list[Poll]:
    return _polls(conn, "WHERE state = 'open' ORDER BY id")


def due(conn: sqlite3.Connection, now: int) -> list[Poll]:
    return _polls(conn, "WHERE state = 'open' AND closes_at <= ? ORDER BY closes_at, id", (now,))


def next_close(conn: sqlite3.Connection) -> int | None:
    return conn.execute("SELECT MIN(closes_at) FROM polls WHERE state = 'open'").fetchone()[0]


def for_user(conn: sqlite3.Connection, user_id: str) -> list[Poll]:
    return _polls(conn, "WHERE starter_id = ? OR id IN (SELECT poll_id FROM poll_ballots WHERE user_id = ?) "
                        "ORDER BY id DESC", (user_id, user_id))


def open_ballot_polls(conn: sqlite3.Connection, user_id: str) -> list[Poll]:
    return _polls(conn, "WHERE state = 'open' AND id IN (SELECT poll_id FROM poll_ballots WHERE user_id = ?) "
                        "ORDER BY id", (user_id,))


def child_of(conn: sqlite3.Connection, parent_poll_id: int) -> Poll | None:
    rows = _polls(conn, "WHERE parent_poll_id = ? ORDER BY id LIMIT 1", (parent_poll_id,))
    return rows[0] if rows else None


def options(conn: sqlite3.Connection, poll_id: int) -> list[PollOption]:
    return _options(conn, "WHERE poll_id = ? ORDER BY position", (poll_id,))


def ballots(conn: sqlite3.Connection, poll_id: int) -> list[Ballot]:
    return _ballots(conn, "WHERE poll_id = ? ORDER BY user_id", (poll_id,))


def ballot(conn: sqlite3.Connection, poll_id: int, user_id: str) -> Ballot | None:
    rows = _ballots(conn, "WHERE poll_id = ? AND user_id = ?", (poll_id, user_id))
    return rows[0] if rows else None


def votes(conn: sqlite3.Connection, poll_id: int) -> list[Vote]:
    rows = conn.execute(
        "SELECT v.user_id, v.option_id, v.rank FROM poll_votes v JOIN poll_options o ON o.id = v.option_id "
        "WHERE v.poll_id = ? ORDER BY v.user_id, v.rank IS NULL, v.rank, o.position", (poll_id,)).fetchall()
    return [Vote(user_id, option_id, rank) for user_id, option_id, rank in rows]


def picks(conn: sqlite3.Connection, poll_id: int, user_id: str) -> list[int]:
    rows = conn.execute(
        "SELECT v.option_id FROM poll_votes v JOIN poll_options o ON o.id = v.option_id "
        "WHERE v.poll_id = ? AND v.user_id = ? ORDER BY v.rank IS NULL, v.rank, o.position",
        (poll_id, user_id)).fetchall()
    return [option_id for (option_id,) in rows]


def add_option(conn: sqlite3.Connection, poll_id: int, *, starts_at: int, origin: str) -> tuple[int, bool]:
    """The id of the option at this start, and whether it was just added. Does not check the poll's state."""
    existing = conn.execute("SELECT id FROM poll_options WHERE poll_id = ? AND starts_at = ?",
                            (poll_id, starts_at)).fetchone()
    if existing is not None:
        return existing[0], False
    count = conn.execute("SELECT COUNT(*) FROM poll_options WHERE poll_id = ?", (poll_id,)).fetchone()[0]
    if count >= MAX_OPTIONS:
        raise TooManyOptions()
    position = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM poll_options WHERE poll_id = ?",
                            (poll_id,)).fetchone()[0]
    cur = conn.execute("INSERT INTO poll_options (poll_id, position, starts_at, origin) VALUES (?, ?, ?, ?)",
                       (poll_id, position, starts_at, origin))
    return cur.lastrowid, True


def replace_picks_if_open(conn: sqlite3.Connection, poll_id: int, user_id: str, option_ids: Sequence[int], *,
                          ranked: bool) -> bool:
    """Replace the user's picks. False, with nothing changed, when the poll is closed or the user has no ballot.
    An option id that is not this poll's raises IntegrityError from the composite foreign key, which rolls back the
    whole db.run transaction, so the answered flag is not set either."""
    if len(set(option_ids)) != len(option_ids):
        raise ValueError("an option may be picked once")
    cur = conn.execute(
        "UPDATE poll_ballots SET answered = 1 WHERE poll_id = ? AND user_id = ? "
        "AND EXISTS (SELECT 1 FROM polls WHERE id = ? AND state = 'open')",
        (poll_id, user_id, poll_id))
    if cur.rowcount != 1:
        return False
    conn.execute("DELETE FROM poll_votes WHERE poll_id = ? AND user_id = ?", (poll_id, user_id))
    for index, option_id in enumerate(option_ids):
        conn.execute("INSERT INTO poll_votes (poll_id, user_id, option_id, rank) VALUES (?, ?, ?, ?)",
                     (poll_id, user_id, option_id, index + 1 if ranked else None))
    return True


def sort_if_open(conn: sqlite3.Connection, poll_id: int, ordered_option_ids: Sequence[int], *, now: int) -> bool:
    """Re-letter the options in the given order and apply any pending ballot size. False when the poll is closed."""
    if conn.execute("SELECT 1 FROM polls WHERE id = ? AND state = 'open'", (poll_id,)).fetchone() is None:
        return False
    current = {option.id for option in options(conn, poll_id)}
    if len(ordered_option_ids) != len(current) or set(ordered_option_ids) != current:
        raise ValueError("the order must name each of the poll's options exactly once")
    # Move every position out of the way first, so the unique (poll_id, position) index never sees a clash.
    conn.execute("UPDATE poll_options SET position = position + 100000 WHERE poll_id = ?", (poll_id,))
    for index, option_id in enumerate(ordered_option_ids):
        conn.execute("UPDATE poll_options SET position = ? WHERE id = ? AND poll_id = ?",
                     (index, option_id, poll_id))
    conn.execute(
        "UPDATE polls SET ballot_size = COALESCE(pending_ballot_size, ballot_size), pending_ballot_size = NULL, "
        "sorted_at = ? WHERE id = ? AND state = 'open'", (now, poll_id))
    return True


def set_pending_ballot_size_if_open(conn: sqlite3.Connection, poll_id: int, size: int) -> bool:
    cur = conn.execute("UPDATE polls SET pending_ballot_size = ? WHERE id = ? AND state = 'open'", (size, poll_id))
    return cur.rowcount == 1


def extend_if_open(conn: sqlite3.Connection, poll_id: int, *, closes_at: int) -> bool:
    """The caller checks the cap."""
    cur = conn.execute("UPDATE polls SET closes_at = ? WHERE id = ? AND state = 'open'", (closes_at, poll_id))
    return cur.rowcount == 1


def close_if_open(conn: sqlite3.Connection, poll_id: int, *, now: int, outcome: str,
                  winner_option_id: int | None) -> bool:
    cur = conn.execute(
        "UPDATE polls SET state = 'closed', closed_at = ?, outcome = ?, winner_option_id = ? "
        "WHERE id = ? AND state = 'open'", (now, outcome, winner_option_id, poll_id))
    return cur.rowcount == 1


def set_ballot_message(conn: sqlite3.Connection, poll_id: int, user_id: str, *, channel_id: str,
                       message_id: str) -> None:
    conn.execute("UPDATE poll_ballots SET dm_channel_id = ?, dm_message_id = ? WHERE poll_id = ? AND user_id = ?",
                 (channel_id, message_id, poll_id, user_id))


def mark_dm_failed(conn: sqlite3.Connection, poll_id: int, user_id: str) -> None:
    conn.execute("UPDATE poll_ballots SET dm_failed = 1 WHERE poll_id = ? AND user_id = ?", (poll_id, user_id))


def mark_reminded_if(conn: sqlite3.Connection, poll_id: int, user_id: str) -> bool:
    """Marks the ballot reminded, once, while it is unanswered and its poll is still open. A reminder is never marked for a
    closed poll: a close that commits first stops it."""
    cur = conn.execute("UPDATE poll_ballots SET reminded = 1 WHERE poll_id = ? AND user_id = ? AND reminded = 0 "
                       "AND answered = 0 AND EXISTS (SELECT 1 FROM polls WHERE id = ? AND state = 'open')",
                       (poll_id, user_id, poll_id))
    return cur.rowcount == 1


def _halfway(poll: Poll) -> int:
    """The reminder time: halfway between the poll's start and its close, from the current close time."""
    return poll.created_at + (poll.closes_at - poll.created_at) // 2


def _reminder_polls(conn: sqlite3.Connection) -> list[Poll]:
    """Open polls that still have an unanswered ballot which has not been reminded."""
    return _polls(conn, "WHERE state = 'open' AND id IN (SELECT poll_id FROM poll_ballots "
                        "WHERE answered = 0 AND reminded = 0) ORDER BY id")


def reminder_due(conn: sqlite3.Connection, now: int) -> list[tuple[Poll, str]]:
    """(poll, user id) for each unanswered, unreminded ballot whose poll has reached its halfway time and has not closed.
    Ordered by poll id, then user id."""
    due: list[tuple[Poll, str]] = []
    for poll in _reminder_polls(conn):
        if _halfway(poll) > now or poll.closes_at <= now:
            continue
        for ballot in _ballots(conn, "WHERE poll_id = ? AND answered = 0 AND reminded = 0 ORDER BY user_id", (poll.id,)):
            due.append((poll, ballot.user_id))
    return due


def next_reminder(conn: sqlite3.Connection, now: int) -> int | None:
    """The earliest halfway time after `now` among open polls that still have an unanswered, unreminded ballot."""
    times = [_halfway(poll) for poll in _reminder_polls(conn) if _halfway(poll) > now]
    return min(times) if times else None


def set_session_if_unset(conn: sqlite3.Connection, poll_id: int, session_id: int) -> bool:
    cur = conn.execute("UPDATE polls SET session_id = ? WHERE id = ? AND outcome = 'winner' AND session_id IS NULL",
                       (session_id, poll_id))
    return cur.rowcount == 1


@dataclass(frozen=True)
class PollView:
    """Everything a poll renderer needs, read in one transaction. The ids in it are for links and checks only."""
    poll: Poll
    card_title: str
    options: tuple[PollOption, ...]                  # position order
    votes: tuple[Vote, ...]
    recipients: int                                  # ballot rows
    answered: int                                    # ballots with answered = 1
    reached: int                                     # ballots with a DM message id
    session_link: tuple[str, str, str] | None        # (guild_id, channel_id, message_id) of the scheduled session


def provisional_view(*, card_id: int, card_title: str, guild_id: str, channel_id: str, starter_id: str, now: int,
                     closes_at: int, ballot_size: int, starts: Sequence[int], recipients: int,
                     kind: str = "approval", parent_poll_id: int | None = None) -> PollView:
    """The view of a poll that is not stored yet: the first poll message is rendered from it before the row exists.
    The poll id is 0, the options are lettered from their order in `starts`, and there are no votes or DMs."""
    poll = Poll(id=0, card_id=card_id, guild_id=guild_id, channel_id=channel_id, message_id="", starter_id=starter_id,
                kind=kind, parent_poll_id=parent_poll_id, state="open", created_at=now, closes_at=closes_at, closed_at=None,
                ballot_size=ballot_size, pending_ballot_size=None, sorted_at=None, outcome=None,
                winner_option_id=None, session_id=None)
    options = tuple(PollOption(id=position, poll_id=0, position=position, starts_at=starts_at, origin="offered")
                    for position, starts_at in enumerate(starts))
    return PollView(poll=poll, card_title=card_title, options=options, votes=(), recipients=recipients, answered=0,
                    reached=0, session_link=None)


def view(conn: sqlite3.Connection, poll_id: int) -> PollView | None:
    poll = get(conn, poll_id)
    if poll is None:
        return None
    title_row = conn.execute("SELECT title FROM cards WHERE id = ?", (poll.card_id,)).fetchone()
    if title_row is None:
        return None
    recipients, answered, reached = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(answered), 0), COUNT(dm_message_id) FROM poll_ballots WHERE poll_id = ?",
        (poll_id,)).fetchone()
    session_link = None
    if poll.session_id is not None:
        row = conn.execute("SELECT guild_id, channel_id, message_id FROM sessions WHERE id = ?",
                           (poll.session_id,)).fetchone()
        session_link = tuple(row) if row is not None else None
    return PollView(poll=poll, card_title=title_row[0], options=tuple(options(conn, poll_id)),
                    votes=tuple(votes(conn, poll_id)), recipients=recipients, answered=answered, reached=reached,
                    session_link=session_link)
