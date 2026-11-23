# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sessions store against a real SQLite file: the insert and its implicit RSVP rows, the caps, and compare-and-set."""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fluxer_panel import rsvp, sessions, subscriptions
from fluxer_panel.db import Database
from fluxer_panel.sessions import SessionCapReached

NOW = 1_800_000_000
HOUR = 3600
_MESSAGE_IDS = itertools.count(5001)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


async def _card(db: Database) -> int:
    """A live card, seeded through raw SQL."""
    return await db.run(lambda c: c.execute(
        "INSERT INTO cards (title, created_by, created_at, updated_at, state) VALUES ('Game', '1001', 1, 1, 'live')"
    ).lastrowid)


async def _add(db: Database, card_id: int, *, proposer: str = "1001", starts_in: int = 2 * HOUR,
               proposer_cap: bool = True, series_id: int | None = None, **extra: Any) -> int:
    message_id = str(next(_MESSAGE_IDS))

    def insert(c: sqlite3.Connection) -> int:
        return sessions.add(c, card_id=card_id, guild_id="3001", channel_id="3002", message_id=message_id,
                            starts_at=NOW + starts_in, ends_at=NOW + starts_in + HOUR, proposer_id=proposer,
                            now=NOW, proposer_cap=proposer_cap, series_id=series_id, **extra)

    return await db.run(insert)


async def _count_rows(db: Database) -> int:
    return await db.run(lambda c: c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])


async def _get(db: Database, session_id: int) -> sessions.Session:
    session = await db.run(lambda c: sessions.get(c, session_id))
    assert session is not None
    return session


# add ------------------------------------------------------------------------------------------------

async def test_add_returns_an_id_and_the_series_is_the_session_itself(db: Database) -> None:
    card = await _card(db)

    session_id = await _add(db, card)

    session = await _get(db, session_id)
    assert session.series_id == session_id
    assert session.state == "upcoming"
    assert session.closed_at is None
    assert (session.moved, session.headsup_sent, session.next_posted) == (False, False, False)


async def test_add_writes_the_proposers_implicit_presence_row_and_going_state(db: Database) -> None:
    card = await _card(db)
    session_id = await _add(db, card, proposer="1001")

    reactions = await db.run(lambda c: c.execute(
        "SELECT user_id, choice, seq, implicit FROM rsvp_reaction WHERE session_id = ?", (session_id,)).fetchall())
    rsvps = await db.run(lambda c: c.execute(
        "SELECT user_id, choice, waitlist_pos FROM rsvps WHERE session_id = ?", (session_id,)).fetchall())

    assert reactions == [("1001", "going", 1, 1)]
    assert rsvps == [("1001", "going", None)]


async def test_add_keeps_a_series_id_it_is_given(db: Database) -> None:
    card = await _card(db)
    first = await _add(db, card)

    second = await _add(db, card, proposer="1002", series_id=first)

    assert (await _get(db, second)).series_id == first


async def test_add_stores_the_optional_fields(db: Database) -> None:
    card = await _card(db)

    session_id = await _add(db, card, location_text="Lobby", seats=6, recurrence="weekly")

    session = await _get(db, session_id)
    assert (session.location_text, session.seats, session.recurrence) == ("Lobby", 6, "weekly")


# Caps -----------------------------------------------------------------------------------------------

async def test_fourth_upcoming_session_by_one_proposer_is_refused(db: Database) -> None:
    card = await _card(db)
    for _ in range(3):
        await _add(db, card, proposer="1001")

    with pytest.raises(SessionCapReached) as info:
        await _add(db, card, proposer="1001")

    assert info.value.kind == "proposer"


async def test_proposer_cap_can_be_skipped_for_a_fourth_session(db: Database) -> None:
    card = await _card(db)
    for _ in range(3):
        await _add(db, card, proposer="1001")

    await _add(db, card, proposer="1001", proposer_cap=False)

    assert await db.run(lambda c: sessions.count_upcoming(c, card, "1001")) == 4


async def test_eleventh_upcoming_session_on_a_card_is_refused_whoever_proposes(db: Database) -> None:
    card = await _card(db)
    for i in range(10):
        await _add(db, card, proposer=str(2000 + i))

    with pytest.raises(SessionCapReached) as info:
        await _add(db, card, proposer="3999")

    assert info.value.kind == "card"


async def test_ended_and_cancelled_sessions_do_not_count_toward_the_caps(db: Database) -> None:
    card = await _card(db)
    ids = [await _add(db, card, proposer="1001") for _ in range(3)]
    await db.run(lambda c: sessions.end_if(c, ids[0], NOW))
    await db.run(lambda c: sessions.cancel_if(c, ids[1], NOW))

    await _add(db, card, proposer="1001")

    assert await db.run(lambda c: sessions.count_upcoming(c, card, "1001")) == 2


async def test_a_refused_add_inserts_nothing(db: Database) -> None:
    card = await _card(db)
    for _ in range(3):
        await _add(db, card, proposer="1001")
    before = await _count_rows(db)

    with pytest.raises(SessionCapReached):
        await _add(db, card, proposer="1001")

    assert await _count_rows(db) == before
    reactions = await db.run(lambda c: c.execute("SELECT COUNT(*) FROM rsvp_reaction").fetchone()[0])
    assert reactions == 3


# Compare-and-set ------------------------------------------------------------------------------------

async def test_end_if_changes_an_upcoming_session_once(db: Database) -> None:
    session_id = await _add(db, await _card(db))

    first = await db.run(lambda c: sessions.end_if(c, session_id, NOW))
    second = await db.run(lambda c: sessions.end_if(c, session_id, NOW + 1))

    assert (first, second) == (True, False)
    session = await _get(db, session_id)
    assert (session.state, session.closed_at) == ("ended", NOW)


async def test_cancel_if_is_refused_once_the_session_has_ended(db: Database) -> None:
    session_id = await _add(db, await _card(db))
    await db.run(lambda c: sessions.end_if(c, session_id, NOW))

    assert await db.run(lambda c: sessions.cancel_if(c, session_id, NOW)) is False
    assert (await _get(db, session_id)).state == "ended"


async def test_move_if_moves_an_upcoming_session_and_clears_its_headsup(db: Database) -> None:
    session_id = await _add(db, await _card(db))
    await db.run(lambda c: sessions.mark_headsup_if(c, session_id))

    moved = await db.run(lambda c: sessions.move_if(c, session_id, starts_at=NOW + 9 * HOUR, ends_at=NOW + 10 * HOUR))

    assert moved is True
    session = await _get(db, session_id)
    assert (session.starts_at, session.ends_at) == (NOW + 9 * HOUR, NOW + 10 * HOUR)
    assert (session.moved, session.headsup_sent) == (True, False)


async def test_move_if_is_refused_for_a_cancelled_session(db: Database) -> None:
    session_id = await _add(db, await _card(db))
    await db.run(lambda c: sessions.cancel_if(c, session_id, NOW))

    moved = await db.run(lambda c: sessions.move_if(c, session_id, starts_at=NOW + 9 * HOUR, ends_at=NOW + 10 * HOUR))

    assert moved is False
    assert (await _get(db, session_id)).moved is False


async def test_mark_next_posted_if_is_true_once_for_a_weekly_series(db: Database) -> None:
    session_id = await _add(db, await _card(db), recurrence="weekly")

    first = await db.run(lambda c: sessions.mark_next_posted_if(c, session_id))
    second = await db.run(lambda c: sessions.mark_next_posted_if(c, session_id))

    assert (first, second) == (True, False)
    assert (await _get(db, session_id)).next_posted is True


async def test_mark_next_posted_if_is_refused_without_a_recurrence(db: Database) -> None:
    session_id = await _add(db, await _card(db))

    assert await db.run(lambda c: sessions.mark_next_posted_if(c, session_id)) is False


async def test_mark_headsup_if_is_true_once(db: Database) -> None:
    session_id = await _add(db, await _card(db))

    first = await db.run(lambda c: sessions.mark_headsup_if(c, session_id))
    second = await db.run(lambda c: sessions.mark_headsup_if(c, session_id))

    assert (first, second) == (True, False)
    assert (await _get(db, session_id)).headsup_sent is True


async def test_location_and_seats_change_only_while_upcoming(db: Database) -> None:
    session_id = await _add(db, await _card(db))
    assert await db.run(lambda c: sessions.set_location_if(c, session_id, "Hall")) is True
    assert await db.run(lambda c: sessions.set_seats_if(c, session_id, 4)) is True

    await db.run(lambda c: sessions.end_if(c, session_id, NOW))

    assert await db.run(lambda c: sessions.set_location_if(c, session_id, "Elsewhere")) is False
    assert await db.run(lambda c: sessions.set_seats_if(c, session_id, 9)) is False
    session = await _get(db, session_id)
    assert (session.location_text, session.seats) == ("Hall", 4)


# Reads ----------------------------------------------------------------------------------------------

async def test_next_for_card_skips_ended_and_past_sessions_and_picks_the_soonest(db: Database) -> None:
    card = await _card(db)
    await _add(db, card, starts_in=-3 * HOUR)                 # ends_at = NOW - 2h: already over
    ended = await _add(db, card, starts_in=1 * HOUR)
    await db.run(lambda c: sessions.end_if(c, ended, NOW))
    soonest = await _add(db, card, starts_in=2 * HOUR)
    await _add(db, card, starts_in=5 * HOUR)

    chosen = await db.run(lambda c: sessions.next_for_card(c, card, NOW))

    assert chosen is not None
    assert chosen.id == soonest


async def test_next_for_card_is_none_when_nothing_is_upcoming(db: Database) -> None:
    card = await _card(db)

    assert await db.run(lambda c: sessions.next_for_card(c, card, NOW)) is None


async def test_upcoming_lists_are_ordered_by_start_then_id(db: Database) -> None:
    card = await _card(db)
    late = await _add(db, card, starts_in=5 * HOUR)
    early = await _add(db, card, starts_in=2 * HOUR)
    other = await _card(db)
    elsewhere = await _add(db, other, starts_in=1 * HOUR)
    await db.run(lambda c: sessions.end_if(c, elsewhere, NOW))

    per_card = await db.run(lambda c: sessions.upcoming_for_card(c, card))
    everything = await db.run(lambda c: sessions.all_upcoming(c))

    assert [s.id for s in per_card] == [early, late]
    assert [s.id for s in everything] == [early, late]


async def test_get_by_message_finds_the_session_by_its_message(db: Database) -> None:
    session_id = await _add(db, await _card(db))
    message_id = (await _get(db, session_id)).message_id

    found = await db.run(lambda c: sessions.get_by_message(c, message_id))

    assert found is not None
    assert found.id == session_id


# set_series_recurrence ------------------------------------------------------------------------------

async def test_set_series_recurrence_touches_only_that_series(db: Database) -> None:
    card = await _card(db)
    first = await _add(db, card, starts_in=2 * HOUR)
    second = await _add(db, card, proposer="1002", starts_in=9 * HOUR, series_id=first)
    other = await _add(db, card, proposer="1003", starts_in=4 * HOUR)

    changed = await db.run(lambda c: sessions.set_series_recurrence(c, first, "weekly"))

    assert changed == 2
    assert (await _get(db, first)).recurrence == "weekly"
    assert (await _get(db, second)).recurrence == "weekly"
    assert (await _get(db, other)).recurrence is None


# Presence and RSVP store (Phase 2) ------------------------------------------------------------------

async def _presence(db: Database, session_id: int) -> list[tuple[str, str, int, bool]]:
    rows = await db.run(lambda c: sessions.presence_of(c, session_id))
    return [(p.user_id, p.choice, p.seq, p.implicit) for p in rows]


async def _rsvps(db: Database, session_id: int) -> rsvp.Rsvps:
    return await db.run(lambda c: sessions.rsvps_of(c, session_id))


async def test_after_add_one_implicit_going_row_and_the_proposer_is_going(db: Database) -> None:
    session_id = await _add(db, await _card(db), proposer="1001")

    assert await _presence(db, session_id) == [("1001", "going", 1, True)]
    assert await _rsvps(db, session_id) == {"1001": ("going", None)}


async def test_the_proposers_real_reaction_replaces_the_implicit_row(db: Database) -> None:
    session_id = await _add(db, await _card(db), proposer="1001")
    session = await _get(db, session_id)

    inserted = await db.run(lambda c: sessions.add_presence(c, session, "1001", "maybe", NOW))
    outcome = await db.run(lambda c: sessions.recompute(c, session, None, NOW))

    assert inserted is True
    assert await _presence(db, session_id) == [("1001", "maybe", 1, False)]
    assert outcome.rsvps == {"1001": ("maybe", None)}


async def test_other_users_reactions_get_increasing_seqs_and_removing_the_newest_falls_back(db: Database) -> None:
    session_id = await _add(db, await _card(db), proposer="1001")
    session = await _get(db, session_id)
    await db.run(lambda c: sessions.add_presence(c, session, "1002", "going", NOW))
    await db.run(lambda c: sessions.add_presence(c, session, "1002", "maybe", NOW))

    assert [seq for _, _, seq, _ in await _presence(db, session_id)] == [1, 2, 3]

    removed = await db.run(lambda c: sessions.remove_presence(c, session_id, "1002", "maybe"))
    await db.run(lambda c: sessions.recompute(c, session, None, NOW))

    assert removed is True
    assert (await _rsvps(db, session_id))["1002"] == ("going", None)


async def test_remove_presence_never_deletes_the_implicit_row_and_a_duplicate_add_is_false(db: Database) -> None:
    session_id = await _add(db, await _card(db), proposer="1001")
    session = await _get(db, session_id)

    removed = await db.run(lambda c: sessions.remove_presence(c, session_id, "1001", "going"))
    first = await db.run(lambda c: sessions.add_presence(c, session, "1002", "going", NOW))
    second = await db.run(lambda c: sessions.add_presence(c, session, "1002", "going", NOW))

    assert removed is False
    assert (first, second) == (True, False)
    assert ("1001", "going", 1, True) in await _presence(db, session_id)


async def test_write_rsvps_keeps_responded_at_for_unchanged_users(db: Database) -> None:
    session_id = await _add(db, await _card(db), proposer="1001")
    session = await _get(db, session_id)
    await db.run(lambda c: sessions.add_presence(c, session, "1002", "going", NOW))
    await db.run(lambda c: sessions.recompute(c, session, None, NOW))

    later = NOW + 100
    await db.run(lambda c: sessions.add_presence(c, session, "1003", "going", later))
    await db.run(lambda c: sessions.recompute(c, session, None, later))
    even_later = NOW + 200
    await db.run(lambda c: sessions.add_presence(c, session, "1001", "maybe", even_later))
    await db.run(lambda c: sessions.recompute(c, session, None, even_later))

    stamps = await db.run(lambda c: dict(c.execute(
        "SELECT user_id, responded_at FROM rsvps WHERE session_id = ?", (session_id,)).fetchall()))
    assert stamps == {"1001": even_later, "1002": NOW, "1003": later}


async def test_recompute_waitlists_a_second_going_and_promotes_when_the_proposer_says_no(db: Database) -> None:
    session_id = await _add(db, await _card(db), proposer="1001")
    session = await _get(db, session_id)
    await db.run(lambda c: sessions.add_presence(c, session, "1002", "going", NOW))

    first = await db.run(lambda c: sessions.recompute(c, session, 1, NOW))
    assert first.rsvps == {"1001": ("going", None), "1002": ("waiting", 1)}

    await db.run(lambda c: sessions.add_presence(c, session, "1001", "no", NOW + 1))
    second = await db.run(lambda c: sessions.recompute(c, session, 1, NOW + 1))

    assert second.rsvps == {"1001": ("no", None), "1002": ("going", None)}
    assert second.promoted == ["1002"]


async def test_counts_of_totals_the_stored_states(db: Database) -> None:
    session_id = await _add(db, await _card(db), proposer="1001")
    session = await _get(db, session_id)
    await db.run(lambda c: sessions.add_presence(c, session, "1002", "maybe", NOW))
    await db.run(lambda c: sessions.recompute(c, session, None, NOW))

    assert await db.run(lambda c: sessions.counts_of(c, session_id)) == rsvp.Counts(going=1, maybe=1, waiting=0)


# Implicit rows for any user (voters of a scheduled poll) ---------------------------------------------

async def _flags(db: Database, session_id: int) -> list[tuple[str, str, bool]]:
    """The presence rows in seq order without their seqs: (user, choice, implicit)."""
    return [(user, choice, implicit) for user, choice, _, implicit in await _presence(db, session_id)]


async def test_add_with_maybe_writes_one_implicit_maybe_row_per_voter_in_id_order_and_counts_them(db: Database) -> None:
    session_id = await _add(db, await _card(db), proposer="1001", maybe=["1003", "1002"])

    assert await _presence(db, session_id) == [("1001", "going", 1, True), ("1002", "maybe", 2, True),
                                               ("1003", "maybe", 3, True)]
    assert await db.run(lambda c: sessions.counts_of(c, session_id)) == rsvp.Counts(going=1, maybe=2, waiting=0)


async def test_add_ignores_the_proposer_and_repeated_voters_in_maybe(db: Database) -> None:
    session_id = await _add(db, await _card(db), proposer="1001", maybe=["1001", "1002", "1002"])

    assert await _presence(db, session_id) == [("1001", "going", 1, True), ("1002", "maybe", 2, True)]


async def test_a_voters_real_maybe_replaces_their_implicit_maybe_so_the_count_keeps_them(db: Database) -> None:
    session_id = await _add(db, await _card(db), proposer="1001", maybe=["1002"])
    session = await _get(db, session_id)

    inserted = await db.run(lambda c: sessions.add_presence(c, session, "1002", "maybe", NOW))
    await db.run(lambda c: sessions.recompute(c, session, None, NOW))

    assert inserted is True
    assert await _flags(db, session_id) == [("1001", "going", True), ("1002", "maybe", False)]
    assert await db.run(lambda c: sessions.counts_of(c, session_id)) == rsvp.Counts(going=1, maybe=1, waiting=0)


async def test_a_voters_going_takes_the_implicit_row_and_removing_it_leaves_them_no_row(db: Database) -> None:
    session_id = await _add(db, await _card(db), proposer="1001", maybe=["1002"])
    session = await _get(db, session_id)

    await db.run(lambda c: sessions.add_presence(c, session, "1002", "going", NOW))
    await db.run(lambda c: sessions.recompute(c, session, None, NOW))
    assert await _flags(db, session_id) == [("1001", "going", True), ("1002", "going", False)]

    removed = await db.run(lambda c: sessions.remove_presence(c, session_id, "1002", "going"))
    await db.run(lambda c: sessions.recompute(c, session, None, NOW))

    assert removed is True
    assert await _flags(db, session_id) == [("1001", "going", True)]
    assert await _rsvps(db, session_id) == {"1001": ("going", None)}


# Effective seats and location -----------------------------------------------------------------------

def _card_stub(seats: int | None, location_text: str | None) -> Any:
    return SimpleNamespace(seats=seats, location=None if location_text is None else SimpleNamespace(text=location_text))


async def test_session_seats_override_the_card_seats(db: Database) -> None:
    session = await _get(db, await _add(db, await _card(db), seats=6))

    assert sessions.effective_seats(session, _card_stub(4, None)) == 6


async def test_the_card_seats_apply_when_the_session_has_none(db: Database) -> None:
    session = await _get(db, await _add(db, await _card(db)))

    assert sessions.effective_seats(session, _card_stub(4, None)) == 4
    assert sessions.effective_seats(session, _card_stub(None, None)) is None


async def test_session_location_overrides_the_card_location(db: Database) -> None:
    override = await _get(db, await _add(db, await _card(db), location_text="Lobby"))
    plain = await _get(db, await _add(db, await _card(db)))

    assert sessions.effective_location(override, _card_stub(None, "Hall")) == "Lobby"
    assert sessions.effective_location(plain, _card_stub(None, "Hall")) == "Hall"
    assert sessions.effective_location(plain, _card_stub(None, None)) is None


async def test_attendees_are_the_going_and_waiting_users_going_first_then_by_waitlist_position(db: Database) -> None:
    session_id = await _add(db, await _card(db), proposer="1001")
    rows = [("1004", "going", None), ("1005", "maybe", None), ("1006", "no", None),
            ("1007", "waiting", 2), ("1008", "waiting", 1)]

    def seed(c: sqlite3.Connection) -> None:
        for user_id, choice, pos in rows:
            c.execute("INSERT INTO rsvps (session_id, user_id, choice, responded_at, waitlist_pos) "
                      "VALUES (?, ?, ?, ?, ?)", (session_id, user_id, choice, NOW, pos))

    await db.run(seed)

    attendees = await db.run(lambda c: sessions.attendees(c, session_id))

    assert attendees == ["1001", "1004", "1008", "1007"]


# Scopes: a subscription's sessions are kept apart from the home card's ---------------------------------

async def _sub(db: Database, card_id: int, channel_id: str = "3050") -> int:
    return await db.run(lambda c: subscriptions.add(c, card_id, "3001", channel_id))


async def test_rows_added_without_a_scope_read_back_as_home(db: Database) -> None:
    card = await _card(db)
    session_id = await _add(db, card)
    assert (await _get(db, session_id)).scope_sub_id is None


async def test_the_caps_count_only_their_own_scope(db: Database) -> None:
    card = await _card(db)
    sub = await _sub(db, card)
    for index in range(sessions.MAX_PER_CARD):
        await _add(db, card, proposer=str(1101 + index))
    with pytest.raises(SessionCapReached):
        await _add(db, card, proposer="1200")
    scoped = await _add(db, card, proposer="1200", scope_sub_id=sub)
    assert (await _get(db, scoped)).scope_sub_id == sub


async def test_the_proposer_cap_is_per_scope(db: Database) -> None:
    card = await _card(db)
    sub = await _sub(db, card)
    for _ in range(sessions.MAX_PER_PROPOSER):
        await _add(db, card, proposer="1001")
    with pytest.raises(SessionCapReached):
        await _add(db, card, proposer="1001")
    await _add(db, card, proposer="1001", scope_sub_id=sub)


async def test_next_for_scope_and_next_for_card_never_return_the_other_scope(db: Database) -> None:
    card = await _card(db)
    sub = await _sub(db, card)
    home = await _add(db, card, starts_in=2 * HOUR)
    scoped = await _add(db, card, starts_in=1 * HOUR, scope_sub_id=sub)

    def read(c: sqlite3.Connection) -> tuple[int | None, int | None, int | None]:
        home_next = sessions.next_for_card(c, card, NOW)
        scoped_next = sessions.next_for_scope(c, card, sub, NOW)
        home_scope = sessions.next_for_scope(c, card, None, NOW)
        return (home_next and home_next.id, scoped_next and scoped_next.id, home_scope and home_scope.id)

    assert await db.run(read) == (home, scoped, home)


async def test_list_for_scope_returns_only_that_scope_in_any_state(db: Database) -> None:
    card = await _card(db)
    sub = await _sub(db, card)
    await _add(db, card)
    scoped = await _add(db, card, scope_sub_id=sub)
    await db.run(lambda c: sessions.cancel_if(c, scoped, NOW))
    listed = await db.run(lambda c: sessions.list_for_scope(c, sub))
    assert [session.id for session in listed] == [scoped]
    assert listed[0].state == "cancelled"


async def test_the_upcoming_list_for_a_card_keeps_every_scope(db: Database) -> None:
    card = await _card(db)
    sub = await _sub(db, card)
    home = await _add(db, card)
    scoped = await _add(db, card, scope_sub_id=sub)
    upcoming = await db.run(lambda c: sessions.upcoming_for_card(c, card))
    assert {session.id for session in upcoming} == {home, scoped}


async def test_upcoming_for_scope_returns_only_that_scopes_upcoming_rows_in_start_order(db: Database) -> None:
    card = await _card(db)
    sub = await _sub(db, card)
    home = await _add(db, card, starts_in=5 * HOUR)
    later = await _add(db, card, starts_in=4 * HOUR, scope_sub_id=sub)
    cancelled = await _add(db, card, starts_in=6 * HOUR, scope_sub_id=sub)
    earlier = await _add(db, card, starts_in=3 * HOUR, scope_sub_id=sub)
    await db.run(lambda c: sessions.cancel_if(c, cancelled, NOW))

    home_only = await db.run(lambda c: sessions.upcoming_for_scope(c, card, None))
    scoped = await db.run(lambda c: sessions.upcoming_for_scope(c, card, sub))

    assert [session.id for session in home_only] == [home]
    assert [session.id for session in scoped] == [earlier, later]


async def test_a_mirror_only_user_has_the_subscription_as_origin(db: Database) -> None:
    card = await _card(db)
    sub = await _sub(db, card)
    session = await _get(db, await _add(db, card))
    await db.run(lambda c: sessions.add_presence(c, session, "1002", "going", NOW, via_sub_id=sub))
    assert await db.run(lambda c: sessions.presence_via(c, session.id, "1002")) == sub


async def test_home_wins_whichever_reaction_comes_first(db: Database) -> None:
    card = await _card(db)
    sub = await _sub(db, card)
    session = await _get(db, await _add(db, card))
    # Mirror first, then the same reaction on the home message.
    await db.run(lambda c: sessions.add_presence(c, session, "1002", "going", NOW, via_sub_id=sub))
    await db.run(lambda c: sessions.add_presence(c, session, "1002", "going", NOW))
    assert await db.run(lambda c: sessions.presence_via(c, session.id, "1002")) is None
    # Home first, then the mirror.
    await db.run(lambda c: sessions.add_presence(c, session, "1003", "maybe", NOW))
    await db.run(lambda c: sessions.add_presence(c, session, "1003", "maybe", NOW, via_sub_id=sub))
    assert await db.run(lambda c: sessions.presence_via(c, session.id, "1003")) is None


async def test_presence_via_of_a_user_with_no_rows_is_none(db: Database) -> None:
    card = await _card(db)
    session_id = await _add(db, card)
    assert await db.run(lambda c: sessions.presence_via(c, session_id, "1009")) is None
