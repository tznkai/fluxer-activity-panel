# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sessions store against a real SQLite file: the insert and its implicit RSVP rows, the caps, and compare-and-set."""

from __future__ import annotations

import itertools
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from fluxer_panel import sessions
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
