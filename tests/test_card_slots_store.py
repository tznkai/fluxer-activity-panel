# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Card slots in the store: migration 0007 from a real v6 file, three slots of each kind, suffixes and public flags."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import fluxer_panel.db as dbmod
from fluxer_panel import cards
from fluxer_panel.cards import Address, ChannelLink
from fluxer_panel.db import Database

NOW = 1_800_000_000


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


async def _create(db: Database, slug: str = "alpha") -> int:
    return await db.run(lambda c: cards.create_card(c, slug=slug, title="Alpha", author_id="1001", now=NOW))


async def _get(db: Database, card_id: int) -> cards.Card:
    card = await db.run(lambda c: cards.get_by_id(c, card_id))
    assert card is not None
    return card


# Migration 0007 from a v6 database --------------------------------------------------------------------

async def test_v6_slots_migrate_to_slot_one_with_no_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "panel.db"
    real_migrations = dbmod.load_migrations()
    assert [version for version, _ in real_migrations] == [1, 2, 3, 4, 5, 6, 7]

    monkeypatch.setattr(dbmod, "load_migrations", lambda: real_migrations[:6])
    monkeypatch.setattr(dbmod, "LATEST_VERSION", 6)
    Database.open(path).close()
    with contextlib.closing(sqlite3.connect(path)) as c:
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("INSERT INTO cards (slug, title, created_by, created_at, updated_at) VALUES ('mc', 'MC', '1001', 1, 1)")
        c.execute("INSERT INTO card_voice (card_id, guild_id, channel_id, channel_name, verified) "
                  "VALUES (1, '2001', '3001', 'Lobby', 1)")
        c.executemany("INSERT INTO card_text_channel (card_id, slot, guild_id, channel_id, channel_name, verified) "
                      "VALUES (1, ?, '2001', ?, ?, ?)",
                      [(1, "3101", "builds", 1), (2, "3102", None, 0), (3, "3103", "rules", 1)])
        c.execute("INSERT INTO card_address (card_id, host, port, public) VALUES (1, 'play.invalid', 25565, 1)")
        c.commit()

    monkeypatch.undo()
    database = Database.open(path)
    try:
        assert path.with_name(path.name + ".pre-v7").exists()
        version = await database.run(lambda c: c.execute("PRAGMA user_version").fetchone()[0])
        card = await database.run(lambda c: cards.get_by_slug(c, "mc"))
    finally:
        database.close()

    assert version == dbmod.LATEST_VERSION == 7
    assert card is not None
    assert card.voices == (ChannelLink("2001", "3001", "Lobby", True), None, None)
    assert card.text_channels == (ChannelLink("2001", "3101", "builds", True), ChannelLink("2001", "3102", None, False),
                                  ChannelLink("2001", "3103", "rules", True))
    assert card.addresses == (Address("play.invalid", 25565, True), None, None)
    assert all(link.suffix is None for link in card.text_channels if link is not None)


# Three slots of each kind -----------------------------------------------------------------------------

async def test_each_voice_slot_round_trips_on_its_own(db: Database) -> None:
    card_id = await _create(db)
    second = ChannelLink("2001", "3002", "Raid", True, suffix="Raid night")
    await db.run(lambda c: cards.set_voice(c, card_id, second, slot=2))

    card = await _get(db, card_id)

    assert card.voices == (None, second, None)
    assert (card.voice, card.voice2, card.voice3) == card.voices


async def test_each_address_slot_round_trips_with_its_own_public_flag(db: Database) -> None:
    card_id = await _create(db)
    java = Address("lan-only.invalid", 25565, False, suffix="Java")
    bedrock = Address("play.invalid", 19132, True, suffix="Bedrock")
    await db.run(lambda c: cards.set_address(c, card_id, java, slot=1))
    await db.run(lambda c: cards.set_address(c, card_id, bedrock, slot=3))

    card = await _get(db, card_id)

    assert card.addresses == (java, None, bedrock)
    assert (card.address, card.address2, card.address3) == card.addresses


async def test_set_address_public_changes_only_its_slot(db: Database) -> None:
    card_id = await _create(db)
    await db.run(lambda c: cards.set_address(c, card_id, Address("a.invalid", 1, False), slot=1))
    await db.run(lambda c: cards.set_address(c, card_id, Address("b.invalid", 2, False), slot=2))

    assert await db.run(lambda c: cards.set_address_public(c, card_id, True, slot=2)) is True
    assert await db.run(lambda c: cards.set_address_public(c, card_id, True, slot=3)) is False

    card = await _get(db, card_id)
    assert [a.public if a else None for a in card.addresses] == [False, True, None]


async def test_clearing_a_slot_never_moves_another_into_its_place(db: Database) -> None:
    card_id = await _create(db)
    for slot in (1, 2, 3):
        await db.run(lambda c, s=slot: cards.set_voice(c, card_id, ChannelLink("2001", f"300{s}", None, False), slot=s))
        await db.run(lambda c, s=slot: cards.set_address(c, card_id, Address(f"h{s}.invalid", s, False), slot=s))

    await db.run(lambda c: cards.clear_voice(c, card_id, 1))
    await db.run(lambda c: cards.clear_address(c, card_id, 2))

    card = await _get(db, card_id)
    assert [v.channel_id if v else None for v in card.voices] == [None, "3002", "3003"]
    assert [a.host if a else None for a in card.addresses] == ["h1.invalid", None, "h3.invalid"]


async def test_voice_name_refresh_is_keyed_by_slot(db: Database) -> None:
    card_id = await _create(db)
    read = ChannelLink("2001", "3003", "Old", True)
    await db.run(lambda c: cards.set_voice(c, card_id, read, slot=3))

    assert await db.run(lambda c: cards.set_voice_name_if(c, card_id, read, "New", slot=1)) is False
    assert await db.run(lambda c: cards.set_voice_name_if(c, card_id, read, "New", slot=3)) is True

    card = await _get(db, card_id)
    assert card.voice3 == ChannelLink("2001", "3003", "New", True)


# Suffixes ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["voice", "text_channel", "address"])
async def test_a_suffix_is_written_only_on_a_slot_that_has_a_value(db: Database, kind: str) -> None:
    card_id = await _create(db)
    await _fill(db, card_id, kind, 2)

    assert await db.run(lambda c: cards.set_suffix(c, card_id, kind, 1, "Empty")) is False
    assert await db.run(lambda c: cards.set_suffix(c, card_id, kind, 2, "Builds")) is True

    assert [_suffix(s) for s in _slots(await _get(db, card_id), kind)] == [None, "Builds", None]


@pytest.mark.parametrize("kind", ["voice", "text_channel", "address"])
async def test_clearing_a_suffix_keeps_the_slot(db: Database, kind: str) -> None:
    card_id = await _create(db)
    await _fill(db, card_id, kind, 1)
    await db.run(lambda c: cards.set_suffix(c, card_id, kind, 1, "Main"))

    assert await db.run(lambda c: cards.set_suffix(c, card_id, kind, 1, None)) is True

    [first, _, _] = _slots(await _get(db, card_id), kind)
    assert first is not None and first.suffix is None


def test_set_suffix_refuses_an_unknown_kind_before_any_sql() -> None:
    with pytest.raises(KeyError):
        cards.set_suffix(None, 1, "cards; DROP TABLE cards", 1, "x")   # type: ignore[arg-type]


async def _fill(db: Database, card_id: int, kind: str, slot: int) -> None:
    link = ChannelLink("2001", f"300{slot}", None, False)
    if kind == "voice":
        await db.run(lambda c: cards.set_voice(c, card_id, link, slot=slot))
    elif kind == "text_channel":
        await db.run(lambda c: cards.set_text_channel(c, card_id, slot, link))
    else:
        await db.run(lambda c: cards.set_address(c, card_id, Address("h.invalid", 1, False), slot=slot))


def _slots(card: cards.Card, kind: str) -> tuple[ChannelLink | Address | None, ...]:
    return {"voice": card.voices, "text_channel": card.text_channels, "address": card.addresses}[kind]


def _suffix(slot: ChannelLink | Address | None) -> str | None:
    return None if slot is None else slot.suffix


@pytest.mark.parametrize("suffix", ["Raid night", "Geyser-Bedrock", "Ñandú 2", "O'Brien+.", "s" * 20, "日本"])
async def test_the_schema_accepts_every_suffix_the_validator_allows(db: Database, suffix: str) -> None:
    card_id = await _create(db)
    await db.run(lambda c: cards.set_voice(c, card_id, ChannelLink("2001", "3001", None, False, suffix=suffix), slot=1))

    assert (await _get(db, card_id)).voice == ChannelLink("2001", "3001", None, False, suffix=suffix)
