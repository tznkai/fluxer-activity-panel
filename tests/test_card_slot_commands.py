# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`!card set|clear` for the slot fields: three of each kind, their suffixes, the address public flags, and the
text_channel1 rename. Replies are fixed texts or rule texts, never the value typed."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest

from fluxer_panel import card_commands, text
from fluxer_panel.cards import Address, ChannelLink
from fluxer_panel.db import Database
from tests.fakes import FakeFluxerState, Override, make_rest_app
from tests.test_card_commands import NOT_VOICE, SENTINEL, World, build, make_mc, seed_text, seed_voice


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState()
    server = await aiohttp_server(make_rest_app(state))
    return state, str(server.make_url("/")).rstrip("/")


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def world(db, fake, session) -> World:
    state, base = fake
    return await build(db, state, base, session)


def other_community(world: World) -> None:
    """The community the bot is not in: its lookup is a 404, so a channel there is stored unverified."""
    world.state.overrides["GET /api/v1/guilds/1009"] = Override(404, b'{"code": "UNKNOWN_GUILD", "message": "no"}')


# Voice slots 2 and 3 are checked exactly like voice -----------------------------------------------------------

@pytest.mark.parametrize(("field_name", "index"), [("voice2", 1), ("voice3", 2)])
async def test_a_voice_slot_is_checked_and_named_in_a_community_the_bot_is_in(
        world: World, field_name: str, index: int) -> None:
    seed_voice(world.state, name="raid", channel="2005")
    await make_mc(world)
    await world.run(f"!card set mc {field_name} 1001 2005")

    card = await world.card("mc")
    assert card is not None
    assert card.voices[index] == ChannelLink("1001", "2005", "raid", True)
    assert [v for v in card.voices if v is not None] == [card.voices[index]]
    assert world.last_reply() == text.CHANNEL_VERIFIED
    assert world.kicks == [1]


async def test_a_voice_slot_in_a_community_the_bot_is_not_in_is_kept_with_the_warning(world: World) -> None:
    other_community(world)
    await make_mc(world)
    await world.run("!card set mc voice2 1009 2009")

    assert (await world.card("mc")).voice2 == ChannelLink("1009", "2009", None, False)
    assert world.last_reply() == text.CHANNEL_UNVERIFIED


async def test_a_text_channel_in_a_voice_slot_is_refused(world: World) -> None:
    seed_text(world.state)
    await make_mc(world)
    await world.run("!card set mc voice3 1001 2002")

    assert world.last_reply() == NOT_VOICE
    assert (await world.card("mc")).voices == (None, None, None)
    assert world.kicks == []


# The rename ---------------------------------------------------------------------------------------------------

async def test_text_channel_is_slot_one(world: World) -> None:
    seed_text(world.state)
    await make_mc(world)
    await world.run("!card set mc text_channel 1001 2002")

    assert (await world.card("mc")).text_channels == (ChannelLink("1001", "2002", "general", True), None, None)


@pytest.mark.parametrize("command", ["!card set mc text_channel1 1001 2002", "!card clear mc text_channel1",
                                     "!card set mc TEXT_CHANNEL1 1001 2002"])
async def test_text_channel1_is_refused_with_the_new_name(world: World, command: str) -> None:
    seed_text(world.state)
    await make_mc(world)
    await world.run("!card set mc text_channel 1001 2002")
    world.kicks.clear()
    await world.run(command)

    assert world.last_reply() == "text_channel1 is now text_channel. Use text_channel instead."
    assert (await world.card("mc")).text_channels[0] == ChannelLink("1001", "2002", "general", True)
    assert world.kicks == []


# Address slots and their public flags -------------------------------------------------------------------------

async def test_each_address_slot_has_its_own_public_flag(world: World) -> None:
    await make_mc(world)
    await world.run("!card set mc address lan-only.invalid:25565")
    await world.run("!card set mc address2 play.invalid:19132")
    await world.run("!card set mc address2_public yes")

    card = await world.card("mc")
    assert card is not None
    assert card.addresses == (Address("lan-only.invalid", 25565, False), Address("play.invalid", 19132, True), None)


@pytest.mark.parametrize(("command", "reply"), [
    ("!card set mc address3_public yes", "Can't set address3_public: it needs an address first."),
    (f"!card set mc address2_public {SENTINEL}", "Can't set address2_public: it must be yes or no."),
    (f"!card set mc address2 {SENTINEL}", "Can't set address2: it must be host:port."),
    ("!card set mc address3 play.invalid:0", "Can't set address3: it must be a port from 1 to 65535."),
    (f"!card set mc voice2 {SENTINEL} 2001", "Can't set voice2: it must be a numeric id."),
])
async def test_a_slot_refusal_names_its_own_field(world: World, command: str, reply: str) -> None:
    await make_mc(world)
    await world.run("!card set mc address play.invalid:19132")
    world.kicks.clear()
    await world.run(command)

    assert world.last_reply() == reply
    assert world.kicks == []
    assert (await world.card("mc")).addresses == (Address("play.invalid", 19132, False), None, None)


# Suffixes ------------------------------------------------------------------------------------------------------

async def test_a_suffix_is_stored_trimmed_and_collapsed(world: World) -> None:
    seed_voice(world.state)
    await make_mc(world)
    await world.run("!card set mc voice 1001 2001")
    await world.run("!card set mc voice_suffix   Raid    night  ")

    assert (await world.card("mc")).voice == ChannelLink("1001", "2001", "lobby", True, suffix="Raid night")
    assert world.last_reply() == text.CARD_UPDATED


@pytest.mark.parametrize("field_name", ["voice2_suffix", "text_channel_suffix", "text_channel3_suffix",
                                        "address_suffix", "address3_suffix"])
async def test_a_suffix_on_an_empty_slot_is_refused(world: World, field_name: str) -> None:
    await make_mc(world)
    world.kicks.clear()
    await world.run(f"!card set mc {field_name} Java")

    assert world.last_reply() == f"Can't set {field_name}: it needs its slot set first."
    assert world.kicks == []


@pytest.mark.parametrize("value", [f"{SENTINEL}(", f"{SENTINEL}`", f"<@{SENTINEL}>", f"{SENTINEL}#1"])
async def test_a_suffix_with_a_refused_character_says_which_are_allowed(world: World, value: str) -> None:
    await make_mc(world)
    await world.run("!card set mc address play.invalid:19132")
    world.kicks.clear()
    await world.run(f"!card set mc address_suffix {value}")

    assert world.last_reply() == ("Can't set address_suffix: it may contain only letters, digits, spaces and "
                                  "- + . '.")
    assert SENTINEL not in world.last_reply()
    assert (await world.card("mc")).address == Address("play.invalid", 19132, False)
    assert world.kicks == []


async def test_a_suffix_over_twenty_characters_is_refused(world: World) -> None:
    await make_mc(world)
    await world.run("!card set mc address play.invalid:19132")
    await world.run(f"!card set mc address_suffix {'s' * 21}")

    assert world.last_reply() == "Can't set address_suffix: it must be 1–20 characters."


async def test_setting_a_slot_again_keeps_its_suffix_and_public_flag(world: World) -> None:
    await make_mc(world)
    await world.run("!card set mc address2 old.invalid:1")
    await world.run("!card set mc address2_suffix Bedrock")
    await world.run("!card set mc address2_public yes")
    await world.run("!card set mc address2 play.invalid:19132")

    assert (await world.card("mc")).address2 == Address("play.invalid", 19132, True, "Bedrock")


# Clearing -----------------------------------------------------------------------------------------------------

async def test_clearing_a_slot_clears_its_suffix_and_public_flag(world: World) -> None:
    await make_mc(world)
    await world.run("!card set mc address2 play.invalid:19132")
    await world.run("!card set mc address2_suffix Bedrock")
    await world.run("!card set mc address2_public yes")
    await world.run("!card clear mc address2")
    assert (await world.card("mc")).address2 is None

    await world.run("!card set mc address2 play.invalid:19132")
    assert (await world.card("mc")).address2 == Address("play.invalid", 19132, False)


async def test_clearing_a_suffix_keeps_the_slot(world: World) -> None:
    seed_text(world.state)
    await make_mc(world)
    await world.run("!card set mc text_channel2 1001 2002")
    await world.run("!card set mc text_channel2_suffix Builds")
    world.kicks.clear()
    await world.run("!card clear mc text_channel2_suffix")

    assert (await world.card("mc")).text_channels[1] == ChannelLink("1001", "2002", "general", True)
    assert world.last_reply() == text.CARD_CLEARED
    assert world.kicks == [1]


async def test_clearing_a_slot_never_moves_another_up(world: World) -> None:
    seed_voice(world.state)
    seed_voice(world.state, name="raid", channel="2005")
    await make_mc(world)
    await world.run("!card set mc voice 1001 2001")
    await world.run("!card set mc voice2 1001 2005")
    await world.run("!card set mc voice2_suffix Raid")
    await world.run("!card clear mc voice")

    card = await world.card("mc")
    assert card is not None
    assert card.voices == (None, ChannelLink("1001", "2005", "raid", True, suffix="Raid"), None)


async def test_a_public_flag_is_not_a_clearable_field(world: World) -> None:
    await make_mc(world)
    await world.run("!card clear mc address_public")

    assert world.last_reply() == text.UNKNOWN_FIELD


async def test_a_refused_suffix_is_logged_with_field_and_rule_only(world: World, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel.card_commands")
    await make_mc(world)
    await world.run(f"!card set mc voice3_suffix {SENTINEL}|")

    [record] = [r for r in caplog.records if r.getMessage() == "card_field_refused"]
    assert record.panel_fields == {"field": "voice3_suffix", "reason": "suffix_chars", "user_id": "1001"}


@pytest.mark.parametrize(("field_name", "value", "rule"), [("address_suffix", "Java", "no_slot"),
                                                           ("address_public", "yes", "no_address")])
async def test_a_slot_cleared_after_the_card_was_read_is_refused_not_reported_updated(
        world: World, monkeypatch: pytest.MonkeyPatch, field_name: str, value: str, rule: str) -> None:
    await make_mc(world)
    await world.run("!card set mc address play.invalid:25565")
    stale = await world.card("mc")
    await world.run("!card clear mc address")

    async def stale_find(ctx: object, slug: str) -> object:
        return stale
    monkeypatch.setattr(card_commands, "_find", stale_find)
    await world.run(f"!card set mc {field_name} {value}")

    assert world.last_reply() == text.FIELD_ERROR.format(field=field_name, rule=text.RULES[rule])
    monkeypatch.undo()
    assert (await world.card("mc")).address is None
