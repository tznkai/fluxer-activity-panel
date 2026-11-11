# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The DM backoff: a failed DM to one user blocks that user's press replies for the window. Promotion and close DMs
are attempted anyway. Clocks are stubs and nothing sleeps. Emoji are written as escapes."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import cards, notify, sessions
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import FOLLOW_EMOJI, WAKE_EMOJI, Presses
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_post import SessionPost
from fluxer_panel.sources.registry import build_sources
from fluxer_panel.wake import WakeRequests
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "SENTINEL-TOKEN-dm-backoff"
OPEN_DM = "/api/v1/users/@me/channels"
BOT_ID = "1900"
ADMIN = "1001"
USER = "1500"
PRESSER = "6001"
GUILD = "2001"
GAMES = "3001"
CARD_MSG = "5001"
CARD_ID = 1
CARD_TITLE = "Mine] (craft"
ALPHA_ROLE = "7001"
BOT_ROLE = "5000"
ROLE_PATH = f"/api/v1/guilds/{GUILD}/members/{PRESSER}/roles/{ALPHA_ROLE}"
PRESSER_DM = "9100"           # the presser's DM channel: pre-set so one override fails only the presser's sends
PRESSER_SEND = f"POST /api/v1/channels/{PRESSER_DM}/messages"
FORBIDDEN = b'{"code":"MISSING_PERMISSIONS","message":"no"}'
JUMP_URL = "https://fluxer.invalid"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# The backoff itself ----------------------------------------------------------------------------------

def test_a_user_is_not_blocked_before_any_failure() -> None:
    backoff = DmBackoff(60, clock=Clock())
    assert backoff.blocked(USER) is False


def test_a_failed_dm_blocks_the_user_until_the_window_ends() -> None:
    clock = Clock()
    backoff = DmBackoff(60, clock=clock)
    backoff.failed(USER)
    clock.now = 59.0
    assert backoff.blocked(USER) is True
    clock.now = 60.0
    assert backoff.blocked(USER) is False


def test_a_success_clears_the_block_at_once() -> None:
    clock = Clock()
    backoff = DmBackoff(60, clock=clock)
    backoff.failed(USER)
    backoff.succeeded(USER)
    assert backoff.blocked(USER) is False


def test_one_users_block_does_not_touch_another() -> None:
    backoff = DmBackoff(60, clock=Clock())
    backoff.failed(USER)
    assert backoff.blocked("1501") is False


# dm_user with a backoff, against the fake REST app ---------------------------------------------------

@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState()
    server = await aiohttp_server(make_rest_app(state))
    return state, str(server.make_url("/")).rstrip("/")


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
def captured(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


def panel_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("fluxer_panel")]


def open_dm_count(state: FakeFluxerState) -> int:
    return len(state.requests_to(OPEN_DM))


async def test_a_failed_dm_starts_the_backoff_and_the_next_dm_makes_no_request(fake, session, captured) -> None:
    state, base = fake
    state.overrides[f"POST {OPEN_DM}"] = Override(403, FORBIDDEN)
    rest = FluxerREST(session, f"{base}/api", TOKEN)
    clock = Clock()
    backoff = DmBackoff(3600, clock=clock)

    assert await notify.dm_user(rest, USER, "first", backoff=backoff) is None
    assert open_dm_count(state) == 1

    assert await notify.dm_user(rest, USER, "second", backoff=backoff) is None
    assert open_dm_count(state) == 1
    [skipped] = [r for r in panel_records(captured) if r.getMessage() == "user_dm_skipped"]
    assert getattr(skipped, FIELDS_ATTR) == {"reason": "backoff"}
    assert USER not in " ".join(r.getMessage() for r in panel_records(captured))


async def test_a_bypassed_dm_is_attempted_and_its_success_clears_the_backoff(fake, session) -> None:
    state, base = fake
    state.overrides[f"POST {OPEN_DM}"] = Override(403, FORBIDDEN)
    rest = FluxerREST(session, f"{base}/api", TOKEN)
    backoff = DmBackoff(3600, clock=Clock())
    await notify.dm_user(rest, USER, "first", backoff=backoff)
    assert backoff.blocked(USER) is True

    del state.overrides[f"POST {OPEN_DM}"]
    result = await notify.dm_user(rest, USER, "promotion", backoff=backoff, bypass=True)

    assert result is not None
    assert open_dm_count(state) == 2
    assert backoff.blocked(USER) is False
    assert await notify.dm_user(rest, USER, "later", backoff=backoff) is not None
    assert open_dm_count(state) == 3


async def test_a_bypassed_dm_that_fails_starts_the_backoff_again(fake, session) -> None:
    state, base = fake
    state.overrides[f"POST {OPEN_DM}"] = Override(403, FORBIDDEN)
    rest = FluxerREST(session, f"{base}/api", TOKEN)
    backoff = DmBackoff(3600, clock=Clock())

    assert await notify.dm_user(rest, USER, "promotion", backoff=backoff, bypass=True) is None
    assert open_dm_count(state) == 1
    assert backoff.blocked(USER) is True


async def test_without_a_backoff_a_failed_dm_does_not_block_the_next_one(fake, session) -> None:
    state, base = fake
    state.overrides[f"POST {OPEN_DM}"] = Override(403, FORBIDDEN)
    rest = FluxerREST(session, f"{base}/api", TOKEN)

    assert await notify.dm_user(rest, USER, "first") is None
    assert await notify.dm_user(rest, USER, "second") is None
    assert open_dm_count(state) == 2


# AC7.1 and AC7.2 through the real press and promotion paths ------------------------------------------

@dataclass
class PressWorld:
    state: FakeFluxerState
    presses: Presses
    backoff: DmBackoff
    clock: Clock


@pytest.fixture
async def press_world(tmp_path: Path, aiohttp_server, session) -> AsyncIterator[PressWorld]:
    state = FakeFluxerState(bot_user_id=BOT_ID)
    state.channels.update({GAMES: {"id": GAMES, "guild_id": GUILD, "type": 0}})
    state.roles = {GUILD: [
        {"id": GUILD, "name": "@everyone", "permissions": "0", "position": 0, "mentionable": False},
        {"id": BOT_ROLE, "name": "bot", "permissions": "8", "position": 10, "mentionable": False},
        {"id": ALPHA_ROLE, "name": "card_subscription_mc", "permissions": "0", "position": 5, "mentionable": True},
    ]}
    state.members = {(GUILD, BOT_ID): {"roles": [BOT_ROLE]}}
    state.dm_channels[PRESSER] = PRESSER_DM
    server = await aiohttp_server(make_rest_app(state))
    api = f"{str(server.make_url('/')).rstrip('/')}/api"
    database = Database.open(tmp_path / "backoff.db")
    try:
        await database.run(_seed_card)
        config = Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC",
                                  "FLUXER_INSTANCE_URL": JUMP_URL, "ADMIN_USER_IDS": ADMIN,
                                  "GAMES_CHANNEL_ID": GAMES})
        rest = FluxerREST(session, api, TOKEN)
        games = GamesGuild(rest, config)
        pacer = ChannelPacer(max_calls=1000)
        identity = BotIdentity(user_id=BOT_ID)
        clock = Clock()
        backoff = DmBackoff(3600, clock=clock)
        sources = build_sources(session, config)
        wake = WakeRequests(db=database, rest=rest, config=config, sources=sources, locks=CardLocks(),
                            status_of=lambda _id: None, clock=clock, backoff=backoff)
        interactions = Interactions(db=database, rest=rest, config=config, locks=CardLocks(), games_guild=games,
                                    seeder=Seeder(rest=rest, pacer=pacer, db=database), pacer=pacer,
                                    identity=identity, roles=RoleCache(rest), wake=wake, backoff=backoff)
        presses = Presses(db=database, rest=rest, config=config, handler=interactions, games_guild=games,
                          pacer=pacer, identity=identity, clock=clock, backoff=backoff)
        yield PressWorld(state=state, presses=presses, backoff=backoff, clock=clock)
    finally:
        database.close()


def _seed_card(conn) -> None:
    card = cards.create_card(conn, slug="mc", title=CARD_TITLE, author_id=ADMIN, now=1)
    cards.set_state(conn, card, "live", 1)
    cards.set_message(conn, card, GAMES, CARD_MSG, "a" * 64)
    cards.set_follow_role(conn, card, GUILD, ALPHA_ROLE)


def press(emoji: str, *, member_roles: frozenset[str] = frozenset()) -> events.ReactionAdd:
    return events.ReactionAdd(user_id=PRESSER, channel_id=GAMES, message_id=CARD_MSG, guild_id=GUILD,
                              emoji=events.Emoji(name=emoji, id=None), member_role_ids=member_roles,
                              user_is_bot=False)


async def test_a_follow_press_during_a_backoff_still_toggles_the_role_and_sends_no_dm(press_world) -> None:
    state = press_world.state
    state.overrides[PRESSER_SEND] = Override(403, FORBIDDEN)

    await press_world.presses.on_reaction(press(FOLLOW_EMOJI))
    assert press_world.backoff.blocked(PRESSER) is True
    assert open_dm_count(state) == 1

    press_world.clock.now += 11   # past the press's user window, still inside the DM backoff
    await press_world.presses.on_reaction(press(FOLLOW_EMOJI, member_roles=frozenset({ALPHA_ROLE})))

    assert [r.method for r in state.requests if r.path == ROLE_PATH] == ["PUT", "DELETE"]
    assert open_dm_count(state) == 1


async def test_a_wake_press_during_a_backoff_is_still_recorded_for_the_admins(press_world) -> None:
    state = press_world.state
    state.overrides[PRESSER_SEND] = Override(403, FORBIDDEN)

    await press_world.presses.on_reaction(press(WAKE_EMOJI))
    assert press_world.backoff.blocked(PRESSER) is True
    admin_channel = state.dm_channels[ADMIN]
    admin_before = len([r for r in state.requests if r.path.startswith(f"/api/v1/channels/{admin_channel}/")])
    requester_sends = len(state.requests_to(f"/api/v1/channels/{PRESSER_DM}/messages"))

    press_world.clock.now += 11
    await press_world.presses.on_reaction(press(WAKE_EMOJI))

    admin_after = len([r for r in state.requests if r.path.startswith(f"/api/v1/channels/{admin_channel}/")])
    assert admin_after > admin_before
    assert len(state.requests_to(f"/api/v1/channels/{PRESSER_DM}/messages")) == requester_sends


async def test_a_promotion_dm_is_attempted_during_a_backoff(tmp_path: Path, aiohttp_server, session) -> None:
    state = FakeFluxerState(bot_user_id=BOT_ID)
    server = await aiohttp_server(make_rest_app(state))
    api = f"{str(server.make_url('/')).rstrip('/')}/api"
    database = Database.open(tmp_path / "promotion.db")
    try:
        config = Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC",
                                  "FLUXER_INSTANCE_URL": JUMP_URL, "ADMIN_USER_IDS": ADMIN,
                                  "GAMES_CHANNEL_ID": GAMES})
        backoff = DmBackoff(3600, clock=Clock())
        backoff.failed(USER)
        post = SessionPost(db=database, rest=FluxerREST(session, api, TOKEN), config=config,
                           pacer=ChannelPacer(), backoff=backoff)

        def add(conn):
            card_id = cards.create_card(conn, slug="mc", title=CARD_TITLE, author_id=ADMIN, now=1)
            cards.set_state(conn, card_id, "live", 1)
            session_id = sessions.add(conn, card_id=card_id, guild_id=GUILD, channel_id="2101", message_id="5101",
                                      starts_at=1_800_000_000, ends_at=1_800_003_600, proposer_id=ADMIN, now=1)
            return cards.get_by_id(conn, card_id), sessions.get(conn, session_id)

        card, stored = await database.run(add)
        await post.dm_promoted(stored, card, [USER])

        assert open_dm_count(state) == 1
        assert len(state.requests_to(f"/api/v1/channels/{state.dm_channels[USER]}/messages")) == 1
    finally:
        database.close()
