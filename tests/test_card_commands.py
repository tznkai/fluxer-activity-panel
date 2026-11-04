# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin card commands end to end: `admin.handle` on a DM, a real SQLite file and the fake Fluxer REST server."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.events import MessageCreate
from fluxer_client.rest import FluxerREST
from fluxer_panel import admin, cards, render, text
from fluxer_panel.card_commands import LIST_CHUNK, CardContext, _chunks, help_fields_reply
from fluxer_panel.cards import ChannelLink
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.logs import setup_logging
from fluxer_panel.sources.registry import build_sources
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "SENTINEL-TOKEN-7f3a9c"
SENTINEL = "SENTINEL-VAL-"
DM = "9100"
GUILD = "2100"                       # the games channel, when configured
DM_MESSAGES = f"/api/v1/channels/{DM}/messages"
VOICE_CHANNEL = "/api/v1/channels/2001"
VOICE_CHANNEL_GET = "GET /api/v1/channels/2001"
POSTED_MESSAGE = f"/api/v1/channels/{GUILD}/messages/5101"
VOICE_GUILD = "/api/v1/guilds/1001"
TEXT_CHANNEL = "/api/v1/channels/2002"
NOT_VOICE = text.CHANNEL_WRONG_TYPE.format(kind=text.CHANNEL_KIND_VOICE)
NOT_TEXT = text.CHANNEL_WRONG_TYPE.format(kind=text.CHANNEL_KIND_TEXT)
PTERO_ENV = {"PTERODACTYL_URL": "https://fluxer.invalid", "PTERODACTYL_API_KEY": "test-key"}


def make_message(content: str, *, author: str = "1001", guild: str | None = None, bot: bool = False) -> MessageCreate:
    return MessageCreate(message_id="8001", channel_id=DM, guild_id=guild, author_id=author,
                         author_is_bot=bot, member_role_ids=None, content=content)


@dataclass
class World:
    db: Database
    state: FakeFluxerState
    config: Config
    rest: FluxerREST
    ctx: CardContext
    kicks: list[int] = field(default_factory=list)

    async def run(self, content: str, *, author: str = "1001", guild: str | None = None, bot: bool = False) -> None:
        await admin.handle(make_message(content, author=author, guild=guild, bot=bot), self.config, self.rest,
                           self.ctx)

    def payloads(self) -> list[dict[str, Any]]:
        return [request.payload for request in self.state.requests_to(DM_MESSAGES)]

    def last_reply(self) -> str:
        return self.payloads()[-1]["content"]

    def clear_requests(self) -> None:
        self.state.requests.clear()

    async def card(self, slug: str) -> cards.Card | None:
        return await self.db.run(lambda c: cards.get_by_slug(c, slug))


async def build(db: Database, state: FakeFluxerState, base: str, session: aiohttp.ClientSession,
                **env: str) -> World:
    config = Config.from_env({"FLUXER_TOKEN": TOKEN, "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
                              "ADMIN_USER_IDS": "1001", **env})
    rest = FluxerREST(session, f"{base}/api", TOKEN)
    kicks: list[int] = []
    ctx = CardContext(db=db, rest=rest, config=config, sources=build_sources(session, config),
                      kick=lambda: kicks.append(1))
    return World(db=db, state=state, config=config, rest=rest, ctx=ctx, kicks=kicks)


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


@pytest.fixture
async def ptero_world(db, fake, session) -> World:
    state, base = fake
    return await build(db, state, base, session, **PTERO_ENV)


@pytest.fixture
def root_logging():
    """setup_logging replaces the root handlers and level; restore both so pytest's capture keeps working."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    logging.captureWarnings(False)


def seed_voice(state: FakeFluxerState, *, name: Any = "lobby", kind: int = 2, guild: str = "1001",
               channel: str = "2001") -> None:
    state.guilds["1001"] = {"id": "1001"}
    state.channels[channel] = {"id": channel, "type": kind, "guild_id": guild, "name": name}


def seed_text(state: FakeFluxerState, *, name: Any = "general", kind: int = 0, guild: str = "1001",
              channel: str = "2002") -> None:
    """A text channel, as the fake reports it: type 0 is a text channel."""
    state.guilds["1001"] = {"id": "1001"}
    state.channels[channel] = {"id": channel, "type": kind, "guild_id": guild, "name": name}


async def make_mc(world: World) -> None:
    await world.run("!card new mc Minecraft")


# AC7.1: create ---------------------------------------------------------------------------------------

async def test_new_creates_a_draft_owned_by_the_admin(world: World) -> None:
    await world.run("!card new mc Minecraft Bedrock Survival")

    card = await world.card("mc")
    assert card is not None
    assert (card.title, card.state, card.created_by) == ("Minecraft Bedrock Survival", "draft", "1001")
    approved = await world.db.run(lambda c: c.execute("SELECT approved_by FROM cards WHERE slug = 'mc'").fetchone())
    assert approved == ("1001",)
    assert world.last_reply() == text.CARD_CREATED
    assert world.kicks == []


async def test_new_with_a_taken_slug_is_refused_and_changes_nothing(world: World) -> None:
    await make_mc(world)
    await world.run("!card new mc Other")

    assert world.last_reply() == "Can't set slug: it is already used by another card."
    assert (await world.card("mc")).title == "Minecraft"


# AC7.2: refusals before any card change --------------------------------------------------------------

@pytest.mark.parametrize(("author", "bot", "guild"), [
    ("2002", False, None),   # not an admin
    ("1001", True, None),    # a bot account
    ("1001", False, GUILD),  # a guild channel
])
async def test_refused_author_or_place_changes_nothing_and_logs_denial(
        world: World, caplog: pytest.LogCaptureFixture, author: str, bot: bool, guild: str | None) -> None:
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.admin")
    await world.run("!card new x X", author=author, bot=bot, guild=guild)

    assert world.state.requests == []
    assert await world.db.run(lambda c: cards.list_cards(c)) == []
    denials = [r for r in caplog.records if r.name == "fluxer_panel.admin" and r.getMessage() == "admin_denied"]
    assert len(denials) == 1


async def test_non_admin_list_makes_no_request(world: World) -> None:
    await world.run("!card list", author="2002")
    assert world.state.requests == []


# AC7.3: field refusals through the full command path --------------------------------------------------

@pytest.mark.parametrize(("setup", "command", "reply"), [
    (None, f"!card new mc {'x' * 201}", "Can't set title: it must be 1–200 characters."),
    (None, f"!card new BAD-{SENTINEL} Title", "Can't set slug: it must be 1–32 of a-z, 0-9 and -."),
    (None, "!card new mc \x07Title", "Can't set title: it must not contain control characters."),
    ("mc", f"!card set mc description {'y' * 1001}", "Can't set description: it must be 1–1000 characters."),
    ("mc", "!card set mc join \u202e" + SENTINEL, "Can't set join: it must not contain control characters."),
    ("mc", f"!card set mc sort {SENTINEL}", "Can't set sort: it is out of range."),
    ("mc", f"!card set mc seats {SENTINEL}", "Can't set seats: it is out of range."),
    ("mc", f"!card set mc address {SENTINEL}", "Can't set address: it must be host:port."),
    ("mc", f"!card set mc address bad host {SENTINEL}:19132",
     "Can't set address: it must be a host name or IP address."),
    ("mc", "!card set mc address mc.example.invalid:99999",
     "Can't set address: it must be a port from 1 to 65535."),
    ("mc", f"!card set mc address_public {SENTINEL}", "Can't set address_public: it must be yes or no."),
    ("mc", "!card set mc address_public yes", "Can't set address_public: it needs an address first."),
    ("mc", f"!card set mc show {SENTINEL}", "Can't set show: it must be none or a list of map, version, game_mode."),
    ("mc", f"!card set mc voice {SENTINEL} 2001", "Can't set voice: it must be a numeric id."),
    ("mc", "!card set mc voice 1001", "Can't set voice: it must be <community id> <channel id> or a channel link."),
    ("mc", "!card set mc text_channel1 1001", "Can't set text_channel1: it must be <community id> <channel id> or a channel link."),
    ("mc", f"!card set mc text_channel3 {SENTINEL} 2001", "Can't set text_channel3: it must be a numeric id."),
    ("mc", f"!card set mc manager steam 12345678{SENTINEL}", "Can't set manager: it is not a known kind."),
    ("mc", "!card set mc manager pterodactyl abcd1234", "Can't set manager: it is not configured on this bot."),
    ("mc", "!card set mc manager pterodactyl", "Can't set manager: it must be <kind> <ref>."),
    ("mc", f"!card set mc probe quake 127.0.0.1 1{SENTINEL}", "Can't set probe: it is not a known kind."),
    ("mc", f"!card set mc probe a2s 127.0.0.1 {SENTINEL}", "Can't set probe: it must be a port from 1 to 65535."),
    ("mc", f"!card set mc probe a2s {SENTINEL}", "Can't set probe: it must be <kind> <host> <port>."),
])
async def test_bad_field_value_is_refused_with_field_and_rule_and_no_echo(
        world: World, setup: str | None, command: str, reply: str) -> None:
    if setup:
        await make_mc(world)
    world.clear_requests()
    await world.run(command)

    assert world.last_reply() == reply
    assert SENTINEL not in world.last_reply()
    assert world.kicks == []


async def test_pterodactyl_ref_is_refused_when_the_manager_is_configured(ptero_world: World) -> None:
    await make_mc(ptero_world)
    await ptero_world.run(f"!card set mc manager pterodactyl ZZZ{SENTINEL}")

    assert ptero_world.last_reply() == "Can't set manager: it must be an 8-character Pterodactyl server id."
    assert SENTINEL not in ptero_world.last_reply()


async def test_refused_field_is_logged_with_field_and_rule_only(world: World, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel.card_commands")
    await make_mc(world)
    await world.run(f"!card set mc sort {SENTINEL}")

    [record] = [r for r in caplog.records if r.name == "fluxer_panel.card_commands"
                and r.getMessage() == "card_field_refused"]
    assert record.panel_fields == {"field": "sort", "reason": "range", "user_id": "1001"}


# AC7.4 and AC7.5: voice ---------------------------------------------------------------------------------

async def test_set_voice_with_two_ids_stores_the_verified_channel(world: World) -> None:
    seed_voice(world.state)
    await make_mc(world)
    await world.run("!card set mc voice 1001 2001")

    assert (await world.card("mc")).voice == ChannelLink("1001", "2001", "lobby", True)
    assert world.last_reply() == text.CHANNEL_VERIFIED
    assert world.last_reply() == "Channel saved and checked."
    assert world.kicks == [1]


async def test_set_voice_accepts_a_pasted_channel_link(world: World) -> None:
    seed_voice(world.state)
    await make_mc(world)
    await world.run("!card set mc voice https://fluxer.invalid/channels/1001/2001")

    assert (await world.card("mc")).voice == ChannelLink("1001", "2001", "lobby", True)


@pytest.mark.parametrize(("seed", "reply"), [
    ({"channel": "2999"}, text.CHANNEL_MISSING),          # the asked-for channel 2001 does not exist: 404
    ({"kind": 0}, NOT_VOICE),                  # a text channel
    ({"guild": "1009"}, text.CHANNEL_OTHER_COMMUNITY),      # a channel in another community
])
async def test_voice_channel_that_does_not_fit_is_refused_and_stores_nothing(
        world: World, seed: dict[str, Any], reply: str) -> None:
    seed_voice(world.state, **seed)
    await make_mc(world)
    await world.run("!card set mc voice 1001 2001")

    assert world.last_reply() == reply
    assert (await world.card("mc")).voice is None
    assert world.kicks == []


# AC7.6: the bot is not in the community -------------------------------------------------------------

@pytest.mark.parametrize("status", [403, 404])
async def test_community_the_bot_is_not_in_stores_unverified_without_name(world: World, status: int) -> None:
    if status == 403:
        world.state.overrides[f"GET {VOICE_GUILD}"] = Override(403, b'{"code": "MISSING_ACCESS", "message": "no"}')
    await make_mc(world)
    await world.run("!card set mc voice 1001 2001")

    assert (await world.card("mc")).voice == ChannelLink("1001", "2001", None, False)
    assert world.last_reply() == text.CHANNEL_UNVERIFIED
    assert world.kicks == [1]


# AC7.7: failures that store nothing -----------------------------------------------------------------

async def test_server_error_on_the_community_refuses_with_retry(world: World) -> None:
    world.state.overrides[f"GET {VOICE_GUILD}"] = Override(500, b'{"code": "X", "message": "boom"}')
    await make_mc(world)
    await world.run("!card set mc voice 1001 2001")

    assert world.last_reply() == text.CHANNEL_RETRY
    assert (await world.card("mc")).voice is None


async def test_server_error_on_the_channel_refuses_with_retry(world: World) -> None:
    seed_voice(world.state)
    world.state.overrides[f"GET {VOICE_CHANNEL}"] = Override(500, b'{"code": "X", "message": "boom"}')
    await make_mc(world)
    await world.run("!card set mc voice 1001 2001")

    assert world.last_reply() == text.CHANNEL_RETRY
    assert (await world.card("mc")).voice is None


async def test_bad_request_on_the_channel_refuses_with_retry(world: World) -> None:
    seed_voice(world.state)
    world.state.overrides[f"GET {VOICE_CHANNEL}"] = Override(400, b'{"code": "X", "message": "bad"}')
    await make_mc(world)
    await world.run("!card set mc voice 1001 2001")

    assert world.last_reply() == text.CHANNEL_RETRY
    assert (await world.card("mc")).voice is None


async def test_forbidden_channel_is_reported_as_missing(world: World) -> None:
    seed_voice(world.state)
    world.state.overrides[f"GET {VOICE_CHANNEL}"] = Override(403, b'{"code": "X", "message": "no"}')
    await make_mc(world)
    await world.run("!card set mc voice 1001 2001")

    assert world.last_reply() == text.CHANNEL_MISSING
    assert (await world.card("mc")).voice is None


async def test_unreachable_community_refuses_with_retry(world: World) -> None:
    world.state.drops.add(f"GET {VOICE_GUILD}")
    await make_mc(world)
    await world.run("!card set mc voice 1001 2001")

    assert world.last_reply() == text.CHANNEL_RETRY
    assert (await world.card("mc")).voice is None


async def test_timed_out_community_refuses_with_retry(db: Database, fake) -> None:
    state, base = fake
    state.delays[f"GET {VOICE_GUILD}"] = 1.0
    seed_voice(state)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.2)) as session:
        world = await build(db, state, base, session)
        await make_mc(world)
        await world.run("!card set mc voice 1001 2001")

    assert world.last_reply() == text.CHANNEL_RETRY
    assert (await world.card("mc")).voice is None


async def test_timed_out_channel_refuses_with_retry(db: Database, fake) -> None:
    state, base = fake
    seed_voice(state)
    state.delays[f"GET {VOICE_CHANNEL}"] = 1.0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.2)) as session:
        world = await build(db, state, base, session)
        await make_mc(world)
        await world.run("!card set mc voice 1001 2001")

    assert world.last_reply() == text.CHANNEL_RETRY
    assert (await world.card("mc")).voice is None


async def test_unexpected_community_shape_refuses_with_retry(world: World) -> None:
    world.state.guilds["1001"] = ["not", "a", "dict"]
    await make_mc(world)
    await world.run("!card set mc voice 1001 2001")

    assert world.last_reply() == text.CHANNEL_RETRY
    assert (await world.card("mc")).voice is None


async def test_connection_error_on_the_channel_refuses_with_retry(world: World) -> None:
    seed_voice(world.state)
    world.state.drops.add(VOICE_CHANNEL_GET)
    await make_mc(world)
    await world.run("!card set mc voice 1001 2001")

    assert world.last_reply() == text.CHANNEL_RETRY
    assert (await world.card("mc")).voice is None


@pytest.mark.parametrize(("raw", "stored"), [
    ("  Quiet\x07  room  ", "Quiet room"),   # control characters removed, whitespace collapsed
    ("n" * 150, "n" * 100),                  # capped at 100 characters
    (5, None),                               # not text: verified, without a name
    ("\x07\x07", None),                      # nothing printable left: verified, without a name
])
async def test_channel_name_is_cleaned_and_capped(world: World, raw: Any, stored: str | None) -> None:
    seed_voice(world.state, name=raw)
    await make_mc(world)
    await world.run("!card set mc voice 1001 2001")

    assert (await world.card("mc")).voice == ChannelLink("1001", "2001", stored, True)
    assert world.last_reply() == text.CHANNEL_VERIFIED


# Text channels: the same checks as voice, for three slots --------------------------------------------

@pytest.mark.parametrize("slot", [1, 2, 3])
async def test_set_text_channel_stores_the_verified_channel_in_its_slot(world: World, slot: int) -> None:
    seed_text(world.state)
    await make_mc(world)
    await world.run(f"!card set mc text_channel{slot} 1001 2002")

    card = await world.card("mc")
    assert card is not None
    assert card.text_channels[slot - 1] == ChannelLink("1001", "2002", "general", True)
    assert [link for link in card.text_channels if link is not None] == [card.text_channels[slot - 1]]
    assert world.last_reply() == text.CHANNEL_VERIFIED
    assert world.kicks == [1]


async def test_text_channel_accepts_a_pasted_channel_link_and_slots_stay_independent(world: World) -> None:
    seed_text(world.state)
    seed_text(world.state, name="ops", channel="2003")
    await make_mc(world)
    await world.run("!card set mc text_channel1 https://fluxer.invalid/channels/1001/2002")
    await world.run("!card set mc text_channel3 1001 2003")

    card = await world.card("mc")
    assert card is not None
    assert card.text_channels == (
        ChannelLink("1001", "2002", "general", True),
        None,
        ChannelLink("1001", "2003", "ops", True))


@pytest.mark.parametrize("status", [403, 404])
async def test_text_channel_in_a_community_the_bot_is_not_in_is_stored_unverified(world: World, status: int) -> None:
    if status == 403:
        world.state.overrides[f"GET {VOICE_GUILD}"] = Override(403, b'{"code": "MISSING_ACCESS", "message": "no"}')
    await make_mc(world)
    await world.run("!card set mc text_channel2 1001 2002")

    card = await world.card("mc")
    assert card is not None
    assert card.text_channels == (None, ChannelLink("1001", "2002", None, False), None)
    assert world.last_reply() == text.CHANNEL_UNVERIFIED
    assert world.kicks == [1]


async def test_missing_text_channel_is_refused_and_stores_nothing(world: World) -> None:
    seed_text(world.state, channel="2999")
    await make_mc(world)
    await world.run("!card set mc text_channel1 1001 2002")

    assert world.last_reply() == text.CHANNEL_MISSING
    assert (await world.card("mc")).text_channels == (None, None, None)
    assert world.kicks == []


@pytest.mark.parametrize(("field_name", "kind", "reply"), [
    ("text_channel1", 2, NOT_TEXT),     # a voice channel in a text slot
    ("text_channel3", 4, NOT_TEXT),     # a category, also not a text channel
    ("voice", 0, NOT_VOICE),            # a text channel in the voice slot
])
async def test_channel_of_the_wrong_type_is_refused_for_its_field(
        world: World, field_name: str, kind: int, reply: str) -> None:
    seed_text(world.state, kind=kind)
    await make_mc(world)
    await world.run(f"!card set mc {field_name} 1001 2002")

    assert world.last_reply() == reply
    assert (await world.card("mc")).text_channels == (None, None, None)
    assert (await world.card("mc")).voice is None


async def test_text_channel_in_another_community_is_refused(world: World) -> None:
    seed_text(world.state, guild="1009")
    await make_mc(world)
    await world.run("!card set mc text_channel2 1001 2002")

    assert world.last_reply() == text.CHANNEL_OTHER_COMMUNITY
    assert (await world.card("mc")).text_channels == (None, None, None)


@pytest.mark.parametrize("setup", ["community-500", "channel-500", "channel-dropped", "channel-timeout"])
async def test_transient_failure_on_a_text_channel_refuses_with_retry_and_stores_nothing(
        db: Database, fake, session, setup: str) -> None:
    state, base = fake
    seed_text(state)
    timeout = aiohttp.ClientTimeout(total=0.2)
    if setup == "community-500":
        state.overrides[f"GET {VOICE_GUILD}"] = Override(500, b'{"code": "X", "message": "boom"}')
    elif setup == "channel-500":
        state.overrides[f"GET {TEXT_CHANNEL}"] = Override(500, b'{"code": "X", "message": "boom"}')
    elif setup == "channel-dropped":
        state.drops.add(f"GET {TEXT_CHANNEL}")
    else:
        state.delays[f"GET {TEXT_CHANNEL}"] = 1.0
    async with aiohttp.ClientSession(timeout=timeout) as client:
        world = await build(db, state, base, client)
        await make_mc(world)
        await world.run("!card set mc text_channel2 1001 2002")

    assert world.last_reply() == text.CHANNEL_RETRY
    assert (await world.card("mc")).text_channels == (None, None, None)
    assert world.kicks == []


@pytest.mark.parametrize("cleared", [1, 2, 3])
async def test_clear_text_channel_removes_only_the_slot_named(world: World, cleared: int) -> None:
    channels = {1: "2002", 2: "2003", 3: "2004"}
    await make_mc(world)
    for slot, channel in channels.items():
        seed_text(world.state, name=f"room{slot}", channel=channel)
        await world.run(f"!card set mc text_channel{slot} 1001 {channel}")
    world.kicks.clear()
    await world.run(f"!card clear mc text_channel{cleared}")

    card = await world.card("mc")
    assert card is not None
    expected = [ChannelLink("1001", channel, f"room{slot}", True) for slot, channel in channels.items()]
    expected[cleared - 1] = None
    assert card.text_channels == tuple(expected)
    assert world.last_reply() == text.CARD_CLEARED
    assert world.kicks == [1]


async def test_clear_of_a_fourth_text_channel_is_unknown(world: World) -> None:
    await make_mc(world)
    await world.run("!card clear mc text_channel4")

    assert world.last_reply() == text.UNKNOWN_FIELD
    assert world.kicks == []


# AC7.1 to AC7.3: subcommand and field names, and the public flag of an address ---------------------------

async def test_subcommand_and_field_names_are_case_insensitive(world: World) -> None:
    await world.run("!card NEW mc Minecraft")
    await world.run("!card set mc TITLE Bedrock")
    assert (await world.card("mc")).title == "Bedrock"
    await world.run("!card set mc DESCRIPTION Survival")
    await world.run("!card clear mc DESCRIPTION")

    card = await world.card("mc")
    assert card is not None
    assert (card.title, card.description) == ("Bedrock", None)
    assert world.last_reply() == text.CARD_CLEARED


async def test_address_public_yes_and_no_store_the_flag_and_kick(world: World) -> None:
    await make_mc(world)
    await world.run("!card set mc address mc.example.invalid:19132")
    world.kicks.clear()
    await world.run("!card set mc address_public yes")

    card = await world.card("mc")
    assert card is not None and card.address is not None
    assert card.address.public is True
    assert world.last_reply() == text.CARD_UPDATED
    assert world.kicks == [1]

    await world.run("!card set mc address_public no")
    card = await world.card("mc")
    assert card is not None and card.address is not None
    assert card.address.public is False
    assert world.kicks == [1, 1]


async def test_new_address_keeps_an_existing_public_flag(world: World) -> None:
    await make_mc(world)
    await world.run("!card set mc address mc.example.invalid:19132")
    await world.run("!card set mc address_public yes")
    await world.run("!card set mc address mc.example.invalid:19133")

    card = await world.card("mc")
    assert card is not None and card.address is not None
    assert (card.address.host, card.address.port, card.address.public) == ("mc.example.invalid", 19133, True)


def test_chunks_split_exactly_at_the_list_limit() -> None:
    assert LIST_CHUNK == 1900
    # 949 + 1 + 950 == 1900: a joined message at the limit is split.
    assert _chunks(["a" * 949, "b" * 950]) == ["a" * 949, "b" * 950]
    # 948 + 1 + 950 == 1899: under the limit, so both lines share one message.
    assert _chunks(["a" * 948, "b" * 950]) == ["a" * 948 + "\n" + "b" * 950]


def test_chunks_count_astral_characters_as_two_units() -> None:
    # 600 and 350 astral characters are 951 characters joined, under the limit, but 1200 + 1 + 700 = 1901 units.
    first = "\U0001F600" * 600
    second = "\U0001F600" * 350
    assert _chunks([first, second]) == [first, second]
    # 500 and 350 astral characters are 1000 + 1 + 700 = 1701 units: under the limit, so one message.
    assert _chunks(["\U0001F600" * 500, second]) == ["\U0001F600" * 500 + "\n" + second]


# AC7.8: clear ---------------------------------------------------------------------------------------------

SLOTS = ("voice", "address", "location", "seats", "manager", "probe")
BASE = ("description", "join", "first_time", "sort", "show")
EMPTY = {"voice": None, "address": None, "location": None, "seats": None, "manager": None, "probe": None,
         "description": None, "join": None, "first_time": None, "sort": 0, "show": ()}


async def snapshot(world: World) -> dict[str, Any]:
    card = await world.card("mc")
    assert card is not None
    return {"voice": card.voice, "address": card.address, "location": card.location, "seats": card.seats,
            "manager": card.manager, "probe": card.probe, "description": card.description,
            "join": card.how_to_join, "first_time": card.first_time, "sort": card.sort_order,
            "show": card.show_details}


async def test_clear_removes_only_the_named_field(ptero_world: World) -> None:
    world = ptero_world
    seed_voice(world.state)
    await make_mc(world)
    for command in ("voice 1001 2001", "address mc.example.invalid:19132", "location Room 4", "seats 4",
                    "manager pterodactyl abcd1234", "probe a2s 127.0.0.1 27015", "description About it",
                    "join Ask in chat", "first_time Read the rules", "sort 3", "show map,version"):
        await world.run(f"!card set mc {command}")
    assert all(value not in (None, ()) for value in (await snapshot(world)).values())

    for field_name in SLOTS + BASE:
        before = await snapshot(world)
        world.kicks.clear()
        await world.run(f"!card clear mc {field_name}")
        after = await snapshot(world)
        assert after[field_name] == EMPTY[field_name], field_name
        assert {k: v for k, v in after.items() if k != field_name} == {
            k: v for k, v in before.items() if k != field_name}, field_name
        assert world.last_reply() == text.CARD_CLEARED
        assert world.kicks == [1], field_name


async def test_clear_title_is_not_clearable(world: World) -> None:
    await make_mc(world)
    await world.run("!card clear mc title")

    assert world.last_reply() == text.UNKNOWN_FIELD
    assert (await world.card("mc")).title == "Minecraft"


async def test_set_unknown_field_is_refused(world: World) -> None:
    await make_mc(world)
    await world.run("!card set mc frobnicate yes")

    assert world.last_reply() == text.UNKNOWN_FIELD
    assert world.kicks == []


# AC7.9: show and list ---------------------------------------------------------------------------------------

async def test_show_sends_the_inside_embed_then_the_labelled_outside_text(world: World) -> None:
    await make_mc(world)
    await world.run("!card set mc description Survival server")
    world.clear_requests()
    await world.run("!card show mc")

    embed_message, outside_message = world.payloads()
    assert embed_message["content"] == text.SHOW_NOTE
    assert embed_message["embeds"][0]["title"] == "Minecraft"
    assert embed_message["allowed_mentions"] == {"parse": []}
    assert outside_message["content"].startswith(text.SHOW_OUTSIDE_LABEL + "\n")
    assert "Minecraft" in outside_message["content"]
    assert outside_message["allowed_mentions"] == {"parse": []}


async def test_show_preview_shows_join_text_as_plain_text_not_a_masked_link(world: World) -> None:
    await make_mc(world)
    await world.run("!card set mc join [x](https://fluxer.invalid/x)")
    world.clear_requests()
    await world.run("!card show mc")

    outside = world.payloads()[1]["content"]
    assert re.search(r"(?<!\\)\]\(", outside) is None
    assert outside.replace("\\", "") == text.SHOW_OUTSIDE_LABEL + "\nMinecraft\n[x](https://fluxer.invalid/x)"


async def test_show_preview_fits_the_message_limit_after_escaping(world: World) -> None:
    await make_mc(world)
    await world.run("!card set mc join " + "*" * 1000)
    world.clear_requests()
    await world.run("!card show mc")

    outside = world.payloads()[1]["content"]
    assert outside.startswith(text.SHOW_OUTSIDE_LABEL + "\n")
    assert len(outside) <= 2000


async def test_show_preview_fits_the_message_limit_in_utf16_units_for_astral_text(world: World) -> None:
    await make_mc(world)
    await world.run("!card set mc join " + "\U0001F600" * 1000)
    world.clear_requests()
    await world.run("!card show mc")

    outside = world.payloads()[1]["content"]
    assert outside.startswith(text.SHOW_OUTSIDE_LABEL + "\n")
    assert render.units(outside) <= 2000


async def test_list_shows_cards_in_sort_order(world: World) -> None:
    await world.run("!card new a Alpha")
    await world.run("!card new b Bravo")
    await world.run("!card new c Charlie")
    await world.run("!card set a sort 2")
    await world.run("!card set b sort 0")
    await world.run("!card set c sort 1")
    world.clear_requests()
    await world.run("!card list")

    [message] = world.payloads()
    assert message["content"].splitlines() == ["b · draft · Bravo", "c · draft · Charlie", "a · draft · Alpha"]
    assert message["allowed_mentions"] == {"parse": []}


async def test_list_with_no_cards_says_so(world: World) -> None:
    await world.run("!card list")
    assert world.last_reply() == text.NO_CARDS


async def test_list_splits_long_listings_into_messages_under_1900_characters(world: World) -> None:
    for n in range(20):
        await world.run(f"!card new c{n:02d} {'T' * 200}")
    world.clear_requests()
    await world.run("!card list")

    contents = [payload["content"] for payload in world.payloads()]
    assert len(contents) > 1
    assert all(render.units(content) < LIST_CHUNK for content in contents)
    listed = [line.split(" · ")[0] for content in contents for line in content.splitlines()]
    assert listed == [f"c{n:02d}" for n in range(20)]


async def test_list_of_astral_titles_keeps_every_message_within_the_message_limit(world: World) -> None:
    # Each title is 200 astral characters, 400 UTF-16 units: a message of four or more such lines is over 2000 units.
    title = "\U0001F600" * 200
    for n in range(20):
        await world.run(f"!card new a{n:02d} {title}")
    world.clear_requests()
    await world.run("!card list")

    contents = [payload["content"] for payload in world.payloads()]
    assert len(contents) > 1
    assert all(render.units(content) <= 2000 for content in contents)
    listed = [line.split(" \u00b7 ")[0] for content in contents for line in content.splitlines()]
    assert listed == [f"a{n:02d}" for n in range(20)]


# AC7.10: publish and archive ----------------------------------------------------------------------------

async def test_publish_from_draft_makes_the_card_live_and_kicks(world: World) -> None:
    await make_mc(world)
    await world.run("!card publish mc")

    assert (await world.card("mc")).state == "live"
    assert world.kicks == [1]


async def test_publish_when_no_games_channel_is_configured_says_nothing_will_post(world: World) -> None:
    await make_mc(world)
    await world.run("!card publish mc")
    assert world.last_reply() == text.PUBLISHED_NO_CHANNEL


async def test_publish_with_a_games_channel_says_it_will_post(db, fake, session) -> None:
    state, base = fake
    world = await build(db, state, base, session, GAMES_CHANNEL_ID=GUILD)
    await make_mc(world)
    await world.run("!card publish mc")
    assert world.last_reply() == text.CARD_PUBLISHED


async def test_publish_from_archived_makes_it_live_again(world: World) -> None:
    await make_mc(world)
    await world.run("!card archive mc")
    await world.run("!card publish mc")

    assert (await world.card("mc")).state == "live"


async def test_publish_when_already_live_is_refused(world: World) -> None:
    await make_mc(world)
    await world.run("!card publish mc")
    world.kicks.clear()
    await world.run("!card publish mc")

    assert world.last_reply() == text.ALREADY_LIVE
    assert world.kicks == []


async def _live_card_with_message(world: World) -> None:
    """A live card with a voice and an address, whose posted message 5101 exists in the games channel."""
    seed_voice(world.state)
    await make_mc(world)
    await world.run("!card set mc voice 1001 2001")
    await world.run("!card set mc address mc.example.invalid:19132")
    await world.run("!card publish mc")
    card = await world.card("mc")
    assert card is not None
    world.state.messages[(GUILD, "5101")] = {"content": "posted"}
    await world.db.run(lambda c: cards.set_message(c, card.id, GUILD, "5101", None))


async def test_archive_deletes_the_message_and_keeps_every_row(ptero_world: World) -> None:
    await _live_card_with_message(ptero_world)
    await ptero_world.run("!card set mc manager pterodactyl abcd1234")
    ptero_world.clear_requests()
    ptero_world.kicks.clear()
    await ptero_world.run("!card archive mc")

    assert [r.method for r in ptero_world.state.requests if r.path == POSTED_MESSAGE] == ["DELETE"]
    card = await ptero_world.card("mc")
    assert card is not None
    assert (card.state, card.message_id, card.channel_id) == ("archived", None, None)
    assert card.voice is not None and card.address is not None and card.manager is not None
    assert ptero_world.last_reply() == text.CARD_ARCHIVED
    assert ptero_world.kicks == [1]


async def test_archive_with_a_server_error_on_the_card_message_changes_nothing(world: World) -> None:
    await _live_card_with_message(world)
    world.state.overrides[f"DELETE {POSTED_MESSAGE}"] = Override(500, b'{"code": "X", "message": "no"}')
    world.kicks.clear()
    await world.run("!card archive mc")

    assert world.last_reply() == text.ARCHIVE_RETRY
    assert world.kicks == []
    card = await world.card("mc")
    assert card is not None
    assert (card.state, card.message_id) == ("live", "5101")


async def test_archive_with_a_missing_message_still_archives(world: World) -> None:
    await _live_card_with_message(world)
    world.state.messages.clear()                     # the message is gone: Fluxer answers 404
    await world.run("!card archive mc")

    assert (await world.card("mc")).state == "archived"
    assert world.last_reply() == text.CARD_ARCHIVED


async def test_archive_of_a_card_with_no_message_skips_the_delete(world: World) -> None:
    await make_mc(world)
    world.clear_requests()
    await world.run("!card archive mc")

    assert [r for r in world.state.requests if r.method == "DELETE"] == []
    assert (await world.card("mc")).state == "archived"


async def test_archive_when_already_archived_is_refused(world: World) -> None:
    await make_mc(world)
    await world.run("!card archive mc")
    await world.run("!card archive mc")
    assert world.last_reply() == text.ALREADY_ARCHIVED


async def test_archive_with_an_unreachable_fluxer_refuses_with_retry(world: World) -> None:
    await _live_card_with_message(world)
    world.state.drops.add(f"DELETE {POSTED_MESSAGE}")
    world.kicks.clear()
    await world.run("!card archive mc")
    assert world.kicks == []

    assert world.last_reply() == text.ARCHIVE_RETRY
    assert (await world.card("mc")).state == "live"


async def test_archive_with_a_timed_out_delete_refuses_with_retry(db: Database, fake) -> None:
    state, base = fake
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.2)) as session:
        world = await build(db, state, base, session)
        await _live_card_with_message(world)
        state.delays[f"DELETE {POSTED_MESSAGE}"] = 1.0
        await world.run("!card archive mc")

    assert world.last_reply() == text.ARCHIVE_RETRY
    assert (await world.card("mc")).state == "live"


async def test_archive_waits_for_the_card_lock(world: World) -> None:
    await make_mc(world)
    await world.run("!card publish mc")
    card = await world.card("mc")
    assert card is not None

    async with world.ctx.locks.get(card.id):
        task = asyncio.create_task(world.run("!card archive mc"))
        await asyncio.sleep(0.1)
        assert not task.done()
    await asyncio.wait_for(task, 5)
    assert (await world.card("mc")).state == "archived"


async def test_archive_of_a_card_deleted_while_waiting_replies_no_such_card(world: World) -> None:
    await make_mc(world)
    card = await world.card("mc")
    assert card is not None
    world.kicks.clear()
    async with world.ctx.locks.get(card.id):
        task = asyncio.create_task(world.run("!card archive mc"))
        await asyncio.sleep(0.1)
        assert not task.done()
        await world.db.run(lambda c: c.execute("DELETE FROM cards WHERE id = ?", (card.id,)))
    await asyncio.wait_for(task, 5)

    assert world.last_reply() == text.NO_SUCH_CARD
    assert world.kicks == []


async def test_publish_of_a_card_deleted_while_waiting_replies_no_such_card(world: World) -> None:
    await make_mc(world)
    card = await world.card("mc")
    assert card is not None
    world.kicks.clear()
    async with world.ctx.locks.get(card.id):
        task = asyncio.create_task(world.run("!card publish mc"))
        await asyncio.sleep(0.1)
        assert not task.done()
        await world.db.run(lambda c: c.execute("DELETE FROM cards WHERE id = ?", (card.id,)))
    await asyncio.wait_for(task, 5)

    assert world.last_reply() == text.NO_SUCH_CARD
    assert world.kicks == []


async def test_publish_waits_for_the_card_lock(world: World) -> None:
    await make_mc(world)
    card = await world.card("mc")
    assert card is not None
    async with world.ctx.locks.get(card.id):
        task = asyncio.create_task(world.run("!card publish mc"))
        await asyncio.sleep(0.1)
        assert not task.done()
        assert (await world.card("mc")).state == "draft"
    await asyncio.wait_for(task, 5)

    assert (await world.card("mc")).state == "live"
    assert world.last_reply() == text.PUBLISHED_NO_CHANNEL


# AC7.11: unknown slug ----------------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "!card set nope title X",
    "!card set nope voice 1001 2001",
    "!card clear nope description",
    "!card show nope",
    "!card publish nope",
    "!card archive nope",
])
async def test_unknown_slug_replies_no_such_card_and_creates_nothing(world: World, command: str) -> None:
    seed_voice(world.state)
    await world.run(command)

    assert world.last_reply() == text.NO_SUCH_CARD
    assert world.kicks == []
    assert await world.db.run(lambda c: cards.list_cards(c)) == []
    assert [r for r in world.state.requests if r.method != "POST"] == []   # no lookups, no Fluxer calls


# Usage: wrong argument counts and unknown subcommands --------------------------------------------------

@pytest.mark.parametrize("args", [
    "", "frobnicate mc", "new mc", "set mc title", "clear mc", "clear mc description extra",
    "show", "show mc extra", "publish", "archive mc extra", "list extra",
])
async def test_wrong_argument_count_gets_usage(world: World, args: str) -> None:
    await world.run(f"!card {args}".rstrip())
    assert world.last_reply() == text.CARD_USAGE


# Help: the subcommand pages, routed by `!card help` ----------------------------------------------------

@pytest.mark.parametrize(("content", "expected"), [
    ("!card help", "commands"),
    ("!card HELP", "commands"),
    ("!card help fields", "fields"),
    ("!card help FIELDS", "fields"),
    ("!card help fields extra", "usage"),
    ("!card help nonsense", "usage"),
])
async def test_help_replies_with_the_matching_page_and_changes_nothing(
        world: World, content: str, expected: str) -> None:
    await world.run(content)

    replies = {"commands": text.HELP_COMMANDS, "fields": help_fields_reply(), "usage": text.CARD_USAGE}
    assert world.last_reply() == replies[expected]
    assert world.kicks == []
    assert await world.db.run(lambda c: cards.list_cards(c)) == []


# AC9.2: logs carry only allowlisted fields -----------------------------------------------------------

@pytest.fixture
def json_logs(root_logging):
    stream = io.StringIO()
    setup_logging("DEBUG", [TOKEN], stream)
    return stream


async def test_command_logs_never_contain_card_text(world: World, json_logs: io.StringIO) -> None:
    seed_voice(world.state, name="SENTINEL-CHAN")
    await world.run("!card new mc SENTINEL-TITLE")
    await world.run("!card set mc location SENTINEL-LOC")
    await world.run("!card set mc address sentinel-host.invalid:19132")
    await world.run("!card set mc voice 1001 2001")
    await world.run("!card set mc probe a2s sentinel-host.invalid 27015")
    await world.run("!card SENTINEL-sub x")
    await world.run("!card set mc SENTINEL-field v")

    output = json_logs.getvalue()
    for sentinel in ("SENTINEL-TITLE", "SENTINEL-LOC", "sentinel-host", "SENTINEL-CHAN", "SENTINEL-sub",
                     "SENTINEL-field", TOKEN):
        assert sentinel not in output, sentinel
    events = [json.loads(line) for line in output.splitlines() if '"admin_command"' in line]
    assert any(e.get("field") == "location" and "card_id" in e for e in events)
    assert any(e.get("command") == "card_unknown" for e in events)
    assert any(e.get("field") == "unknown" for e in events)


@pytest.mark.parametrize(("content", "command"), [
    ("!card list", "card_list"),
    ("!card frobnicate", "card_unknown"),
    ("!card", "card_unknown"),
])
async def test_admin_command_logs_fixed_command_vocabulary(world: World, caplog, content: str,
                                                            command: str) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel.card_commands")
    await world.run(content)

    [record] = [r for r in caplog.records if r.name == "fluxer_panel.card_commands"
                and r.getMessage() == "admin_command"]
    assert record.panel_fields["command"] == command
    assert "field" not in record.panel_fields


async def test_clear_logs_the_field_only_when_it_is_clearable(world: World, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel.card_commands")
    await make_mc(world)
    await world.run("!card clear mc title")   # title is settable but not clearable

    [record] = [r for r in caplog.records if r.name == "fluxer_panel.card_commands"
                and r.getMessage() == "admin_command" and r.panel_fields["command"] == "card_clear"]
    assert record.panel_fields == {"command": "card_clear", "field": "unknown", "card_id": 1, "user_id": "1001"}
