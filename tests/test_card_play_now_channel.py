# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`!card set` and `!card clear` for `play_now_channel`, through the admin path and the fake Fluxer REST server. A
channel is checked the way a text channel slot is checked, and the games channel is refused before any Fluxer call. A
set on a posted card seeds the play-now reaction on it."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest

from fluxer_panel import cards, text
from fluxer_panel.cards import ChannelLink
from fluxer_panel.db import Database
from fluxer_panel.games import GamesGuild
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import PLAY_NOW_EMOJI
from fluxer_panel.seeding import Seeder
from tests.fakes import FakeFluxerState, Override, make_rest_app
from tests.test_card_commands import World, build, make_mc, seed_text, seed_voice

GUILD = "1001"
OTHER_GUILD = "1009"
GAMES = "2100"
TEXT = "2002"
VOICE = "2001"
POSTED = "5101"
NOT_TEXT = text.CHANNEL_WRONG_TYPE.format(kind=text.CHANNEL_KIND_TEXT)


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
    seed_text(state)
    return await build(db, state, base, session)


async def make_games_world(db: Database, fake: tuple[FakeFluxerState, str], session: aiohttp.ClientSession) -> World:
    """A world with a games channel and the seeder wired, as the poller's context has it."""
    state, base = fake
    seed_text(state)
    state.channels[GAMES] = {"id": GAMES, "type": 0, "guild_id": GUILD, "name": "games"}
    world = await build(db, state, base, session, GAMES_CHANNEL_ID=GAMES)
    world.ctx.games_guild = GamesGuild(world.rest, world.config)
    world.ctx.seeder = Seeder(rest=world.rest, pacer=ChannelPacer(), db=db)
    return world


async def stored(world: World, slug: str = "mc") -> ChannelLink | None:
    card = await world.card(slug)
    assert card is not None
    return card.play_now_channel


def play_now_puts(world: World, channel_id: str, message_id: str) -> list[str]:
    prefix = f"/api/v1/channels/{channel_id}/messages/{message_id}/reactions/"
    return [r.path for r in world.state.requests if r.method == "PUT" and r.path.startswith(prefix)
            and PLAY_NOW_EMOJI in r.path]


# Set: a verified text channel is stored --------------------------------------------------------------

async def test_set_stores_the_verified_text_channel(world: World) -> None:
    await make_mc(world)
    await world.run(f"!card set mc play_now_channel {GUILD} {TEXT}")

    assert await stored(world) == ChannelLink(GUILD, TEXT, "general", True)
    assert world.last_reply() == text.CHANNEL_VERIFIED
    assert world.kicks == [1]


async def test_set_accepts_a_pasted_channel_link(world: World) -> None:
    await make_mc(world)
    await world.run(f"!card set mc play_now_channel https://fluxer.invalid/channels/{GUILD}/{TEXT}")

    assert await stored(world) == ChannelLink(GUILD, TEXT, "general", True)


# AC4.5: the games channel is refused before any Fluxer call -----------------------------------------

async def test_set_refuses_the_games_channel_before_any_fluxer_call(db, fake, session) -> None:
    world = await make_games_world(db, fake, session)
    await make_mc(world)
    world.state.requests.clear()
    await world.run(f"!card set mc play_now_channel {GUILD} {GAMES}")

    assert world.last_reply() == text.PLAY_NOW_IS_GAMES
    assert await stored(world) is None
    assert world.kicks == []
    assert [r for r in world.state.requests if GAMES in r.path or f"/guilds/{GUILD}" in r.path] == []


# AC4.6: a wrong type, a community the bot is not in, or another community is refused ---------------

async def test_set_refuses_a_voice_channel(world: World) -> None:
    seed_voice(world.state, channel=VOICE)
    await make_mc(world)
    await world.run(f"!card set mc play_now_channel {GUILD} {VOICE}")

    assert world.last_reply() == text.CHANNEL_WRONG_TYPE.format(kind=text.CHANNEL_KIND_TEXT)
    assert await stored(world) is None
    assert world.kicks == []


@pytest.mark.parametrize("status", [403, 404])
async def test_set_refuses_a_community_the_bot_is_not_in_and_stores_nothing(world: World, status: int) -> None:
    world.state.overrides[f"GET /api/v1/guilds/{GUILD}"] = Override(status, b'{"code": "X", "message": "no"}')
    await make_mc(world)
    await world.run(f"!card set mc play_now_channel {GUILD} {TEXT}")

    assert world.last_reply() == text.SUBSCRIPTION_NOT_IN_COMMUNITY
    assert await stored(world) is None
    assert world.kicks == []


async def test_set_refuses_a_channel_of_another_community(world: World) -> None:
    seed_text(world.state, guild=OTHER_GUILD)
    await make_mc(world)
    await world.run(f"!card set mc play_now_channel {GUILD} {TEXT}")

    assert world.last_reply() == text.CHANNEL_OTHER_COMMUNITY
    assert await stored(world) is None


# A set on a posted card seeds the play-now reaction -------------------------------------------------

async def test_set_on_a_posted_live_card_seeds_the_play_now_reaction(db, fake, session) -> None:
    world = await make_games_world(db, fake, session)
    await make_mc(world)
    card = await world.card("mc")
    assert card is not None
    world.state.messages[(GAMES, POSTED)] = {"content": "posted"}
    await world.db.run(lambda c: cards.set_state(c, card.id, "live", 1_800_000_000))
    await world.db.run(lambda c: cards.set_message(c, card.id, GAMES, POSTED, None))
    await world.run(f"!card set mc play_now_channel {GUILD} {TEXT}")

    assert len(play_now_puts(world, GAMES, POSTED)) == 1


# Clear ----------------------------------------------------------------------------------------------

async def test_clear_removes_the_stored_channel(world: World) -> None:
    await make_mc(world)
    await world.run(f"!card set mc play_now_channel {GUILD} {TEXT}")
    await world.run("!card clear mc play_now_channel")

    assert await stored(world) is None
    assert world.last_reply() == text.CARD_CLEARED
