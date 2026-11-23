# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sessions: the rows behind a scheduled game session. Synchronous SQL over one sqlite3 connection, run through
`await db.run(fn)` like the card store.

The caps are checked in the insert's transaction, and every state change is compare-and-set on the state it
expects, so a second command or a late event cannot undo a change that already happened.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from fluxer_panel import mirrors, render, rsvp

if TYPE_CHECKING:
    from fluxer_panel.cards import Card
    from fluxer_panel.subscriptions import Subscription

MAX_PER_PROPOSER = 3   # upcoming sessions per proposer per card
MAX_PER_CARD = 10      # upcoming sessions per card
RECURRENCE_LEAD = 86400   # a weekly series posts its next occurrence this long after the current start
HEADSUP_WINDOW = 1800     # the admins' heads-up goes out this long before a session with people going


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
    scope_sub_id: int | None = None   # None is the home card; otherwise the subscription the session belongs to


_COLUMNS = ("id, card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, location_text, seats, "
            "recurrence, series_id, next_posted, state, closed_at, moved, headsup_sent, created_at, scope_sub_id")


def _load(row: tuple) -> Session:
    (session_id, card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, location_text, seats,
     recurrence, series_id, next_posted, state, closed_at, moved, headsup_sent, created_at, scope_sub_id) = row
    return Session(session_id, card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id,
                   location_text, seats, recurrence, series_id, bool(next_posted), state, closed_at, bool(moved),
                   bool(headsup_sent), created_at, scope_sub_id)


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[Session]:
    return [_load(row) for row in conn.execute(f"SELECT {_COLUMNS} FROM sessions {sql}", params).fetchall()]


def count_upcoming(conn: sqlite3.Connection, card_id: int, proposer_id: str | None = None, *,
                   scope_sub_id: int | None = None) -> int:
    """Upcoming sessions of one scope (NULL is the home card), optionally of one proposer."""
    sql = "SELECT COUNT(*) FROM sessions WHERE card_id = ? AND state = 'upcoming' AND scope_sub_id IS ?"
    params: tuple = (card_id, scope_sub_id)
    if proposer_id is not None:
        sql += " AND proposer_id = ?"
        params += (proposer_id,)
    return int(conn.execute(sql, params).fetchone()[0])


def check_caps(conn: sqlite3.Connection, card_id: int, proposer_id: str | None, *,
               scope_sub_id: int | None = None) -> None:
    """The card cap is checked first. The proposer cap applies only when a proposer is given. Both count one scope."""
    if count_upcoming(conn, card_id, scope_sub_id=scope_sub_id) >= MAX_PER_CARD:
        raise SessionCapReached("card")
    if proposer_id is not None and count_upcoming(conn, card_id, proposer_id,
                                                  scope_sub_id=scope_sub_id) >= MAX_PER_PROPOSER:
        raise SessionCapReached("proposer")


def add(conn: sqlite3.Connection, *, card_id: int, guild_id: str, channel_id: str, message_id: str,
        starts_at: int, ends_at: int, proposer_id: str, now: int, location_text: str | None = None,
        seats: int | None = None, recurrence: str | None = None, series_id: int | None = None,
        proposer_cap: bool = True, maybe: Sequence[str] = (), scope_sub_id: int | None = None) -> int:
    """Inserts an upcoming session with the proposer going. Runs in the caller's transaction, so the caps and the
    insert are one step. A series' first occurrence takes its own id as its series id. Each user in `maybe` gets an
    implicit maybe row after the proposer's, in ascending id order (the proposer and repeats are ignored), and the
    states are recomputed in the same transaction. With `maybe` empty, no statement beyond the insert runs."""
    check_caps(conn, card_id, proposer_id if proposer_cap else None, scope_sub_id=scope_sub_id)
    cur = conn.execute(
        "INSERT INTO sessions (card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, "
        "location_text, seats, recurrence, series_id, created_at, scope_sub_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, location_text, seats,
         recurrence, series_id, now, scope_sub_id))
    session_id = cur.lastrowid
    conn.execute("UPDATE sessions SET series_id = ? WHERE id = ?",
                 (series_id if series_id is not None else session_id, session_id))
    # The proposer's presence row and state: exactly what the Phase 2 engine gives for one implicit going row.
    # The proposer always takes the first seat, so the state carries no waitlist position.
    conn.execute("INSERT INTO rsvp_reaction (session_id, user_id, choice, added_at, seq, implicit) "
                 "VALUES (?, ?, 'going', ?, 1, 1)", (session_id, proposer_id, now))
    conn.execute("INSERT INTO rsvps (session_id, user_id, choice, responded_at, waitlist_pos) "
                 "VALUES (?, ?, 'going', ?, NULL)", (session_id, proposer_id, now))
    voters = sorted(set(maybe) - {proposer_id}, key=int)
    if voters:
        for user_id in voters:
            conn.execute("INSERT INTO rsvp_reaction (session_id, user_id, choice, added_at, seq, implicit) "
                         "SELECT ?, ?, 'maybe', ?, COALESCE(MAX(seq), 0) + 1, 1 FROM rsvp_reaction WHERE session_id = ?",
                         (session_id, user_id, now, session_id))
        # Only the proposer is going, so the seat count cannot change the result: the session's own seats are passed.
        _settle(conn, session_id, seats, now)
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


def upcoming_for_scope(conn: sqlite3.Connection, card_id: int, scope_sub_id: int | None) -> list[Session]:
    return _rows(conn, "WHERE card_id = ? AND state = 'upcoming' AND scope_sub_id IS ? ORDER BY starts_at, id",
                 (card_id, scope_sub_id))


def next_for_scope(conn: sqlite3.Connection, card_id: int, scope_sub_id: int | None, now: int) -> Session | None:
    """The soonest upcoming session of one scope of the card that has not yet ended."""
    rows = _rows(conn, "WHERE card_id = ? AND state = 'upcoming' AND ends_at > ? AND scope_sub_id IS ? "
                       "ORDER BY starts_at, id LIMIT 1", (card_id, now, scope_sub_id))
    return rows[0] if rows else None


def next_for_card(conn: sqlite3.Connection, card_id: int, now: int) -> Session | None:
    """The soonest upcoming home session of the card that has not yet ended. A subscription's session never shows here."""
    return next_for_scope(conn, card_id, None, now)


def list_for_scope(conn: sqlite3.Connection, scope_sub_id: int) -> list[Session]:
    """Every session of one subscription scope, in any state. The unsubscribe delete reads them."""
    return _rows(conn, "WHERE scope_sub_id = ? ORDER BY id", (scope_sub_id,))


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


_QUALIFIED = ", ".join(f"s.{name.strip()}" for name in _COLUMNS.split(","))


def upcoming_for_user(conn: sqlite3.Connection, user_id: str) -> list[tuple[Session, str, str, int | None]]:
    """The upcoming sessions the user proposed or has an RSVP row in, each as (session, card title, the user's state,
    waitlist position). The state is the RSVP choice; a proposer with no RSVP row shows as "proposer"."""
    rows = conn.execute(
        f"SELECT {_QUALIFIED}, c.title, r.choice, r.waitlist_pos FROM sessions s JOIN cards c ON c.id = s.card_id "
        "LEFT JOIN rsvps r ON r.session_id = s.id AND r.user_id = ? "
        "WHERE s.state = 'upcoming' AND (s.proposer_id = ? OR r.user_id IS NOT NULL) "
        "ORDER BY s.starts_at, s.id", (user_id, user_id)).fetchall()
    return [(_load(tuple(row[:-3])), row[-3], row[-2] if row[-2] is not None else "proposer", row[-1])
            for row in rows]


def upcoming_with_titles(conn: sqlite3.Connection) -> list[tuple[Session, str]]:
    """Every upcoming session with its card's title, in start order."""
    rows = conn.execute(
        f"SELECT {_QUALIFIED}, c.title FROM sessions s JOIN cards c ON c.id = s.card_id "
        "WHERE s.state = 'upcoming' ORDER BY s.starts_at, s.id").fetchall()
    return [(_load(tuple(row[:-1])), row[-1]) for row in rows]


def due_to_end(conn: sqlite3.Connection, now: int) -> list[Session]:
    """Upcoming sessions whose end time has passed."""
    return _rows(conn, "WHERE state = 'upcoming' AND ends_at <= ? ORDER BY ends_at, id", (now,))


# A closed session the retention purge may delete, with one parameter: the cutoff. The strict `<` keeps a session
# closed exactly at the cutoff. A weekly occurrence whose successor is not posted yet is kept, because purging it would
# end the series. `IS` is NULL-safe, so a session with no series (recurrence NULL) is never kept by that test.
PURGE_DUE_SQL = ("state <> 'upcoming' AND closed_at < ? "
                 "AND NOT (recurrence IS 'weekly' AND next_posted = 0)")


def closed_before(conn: sqlite3.Connection, cutoff: int) -> list[Session]:
    """The sessions the purge is due to delete, in id order. The purge scrubs each one before its delete."""
    rows = conn.execute(f"SELECT {_COLUMNS} FROM sessions WHERE {PURGE_DUE_SQL} ORDER BY id", (cutoff,)).fetchall()
    return [_load(row) for row in rows]


def recurrence_due(conn: sqlite3.Connection, now: int) -> list[Session]:
    """Weekly occurrences whose successor is not posted yet and is due. Any state: a cancelled or ended occurrence
    still spawns its successor."""
    return _rows(conn, "WHERE recurrence = 'weekly' AND next_posted = 0 AND starts_at + ? <= ? ORDER BY starts_at, id",
                 (RECURRENCE_LEAD, now))


def headsup_candidates(conn: sqlite3.Connection, now: int) -> list[Session]:
    """Upcoming sessions that have not started and are inside their heads-up window, and have not had one yet."""
    return _rows(conn, "WHERE state = 'upcoming' AND headsup_sent = 0 AND starts_at > ? AND starts_at - ? <= ? "
                       "ORDER BY starts_at, id", (now, HEADSUP_WINDOW, now))


def next_times(conn: sqlite3.Connection, now: int) -> list[int]:
    """The soonest future time of each pending job: an end, a recurrence and a heads-up. Work that is already due but
    still pending is never a wake time, so the poller cannot spin on it. Each query ignores NULLs and the times <= now."""
    queries = (
        ("SELECT MIN(ends_at) FROM sessions WHERE state = 'upcoming' AND ends_at > ?", (now,)),
        ("SELECT MIN(starts_at + ?) FROM sessions WHERE recurrence = 'weekly' AND next_posted = 0 AND starts_at + ? > ?",
         (RECURRENCE_LEAD, RECURRENCE_LEAD, now)),
        ("SELECT MIN(starts_at - ?) FROM sessions WHERE state = 'upcoming' AND headsup_sent = 0 AND starts_at - ? > ?",
         (HEADSUP_WINDOW, HEADSUP_WINDOW, now)),
    )
    found: list[int] = []
    for sql, params in queries:
        value = conn.execute(sql, params).fetchone()[0]
        if value is not None:
            found.append(int(value))
    return found


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


def attendees(conn: sqlite3.Connection, session_id: int) -> list[str]:
    """The users who are going or waiting: going first, then the waitlist in position order."""
    rows = conn.execute(
        "SELECT user_id FROM rsvps WHERE session_id = ? AND choice IN ('going', 'waiting') "
        "ORDER BY CASE choice WHEN 'going' THEN 0 ELSE 1 END, COALESCE(waitlist_pos, 0), user_id",
        (session_id,)).fetchall()
    return [row[0] for row in rows]


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


def clear_card_series(conn: sqlite3.Connection, card_id: int) -> int:
    """Stops every weekly series of the card, whatever each occurrence's state: an ended occurrence still posts its
    successor, so an upcoming-only cancel is not enough. Returns how many rows changed."""
    cur = conn.execute("UPDATE sessions SET recurrence = NULL WHERE card_id = ? AND recurrence = 'weekly'", (card_id,))
    return cur.rowcount


def clear_scope_series(conn: sqlite3.Connection, card_id: int, scope_sub_id: int | None) -> int:
    """Stops every weekly series of one scope of the card (None is the home scope), whatever each occurrence's state."""
    cur = conn.execute("UPDATE sessions SET recurrence = NULL WHERE card_id = ? AND scope_sub_id IS ? "
                       "AND recurrence = 'weekly'", (card_id, scope_sub_id))
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


def add_presence(conn: sqlite3.Connection, session: Session, user_id: str, choice: str, now: int, *,
                 via_sub_id: int | None = None) -> bool:
    """Adds one RSVP reaction. A user's implicit row goes first (the proposer's going, or a voter's maybe), so their
    first real reaction replaces it and both rows never coexist. Returns False when the same reaction is already there.
    `via_sub_id` is the mirror the reaction came through (None for the home message). When the same reaction is already
    stored, a home reaction clears its origin and a mirror reaction leaves it alone, so home always wins."""
    conn.execute("DELETE FROM rsvp_reaction WHERE session_id = ? AND user_id = ? AND implicit = 1",
                 (session.id, user_id))
    cur = conn.execute(
        "INSERT INTO rsvp_reaction (session_id, user_id, choice, added_at, seq, implicit, via_sub_id) "
        "SELECT ?, ?, ?, ?, COALESCE(MAX(seq), 0) + 1, 0, ? FROM rsvp_reaction WHERE session_id = ? "
        "ON CONFLICT DO NOTHING",
        (session.id, user_id, choice, now, via_sub_id, session.id))
    if cur.rowcount == 1:
        return True
    if via_sub_id is None:
        conn.execute("UPDATE rsvp_reaction SET via_sub_id = NULL WHERE session_id = ? AND user_id = ? AND choice = ? "
                     "AND implicit = 0", (session.id, user_id, choice))
    return False


def presence_via(conn: sqlite3.Connection, session_id: int, user_id: str) -> int | None:
    """Where the user's RSVP came from: None when any of their rows came from the home message (home wins), else the
    smallest mirror subscription id. None too when the user has no rows."""
    origins = [row[0] for row in conn.execute(
        "SELECT via_sub_id FROM rsvp_reaction WHERE session_id = ? AND user_id = ?", (session_id, user_id)).fetchall()]
    if not origins or any(origin is None for origin in origins):
        return None
    return min(origins)


def remove_presence(conn: sqlite3.Connection, session_id: int, user_id: str, choice: str) -> bool:
    """Removes one real RSVP reaction. The implicit proposer row is never removed here."""
    cur = conn.execute("DELETE FROM rsvp_reaction WHERE session_id = ? AND user_id = ? AND choice = ? AND implicit = 0",
                       (session_id, user_id, choice))
    return cur.rowcount == 1


RECONCILE_ORDER: tuple[rsvp.Choice, ...] = ("going", "maybe", "no")


def origin_subs(conn: sqlite3.Connection, session_id: int) -> frozenset[int]:
    """The mirror subscriptions the session's real RSVP rows came through. A row keeps its origin after the mirror is
    gone, so this can name a subscription that no longer has a mirror of the session."""
    rows = conn.execute("SELECT DISTINCT via_sub_id FROM rsvp_reaction WHERE session_id = ? AND implicit = 0 "
                        "AND via_sub_id IS NOT NULL", (session_id,)).fetchall()
    return frozenset(int(row[0]) for row in rows)


def presence_from_subs(conn: sqlite3.Connection, session_id: int, sub_ids: Iterable[int]) -> list[tuple[str, str, int]]:
    """The real RSVP rows of a session whose origin is one of the given mirror subscriptions, as (user id, choice,
    subscription id), in user id order."""
    ids = sorted(sub_ids)
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT user_id, choice, via_sub_id FROM rsvp_reaction WHERE session_id = ? AND implicit = 0 "
        f"AND via_sub_id IN ({marks})", (session_id, *ids)).fetchall()
    return sorted(((user_id, choice, sub_id) for user_id, choice, sub_id in rows), key=lambda row: int(row[0]))


def set_presence_via(conn: sqlite3.Connection, session_id: int, user_id: str, via_sub_id: int | None) -> None:
    """Sets where a user's real RSVP rows came from: None for the home message, else the mirror's subscription. Only the
    startup reconcile and an ignored remove call it, so home always wins when the reconcile computes it."""
    conn.execute("UPDATE rsvp_reaction SET via_sub_id = ? WHERE session_id = ? AND user_id = ? AND implicit = 0",
                 (via_sub_id, session_id, user_id))


def reconcile_presence(conn: sqlite3.Connection, session: Session, actual: dict[rsvp.Choice, set[str]], now: int,
                       via: Mapping[str, int | None] | None = None,
                       kept: frozenset[tuple[str, str]] = frozenset()) -> bool:
    """Makes the real (implicit = 0) presence rows match the reactions read from the message and its mirrors. A row whose
    reaction is gone is deleted, and a reaction with no row is added. Each user who has any reaction on the message loses
    their implicit row first (the proposer's going, or a voter's maybe), since it can share a key with a real row. Missing
    rows go in the fixed order going, maybe, no, and ascending user id within a choice, so the seqs are deterministic.
    `via` gives each user's origin (None for home): new rows take it, and the rows of users still reacting are set to it.
    `kept` lists the (user, choice) rows that are already stored and must stay as they are, with their origin: the
    reactions of a 403 mirror the bot cannot read. Neither their add nor their origin is changed. The order in which
    reactions were made while the bot was down is unknown, so this order is accepted. Returns whether a row changed (an
    origin change alone is not a change to the roster)."""
    changed = False
    for user_id in sorted({user for users in actual.values() for user in users}, key=int):
        cur = conn.execute("DELETE FROM rsvp_reaction WHERE session_id = ? AND user_id = ? AND implicit = 1",
                           (session.id, user_id))
        if cur.rowcount > 0:
            changed = True
    for user_id, choice in conn.execute(
            "SELECT user_id, choice FROM rsvp_reaction WHERE session_id = ? AND implicit = 0",
            (session.id,)).fetchall():
        if user_id not in actual.get(choice, set()) and remove_presence(conn, session.id, user_id, choice):
            changed = True
    for choice in RECONCILE_ORDER:
        for user_id in sorted(actual.get(choice, set()), key=int):
            if (user_id, choice) in kept:
                continue
            origin = None if via is None else via.get(user_id)
            if add_presence(conn, session, user_id, choice, now, via_sub_id=origin):
                changed = True
    if via is not None:
        for user_id, choice in conn.execute(
                "SELECT user_id, choice FROM rsvp_reaction WHERE session_id = ? AND implicit = 0",
                (session.id,)).fetchall():
            if (user_id, choice) not in kept:
                conn.execute("UPDATE rsvp_reaction SET via_sub_id = ? WHERE session_id = ? AND user_id = ? "
                             "AND choice = ? AND implicit = 0", (via.get(user_id), session.id, user_id, choice))
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
    return _settle(conn, session.id, seats, now)


def _settle(conn: sqlite3.Connection, session_id: int, seats: int | None, now: int) -> rsvp.Outcome:
    outcome = rsvp.apply(rsvps_of(conn, session_id), presence_of(conn, session_id), seats)
    write_rsvps(conn, session_id, outcome.rsvps, now)
    return outcome


def counts_of(conn: sqlite3.Connection, session_id: int) -> rsvp.Counts:
    return rsvp.counts(rsvps_of(conn, session_id))


def effective_seats(session: Session, card: Card) -> int | None:
    return session.seats if session.seats is not None else card.seats


def effective_location(session: Session, card: Card) -> str | None:
    if session.location_text is not None:
        return session.location_text
    return card.location.text if card.location is not None else None


def next_session_views(conn: sqlite3.Connection, card: Card, now: int
                       ) -> tuple[render.NextSession | None, render.NextSession | None]:
    """The card's next session as the card shows it, read in one go: the near view, with the jump link to the session's
    message, and the far view, the same without the link (a copy outside the games community). Both are None when the card
    has no session that has not ended yet. Only whether a location is set is passed on, never its text."""
    session = next_for_card(conn, card.id, now)
    if session is None:
        return None, None
    near = _next_view(conn, card, session)
    return near, replace(near, jump=None)


def scope_session_view(conn: sqlite3.Connection, card: Card, sub_id: int, now: int) -> render.NextSession | None:
    """The next session of one subscription scope, as that scope's copy shows it, with the jump link to the session's
    message. The session lives in the subscription's own community, so the link stays inside it. None when the scope has
    no upcoming session that has not ended."""
    session = next_for_scope(conn, card.id, sub_id, now)
    return None if session is None else _next_view(conn, card, session)


def both_session_view(conn: sqlite3.Connection, card: Card, sub: Subscription, now: int
                      ) -> render.NextSession | None:
    """The next session of a both copy: the sooner (by start, then id) of the copy's own next session, with its own
    jump, and the card's next home session. The home session is shown with the link to its mirror in this copy, or with
    no link when the copy has no mirror of it. It never carries the home message's link. None when neither exists."""
    own = next_for_scope(conn, card.id, sub.id, now)
    home = next_for_scope(conn, card.id, None, now)
    if home is None:
        return None if own is None else _next_view(conn, card, own)
    if own is not None and (own.starts_at, own.id) < (home.starts_at, home.id):
        return _next_view(conn, card, own)
    mirror = mirrors.get(conn, "session", home.id, sub.id)
    jump = None if mirror is None else (sub.guild_id, mirror.channel_id, mirror.message_id)
    return _view(conn, card, home, jump)


def _next_view(conn: sqlite3.Connection, card: Card, session: Session) -> render.NextSession:
    return _view(conn, card, session, (session.guild_id, session.channel_id, session.message_id))


def _view(conn: sqlite3.Connection, card: Card, session: Session, jump: tuple[str, str, str] | None
          ) -> render.NextSession:
    return render.NextSession(starts_at=session.starts_at, going=counts_of(conn, session.id).going, jump=jump,
                              has_location=effective_location(session, card) is not None)
