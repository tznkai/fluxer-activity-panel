# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scopes against a real SQLite file: which scope a press resolves to, where a scope posts, and the checked target. The
fake REST app records every request, so a test can show that a subscription's target costs no REST call."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, scope, subscriptions
from fluxer_panel.cards import ChannelLink
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.presses import Surface
from tests.fakes import FakeFluxerState, make_rest_app

TOKEN = "SENTINEL-TOKEN-scope"
JUMP_URL = "https://fluxer.invalid"
ADMIN = "1001"
GUILD = "2001"          # the games community
OTHER_GUILD = "2002"    # a far community
GAMES = "3001"          # the games channel, in GUILD
PLAY_CH = "3005"        # the card's own verified play-now channel, in GUILD
CONFIG_PLAY = "3006"    # PLAY_NOW_CHANNEL_ID
COPY = "3002"           # a redirect copy in GUILD
FAR_REDIRECT = "3003"   # a redirect copy in OTHER_GUILD
FAR_LOCAL = "3004"      # a local copy in OTHER_GUILD, with its own channel
FAR_LOCAL_CH = "3007"   # the local subscription's chosen channel
FAR_BOTH = "3008"       # a both copy in OTHER_GUILD
CARD_MSG = "5001"
COPY_MSG = "5002"
FAR_MSG = "5003"
LOCAL_MSG = "5004"
BOTH_MSG = "5005"
ALPHA_ROLE = "7001"
SUB_ROLE = "7002"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


def config(**env: str) -> Config:
    return Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": JUMP_URL,
                            "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES, **env})


def build_world(conn) -> dict[str, int]:
    card = cards.create_card(conn, slug="mc", title="Mine", author_id=ADMIN, now=1)
    cards.set_state(conn, card, "live", 1)
    cards.set_message(conn, card, GAMES, CARD_MSG, "a" * 64)
    cards.set_follow_role(conn, card, GUILD, ALPHA_ROLE)
    cards.set_play_now_channel(conn, card, ChannelLink(GUILD, PLAY_CH, "play", True))
    copy = subscriptions.add(conn, card, GUILD, COPY)
    subscriptions.set_message(conn, card, COPY, COPY_MSG, "a" * 64)
    redirect = subscriptions.add(conn, card, OTHER_GUILD, FAR_REDIRECT)
    subscriptions.set_message(conn, card, FAR_REDIRECT, FAR_MSG, "a" * 64)
    local = subscriptions.add(conn, card, OTHER_GUILD, FAR_LOCAL)
    subscriptions.set_message(conn, card, FAR_LOCAL, LOCAL_MSG, "a" * 64)
    subscriptions.set_mode_if(conn, local, "redirect", "local")
    cards.set_subscription_role(conn, local, SUB_ROLE)
    both = subscriptions.add(conn, card, OTHER_GUILD, FAR_BOTH)
    subscriptions.set_message(conn, card, FAR_BOTH, BOTH_MSG, "a" * 64)
    subscriptions.set_mode_if(conn, both, "redirect", "both")
    return {"card": card, "copy": copy, "redirect": redirect, "local": local, "both": both}


def surface(channel: str, message: str, guild: str, home: bool, sub_id: int | None = None,
            mode: str | None = None) -> Surface:
    return Surface("copy" if sub_id is not None else "card", channel, message, guild, home, sub_id, mode)


async def test_resolve_scope_is_home_for_the_card_and_for_a_home_copy(db: Database) -> None:
    ids = await db.run(build_world)
    card = await db.run(lambda c: cards.get_by_id(c, ids["card"]))
    assert card is not None
    home = await db.run(lambda c: scope.resolve_scope(c, card, Surface("card", GAMES, CARD_MSG, GUILD, True), GUILD))
    assert home == scope.home_scope(card, GUILD)
    assert home.sub is None and home.sub_id is None and home.role == card.follow_role
    copy = await db.run(lambda c: scope.resolve_scope(c, card, surface(COPY, COPY_MSG, GUILD, True, ids["copy"],
                                                                      "redirect"), GUILD))
    assert copy == scope.home_scope(card, GUILD)


async def test_resolve_scope_is_elsewhere_for_a_far_redirect_copy(db: Database) -> None:
    ids = await db.run(build_world)
    card = await db.run(lambda c: cards.get_by_id(c, ids["card"]))
    assert card is not None
    far = surface(FAR_REDIRECT, FAR_MSG, OTHER_GUILD, False, ids["redirect"], "redirect")
    assert await db.run(lambda c: scope.resolve_scope(c, card, far, GUILD)) is scope.ELSEWHERE


async def test_resolve_scope_is_the_subscription_for_a_local_copy_with_its_stored_role(db: Database) -> None:
    ids = await db.run(build_world)
    card = await db.run(lambda c: cards.get_by_id(c, ids["card"]))
    assert card is not None
    far = surface(FAR_LOCAL, LOCAL_MSG, OTHER_GUILD, False, ids["local"], "local")
    resolved = await db.run(lambda c: scope.resolve_scope(c, card, far, GUILD))
    assert isinstance(resolved, scope.Scope)
    assert resolved.sub_id == ids["local"]
    assert resolved.guild_id == OTHER_GUILD
    assert resolved.role is not None and resolved.role.role_id == SUB_ROLE


async def test_resolve_scope_reads_the_mode_now_not_the_mode_at_the_press(db: Database) -> None:
    # The press saw a local copy; the mode was then set back to redirect. The fresh read wins.
    ids = await db.run(build_world)
    card = await db.run(lambda c: cards.get_by_id(c, ids["card"]))
    assert card is not None
    await db.run(lambda c: subscriptions.set_mode_if(c, ids["local"], "local", "redirect"))
    far = surface(FAR_LOCAL, LOCAL_MSG, OTHER_GUILD, False, ids["local"], "local")
    assert await db.run(lambda c: scope.resolve_scope(c, card, far, GUILD)) is scope.ELSEWHERE


async def test_a_local_scope_without_a_stored_role_has_no_role(db: Database) -> None:
    ids = await db.run(build_world)
    card = await db.run(lambda c: cards.get_by_id(c, ids["card"]))
    assert card is not None
    await db.run(lambda c: subscriptions.set_mode_if(c, ids["both"], "redirect", "both"))
    both = surface(FAR_BOTH, BOTH_MSG, OTHER_GUILD, False, ids["both"], "both")
    resolved = await db.run(lambda c: scope.resolve_scope(c, card, both, GUILD))
    assert isinstance(resolved, scope.Scope)
    assert resolved.sub_id == ids["both"] and resolved.role is None


async def test_scope_target_for_home_is_the_verified_card_channel_then_the_configured_one(db: Database) -> None:
    ids = await db.run(build_world)
    card = await db.run(lambda c: cards.get_by_id(c, ids["card"]))
    assert card is not None
    cfg = config(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    assert scope.scope_target(card, cfg, scope.home_scope(card, GUILD)) == PLAY_CH
    await db.run(lambda c: cards.clear_play_now_channel(c, card.id))
    unlinked = await db.run(lambda c: cards.get_by_id(c, card.id))
    assert unlinked is not None
    assert scope.scope_target(unlinked, cfg, scope.home_scope(unlinked, GUILD)) == CONFIG_PLAY


async def test_scope_target_for_a_subscription_is_its_local_channel_then_its_copy_channel(db: Database) -> None:
    ids = await db.run(build_world)
    card = await db.run(lambda c: cards.get_by_id(c, ids["card"]))
    assert card is not None
    cfg = config(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    assert await sub_target(db, card, cfg, ids["local"]) == FAR_LOCAL
    await db.run(lambda c: subscriptions.set_local_channel(c, ids["local"], FAR_LOCAL_CH))
    assert await sub_target(db, card, cfg, ids["local"]) == FAR_LOCAL_CH


async def sub_target(db: Database, card: cards.Card, cfg: Config, sub_id: int) -> str | None:
    def build(conn) -> scope.Scope:
        sub = subscriptions.get_by_id(conn, sub_id)
        assert sub is not None
        return scope.scope_for_sub(conn, card, sub)
    return scope.scope_target(card, cfg, await db.run(build))


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def rest_api(aiohttp_server) -> AsyncIterator[tuple[FakeFluxerState, str]]:
    state = FakeFluxerState(bot_user_id="1900")
    state.channels.update({
        FAR_LOCAL: {"id": FAR_LOCAL, "guild_id": OTHER_GUILD, "type": 0},
        FAR_LOCAL_CH: {"id": FAR_LOCAL_CH, "guild_id": OTHER_GUILD, "type": 0},
    })
    server = await aiohttp_server(make_rest_app(state))
    yield state, f"{str(server.make_url('/')).rstrip('/')}/api"


async def test_checked_target_for_a_subscription_is_its_channel_with_no_rest_call(
        db: Database, session: aiohttp.ClientSession, rest_api: tuple[FakeFluxerState, str]) -> None:
    ids = await db.run(build_world)
    card = await db.run(lambda c: cards.get_by_id(c, ids["card"]))
    assert card is not None
    local = await db.run(lambda c: subscriptions.get_by_id(c, ids["local"]))
    assert local is not None
    state, api = rest_api
    rest = FluxerREST(session, api, TOKEN)
    before = len(state.requests)
    target, reason = await scope.checked_target(rest, config(), card,
                                                await db.run(lambda c: scope.scope_for_sub(c, card, local)))
    assert target == (FAR_LOCAL, OTHER_GUILD)
    assert reason is None
    assert len(state.requests) == before
