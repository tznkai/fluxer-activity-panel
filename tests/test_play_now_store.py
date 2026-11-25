# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The play-now store against a real SQLite file: a press is stored once, expires once, and is found while live."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fluxer_panel import cards, play_now, subscriptions
from fluxer_panel.db import Database
from fluxer_panel.play_now import PlayNow

GUILD = "1001"
CHANNEL = "2001"
PRESSER = "1101"
MESSAGE = "5001"
OTHER_MESSAGE = "5002"
TTL = 3600


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


async def press(db: Database, card_id: int, message_id: str, now: int, *, offers_wake: bool = True) -> int:
    return await db.run(lambda c: play_now.add(
        c, card_id=card_id, guild_id=GUILD, channel_id=CHANNEL, message_id=message_id, presser_id=PRESSER,
        offers_wake=offers_wake, now=now, expires_at=now + TTL))


async def test_a_press_round_trips_through_the_live_lookup(db: Database, card_id: int) -> None:
    row_id = await press(db, card_id, MESSAGE, 100, offers_wake=False)
    found = await db.run(lambda c: play_now.get_live_by_message(c, MESSAGE))
    assert found == PlayNow(id=row_id, card_id=card_id, guild_id=GUILD, channel_id=CHANNEL, message_id=MESSAGE,
                            presser_id=PRESSER, offers_wake=False, state="live", created_at=100,
                            expires_at=100 + TTL)


async def test_an_unknown_message_is_not_found(db: Database, card_id: int) -> None:
    assert await db.run(lambda c: play_now.get_live_by_message(c, OTHER_MESSAGE)) is None


async def test_expire_if_expires_a_live_row_once(db: Database, card_id: int) -> None:
    row_id = await press(db, card_id, MESSAGE, 100)
    assert await db.run(lambda c: play_now.expire_if(c, row_id)) is True
    assert await db.run(lambda c: play_now.expire_if(c, row_id)) is False


async def test_an_expired_row_is_no_longer_found_while_live(db: Database, card_id: int) -> None:
    row_id = await press(db, card_id, MESSAGE, 100)
    await db.run(lambda c: play_now.expire_if(c, row_id))
    assert await db.run(lambda c: play_now.get_live_by_message(c, MESSAGE)) is None


async def test_due_lists_only_live_rows_past_their_expiry_in_id_order(db: Database, card_id: int) -> None:
    early = await press(db, card_id, MESSAGE, 100)                 # expires at 3700
    late = await press(db, card_id, OTHER_MESSAGE, 2000)           # expires at 5600
    expired = await press(db, card_id, "5003", 50)                 # expires at 3650, then expired below
    await db.run(lambda c: play_now.expire_if(c, expired))
    assert [row.id for row in await db.run(lambda c: play_now.due(c, 3700))] == [early]
    assert [row.id for row in await db.run(lambda c: play_now.due(c, 6000))] == [early, late]


async def test_latest_for_card_returns_the_newest_row_in_any_state(db: Database, card_id: int) -> None:
    assert await db.run(lambda c: play_now.latest_for_card(c, card_id)) is None
    await press(db, card_id, MESSAGE, 100)
    newest = await press(db, card_id, OTHER_MESSAGE, 200)
    await db.run(lambda c: play_now.expire_if(c, newest))
    latest = await db.run(lambda c: play_now.latest_for_card(c, card_id))
    assert latest is not None and (latest.id, latest.state) == (newest, "expired")


async def test_a_message_id_is_stored_once(db: Database, card_id: int) -> None:
    await press(db, card_id, MESSAGE, 100)
    with pytest.raises(sqlite3.IntegrityError):
        await press(db, card_id, MESSAGE, 200)


async def _sub(db: Database, card_id: int) -> int:
    return await db.run(lambda c: subscriptions.add(c, card_id, GUILD, "2050"))


async def test_latest_for_scope_is_independent_per_scope(db: Database, card_id: int) -> None:
    sub = await _sub(db, card_id)
    home = await press(db, card_id, MESSAGE, 1000)
    scoped = await db.run(lambda c: play_now.add(
        c, card_id=card_id, guild_id=GUILD, channel_id="2050", message_id=OTHER_MESSAGE, presser_id=PRESSER,
        offers_wake=True, now=1001, expires_at=1001 + TTL, scope_sub_id=sub))
    home_latest = await db.run(lambda c: play_now.latest_for_scope(c, card_id, None))
    scoped_latest = await db.run(lambda c: play_now.latest_for_scope(c, card_id, sub))
    assert home_latest is not None and home_latest.id == home
    assert scoped_latest is not None and scoped_latest.id == scoped
    assert scoped_latest.scope_sub_id == sub
    assert home_latest.scope_sub_id is None
    card_latest = await db.run(lambda c: play_now.latest_for_card(c, card_id))
    assert card_latest is not None and card_latest.id == home  # the home card never reads a subscription's post


async def test_live_lists_split_by_scope_and_live_for_card_takes_every_scope(db: Database, card_id: int) -> None:
    sub = await _sub(db, card_id)
    home = await press(db, card_id, MESSAGE, 1000)
    scoped = await db.run(lambda c: play_now.add(
        c, card_id=card_id, guild_id=GUILD, channel_id="2050", message_id=OTHER_MESSAGE, presser_id=PRESSER,
        offers_wake=True, now=1001, expires_at=1001 + TTL, scope_sub_id=sub))
    assert [row.id for row in await db.run(lambda c: play_now.live_for_scope(c, card_id, None))] == [home]
    assert [row.id for row in await db.run(lambda c: play_now.live_for_scope(c, card_id, sub))] == [scoped]
    assert {row.id for row in await db.run(lambda c: play_now.live_for_card(c, card_id))} == {home, scoped}
    assert [row.id for row in await db.run(lambda c: play_now.list_for_scope(c, sub))] == [scoped]
