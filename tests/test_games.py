# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The games community lookup: one GET per channel, kept for the process, retried after a failure."""

from __future__ import annotations

from dataclasses import replace

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_panel.config import Config
from fluxer_panel.games import BotIdentity, GamesGuild
from tests.fakes import FakeFluxerState, Override, Recorded, make_rest_app

TOKEN = "SENTINEL-TOKEN-games"
GAMES = "3001"
OTHER = "3002"
NO_COMMUNITY = "3003"
GUILD = "1001"
OTHER_GUILD = "1002"


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState()
    state.channels[GAMES] = {"id": GAMES, "name": "games", "type": 0, "guild_id": GUILD}
    state.channels[OTHER] = {"id": OTHER, "name": "other", "type": 0, "guild_id": OTHER_GUILD}
    state.channels[NO_COMMUNITY] = {"id": NO_COMMUNITY, "name": "dm-like", "type": 1}
    server = await aiohttp_server(make_rest_app(state))
    return state, str(server.make_url("/")).rstrip("/")


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


def make_config(games: str | None = GAMES) -> Config:
    env = {"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": "https://fluxer.invalid", "ADMIN_USER_IDS": "1001"}
    return replace(Config.from_env(env), games_channel_id=games)


def channel_gets(state: FakeFluxerState, channel: str) -> list[Recorded]:
    return state.requests_to(f"/api/v1/channels/{channel}")


async def test_the_community_of_the_channel_is_returned(fake, session) -> None:
    state, base = fake
    games = GamesGuild(FluxerREST(session, f"{base}/api", TOKEN), make_config())

    assert await games.get(GAMES) == GUILD


async def test_two_calls_for_one_channel_make_one_request(fake, session) -> None:
    state, base = fake
    games = GamesGuild(FluxerREST(session, f"{base}/api", TOKEN), make_config())

    await games.get(GAMES)
    await games.get(GAMES)

    assert len(channel_gets(state, GAMES)) == 1


async def test_another_channel_is_looked_up_separately(fake, session) -> None:
    state, base = fake
    games = GamesGuild(FluxerREST(session, f"{base}/api", TOKEN), make_config())

    assert await games.get(GAMES) == GUILD
    assert await games.get(OTHER) == OTHER_GUILD
    assert len(channel_gets(state, GAMES)) == 1
    assert len(channel_gets(state, OTHER)) == 1


async def test_a_failed_lookup_gives_none_and_is_retried_on_the_next_call(fake, session) -> None:
    state, base = fake
    state.overrides[f"GET /api/v1/channels/{GAMES}"] = Override(500, b'{"code":"X","message":"x"}')
    games = GamesGuild(FluxerREST(session, f"{base}/api", TOKEN), make_config())

    assert await games.get(GAMES) is None
    del state.overrides[f"GET /api/v1/channels/{GAMES}"]
    assert await games.get(GAMES) == GUILD
    assert len(channel_gets(state, GAMES)) == 2


async def test_a_channel_without_a_community_gives_none_and_is_retried(fake, session) -> None:
    state, base = fake
    games = GamesGuild(FluxerREST(session, f"{base}/api", TOKEN), make_config())

    assert await games.get(NO_COMMUNITY) is None
    assert await games.get(NO_COMMUNITY) is None
    assert len(channel_gets(state, NO_COMMUNITY)) == 2


async def test_no_channel_and_no_games_channel_gives_none_without_a_request(fake, session) -> None:
    state, base = fake
    games = GamesGuild(FluxerREST(session, f"{base}/api", TOKEN), make_config(games=None))

    assert await games.get() is None
    assert state.requests == []


async def test_no_channel_argument_uses_the_configured_games_channel(fake, session) -> None:
    state, base = fake
    games = GamesGuild(FluxerREST(session, f"{base}/api", TOKEN), make_config(games=OTHER))

    assert await games.get() == OTHER_GUILD
    assert len(channel_gets(state, OTHER)) == 1


def test_bot_identity_starts_unset() -> None:
    assert BotIdentity().user_id is None
