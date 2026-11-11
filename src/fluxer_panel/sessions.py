# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sessions: the rows behind a scheduled game session. Synchronous SQL over one sqlite3 connection, run through
`await db.run(fn)` like the card store.

The caps are checked in the insert's transaction, and every state change is compare-and-set on the state it
expects, so a second command or a late event cannot undo a change that already happened.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fluxer_panel import rsvp

if TYPE_CHECKING:
    from fluxer_panel.cards import Card

MAX_PER_PROPOSER = 3   # upcoming sessions per proposer per card
MAX_PER_CARD = 10      # upcoming sessions per card


class SessionCapReached(Exception):
    def __init__(self, kind: str) -> None:  # "proposer" or "card"
        super().__init__(kind)
        self.kind = kind


@dataclass(frozen=True)
class Session:
    id: int
    card_id: int
    guild_id: str
    channel_id: str
    message_id: str
    starts_at: int
    ends_at: int
    proposer_id: str
    location_text: str | None = field(repr=False)
    seats: int | None
    recurrence: str | None
    series_id: int
    next_posted: bool
    state: str            # "upcoming" | "ended" | "cancelled"
    closed_at: int | None
    moved: bool
    headsup_sent: bool
    created_at: int


_COLUMNS = ("id, card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, location_text, seats, "
            "recurrence, series_id, next_posted, state, closed_at, moved, headsup_sent, created_at")


def _load(row: tuple) -> Session:
    (session_id, card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, location_text, seats,
     recurrence, series_id, next_posted, state, closed_at, moved, headsup_sent, created_at) = row
    return Session(session_id, card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id,
                   location_text, seats, recurrence, series_id, bool(next_posted), state, closed_at, bool(moved),
                   bool(headsup_sent), created_at)


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[Session]:
    return [_load(row) for row in conn.execute(f"SELECT {_COLUMNS} FROM sessions {sql}", params).fetchall()]


def count_upcoming(conn: sqlite3.Connection, card_id: int, proposer_id: str | None = None) -> int:
    if proposer_id is None:
        row = conn.execute("SELECT COUNT(*) FROM sessions WHERE card_id = ? AND state = 'upcoming'",
                           (card_id,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) FROM sessions WHERE card_id = ? AND state = 'upcoming' AND proposer_id = ?",
                           (card_id, proposer_id)).fetchone()
    return int(row[0])


def check_caps(conn: sqlite3.Connection, card_id: int, proposer_id: str | None) -> None:
    """The card cap is checked first. The proposer cap applies only when a proposer is given."""
    if count_upcoming(conn, card_id) >= MAX_PER_CARD:
        raise SessionCapReached("card")
    if proposer_id is not None and count_upcoming(conn, card_id, proposer_id) >= MAX_PER_PROPOSER:
        raise SessionCapReached("proposer")


def add(conn: sqlite3.Connection, *, card_id: int, guild_id: str, channel_id: str, message_id: str,
        starts_at: int, ends_at: int, proposer_id: str, now: int, location_text: str | None = None,
        seats: int | None = None, recurrence: str | None = None, series_id: int | None = None,
        proposer_cap: bool = True) -> int:
    """Inserts an upcoming session with the proposer going. Runs in the caller's transaction, so the caps and the
    insert are one step. A series' first occurrence takes its own id as its series id."""
    check_caps(conn, card_id, proposer_id if proposer_cap else None)
    cur = conn.execute(
        "INSERT INTO sessions (card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, "
        "location_text, seats, recurrence, series_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, location_text, seats,
         recurrence, series_id, now))
    session_id = cur.lastrowid
    conn.execute("UPDATE sessions SET series_id = ? WHERE id = ?",
                 (series_id if series_id is not None else session_id, session_id))
    # The proposer's presence row and state: exactly what the Phase 2 engine gives for one implicit going row.
    # The proposer always takes the first seat, so the state carries no waitlist position.
    conn.execute("INSERT INTO rsvp_reaction (session_id, user_id, choice, added_at, seq, implicit) "
                 "VALUES (?, ?, 'going', ?, 1, 1)", (session_id, proposer_id, now))
    conn.execute("INSERT INTO rsvps (session_id, user_id, choice, responded_at, waitlist_pos) "
                 "VALUES (?, ?, 'going', ?, NULL)", (session_id, proposer_id, now))
    return session_id


def provisional(*, card_id: int, guild_id: str, channel_id: str, starts_at: int, ends_at: int, proposer_id: str,
                now: int, location_text: str | None = None, seats: int | None = None) -> Session:
    """The session that a first post renders before it is stored: no id yet, and every field set explicitly. Nothing
    reads it from the database. The store assigns the real id in `add`."""
    return Session(id=0, card_id=card_id, guild_id=guild_id, channel_id=channel_id, message_id="0",
                   starts_at=starts_at, ends_at=ends_at, proposer_id=proposer_id, location_text=location_text,
                   seats=seats, recurrence=None, series_id=0, next_posted=False, state="upcoming", closed_at=None,
                   moved=False, headsup_sent=False, created_at=now)


def get(conn: sqlite3.Connection, session_id: int) -> Session | None:
    rows = _rows(conn, "WHERE id = ?", (session_id,))
    return rows[0] if rows else None


def get_by_message(conn: sqlite3.Connection, message_id: str) -> Session | None:
    rows = _rows(conn, "WHERE message_id = ?", (message_id,))
    return rows[0] if rows else None


def upcoming_for_card(conn: sqlite3.Connection, card_id: int) -> list[Session]:
    return _rows(conn, "WHERE card_id = ? AND state = 'upcoming' ORDER BY starts_at, id", (card_id,))


def next_for_card(conn: sqlite3.Connection, card_id: int, now: int) -> Session | None:
    """The soonest upcoming session of the card that has not yet ended."""
    rows = _rows(conn, "WHERE card_id = ? AND state = 'upcoming' AND ends_at > ? ORDER BY starts_at, id LIMIT 1",
                 (card_id, now))
    return rows[0] if rows else None


def all_upcoming(conn: sqlite3.Connection) -> list[Session]:
    return _rows(conn, "WHERE state = 'upcoming' ORDER BY starts_at, id")


def conflicts_for(conn: sqlite3.Connection, user_id: str, starts_at: int, ends_at: int) -> list[tuple[Session, str]]:
    """The upcoming sessions where this user is going or waiting, and that overlap [starts_at, ends_at), each with its
    card's title, in start order. The caller shows only the proposer's own conflicts, and never another user."""
    columns = ", ".join(f"s.{name.strip()}" for name in _COLUMNS.split(","))
    rows = conn.execute(
        f"SELECT {columns}, c.title FROM sessions s JOIN cards c ON c.id = s.card_id "
        "JOIN rsvps r ON r.session_id = s.id AND r.user_id = ? "
        "WHERE r.choice IN ('going', 'waiting') AND s.state = 'upcoming' AND s.starts_at < ? AND s.ends_at > ? "
        "ORDER BY s.starts_at, s.id", (user_id, ends_at, starts_at)).fetchall()
    return [(_load(tuple(row[:-1])), row[-1]) for row in rows]


def _close_if(conn: sqlite3.Connection, session_id: int, now: int, state: str) -> bool:
    cur = conn.execute("UPDATE sessions SET state = ?, closed_at = ? WHERE id = ? AND state = 'upcoming'",
                       (state, now, session_id))
    return cur.rowcount == 1


def end_if(conn: sqlite3.Connection, session_id: int, now: int) -> bool:
    return _close_if(conn, session_id, now, "ended")


def cancel_if(conn: sqlite3.Connection, session_id: int, now: int) -> bool:
    return _close_if(conn, session_id, now, "cancelled")


def move_if(conn: sqlite3.Connection, session_id: int, *, starts_at: int, ends_at: int) -> bool:
    """Moves an upcoming session. The move clears the headsup flag so the new time gets its own heads-up."""
    cur = conn.execute("UPDATE sessions SET starts_at = ?, ends_at = ?, moved = 1, headsup_sent = 0 "
                       "WHERE id = ? AND state = 'upcoming'", (starts_at, ends_at, session_id))
    return cur.rowcount == 1


def mark_next_posted_if(conn: sqlite3.Connection, session_id: int) -> bool:
    cur = conn.execute("UPDATE sessions SET next_posted = 1 WHERE id = ? AND next_posted = 0 "
                       "AND recurrence = 'weekly'", (session_id,))
    return cur.rowcount == 1


def mark_headsup_if(conn: sqlite3.Connection, session_id: int) -> bool:
    cur = conn.execute("UPDATE sessions SET headsup_sent = 1 WHERE id = ? AND headsup_sent = 0 AND state = 'upcoming'",
                       (session_id,))
    return cur.rowcount == 1


def set_series_recurrence(conn: sqlite3.Connection, series_id: int, recurrence: str | None) -> int:
    """Sets the recurrence of every occurrence in the series. Returns how many rows changed."""
    cur = conn.execute("UPDATE sessions SET recurrence = ? WHERE series_id = ?", (recurrence, series_id))
    return cur.rowcount


def set_location_if(conn: sqlite3.Connection, session_id: int, text: str | None) -> bool:
    cur = conn.execute("UPDATE sessions SET location_text = ? WHERE id = ? AND state = 'upcoming'",
                       (text, session_id))
    return cur.rowcount == 1


def set_seats_if(conn: sqlite3.Connection, session_id: int, seats: int | None) -> bool:
    cur = conn.execute("UPDATE sessions SET seats = ? WHERE id = ? AND state = 'upcoming'", (seats, session_id))
    return cur.rowcount == 1


# Presence and RSVP rows. The engine is rsvp.apply; these functions read and write the rows around it. Every
# caller runs them inside one db.run transaction.

def presence_of(conn: sqlite3.Connection, session_id: int) -> list[rsvp.Presence]:
    rows = conn.execute("SELECT user_id, choice, seq, implicit FROM rsvp_reaction WHERE session_id = ? ORDER BY seq",
                        (session_id,)).fetchall()
    return [rsvp.Presence(user_id=user_id, choice=choice, seq=seq, implicit=bool(implicit))
            for user_id, choice, seq, implicit in rows]


def rsvps_of(conn: sqlite3.Connection, session_id: int) -> rsvp.Rsvps:
    rows = conn.execute("SELECT user_id, choice, waitlist_pos FROM rsvps WHERE session_id = ?",
                        (session_id,)).fetchall()
    return {user_id: (state, pos) for user_id, state, pos in rows}


def add_presence(conn: sqlite3.Connection, session: Session, user_id: str, choice: str, now: int) -> bool:
    """Adds one RSVP reaction. The proposer's implicit row goes first, so the first real reaction replaces it and
    both rows never coexist. Returns False when the same reaction is already there."""
    if user_id == session.proposer_id:
        conn.execute("DELETE FROM rsvp_reaction WHERE session_id = ? AND user_id = ? AND implicit = 1",
                     (session.id, user_id))
    cur = conn.execute(
        "INSERT INTO rsvp_reaction (session_id, user_id, choice, added_at, seq, implicit) "
        "SELECT ?, ?, ?, ?, COALESCE(MAX(seq), 0) + 1, 0 FROM rsvp_reaction WHERE session_id = ? "
        "ON CONFLICT DO NOTHING",
        (session.id, user_id, choice, now, session.id))
    return cur.rowcount == 1


def remove_presence(conn: sqlite3.Connection, session_id: int, user_id: str, choice: str) -> bool:
    """Removes one real RSVP reaction. The implicit proposer row is never removed here."""
    cur = conn.execute("DELETE FROM rsvp_reaction WHERE session_id = ? AND user_id = ? AND choice = ? AND implicit = 0",
                       (session_id, user_id, choice))
    return cur.rowcount == 1


RECONCILE_ORDER: tuple[rsvp.Choice, ...] = ("going", "maybe", "no")


def reconcile_presence(conn: sqlite3.Connection, session: Session, actual: dict[rsvp.Choice, set[str]], now: int) -> bool:
    """Makes the real (implicit = 0) presence rows match the reactions read from the message. A row whose reaction is
    gone is deleted, and a reaction with no row is added. The proposer's implicit row goes first when they have any
    reaction, since it shares the (session, user, going) key with a real going row. Missing rows go in the fixed order
    going, maybe, no, and ascending user id within a choice, so the seqs are deterministic. The order in which reactions
    were made while the bot was down is unknown, so this order is accepted. Returns whether anything changed."""
    changed = False
    if any(session.proposer_id in users for users in actual.values()):
        cur = conn.execute("DELETE FROM rsvp_reaction WHERE session_id = ? AND user_id = ? AND implicit = 1",
                           (session.id, session.proposer_id))
        if cur.rowcount == 1:
            changed = True
    for user_id, choice in conn.execute(
            "SELECT user_id, choice FROM rsvp_reaction WHERE session_id = ? AND implicit = 0",
            (session.id,)).fetchall():
        if user_id not in actual.get(choice, set()) and remove_presence(conn, session.id, user_id, choice):
            changed = True
    for choice in RECONCILE_ORDER:
        for user_id in sorted(actual.get(choice, set()), key=int):
            if add_presence(conn, session, user_id, choice, now):
                changed = True
    return changed


def write_rsvps(conn: sqlite3.Connection, session_id: int, rsvps: rsvp.Rsvps, now: int) -> None:
    """Replaces the session's RSVP states. A user whose choice is unchanged keeps responded_at; anyone else gets now.
    Waiting rows go in ascending position so the waitlist index never sees a duplicate mid-write."""
    previous = {user_id: (choice, responded_at) for user_id, choice, responded_at in conn.execute(
        "SELECT user_id, choice, responded_at FROM rsvps WHERE session_id = ?", (session_id,)).fetchall()}
    conn.execute("DELETE FROM rsvps WHERE session_id = ?", (session_id,))
    for user_id, (state, pos) in sorted(rsvps.items(), key=lambda item: item[1][1] or 0):
        old_choice, old_at = previous.get(user_id, (None, None))
        responded_at = old_at if old_choice == state else now
        conn.execute("INSERT INTO rsvps (session_id, user_id, choice, responded_at, waitlist_pos) "
                     "VALUES (?, ?, ?, ?, ?)", (session_id, user_id, state, responded_at, pos))


def recompute(conn: sqlite3.Connection, session: Session, seats: int | None, now: int) -> rsvp.Outcome:
    """Runs the engine on the stored presence and states and writes the result. Every presence change calls this in
    the same transaction as the change."""
    outcome = rsvp.apply(rsvps_of(conn, session.id), presence_of(conn, session.id), seats)
    write_rsvps(conn, session.id, outcome.rsvps, now)
    return outcome


def counts_of(conn: sqlite3.Connection, session_id: int) -> rsvp.Counts:
    return rsvp.counts(rsvps_of(conn, session_id))


def effective_seats(session: Session, card: Card) -> int | None:
    return session.seats if session.seats is not None else card.seats


def effective_location(session: Session, card: Card) -> str | None:
    if session.location_text is not None:
        return session.location_text
    return card.location.text if card.location is not None else None
