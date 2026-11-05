# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The wake request store against a real SQLite file: one open request per requester and card, the close that fires
once, the open summaries, and the admin DM rows."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fluxer_panel import cards, wake_requests
from fluxer_panel.db import Database
from fluxer_panel.wake_requests import AdminDm

ALICE = "1101"
BOB = "1102"
ADMIN_A = "1201"
ADMIN_B = "1202"
MESSAGE = "5001"
OTHER_MESSAGE = "5002"
CHANNEL = "2001"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def card_id(db: Database) -> int:
    return await db.run(lambda c: cards.create_card(c, slug="mc", title="Minecraft", author_id="1001", now=1))


async def test_a_repeat_open_by_the_same_requester_inserts_no_row(db: Database, card_id: int) -> None:
    assert await db.run(lambda c: wake_requests.add_open(c, card_id, ALICE, 100)) is True
    assert await db.run(lambda c: wake_requests.add_open(c, card_id, ALICE, 200)) is False
    assert await db.run(lambda c: wake_requests.open_requesters(c, card_id)) == [ALICE]
    rows = await db.run(lambda c: c.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
    assert rows == 1


async def test_a_second_requester_is_counted_separately(db: Database, card_id: int) -> None:
    await db.run(lambda c: wake_requests.add_open(c, card_id, ALICE, 100))
    assert await db.run(lambda c: wake_requests.add_open(c, card_id, BOB, 110)) is True
    assert await db.run(lambda c: wake_requests.open_counts(c)) == {card_id: 2}


async def test_open_requesters_are_listed_oldest_first(db: Database, card_id: int) -> None:
    await db.run(lambda c: wake_requests.add_open(c, card_id, BOB, 110))
    await db.run(lambda c: wake_requests.add_open(c, card_id, ALICE, 100))
    assert await db.run(lambda c: wake_requests.open_requesters(c, card_id)) == [ALICE, BOB]


async def test_close_returns_every_open_requester_once(db: Database, card_id: int) -> None:
    await db.run(lambda c: wake_requests.add_open(c, card_id, BOB, 110))
    await db.run(lambda c: wake_requests.add_open(c, card_id, ALICE, 100))
    closed = await db.run(lambda c: wake_requests.close(c, card_id, "expired", 500))
    assert closed == [ALICE, BOB]
    assert await db.run(lambda c: wake_requests.close(c, card_id, "expired", 600)) == []
    assert await db.run(lambda c: wake_requests.open_counts(c)) == {}


async def test_after_a_close_the_same_requester_opens_a_new_request(db: Database, card_id: int) -> None:
    await db.run(lambda c: wake_requests.add_open(c, card_id, ALICE, 100))
    await db.run(lambda c: wake_requests.close(c, card_id, "ready", 500))
    assert await db.run(lambda c: wake_requests.add_open(c, card_id, ALICE, 600)) is True
    assert await db.run(lambda c: wake_requests.open_requesters(c, card_id)) == [ALICE]


async def test_close_records_the_status_and_the_close_time(db: Database, card_id: int) -> None:
    await db.run(lambda c: wake_requests.add_open(c, card_id, ALICE, 100))
    await db.run(lambda c: wake_requests.close(c, card_id, "cleared", 500))
    row = await db.run(lambda c: c.execute("SELECT status, closed_at FROM requests").fetchone())
    assert tuple(row) == ("cleared", 500)


async def test_close_refuses_a_status_it_does_not_own(db: Database, card_id: int) -> None:
    await db.run(lambda c: wake_requests.add_open(c, card_id, ALICE, 100))
    with pytest.raises(ValueError):
        await db.run(lambda c: wake_requests.close(c, card_id, "done", 500))
    assert await db.run(lambda c: wake_requests.open_requesters(c, card_id)) == [ALICE]


async def test_close_touches_only_the_named_card(db: Database, card_id: int) -> None:
    other = await db.run(lambda c: cards.create_card(c, slug="ark", title="Ark", author_id="1001", now=1))
    await db.run(lambda c: wake_requests.add_open(c, card_id, ALICE, 100))
    await db.run(lambda c: wake_requests.add_open(c, other, ALICE, 100))
    await db.run(lambda c: wake_requests.close(c, card_id, "expired", 500))
    assert await db.run(lambda c: wake_requests.open_counts(c)) == {other: 1}


async def test_open_summaries_report_the_oldest_open_row_per_card(db: Database, card_id: int) -> None:
    other = await db.run(lambda c: cards.create_card(c, slug="ark", title="Ark", author_id="1001", now=1))
    await db.run(lambda c: wake_requests.add_open(c, card_id, ALICE, 300))
    await db.run(lambda c: wake_requests.add_open(c, card_id, BOB, 100))
    await db.run(lambda c: wake_requests.add_open(c, other, ALICE, 200))
    summaries = await db.run(lambda c: wake_requests.open_summaries(c))
    assert summaries == [
        wake_requests.OpenSummary(card_id=card_id, count=2, oldest_at=100),
        wake_requests.OpenSummary(card_id=other, count=1, oldest_at=200),
    ]


async def test_put_dm_inserts_then_updates_the_same_admin_row(db: Database, card_id: int) -> None:
    first = AdminDm(card_id, ADMIN_A, "request", CHANNEL, MESSAGE, 100)
    second = AdminDm(card_id, ADMIN_A, "signal", CHANNEL, OTHER_MESSAGE, 200)
    await db.run(lambda c: wake_requests.put_dm(c, first))
    await db.run(lambda c: wake_requests.put_dm(c, second))
    assert await db.run(lambda c: wake_requests.get_dms(c, card_id)) == [second]


async def test_get_dms_lists_admins_in_id_order(db: Database, card_id: int) -> None:
    await db.run(lambda c: wake_requests.put_dm(c, AdminDm(card_id, ADMIN_B, "request", CHANNEL, MESSAGE, 100)))
    await db.run(lambda c: wake_requests.put_dm(c, AdminDm(card_id, ADMIN_A, "request", CHANNEL, OTHER_MESSAGE, 100)))
    listed = await db.run(lambda c: wake_requests.get_dms(c, card_id))
    assert [dm.admin_id for dm in listed] == [ADMIN_A, ADMIN_B]


async def test_drop_dms_removes_only_that_cards_rows(db: Database, card_id: int) -> None:
    other = await db.run(lambda c: cards.create_card(c, slug="ark", title="Ark", author_id="1001", now=1))
    await db.run(lambda c: wake_requests.put_dm(c, AdminDm(card_id, ADMIN_A, "request", CHANNEL, MESSAGE, 100)))
    await db.run(lambda c: wake_requests.put_dm(c, AdminDm(other, ADMIN_A, "request", CHANNEL, MESSAGE, 100)))
    await db.run(lambda c: wake_requests.drop_dms(c, card_id))
    assert await db.run(lambda c: wake_requests.get_dms(c, card_id)) == []
    assert len(await db.run(lambda c: wake_requests.get_dms(c, other))) == 1
