# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Follow roles against the fake REST app and a real SQLite file: the toggle, the hierarchy helpers, and the
publish-time ensure."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import cards, follow_roles, subscriptions
from fluxer_panel.cards import Card, FollowRole
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.follow_roles import (
    EnsureResult,
    Ensured,
    FollowDeps,
    Toggle,
    ToggleResult,
    bot_top,
    ensure_follow_role,
    manageable,
    rank,
    role_name,
    toggle_follow,
    zero_permissions,
)
from fluxer_panel.games import BotIdentity
from tests.fakes import FakeFluxerState, Override, Recorded, make_rest_app, wait_until

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
    env = {"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": "https://fluxer.invalid", "ADMIN_USER_IDS": "1001"}
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


# Ensure (publish and manual set) --------------------------------------------------------------------

def fetches(state: FakeFluxerState, path: str) -> int:
    return len([r for r in state.requests if r.method == "GET" and r.path == path])


def requests_of(state: FakeFluxerState, method: str, path: str) -> list[Recorded]:
    return [r for r in state.requests if r.method == method and r.path == path]


def test_role_name_is_the_prefix_and_the_slug() -> None:
    card = SimpleNamespace(slug="alpha")
    assert role_name(make_config(), card) == "card_subscription_alpha"
    assert role_name(replace(make_config(), follow_role_prefix="g_"), card) == "g_alpha"


CREATE_PATH = f"/api/v1/guilds/{GUILD}/roles"
NEW_ROLE_PATH = f"/api/v1/guilds/{GUILD}/roles/7001"
ALPHA_PATCH = f"/api/v1/guilds/{GUILD}/roles/{ALPHA_ROLE}"
OTHER_PATCH = f"/api/v1/guilds/{GUILD}/roles/{OTHER_ROLE}"


async def stored(db: Database, card: Card) -> FollowRole | None:
    return await db.run(lambda c: cards.get_follow_role(c, card.id))


async def test_publish_creates_a_mentionable_role_and_stores_it(fake, deps, db) -> None:
    state, _ = fake
    card = await make_card(db, "beta")

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.CREATED, role_id="7001")
    assert requests_of(state, "POST", CREATE_PATH)[0].payload == {
        "name": "card_subscription_beta", "permissions": "0", "color": 0, "hoist": False, "mentionable": True,
    }
    assert requests_of(state, "PATCH", NEW_ROLE_PATH)[0].payload == {"mentionable": True}
    assert await stored(db, card) == FollowRole(GUILD, "7001")


@pytest.mark.parametrize("status", [403, 500])
async def test_create_then_failed_mentionable_call_stores_nothing(fake, deps, db, status: int) -> None:
    state, _ = fake
    state.overrides[f"PATCH {NEW_ROLE_PATH}"] = Override(status, b'{"code":"X","message":"x"}')
    card = await make_card(db, "beta")
    await deps.roles.role_ids(GUILD, "card_subscription_beta")   # the cache now holds the name list

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.FAILED, reason="mentionable_failed")
    assert await stored(db, card) is None
    assert len(requests_of(state, "POST", CREATE_PATH)) == 1
    assert len(requests_of(state, "PATCH", NEW_ROLE_PATH)) == 1
    assert [r.method for r in state.requests if r.method in ("PUT", "DELETE")] == []
    # The created role exists in Fluxer, so the cache must not answer from the old list: the next lookup fetches.
    fetched_by_ensure = fetches(state, ROLES_PATH)
    await deps.roles.role_ids(GUILD, "card_subscription_beta")
    assert fetches(state, ROLES_PATH) == fetched_by_ensure + 1


async def test_mentionable_call_that_reads_back_not_mentionable_stores_nothing(fake, deps, db) -> None:
    state, _ = fake
    state.overrides[f"PATCH {NEW_ROLE_PATH}"] = Override(
        200, b'{"id":"7001","mentionable":false}')
    card = await make_card(db, "beta")

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.FAILED, reason="mentionable_failed")
    assert await stored(db, card) is None


async def test_create_failure_is_failed_and_stores_nothing(fake, deps, db) -> None:
    state, _ = fake
    state.overrides[f"POST {CREATE_PATH}"] = Override(403, b'{"code":"MISSING_PERMISSIONS","message":"no"}')
    card = await make_card(db, "beta")

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.FAILED, reason="create_failed")
    assert await stored(db, card) is None
    assert requests_of(state, "PATCH", NEW_ROLE_PATH) == []


async def test_create_500_still_invalidates_the_role_cache(fake, deps, db) -> None:
    # A 5xx or a timeout can land after Fluxer has created the role, so the cached name list must not survive it.
    state, _ = fake
    state.overrides[f"POST {CREATE_PATH}"] = Override(500, b'{"code":"X","message":"x"}')
    card = await make_card(db, "beta")
    await deps.roles.role_ids(GUILD, "card_subscription_beta")   # the cache now holds the name list

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.FAILED, reason="create_failed")
    assert await stored(db, card) is None
    fetched_by_ensure = fetches(state, ROLES_PATH)
    await deps.roles.role_ids(GUILD, "card_subscription_beta")
    assert fetches(state, ROLES_PATH) == fetched_by_ensure + 1


async def test_lookup_500_is_failed_and_does_not_raise(fake, deps, db) -> None:
    state, _ = fake
    state.overrides[f"GET {ROLES_PATH}"] = Override(500, b'{"code":"X","message":"x"}')
    card = await make_card(db, "beta")

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.FAILED, reason="lookup_failed")
    assert writes(state) == []


async def test_dropped_lookup_is_failed_and_does_not_raise(fake, deps, db) -> None:
    state, _ = fake
    state.drops.add(f"GET {ROLES_PATH}")
    card = await make_card(db, "beta")

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.FAILED, reason="lookup_failed")
    assert writes(state) == []


async def test_publish_without_bot_id_makes_no_role_call(fake, deps, db) -> None:
    state, _ = fake
    card = await make_card(db, "beta")
    deps = replace(deps, identity=BotIdentity(user_id=None))

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.REFUSED, reason="no_bot_id")
    assert writes(state) == []


async def test_publish_reuses_the_stored_role_that_still_exists(fake, deps, db) -> None:
    state, _ = fake
    card = await store_role(db, await make_card(db), ALPHA_ROLE)

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.REUSED, role_id=ALPHA_ROLE)
    assert writes(state) == []


async def test_publish_adopts_the_named_role_when_the_stored_one_is_gone(fake, deps, db) -> None:
    state, _ = fake
    card = await store_role(db, await make_card(db, "alpha"), OTHER_ROLE)
    state.roles[GUILD] = [r for r in state.roles[GUILD] if r["id"] != OTHER_ROLE]

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.ADOPTED, role_id=ALPHA_ROLE)
    assert await stored(db, card) == FollowRole(GUILD, ALPHA_ROLE)
    assert writes(state) == []


async def test_publish_adopts_a_matching_mentionable_role_with_no_writes(fake, deps, db) -> None:
    state, _ = fake
    card = await make_card(db, "alpha")

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.ADOPTED, role_id=ALPHA_ROLE)
    assert await stored(db, card) == FollowRole(GUILD, ALPHA_ROLE)
    assert writes(state) == []


async def test_publish_makes_a_matching_non_mentionable_role_mentionable_before_storing(fake, deps, db) -> None:
    state, _ = fake
    for r in state.roles[GUILD]:
        if r["id"] == ALPHA_ROLE:
            r["mentionable"] = False
    card = await make_card(db, "alpha")

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.ADOPTED, role_id=ALPHA_ROLE)
    assert writes(state) == [("PATCH", ALPHA_PATCH)]
    assert requests_of(state, "PATCH", ALPHA_PATCH)[0].payload == {"mentionable": True}
    assert await stored(db, card) == FollowRole(GUILD, ALPHA_ROLE)


@pytest.mark.parametrize("status", [403, 500])
async def test_adopt_with_failed_mentionable_call_stores_nothing(fake, deps, db, status: int) -> None:
    state, _ = fake
    for r in state.roles[GUILD]:
        if r["id"] == ALPHA_ROLE:
            r["mentionable"] = False
    state.overrides[f"PATCH {ALPHA_PATCH}"] = Override(status, b'{"code":"X","message":"x"}')
    card = await make_card(db, "alpha")

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.FAILED, reason="mentionable_failed")
    assert await stored(db, card) is None


async def test_matching_role_owned_by_another_card_is_taken_with_no_write(fake, deps, db) -> None:
    state, _ = fake
    for r in state.roles[GUILD]:
        if r["id"] == ALPHA_ROLE:
            r["mentionable"] = False
    await store_role(db, await make_card(db, "zulu"), ALPHA_ROLE)
    card = await make_card(db, "alpha")

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.REFUSED, reason="taken")
    assert writes(state) == []
    assert await stored(db, card) is None


async def test_matching_role_with_permissions_is_refused_and_never_patched(fake, deps, db) -> None:
    state, _ = fake
    for r in state.roles[GUILD]:
        if r["id"] == ALPHA_ROLE:
            r.update(permissions="8", mentionable=False)
    card = await make_card(db, "alpha")

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.REFUSED, reason="role_permissions")
    assert writes(state) == []
    assert await stored(db, card) is None


async def test_matching_role_at_or_above_the_bot_is_refused_and_never_patched(fake, deps, db) -> None:
    state, _ = fake
    for r in state.roles[GUILD]:
        if r["id"] == ALPHA_ROLE:
            r.update(position=20, mentionable=False)
    card = await make_card(db, "alpha")

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.REFUSED, reason="hierarchy")
    assert writes(state) == []
    assert await stored(db, card) is None


async def test_two_roles_with_the_same_name_are_refused(fake, deps, db) -> None:
    state, _ = fake
    state.roles[GUILD].append({"id": "6300", "name": "CARD_SUBSCRIPTION_ALPHA", "permissions": "0",
                               "position": 4, "mentionable": True})
    card = await make_card(db, "alpha")

    result = await ensure_follow_role(deps, GUILD, card)

    assert result == EnsureResult(Ensured.REFUSED, reason="duplicate_name")
    assert writes(state) == []
    assert await stored(db, card) is None


# Manual set (role_id given) -------------------------------------------------------------------------

async def test_set_stores_a_valid_mentionable_role_below_the_bot(fake, deps, db) -> None:
    state, _ = fake
    card = await make_card(db, "alpha")

    result = await ensure_follow_role(deps, GUILD, card, role_id=OTHER_ROLE)

    assert result == EnsureResult(Ensured.SET, role_id=OTHER_ROLE)
    assert await stored(db, card) == FollowRole(GUILD, OTHER_ROLE)
    assert writes(state) == []


@pytest.mark.parametrize("mutate, role_id, reason", [
    (lambda roles: None, "9999", "missing"),
    (lambda roles: None, GUILD, "everyone"),
    (lambda roles: roles.append({"id": "6500", "name": "x", "permissions": "8", "position": 2,
                                 "mentionable": True}), "6500", "role_permissions"),
    (lambda roles: roles.append({"id": "6500", "name": "x", "permissions": "0", "position": 2,
                                 "mentionable": False}), "6500", "not_mentionable"),
    (lambda roles: roles.append({"id": "6600", "name": "x", "permissions": "0", "position": 20,
                                 "mentionable": True}), "6600", "hierarchy"),
    (lambda roles: roles.append({"id": "4000", "name": "x", "permissions": "0", "position": 10,
                                 "mentionable": True}), "4000", "hierarchy"),
])
async def test_set_refuses_with_a_reason_and_stores_nothing(fake, deps, db, mutate, role_id: str,
                                                            reason: str) -> None:
    state, _ = fake
    mutate(state.roles[GUILD])
    card = await make_card(db, "alpha")

    result = await ensure_follow_role(deps, GUILD, card, role_id=role_id)

    assert result == EnsureResult(Ensured.REFUSED, reason=reason)
    assert await stored(db, card) is None
    assert writes(state) == []


async def test_set_refuses_a_role_another_card_already_follows(fake, deps, db) -> None:
    state, _ = fake
    await store_role(db, await make_card(db, "zulu"), OTHER_ROLE)
    card = await make_card(db, "alpha")

    result = await ensure_follow_role(deps, GUILD, card, role_id=OTHER_ROLE)

    assert result == EnsureResult(Ensured.REFUSED, reason="taken")
    assert await stored(db, card) is None
    assert writes(state) == []


# Subscription roles in a far community ---------------------------------------------------------------

FAR = "1002"
FAR_CHANNEL = "1100"
FAR_BOT_ROLE = "5050"
FAR_NEW_ROLE_PATH = f"/api/v1/guilds/{FAR}/roles/7001"
FAR_OTHER_ROLE = "6700"
FAR_ROLES_PATH = f"/api/v1/guilds/{FAR}/roles"


def add_far_community(state: FakeFluxerState) -> None:
    state.roles[FAR] = [
        {"id": FAR, "name": "@everyone", "permissions": "0", "position": 0, "mentionable": False},
        {"id": FAR_BOT_ROLE, "name": "bot", "permissions": "8", "position": 10, "mentionable": False},
    ]
    state.members[(FAR, BOT)] = {"roles": [FAR_BOT_ROLE]}


async def make_sub(db: Database, card: Card, channel_id: str = FAR_CHANNEL) -> int:
    return await db.run(lambda c: subscriptions.add(c, card.id, FAR, channel_id))


async def sub_role(db: Database, sub_id: int) -> FollowRole | None:
    return await db.run(lambda c: cards.get_subscription_role(c, sub_id))


async def test_sub_publish_creates_a_mentionable_role_in_its_community(fake, deps, db) -> None:
    state, _ = fake
    add_far_community(state)
    card = await make_card(db, "alpha")
    sub_id = await make_sub(db, card)

    result = await ensure_follow_role(deps, FAR, card, sub_id=sub_id)

    assert result == EnsureResult(Ensured.CREATED, role_id="7001")
    assert requests_of(state, "POST", FAR_ROLES_PATH)[0].payload["name"] == "card_subscription_alpha"
    assert requests_of(state, "PATCH", FAR_NEW_ROLE_PATH)[0].payload == {"mentionable": True}
    assert await sub_role(db, sub_id) == FollowRole(FAR, "7001")
    assert await stored(db, card) is None


async def test_sub_publish_adopts_a_same_named_role_in_its_community(fake, deps, db) -> None:
    state, _ = fake
    add_far_community(state)
    state.roles[FAR].append({"id": "6400", "name": "card_subscription_alpha", "permissions": "0",
                             "position": 5, "mentionable": True})
    card = await make_card(db, "alpha")
    sub_id = await make_sub(db, card)

    result = await ensure_follow_role(deps, FAR, card, sub_id=sub_id)

    assert result == EnsureResult(Ensured.ADOPTED, role_id="6400")
    assert await sub_role(db, sub_id) == FollowRole(FAR, "6400")
    assert requests_of(state, "POST", FAR_ROLES_PATH) == []
    assert await stored(db, card) is None


async def test_second_sub_clashing_with_the_first_is_taken_and_stores_nothing(fake, deps, db) -> None:
    state, _ = fake
    add_far_community(state)
    card = await make_card(db, "alpha")
    first = await make_sub(db, card, FAR_CHANNEL)
    second = await make_sub(db, card, "1101")
    assert (await ensure_follow_role(deps, FAR, card, sub_id=first)).outcome is Ensured.CREATED
    writes_before = writes(state)

    result = await ensure_follow_role(deps, FAR, card, sub_id=second)

    assert result == EnsureResult(Ensured.REFUSED, reason="taken")
    assert await sub_role(db, second) is None
    assert await sub_role(db, first) == FollowRole(FAR, "7001")
    assert writes(state) == writes_before


async def test_two_unmanaged_same_named_roles_in_its_community_are_duplicate_name(fake, deps, db) -> None:
    state, _ = fake
    add_far_community(state)
    for role_id in ("6400", "6401"):
        state.roles[FAR].append({"id": role_id, "name": "card_subscription_alpha", "permissions": "0",
                                 "position": 5, "mentionable": True})
    card = await make_card(db, "alpha")
    sub_id = await make_sub(db, card)

    result = await ensure_follow_role(deps, FAR, card, sub_id=sub_id)

    assert result == EnsureResult(Ensured.REFUSED, reason="duplicate_name")
    assert await sub_role(db, sub_id) is None
    assert writes(state) == []


async def test_sub_set_stores_another_role_of_its_community(fake, deps, db) -> None:
    state, _ = fake
    add_far_community(state)
    state.roles[FAR].append({"id": FAR_OTHER_ROLE, "name": "other", "permissions": "0", "position": 5,
                             "mentionable": True})
    card = await make_card(db, "alpha")
    sub_id = await make_sub(db, card)

    result = await ensure_follow_role(deps, FAR, card, role_id=FAR_OTHER_ROLE, sub_id=sub_id)

    assert result == EnsureResult(Ensured.SET, role_id=FAR_OTHER_ROLE)
    assert await sub_role(db, sub_id) == FollowRole(FAR, FAR_OTHER_ROLE)
    assert writes(state) == []


@pytest.mark.parametrize("position, permissions, reason", [
    (20, "0", "hierarchy"),
    (5, "8", "role_permissions"),
])
async def test_sub_set_refuses_a_role_above_the_bot_or_with_permissions(fake, deps, db, position: int,
                                                                       permissions: str, reason: str) -> None:
    state, _ = fake
    add_far_community(state)
    state.roles[FAR].append({"id": FAR_OTHER_ROLE, "name": "other", "permissions": permissions,
                             "position": position, "mentionable": True})
    card = await make_card(db, "alpha")
    sub_id = await make_sub(db, card)

    result = await ensure_follow_role(deps, FAR, card, role_id=FAR_OTHER_ROLE, sub_id=sub_id)

    assert result == EnsureResult(Ensured.REFUSED, reason=reason)
    assert await sub_role(db, sub_id) is None
    assert writes(state) == []


async def test_sub_role_step_for_a_removed_subscription_is_refused_before_any_request(fake, deps, db) -> None:
    state, _ = fake
    card = await make_card(db, "alpha")
    sub_id = await make_sub(db, card)
    await db.run(lambda c: subscriptions.remove_by_id(c, sub_id))

    result = await ensure_follow_role(deps, FAR, card, sub_id=sub_id)

    assert result == EnsureResult(Ensured.REFUSED, reason="missing_subscription")
    assert writes(state) == []
    assert state.requests_to(FAR_ROLES_PATH) == []


async def test_subscription_removed_during_the_role_lookup_is_refused_and_stores_nothing(fake, deps, db) -> None:
    state, _ = fake
    add_far_community(state)
    card = await make_card(db, "alpha")
    sub_id = await make_sub(db, card)
    # Hold the roles GET after the up-front subscription check has passed, then remove the row while it is in flight.
    gate_key = f"GET {FAR_ROLES_PATH}"
    state.gates[gate_key] = asyncio.Event()
    step = asyncio.create_task(ensure_follow_role(deps, FAR, card, sub_id=sub_id))
    await wait_until(lambda: gate_key in state.gated)
    assert await db.run(lambda c: subscriptions.remove_by_id(c, sub_id))
    state.gates[gate_key].set()

    result = await asyncio.wait_for(step, timeout=5)

    assert result == EnsureResult(Ensured.REFUSED, reason="missing_subscription")
    assert await sub_role(db, sub_id) is None
    assert await stored(db, card) is None


async def test_sub_create_403_is_failed_and_stores_nothing(fake, deps, db) -> None:
    state, _ = fake
    add_far_community(state)
    state.overrides[f"POST {FAR_ROLES_PATH}"] = Override(403, b'{"code":"MISSING_PERMISSIONS","message":"no"}')
    card = await make_card(db, "alpha")
    sub_id = await make_sub(db, card)

    result = await ensure_follow_role(deps, FAR, card, sub_id=sub_id)

    assert result == EnsureResult(Ensured.FAILED, reason="create_failed")
    assert await sub_role(db, sub_id) is None
    assert requests_of(state, "PATCH", FAR_NEW_ROLE_PATH) == []


# A subscription's own follow role ----------------------------------------------------------------------

SUB_ROLE_PATH = f"/api/v1/guilds/{FAR}/members/{USER}/roles/7001"


async def test_sub_toggle_adds_the_subscription_role_without_the_home_role(fake, deps, db) -> None:
    state, _ = fake
    add_far_community(state)
    state.roles[FAR].append({"id": "7001", "name": "card_subscription_alpha", "permissions": "0", "position": 5,
                             "mentionable": True})
    card = await make_card(db)   # no home role at all
    sub_id = await make_sub(db, card)
    await db.run(lambda c: cards.set_subscription_role(c, sub_id, "7001"))

    result = await toggle_follow(deps, FAR, USER, card, member_role_ids=frozenset(), sub_id=sub_id)

    assert result == ToggleResult(Toggle.ADDED)
    assert writes(state) == [("PUT", SUB_ROLE_PATH)]


async def test_sub_toggle_with_a_role_stored_in_another_community_is_refused(fake, deps, db) -> None:
    state, _ = fake
    add_far_community(state)
    card = await make_card(db)
    sub_id = await make_sub(db, card)
    await db.run(lambda c: cards.set_subscription_role(c, sub_id, "7001"))

    result = await toggle_follow(deps, GUILD, USER, card, member_role_ids=frozenset(), sub_id=sub_id)

    assert result == ToggleResult(Toggle.UNAVAILABLE, "not_allowlisted")
    assert writes(state) == []
