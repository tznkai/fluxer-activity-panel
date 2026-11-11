# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Follow roles through the card commands: publish creates, adopts or reuses the card's role, `!card set` and `!card clear`
handle `follow_role`, and the bell is seeded on posted messages once the role is stored."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.roles import RoleCache
from fluxer_panel import cards, subscriptions, text
from fluxer_panel.cards import FollowRole
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import FOLLOW_EMOJI
from fluxer_panel.seeding import Seeder
from tests.fakes import FakeFluxerState, Override, make_rest_app
from tests.test_card_commands import World, build, make_mc

GAMES_CHANNEL = "2100"
GAMES_GUILD = "1001"
OTHER_GUILD = "1002"
BOT = "1900"
BOT_ROLE = "5000"
NEW_ROLE = "7001"        # the id the fake gives the next role it creates
ROLES = f"/api/v1/guilds/{GAMES_GUILD}/roles"
POSTED = "5101"          # the card's own message in the games channel
HOME_COPY = ("2003", "5203")       # a copy in the games community
OTHER_COPY = ("2004", "5204")      # a copy in another community
FORBIDDEN = b'{"code": "X", "message": "no"}'


def role(rid: str, name: str, *, permissions: str = "0", position: int = 5, mentionable: bool = True) -> dict[str, Any]:
    return {"id": rid, "name": name, "permissions": permissions, "position": position, "mentionable": mentionable}


def base_roles() -> list[dict[str, Any]]:
    everyone = {"id": GAMES_GUILD, "name": "@everyone", "permissions": "0", "position": 0, "mentionable": False}
    return [everyone, role(BOT_ROLE, "bot", permissions="8", position=10, mentionable=False)]


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
    state.guilds[GAMES_GUILD] = {"id": GAMES_GUILD}
    state.channels[GAMES_CHANNEL] = {"id": GAMES_CHANNEL, "type": 0, "guild_id": GAMES_GUILD, "name": "games"}
    state.roles = {GAMES_GUILD: base_roles()}
    state.members = {(GAMES_GUILD, BOT): {"roles": [BOT_ROLE]}}
    state.next_role_id = int(NEW_ROLE)
    server = await aiohttp_server(make_rest_app(state))
    return state, str(server.make_url("/")).rstrip("/")


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


async def make_world(db: Database, fake: tuple[FakeFluxerState, str], session: aiohttp.ClientSession, *,
                     games: bool = True) -> World:
    """A world with following wired (roles, identity, games community, seeder), as the poller's context has it."""
    state, base = fake
    env = {"GAMES_CHANNEL_ID": GAMES_CHANNEL} if games else {}
    world = await build(db, state, base, session, **env)
    rest = world.rest
    world.ctx.roles = RoleCache(rest)
    world.ctx.identity = BotIdentity(user_id=BOT)
    world.ctx.games_guild = GamesGuild(rest, world.config)
    world.ctx.seeder = Seeder(rest=rest, pacer=ChannelPacer(), db=db)
    return world


@pytest.fixture
async def world(db, fake, session) -> World:
    return await make_world(db, fake, session)


def add_role(world: World, *roles: dict[str, Any]) -> None:
    world.state.roles[GAMES_GUILD].extend(roles)


def role_writes(world: World) -> list[tuple[str, str]]:
    """Every role call that changes something or creates one (the reads are left out)."""
    return [(r.method, r.path) for r in world.state.requests if "/roles" in r.path and r.method != "GET"]


def role_requests(world: World) -> list[Any]:
    return [r for r in world.state.requests if "/roles" in r.path]


def bell_puts(world: World, channel_id: str, message_id: str) -> list[Any]:
    prefix = f"/api/v1/channels/{channel_id}/messages/{message_id}/reactions/"
    return [r for r in world.state.requests if r.method == "PUT" and r.path.startswith(prefix)
            and FOLLOW_EMOJI in r.path]


async def posted_draft(world: World) -> None:
    """A draft card whose message is already in the games channel, as the poller leaves it before a role exists."""
    await make_mc(world)
    card = await world.card("mc")
    assert card is not None
    world.state.messages[(GAMES_CHANNEL, POSTED)] = {"content": "posted"}
    await world.db.run(lambda c: cards.set_message(c, card.id, GAMES_CHANNEL, POSTED, None))


async def make_live(world: World, slug: str = "mc") -> cards.Card:
    card = await world.card(slug)
    assert card is not None
    await world.db.run(lambda c: cards.set_state(c, card.id, "live", 1_800_000_000))
    return card


async def add_copies(world: World, card_id: int) -> None:
    """One copy in the games community and one in another community, each with a message in the fake."""
    for guild, (channel, message) in (
        (GAMES_GUILD, HOME_COPY),
        (OTHER_GUILD, OTHER_COPY),
    ):
        world.state.messages[(channel, message)] = {"content": "copy"}

        def store(c: Any, guild: str = guild, channel: str = channel, message: str = message) -> None:
            subscriptions.add(c, card_id, guild, channel)
            subscriptions.set_message(c, card_id, channel, message, "0" * 64)

        await world.db.run(store)


# AC3.1 and AC3.2 through publish -----------------------------------------------------------------------

async def test_publish_creates_a_mentionable_follow_role_and_says_following_is_on(world: World) -> None:
    await make_mc(world)
    await world.run("!card publish mc")

    assert role_writes(world) == [("POST", ROLES), ("PATCH", f"{ROLES}/{NEW_ROLE}")]
    created = next(r for r in world.state.requests if r.method == "POST" and r.path == ROLES)
    assert (created.payload["name"], created.payload["permissions"]) == ("card_subscription_mc", "0")
    patch = next(r for r in world.state.requests if r.method == "PATCH")
    assert patch.payload == {"mentionable": True}
    card = await world.card("mc")
    assert card is not None
    assert card.follow_role == FollowRole(GAMES_GUILD, NEW_ROLE)
    assert world.last_reply() == text.CARD_PUBLISHED + text.FOLLOW_ON


async def test_publish_adopts_a_matching_role_below_the_bot_and_makes_it_mentionable(world: World) -> None:
    add_role(world, role("6000", "card_subscription_mc", mentionable=False))
    await make_mc(world)
    await world.run("!card publish mc")

    assert role_writes(world) == [("PATCH", f"{ROLES}/6000")]
    card = await world.card("mc")
    assert card is not None and card.follow_role == FollowRole(GAMES_GUILD, "6000")
    assert world.last_reply() == text.CARD_PUBLISHED + text.FOLLOW_ON


async def test_publish_reuses_the_stored_role_without_any_write(world: World) -> None:
    add_role(world, role("6000", "card_subscription_mc"))
    await make_mc(world)
    card = await world.card("mc")
    assert card is not None
    await world.db.run(lambda c: cards.set_follow_role(c, card.id, GAMES_GUILD, "6000"))
    await world.run("!card publish mc")

    assert role_writes(world) == []
    assert world.last_reply() == text.CARD_PUBLISHED + text.FOLLOW_ON


# AC3.3 and AC3.4: publish still publishes, and says following is off ----------------------------------

async def test_publish_refuses_a_matching_role_with_permissions_and_still_publishes(world: World) -> None:
    add_role(world, role("6000", "card_subscription_mc", permissions="8"))
    await make_mc(world)
    await world.run("!card publish mc")

    assert role_writes(world) == []
    card = await world.card("mc")
    assert card is not None
    assert (card.state, card.follow_role) == ("live", None)
    assert world.last_reply() == text.CARD_PUBLISHED + text.FOLLOW_OFF


async def test_publish_with_a_forbidden_role_create_still_publishes_and_seeds_no_bell(world: World) -> None:
    await posted_draft(world)
    world.state.overrides[f"POST {ROLES}"] = Override(403, FORBIDDEN)
    await world.run("!card publish mc")

    card = await world.card("mc")
    assert card is not None
    assert (card.state, card.follow_role) == ("live", None)
    assert world.last_reply() == text.CARD_PUBLISHED + text.FOLLOW_OFF
    assert bell_puts(world, GAMES_CHANNEL, POSTED) == []


async def test_publish_with_a_forbidden_mentionable_call_stores_nothing_and_seeds_no_bell(world: World) -> None:
    await posted_draft(world)
    world.state.overrides[f"PATCH {ROLES}/{NEW_ROLE}"] = Override(403, FORBIDDEN)
    await world.run("!card publish mc")

    card = await world.card("mc")
    assert card is not None
    assert (card.state, card.follow_role) == ("live", None)
    assert world.last_reply() == text.CARD_PUBLISHED + text.FOLLOW_OFF
    assert bell_puts(world, GAMES_CHANNEL, POSTED) == []


async def test_publish_without_a_games_channel_makes_no_role_call(db, fake, session) -> None:
    world = await make_world(db, fake, session, games=False)
    await make_mc(world)
    await world.run("!card publish mc")

    assert role_requests(world) == []
    assert world.last_reply() == text.PUBLISHED_NO_CHANNEL


# C2: the bell is seeded once the role is stored ---------------------------------------------------------

async def test_publish_seeds_the_bell_on_the_card_and_on_every_copy(world: World) -> None:
    await posted_draft(world)
    card = await world.card("mc")
    assert card is not None
    await add_copies(world, card.id)
    await world.run("!card publish mc")

    assert len(bell_puts(world, GAMES_CHANNEL, POSTED)) == 1
    assert len(bell_puts(world, *HOME_COPY)) == 1
    assert len(bell_puts(world, *OTHER_COPY)) == 1


# Publishing a card that is already live gives it the follow role step too ------------------------------

async def test_publish_on_a_live_card_with_no_role_creates_one_and_says_following_is_on(world: World) -> None:
    await posted_draft(world)
    await make_live(world)
    card = await world.card("mc")
    assert card is not None
    await add_copies(world, card.id)
    world.kicks.clear()
    await world.run("!card publish mc")

    assert role_writes(world) == [("POST", ROLES), ("PATCH", f"{ROLES}/{NEW_ROLE}")]
    created = next(r for r in world.state.requests if r.method == "POST" and r.path == ROLES)
    assert (created.payload["name"], created.payload["permissions"]) == ("card_subscription_mc", "0")
    patch = next(r for r in world.state.requests if r.method == "PATCH")
    assert patch.payload == {"mentionable": True}
    card = await world.card("mc")
    assert card is not None
    assert (card.state, card.follow_role) == ("live", FollowRole(GAMES_GUILD, NEW_ROLE))
    assert world.last_reply() == text.ALREADY_LIVE + text.FOLLOW_ON
    assert len(bell_puts(world, GAMES_CHANNEL, POSTED)) == 1
    assert len(bell_puts(world, *HOME_COPY)) == 1
    assert len(bell_puts(world, *OTHER_COPY)) == 1
    assert world.kicks == []


async def test_publish_on_a_live_card_reuses_the_stored_role_without_a_write(world: World) -> None:
    add_role(world, role("6000", "card_subscription_mc"))
    await make_mc(world)
    await make_live(world)
    card = await world.card("mc")
    assert card is not None
    await world.db.run(lambda c: cards.set_follow_role(c, card.id, GAMES_GUILD, "6000"))
    await world.run("!card publish mc")

    assert role_writes(world) == []
    assert world.last_reply() == text.ALREADY_LIVE + text.FOLLOW_ON


async def test_publish_on_a_live_card_with_a_forbidden_role_create_says_following_is_off(world: World) -> None:
    await posted_draft(world)
    await make_live(world)
    world.state.overrides[f"POST {ROLES}"] = Override(403, FORBIDDEN)
    await world.run("!card publish mc")

    card = await world.card("mc")
    assert card is not None
    assert (card.state, card.follow_role) == ("live", None)
    assert world.last_reply() == text.ALREADY_LIVE + text.FOLLOW_OFF
    assert bell_puts(world, GAMES_CHANNEL, POSTED) == []


async def test_publish_on_a_live_card_without_a_games_channel_makes_no_role_call(db, fake, session) -> None:
    world = await make_world(db, fake, session, games=False)
    await make_mc(world)
    await make_live(world)
    world.kicks.clear()
    await world.run("!card publish mc")

    assert role_requests(world) == []
    assert world.last_reply() == text.ALREADY_LIVE
    assert world.kicks == []


# AC3.5 through !card set --------------------------------------------------------------------------------

async def test_set_follow_role_stores_a_mentionable_zero_permission_role_below_the_bot(world: World) -> None:
    add_role(world, role("6000", "card_subscription_mc"))
    await make_mc(world)
    await world.run("!card set mc follow_role 6000")

    card = await world.card("mc")
    assert card is not None and card.follow_role == FollowRole(GAMES_GUILD, "6000")
    assert world.last_reply() == text.FOLLOW_ROLE_SET.format(slug="mc")
    assert role_writes(world) == []


REFUSALS = [
    ("missing", None),
    ("everyone", GAMES_GUILD),
    ("role_permissions", role("6000", "x", permissions="8")),
    ("not_mentionable", role("6000", "x", mentionable=False)),
    ("hierarchy", role("6000", "x", position=11)),
    ("hierarchy", role("4000", "x", position=10)),   # the tie rule: the lower id ranks higher
]


@pytest.mark.parametrize(("reason", "candidate"), REFUSALS)
async def test_set_follow_role_refuses_and_stores_nothing(world: World, reason: str, candidate: Any) -> None:
    if isinstance(candidate, dict):
        add_role(world, candidate)
        role_id = candidate["id"]
    else:
        role_id = candidate or "9999"
    await make_mc(world)
    await world.run(f"!card set mc follow_role {role_id}")

    card = await world.card("mc")
    assert card is not None and card.follow_role is None
    assert world.last_reply() == text.FOLLOW_REFUSED[reason]
    assert role_writes(world) == []


async def test_set_follow_role_refuses_another_cards_follow_role(world: World) -> None:
    add_role(world, role("6000", "card_subscription_mc"))
    await make_mc(world)
    await world.run("!card new other Other")
    other = await world.card("other")
    assert other is not None
    await world.db.run(lambda c: cards.set_follow_role(c, other.id, GAMES_GUILD, "6000"))
    await world.run("!card set mc follow_role 6000")

    card = await world.card("mc")
    assert card is not None and card.follow_role is None
    assert world.last_reply() == text.FOLLOW_REFUSED["taken"]


async def test_set_follow_role_on_a_posted_card_seeds_the_bell_on_every_surface(world: World) -> None:
    add_role(world, role("6000", "card_subscription_mc"))
    await posted_draft(world)
    card = await make_live(world)
    await add_copies(world, card.id)
    await world.run("!card set mc follow_role 6000")

    assert len(bell_puts(world, GAMES_CHANNEL, POSTED)) == 1
    assert len(bell_puts(world, *HOME_COPY)) == 1
    assert len(bell_puts(world, *OTHER_COPY)) == 1


async def test_set_follow_role_without_a_games_channel_says_so_and_stores_nothing(db, fake, session) -> None:
    world = await make_world(db, fake, session, games=False)
    add_role(world, role("6000", "card_subscription_mc"))
    await make_mc(world)
    await world.run("!card set mc follow_role 6000")

    card = await world.card("mc")
    assert card is not None and card.follow_role is None
    assert world.last_reply() == text.FOLLOW_NO_GAMES
    assert role_requests(world) == []


# AC3.10: clear forgets the id, archive keeps it, nothing deletes a role -----------------------------

async def test_clear_follow_role_forgets_the_id_and_names_the_role(world: World) -> None:
    add_role(world, role("6000", "card_subscription_mc"))
    await make_mc(world)
    card = await world.card("mc")
    assert card is not None
    await world.db.run(lambda c: cards.set_follow_role(c, card.id, GAMES_GUILD, "6000"))
    await world.run("!card clear mc follow_role")

    card = await world.card("mc")
    assert card is not None and card.follow_role is None
    assert world.last_reply() == text.FOLLOW_ROLE_CLEARED.format(slug="mc", role="<@&6000>")
    assert [r for r in world.state.requests if r.method == "DELETE"] == []


async def test_clear_follow_role_when_none_is_stored_says_so(world: World) -> None:
    await make_mc(world)
    await world.run("!card clear mc follow_role")
    assert world.last_reply() == text.FOLLOW_ROLE_NONE.format(slug="mc")


async def test_archive_keeps_the_follow_role_and_deletes_no_role(world: World) -> None:
    add_role(world, role("6000", "card_subscription_mc"))
    await posted_draft(world)
    card = await world.card("mc")
    assert card is not None
    await world.db.run(lambda c: cards.set_follow_role(c, card.id, GAMES_GUILD, "6000"))
    await world.run("!card publish mc")
    await world.run("!card archive mc")

    card = await world.card("mc")
    assert card is not None
    assert card.follow_role == FollowRole(GAMES_GUILD, "6000")
    assert [r for r in world.state.requests if r.method == "DELETE" and "/roles" in r.path] == []


# Concurrent role steps on one card: the publish and the manual set share one per-card role lock ---------

async def test_concurrent_publishes_of_a_live_card_with_no_role_create_one_role(world: World) -> None:
    await posted_draft(world)
    await make_live(world)
    await asyncio.gather(world.run("!card publish mc"), world.run("!card publish mc"))

    assert role_writes(world) == [("POST", ROLES), ("PATCH", f"{ROLES}/{NEW_ROLE}")]
    card = await world.card("mc")
    assert card is not None
    assert (card.state, card.follow_role) == ("live", FollowRole(GAMES_GUILD, NEW_ROLE))


async def test_concurrent_publishes_of_a_draft_create_one_role(world: World) -> None:
    await make_mc(world)
    await asyncio.gather(world.run("!card publish mc"), world.run("!card publish mc"))

    assert role_writes(world) == [("POST", ROLES), ("PATCH", f"{ROLES}/{NEW_ROLE}")]
    card = await world.card("mc")
    assert card is not None
    assert (card.state, card.follow_role) == ("live", FollowRole(GAMES_GUILD, NEW_ROLE))


async def test_a_clear_waits_for_a_role_step_already_running_on_the_card(world: World) -> None:
    add_role(world, role("6000", "manual_role"))
    await make_mc(world)
    await world.run("!card set mc follow_role 6000")
    card = await world.card("mc")
    assert card is not None and card.follow_role == FollowRole(GAMES_GUILD, "6000")
    lock = world.ctx.follow_locks.get(card.id)
    await lock.acquire()   # a publish's role step is running on this card
    try:
        task = asyncio.ensure_future(world.run("!card clear mc follow_role"))
        done, _ = await asyncio.wait({task}, timeout=0.5)
        assert done == set()   # the clear does not run while the role step holds the card's role lock
        card = await world.card("mc")
        assert card is not None and card.follow_role == FollowRole(GAMES_GUILD, "6000")
    finally:
        lock.release()
    await task
    card = await world.card("mc")
    assert card is not None and card.follow_role is None


async def test_a_manual_set_waits_for_a_role_step_already_running_on_the_card(world: World) -> None:
    add_role(world, role("6000", "manual_role"))
    await make_mc(world)
    card = await world.card("mc")
    assert card is not None
    lock = world.ctx.follow_locks.get(card.id)
    await lock.acquire()   # a publish's role step is running on this card
    try:
        task = asyncio.ensure_future(world.run("!card set mc follow_role 6000"))
        done, _ = await asyncio.wait({task}, timeout=0.5)
        assert done == set()   # the set does not store while the role step holds the card's role lock
        card = await world.card("mc")
        assert card is not None and card.follow_role is None
    finally:
        lock.release()
    await task
    card = await world.card("mc")
    assert card is not None
    assert card.follow_role == FollowRole(GAMES_GUILD, "6000")
