# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Follow roles against the fake REST app and a real SQLite file: the toggle, the hierarchy helpers, and the
publish-time ensure."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import cards, follow_roles
from fluxer_panel.cards import Card, FollowRole
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.follow_roles import (
    FollowDeps,
    Toggle,
    ToggleResult,
    bot_top,
    manageable,
    rank,
    toggle_follow,
    zero_permissions,
)
from fluxer_panel.games import BotIdentity
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "test.token"
GUILD = "1001"
BOT = "1900"
USER = "2001"
BOT_ROLE = "5000"
ALPHA_ROLE = "6000"       # the card's follow role: permissions 0, position 5, below the bot
OTHER_ROLE = "6100"       # another valid role below the bot, not any card's follow role
NOW = 1_800_000_000
ROLES_PATH = f"/api/v1/guilds/{GUILD}/roles"
PUT_ALPHA = f"/api/v1/guilds/{GUILD}/members/{USER}/roles/{ALPHA_ROLE}"
EVERYONE = {"id": GUILD, "name": "@everyone", "permissions": "0", "position": 0, "mentionable": False}
BOT_ROLE_DATA = {"id": BOT_ROLE, "name": "bot", "permissions": "8", "position": 10, "mentionable": False}
ALPHA_DATA = {"id": ALPHA_ROLE, "name": "card_subscription_alpha", "permissions": "0", "position": 5,
              "mentionable": True}
OTHER_DATA = {"id": OTHER_ROLE, "name": "other", "permissions": "0", "position": 6, "mentionable": True}


def base_roles() -> list[dict[str, Any]]:
    return [dict(EVERYONE), dict(BOT_ROLE_DATA), dict(ALPHA_DATA), dict(OTHER_DATA)]


def make_config() -> Config:
    env = {"FLUXER_TOKEN": TOKEN, "FLUXER_INSTANCE_URL": "https://fluxer.invalid", "ADMIN_USER_IDS": "1001"}
    return Config.from_env(env)


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState()
    state.roles = {GUILD: base_roles()}
    state.members = {(GUILD, BOT): {"roles": [BOT_ROLE]}, (GUILD, USER): {"roles": []}}
    server = await aiohttp_server(make_rest_app(state))
    return state, f"{str(server.make_url('/')).rstrip('/')}/api"


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def db(tmp_path: Path):
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def deps(fake, session, db):
    _, api = fake
    rest = FluxerREST(session, api, TOKEN)
    return FollowDeps(db=db, rest=rest, config=make_config(), roles=RoleCache(rest),
                      identity=BotIdentity(user_id=BOT))


async def make_card(db: Database, slug: str = "alpha") -> Card:
    card_id = await db.run(lambda c: cards.create_card(c, slug=slug, title="Alpha", author_id="1001", now=NOW))
    return await db.run(lambda c: cards.get_by_id(c, card_id))


async def store_role(db: Database, card: Card, role_id: str) -> Card:
    await db.run(lambda c: cards.set_follow_role(c, card.id, GUILD, role_id))
    return await db.run(lambda c: cards.get_by_id(c, card.id))


def writes(state: FakeFluxerState) -> list[tuple[str, str]]:
    """Every request that is not a GET, as (method, path)."""
    return [(r.method, r.path) for r in state.requests if r.method != "GET"]


# Toggle ---------------------------------------------------------------------------------------------

async def test_press_by_user_without_the_role_adds_it(fake, deps, db) -> None:
    state, _ = fake
    card = await store_role(db, await make_card(db), ALPHA_ROLE)

    result = await toggle_follow(deps, GUILD, USER, card, member_role_ids=frozenset())

    assert result == ToggleResult(Toggle.ADDED)
    assert writes(state) == [("PUT", PUT_ALPHA)]
    assert state.requests_to(PUT_ALPHA)[0].headers["X-Audit-Log-Reason"] == follow_roles.REASON


async def test_press_by_user_with_the_role_removes_it(fake, deps, db) -> None:
    state, _ = fake
    card = await store_role(db, await make_card(db), ALPHA_ROLE)

    result = await toggle_follow(deps, GUILD, USER, card, member_role_ids=frozenset({ALPHA_ROLE}))

    assert result == ToggleResult(Toggle.REMOVED)
    assert writes(state) == [("DELETE", PUT_ALPHA)]


async def test_member_roles_are_fetched_when_the_event_carries_none(fake, deps, db) -> None:
    state, _ = fake
    state.members[(GUILD, USER)] = {"roles": [ALPHA_ROLE]}
    card = await store_role(db, await make_card(db), ALPHA_ROLE)

    result = await toggle_follow(deps, GUILD, USER, card, member_role_ids=None)

    assert result == ToggleResult(Toggle.REMOVED)
    assert writes(state) == [("DELETE", PUT_ALPHA)]


@pytest.mark.parametrize("stored_role, snapshot_role", [
    (None, OTHER_ROLE),       # a valid role below the bot that no card has as its follow role
    (ALPHA_ROLE, OTHER_ROLE),  # the press's snapshot disagrees with the database
    (None, None),
])
async def test_role_outside_the_allowlist_is_refused_with_no_role_call(
        fake, deps, db, stored_role: str | None, snapshot_role: str | None) -> None:
    state, _ = fake
    card = await make_card(db)
    if stored_role is not None:
        card = await store_role(db, card, stored_role)
    snapshot = None if snapshot_role is None else FollowRole(GUILD, snapshot_role)
    card = replace(card, follow_role=snapshot)

    result = await toggle_follow(deps, GUILD, USER, card, member_role_ids=frozenset())

    assert result == ToggleResult(Toggle.UNAVAILABLE, "not_allowlisted")
    assert writes(state) == []


async def test_role_at_or_above_the_bot_is_refused_with_no_role_call(fake, deps, db) -> None:
    state, _ = fake
    state.roles[GUILD].append({"id": "6200", "name": "high", "permissions": "0", "position": 20,
                               "mentionable": True})
    card = await store_role(db, await make_card(db), "6200")

    result = await toggle_follow(deps, GUILD, USER, card, member_role_ids=frozenset())

    assert result == ToggleResult(Toggle.UNAVAILABLE, "hierarchy")
    assert writes(state) == []


async def test_equal_position_with_a_higher_bot_role_id_is_refused(fake, deps, db) -> None:
    state, _ = fake
    state.roles[GUILD].append({"id": "4000", "name": "tie", "permissions": "0", "position": 10,
                               "mentionable": True})
    card = await store_role(db, await make_card(db), "4000")

    result = await toggle_follow(deps, GUILD, USER, card, member_role_ids=frozenset())

    assert result == ToggleResult(Toggle.UNAVAILABLE, "hierarchy")
    assert writes(state) == []


async def test_role_with_permissions_is_refused_with_no_role_call(fake, deps, db) -> None:
    state, _ = fake
    state.roles[GUILD] = [dict(EVERYONE), dict(BOT_ROLE_DATA), {**ALPHA_DATA, "permissions": "8"}]
    card = await store_role(db, await make_card(db), ALPHA_ROLE)

    result = await toggle_follow(deps, GUILD, USER, card, member_role_ids=frozenset())

    assert result == ToggleResult(Toggle.UNAVAILABLE, "role_permissions")
    assert writes(state) == []


async def test_stored_role_missing_from_the_guild_is_refused(fake, deps, db) -> None:
    state, _ = fake
    state.roles[GUILD] = [dict(EVERYONE), dict(BOT_ROLE_DATA)]
    card = await store_role(db, await make_card(db), ALPHA_ROLE)

    result = await toggle_follow(deps, GUILD, USER, card, member_role_ids=frozenset())

    assert result == ToggleResult(Toggle.UNAVAILABLE, "role_missing")
    assert writes(state) == []


async def test_forbidden_on_the_role_call_is_unavailable(fake, deps, db) -> None:
    state, _ = fake
    state.overrides[f"PUT {PUT_ALPHA}"] = Override(403, b'{"code":"MISSING_PERMISSIONS","message":"no"}')
    card = await store_role(db, await make_card(db), ALPHA_ROLE)

    result = await toggle_follow(deps, GUILD, USER, card, member_role_ids=frozenset())

    assert result == ToggleResult(Toggle.UNAVAILABLE, "forbidden")


async def test_no_bot_id_makes_no_role_call(fake, deps, db) -> None:
    state, _ = fake
    card = await store_role(db, await make_card(db), ALPHA_ROLE)
    deps = replace(deps, identity=BotIdentity(user_id=None))

    result = await toggle_follow(deps, GUILD, USER, card, member_role_ids=frozenset())

    assert result == ToggleResult(Toggle.UNAVAILABLE, "no_bot_id")
    assert writes(state) == []


# Pure helpers ---------------------------------------------------------------------------------------

def role(rid: str, position: int, permissions: Any = "0") -> dict[str, Any]:
    return {"id": rid, "name": f"r{rid}", "permissions": permissions, "position": position, "mentionable": True}


def test_zero_permissions_accepts_string_and_int_zero() -> None:
    assert zero_permissions({"permissions": "0"}) is True
    assert zero_permissions({"permissions": 0}) is True
    assert zero_permissions({"permissions": "8"}) is False
    assert zero_permissions({}) is False


def test_rank_is_position_then_negated_id() -> None:
    assert rank(role("5000", 10)) == (10, -5000)


def test_bot_top_is_the_highest_rank_of_the_bots_roles() -> None:
    roles = [role(GUILD, 0), role("5000", 10), role("5100", 12), role("5200", 3)]
    assert bot_top(roles, ["5000", "5200"], GUILD) == rank(role("5000", 10))


def test_bot_with_no_roles_ranks_as_everyone() -> None:
    roles = [role(GUILD, 0), role("6000", 1)]
    assert bot_top(roles, [], GUILD) == rank(role(GUILD, 0))


def test_bot_with_no_roles_and_no_everyone_manages_nothing() -> None:
    roles = [role("6000", 1)]
    assert manageable(role("6000", 1), roles, [], GUILD) is False


def test_manageable_below_the_bot() -> None:
    roles = [role(GUILD, 0), role("5000", 10), role("6000", 5)]
    assert manageable(role("6000", 5), roles, ["5000"], GUILD) is True


def test_manageable_refuses_above_the_bots_position() -> None:
    roles = [role(GUILD, 0), role("5000", 10), role("6000", 11)]
    assert manageable(role("6000", 11), roles, ["5000"], GUILD) is False


def test_tie_rule_manages_only_a_target_with_a_higher_id_than_the_bot_role() -> None:
    roles = [role(GUILD, 0), role("5000", 10), role("4000", 10), role("6000", 10)]
    assert manageable(role("4000", 10), roles, ["5000"], GUILD) is False   # lower id: bot is below it
    assert manageable(role("6000", 10), roles, ["5000"], GUILD) is True    # higher id: bot is above it
