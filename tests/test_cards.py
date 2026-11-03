# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Card store against a real SQLite file: slots, status sources, message tracking and listing."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fluxer_panel import cards
from fluxer_panel.cards import Address, CardFieldError, Location, ManagerRef, ProbeRef, Voice
from fluxer_panel.db import Database

NOW = 1_800_000_000
HASH = "a" * 64
SENTINEL_SLUG = "sentinel-slug-91c2"

SLOT_SETTERS = {
    "voice": lambda c, card_id: cards.set_voice(c, card_id, Voice("1001", "1002", "general", True)),
    "address": lambda c, card_id: cards.set_address(c, card_id, Address("h.invalid", 25565, False)),
    "location": lambda c, card_id: cards.set_location(c, card_id, Location("Room 4")),
    "seats": lambda c, card_id: cards.set_seats(c, card_id, 4),
    "manager": lambda c, card_id: cards.set_manager(c, card_id, ManagerRef("ptero", "srv-1")),
    "probe": lambda c, card_id: cards.set_probe(c, card_id, ProbeRef("a2s", "h.invalid", 27015)),
}


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


async def _create(db: Database, slug: str = "alpha", title: str = "Alpha") -> int:
    return await db.run(lambda c: cards.create_card(c, slug=slug, title=title, author_id="1001", now=NOW))


async def _get(db: Database, slug: str) -> cards.Card | None:
    return await db.run(lambda c: cards.get_by_slug(c, slug))


# AC1.4 ----------------------------------------------------------------------------------------------

async def test_new_card_loads_with_no_optional_slots(db: Database) -> None:
    card_id = await _create(db)

    card = await _get(db, "alpha")

    assert card is not None
    assert card.id == card_id
    assert card.state == "draft"
    assert card.show_details == ()
    assert card.created_by == "1001"
    assert (card.voice, card.address, card.location, card.seats, card.manager, card.probe) == (
        None, None, None, None, None, None)
    assert card.has_status_source is False
    assert card.runtime == cards.Runtime()


SLOT_COMBINATIONS = [
    ("voice",), ("address",), ("location",), ("seats",), ("manager",), ("probe",),
    ("manager", "probe"),
    ("voice", "address", "location", "seats", "manager", "probe"),
]


@pytest.mark.parametrize("slots", SLOT_COMBINATIONS, ids=["+".join(s) for s in SLOT_COMBINATIONS])
async def test_card_loads_with_exactly_the_slots_set(db: Database, slots: tuple[str, ...]) -> None:
    card_id = await _create(db)

    def fill(c) -> None:
        for name in slots:
            SLOT_SETTERS[name](c, card_id)

    await db.run(fill)
    card = await _get(db, "alpha")

    assert card is not None
    for name in SLOT_SETTERS:
        assert (getattr(card, name) is not None) == (name in slots), name
    assert card.has_status_source == bool({"manager", "probe"} & set(slots))


async def test_loaded_slots_hold_the_values_written(db: Database) -> None:
    card_id = await _create(db)
    await db.run(lambda c: cards.set_voice(c, card_id, Voice("1001", "1002", None, False)))
    await db.run(lambda c: cards.set_address(c, card_id, Address("h.invalid", 25565, True)))
    await db.run(lambda c: cards.set_probe(c, card_id, ProbeRef("a2s", "h.invalid", 27015)))

    card = await _get(db, "alpha")

    assert card is not None
    assert card.voice == Voice("1001", "1002", None, False)
    assert card.address == Address("h.invalid", 25565, True)
    assert card.probe == ProbeRef("a2s", "h.invalid", 27015)


async def test_set_address_public_reports_missing_row(db: Database) -> None:
    card_id = await _create(db)

    assert await db.run(lambda c: cards.set_address_public(c, card_id, True)) is False

    await db.run(lambda c: cards.set_address(c, card_id, Address("h.invalid", 25565, False)))
    assert await db.run(lambda c: cards.set_address_public(c, card_id, True)) is True
    card = await _get(db, "alpha")
    assert card is not None and card.address is not None and card.address.public is True


# Round trips ----------------------------------------------------------------------------------------

async def test_update_fields_round_trips_show_details(db: Database) -> None:
    card_id = await _create(db)

    await db.run(lambda c: cards.update_fields(c, card_id, NOW + 5, show_details=("map", "game_mode")))

    card = await _get(db, "alpha")
    assert card is not None
    assert card.show_details == ("map", "game_mode")


async def test_update_fields_stores_show_details_in_canonical_order(db: Database) -> None:
    card_id = await _create(db)

    await db.run(lambda c: cards.update_fields(c, card_id, NOW, show_details=("game_mode", "map")))

    card = await _get(db, "alpha")
    assert card is not None and card.show_details == ("map", "game_mode")


async def test_update_fields_rejects_an_unknown_show_item(db: Database) -> None:
    card_id = await _create(db)

    with pytest.raises(CardFieldError) as info:
        await db.run(lambda c: cards.update_fields(c, card_id, NOW, show_details=("players",)))

    assert info.value.field == "show_details"


async def test_update_fields_refuses_a_column_outside_the_allowlist(db: Database) -> None:
    card_id = await _create(db)

    with pytest.raises(KeyError):
        await db.run(lambda c: cards.update_fields(c, card_id, NOW, slug="other"))


async def test_clear_slot_removes_only_that_slot(db: Database) -> None:
    card_id = await _create(db)
    await db.run(lambda c: cards.set_voice(c, card_id, Voice("1001", "1002", None, False)))
    await db.run(lambda c: cards.set_seats(c, card_id, 4))

    await db.run(lambda c: cards.clear_slot(c, card_id, "voice"))

    card = await _get(db, "alpha")
    assert card is not None
    assert card.voice is None
    assert card.seats == 4


async def test_clear_slot_rejects_an_unknown_slot(db: Database) -> None:
    card_id = await _create(db)

    with pytest.raises(KeyError):
        await db.run(lambda c: cards.clear_slot(c, card_id, "cards; DROP TABLE cards"))


async def test_set_state_round_trips(db: Database) -> None:
    card_id = await _create(db)

    await db.run(lambda c: cards.set_state(c, card_id, "live", NOW + 1))

    card = await _get(db, "alpha")
    assert card is not None and card.state == "live"


async def test_set_state_rejects_an_unknown_state(db: Database) -> None:
    card_id = await _create(db)

    with pytest.raises(ValueError):
        await db.run(lambda c: cards.set_state(c, card_id, "gone", NOW + 1))


# Message tracking -----------------------------------------------------------------------------------

async def test_set_message_sets_message_and_hash_together(db: Database) -> None:
    card_id = await _create(db)

    await db.run(lambda c: cards.set_message(c, card_id, "1002", "1003", HASH))

    card = await _get(db, "alpha")
    assert card is not None
    assert (card.channel_id, card.message_id) == ("1002", "1003")
    assert card.runtime.rendered_hash == HASH
    by_message = await db.run(lambda c: cards.get_by_message_id(c, "1003"))
    assert by_message is not None and by_message.id == card_id


async def test_clear_message_if_matching_clears_message_and_hash(db: Database) -> None:
    card_id = await _create(db)
    await db.run(lambda c: cards.set_message(c, card_id, "1002", "1003", HASH))

    cleared = await db.run(lambda c: cards.clear_message_if(c, "1002", "1003"))

    assert cleared == card_id
    card = await _get(db, "alpha")
    assert card is not None
    assert (card.channel_id, card.message_id) == (None, None)
    assert card.runtime.rendered_hash is None


async def test_clear_message_if_stale_changes_nothing(db: Database) -> None:
    card_id = await _create(db)
    await db.run(lambda c: cards.set_message(c, card_id, "1002", "1004", HASH))

    cleared = await db.run(lambda c: cards.clear_message_if(c, "1002", "1003"))

    assert cleared is None
    card = await _get(db, "alpha")
    assert card is not None
    assert (card.channel_id, card.message_id) == ("1002", "1004")
    assert card.runtime.rendered_hash == HASH


async def test_set_probe_tracking_round_trips(db: Database) -> None:
    card_id = await _create(db)

    await db.run(lambda c: cards.set_probe_tracking(c, card_id, NOW, True))

    card = await _get(db, "alpha")
    assert card is not None
    assert card.runtime.probe_failing_since == NOW
    assert card.runtime.probe_alert_sent is True


# Listing and creation -------------------------------------------------------------------------------

async def test_list_cards_orders_by_sort_order_then_id(db: Database) -> None:
    c_id = await _create(db, slug="c-card")
    a_id = await _create(db, slug="a-card")
    b_id = await _create(db, slug="b-card")
    await db.run(lambda c: cards.update_fields(c, c_id, NOW, sort_order=2))
    await db.run(lambda c: cards.update_fields(c, a_id, NOW, sort_order=1))
    await db.run(lambda c: cards.update_fields(c, b_id, NOW, sort_order=1))

    listed = await db.run(lambda c: cards.list_cards(c))

    assert [card.slug for card in listed] == ["a-card", "b-card", "c-card"]


async def test_list_cards_filters_by_state(db: Database) -> None:
    live_id = await _create(db, slug="live-card")
    await _create(db, slug="draft-card")
    await db.run(lambda c: cards.set_state(c, live_id, "live", NOW))

    live = await db.run(lambda c: cards.list_cards(c, "live"))
    everything = await db.run(lambda c: cards.list_cards(c))

    assert [card.slug for card in live] == ["live-card"]
    assert len(everything) == 2


async def test_list_cards_skips_rows_without_a_slug(db: Database) -> None:
    await db.run(lambda c: c.execute("INSERT INTO cards (title, created_by, created_at, updated_at) "
                                     "VALUES ('old', '1001', 1, 1)"))
    await _create(db, slug="new-card")

    listed = await db.run(lambda c: cards.list_cards(c))

    assert [card.slug for card in listed] == ["new-card"]


async def test_duplicate_slug_raises_without_echoing_the_value(db: Database) -> None:
    await _create(db, slug=SENTINEL_SLUG)

    with pytest.raises(CardFieldError) as info:
        await _create(db, slug=SENTINEL_SLUG)

    assert info.value.field == "slug"
    assert info.value.rule == "taken"
    assert SENTINEL_SLUG not in str(info.value)


async def test_invalid_slug_is_left_to_the_schema_and_not_reported_as_taken(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        await _create(db, slug="Not A Slug")


def test_location_repr_hides_its_text() -> None:
    assert "SENTINEL-LOC" not in repr(Location("SENTINEL-LOC"))
