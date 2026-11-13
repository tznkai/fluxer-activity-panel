# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The poll flow: starting a poll from 🗳️ (AC2) and, from Task 3 on, voting by typed reply (AC3, AC4). A real PollFlow,
PollPost and Interactions run against the fake REST server and a SQLite file. The clock moves only when a test moves
it, and the edits are debounced with no delay. The emoji are written as escapes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.roles import RoleCache
from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, polls, render, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poll_flow import PollFlow
from fluxer_panel.poll_post import PollPost
from fluxer_panel.presses import Action, Press, Surface
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_flow import offered_slot_times
from tests.fakes import FakeFluxerState, Override, make_rest_app, wait_until

TOKEN = "SENTINEL-TOKEN-poll-flow"
BOT = "1900"
ADMIN = "1001"
STARTER = "6001"
HOLDER_A = "6002"
HOLDER_B = "6003"
GUILD = "2001"           # the games community
OTHER_GUILD = "2002"
GAMES = "3100"           # the games channel, in GUILD
PLAY = "3200"            # PLAY_NOW_CHANNEL_ID: the card's session channel, in GUILD
FAR = "3400"             # a far copy's channel, in OTHER_GUILD
CARD_MSG = "5101"
ROLE = "7001"
HASH = "a" * 64
NOW = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc).timestamp()   # a Wednesday, 08:00 UTC
POLL_POSTS = f"/api/v1/channels/{PLAY}/messages"
MEMBERS = f"/api/v1/guilds/{GUILD}/members"
MEMBERS_GET = f"GET {MEMBERS}"
FIRST_POLL_MESSAGE = "5001"   # the fake's next message id when the flow posts the poll message


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@dataclass
class World:
    db: Database
    state: FakeFluxerState
    rest: FluxerREST
    config: Config
    clock: Clock
    post: PollPost
    flow: PollFlow
    interactions: Interactions
    card_id: int


def config_for(**extra: str) -> Config:
    env = {"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
           "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES, "PLAY_NOW_CHANNEL_ID": PLAY,
           "POLL_INTERVAL_SECONDS": "3600", **extra}
    return Config.from_env(env)


def seed_card(conn: Any, *, follow: bool) -> int:
    card_id = cards.create_card(conn, slug="mc", title="Lobby", author_id=ADMIN, now=1)
    cards.set_state(conn, card_id, "live", 1)
    cards.set_message(conn, card_id, GAMES, CARD_MSG, HASH)
    if follow:
        cards.set_follow_role(conn, card_id, GUILD, ROLE)
    return card_id


@pytest.fixture
async def build(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[Callable[..., Awaitable[World]]]:
    opened: list[Database] = []
    async with aiohttp.ClientSession() as client:

        async def make(*, follow: bool = True, **env: str) -> World:
            state = FakeFluxerState(bot_user_id=BOT)
            state.guilds.update({GUILD: {"id": GUILD}, OTHER_GUILD: {"id": OTHER_GUILD}})
            state.channels.update({
                GAMES: {"id": GAMES, "guild_id": GUILD, "type": 0},
                PLAY: {"id": PLAY, "guild_id": GUILD, "type": 0},
                FAR: {"id": FAR, "guild_id": OTHER_GUILD, "type": 0},
            })
            server = await aiohttp_server(make_rest_app(state))
            base = str(server.make_url("/")).rstrip("/")
            rest = FluxerREST(client, f"{base}/api", TOKEN)
            database = Database.open(tmp_path / f"poll{len(opened)}.db")
            opened.append(database)
            config = config_for(**env)
            clock = Clock()
            pacer = ChannelPacer(max_calls=1000)
            identity = BotIdentity(user_id=BOT)
            games_guild = GamesGuild(rest, config)
            locks = CardLocks()
            backoff = DmBackoff(config.dm_backoff_seconds, clock=clock)
            post = PollPost(db=database, rest=rest, config=config, pacer=pacer, identity=identity, backoff=backoff,
                            edit_delay=0)
            flow = PollFlow(db=database, rest=rest, config=config, locks=locks, poll_post=post, backoff=backoff,
                            games_guild=games_guild, clock=clock)
            interactions = Interactions(db=database, rest=rest, config=config, locks=locks, games_guild=games_guild,
                                        seeder=Seeder(rest=rest, pacer=pacer, db=database), pacer=pacer,
                                        identity=identity, roles=RoleCache(rest), clock=clock, backoff=backoff,
                                        poll_flow=flow)
            card_id = await database.run(lambda c: seed_card(c, follow=follow))
            return World(db=database, state=state, rest=rest, config=config, clock=clock, post=post, flow=flow,
                         interactions=interactions, card_id=card_id)

        yield make
    for database in opened:
        database.close()


def press(card_id: int, user: str = STARTER, *, home: bool = True) -> Press:
    surface = (Surface(kind="card", channel_id=GAMES, message_id=CARD_MSG, guild_id=GUILD, home_community=True)
               if home else
               Surface(kind="copy", channel_id=FAR, message_id="5202", guild_id=OTHER_GUILD, home_community=False))
    return Press(card_id=card_id, action=Action.POLL, user_id=user, member_role_ids=None, surface=surface)


def members(state: FakeFluxerState, *user_ids: str) -> None:
    """Members of the games community who hold the follow role."""
    for uid in user_ids:
        state.members[(GUILD, uid)] = {"user": {"id": uid}, "roles": [ROLE]}


def posts_to(state: FakeFluxerState, path: str) -> list[Any]:
    return [r for r in state.requests if r.method == "POST" and r.path == path]


def dm_contents(state: FakeFluxerState, user_id: str) -> list[str]:
    """The text of every DM message the bot sent to one user, in order."""
    channel = state.dm_channels.get(user_id)
    if channel is None:
        return []
    path = f"/api/v1/channels/{channel}/messages"
    return [r.payload["content"] for r in state.requests if r.method == "POST" and r.path == path]


def ballot_contents(state: FakeFluxerState, user_id: str) -> list[str]:
    return [content for content in dm_contents(state, user_id) if content.startswith(text.POLL_BALLOT_HEAD.split("{")[0])]


async def open_poll(world: World) -> polls.Poll | None:
    return await world.db.run(lambda c: polls.open_for_card(c, world.card_id))


async def ballot_users(world: World, poll_id: int) -> set[str]:
    return {ballot.user_id for ballot in await world.db.run(lambda c: polls.ballots(c, poll_id))}


def link_to(world: World, channel: str, message: str) -> str:
    return render.jump_link(text.POLL_LINK_LABEL, world.config.instance_url, GUILD, channel, message)


def bot_named_fields(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [getattr(r, FIELDS_ATTR, {}) for r in caplog.records if r.name.startswith("fluxer_panel")]


# AC2.2: one message, one row, one ballot per recipient ----------------------------------------------------

async def test_a_press_posts_one_poll_message_and_dms_each_holder_and_the_starter_once(build) -> None:
    world = await build()
    members(world.state, STARTER, HOLDER_A, HOLDER_B)

    await world.flow.start(press(world.card_id))
    await world.post.drain()

    assert len(posts_to(world.state, POLL_POSTS)) == 1
    poll = await open_poll(world)
    assert poll is not None
    assert await ballot_users(world, poll.id) == {STARTER, HOLDER_A, HOLDER_B}
    for user in (STARTER, HOLDER_A, HOLDER_B):
        assert len(ballot_contents(world.state, user)) == 1


async def test_the_starter_gets_one_ballot_even_when_they_hold_the_role(build) -> None:
    world = await build()
    members(world.state, STARTER, HOLDER_A)

    await world.flow.start(press(world.card_id))
    await world.post.drain()

    poll = await open_poll(world)
    assert poll is not None
    assert len(await world.db.run(lambda c: polls.ballots(c, poll.id))) == 2
    assert len(ballot_contents(world.state, STARTER)) == 1


async def test_after_the_drain_the_poll_message_is_edited_to_its_stored_form(build) -> None:
    world = await build()
    members(world.state, STARTER, HOLDER_A)

    await world.flow.start(press(world.card_id))
    await world.post.drain()

    poll = await open_poll(world)
    assert poll is not None and poll.message_id == FIRST_POLL_MESSAGE
    stored = world.state.messages[(PLAY, poll.message_id)]
    assert "footer" in stored["embeds"][0]
    assert f"Poll {poll.id}" in stored["embeds"][0]["footer"]["text"]


# AC2.3: the options are the 📅 slots at this moment, soonest first, lettered from a ---------------------------

async def test_the_options_are_the_offered_slots_in_soonest_order_lettered_from_a(build) -> None:
    world = await build()
    members(world.state, STARTER)

    await world.flow.start(press(world.card_id))
    await world.post.drain()

    poll = await open_poll(world)
    assert poll is not None
    expected = offered_slot_times(world.config, NOW)
    stored = await world.db.run(lambda c: polls.options(c, poll.id))
    assert [o.starts_at for o in stored] == expected
    assert [o.position for o in stored] == list(range(len(expected)))
    assert all(o.origin == "offered" for o in stored)
    [ballot] = ballot_contents(world.state, STARTER)
    assert f"a. <t:{expected[0]}:F>" in ballot
    assert f"b. <t:{expected[1]}:F>" in ballot


# AC2.5: more holders than the cap -----------------------------------------------------------------------

async def test_the_cap_polls_the_lowest_ids_and_the_starter_is_told_how_many_were_left_out(build) -> None:
    world = await build(POLL_MAX_RECIPIENTS="3")
    holders = ["6002", "6003", "6004", "6005", "6006"]
    members(world.state, *holders)

    await world.flow.start(press(world.card_id))
    await world.post.drain()

    poll = await open_poll(world)
    assert poll is not None
    assert await ballot_users(world, poll.id) == {STARTER, "6002", "6003"}
    assert text.POLL_LEFT_OUT.format(n=3) in dm_contents(world.state, STARTER)


# AC2.6: no follow role -----------------------------------------------------------------------------------

async def test_a_card_with_no_follow_role_polls_only_the_starter_and_says_so(build) -> None:
    world = await build(follow=False)

    await world.flow.start(press(world.card_id))
    await world.post.drain()

    poll = await open_poll(world)
    assert poll is not None
    assert await ballot_users(world, poll.id) == {STARTER}
    assert [r for r in world.state.requests if r.method == "GET" and r.path == MEMBERS] == []
    assert text.POLL_NO_ROLE in dm_contents(world.state, STARTER)


# AC2.7: a second press while a poll is open ----------------------------------------------------------------

async def test_a_second_press_while_a_poll_is_open_posts_nothing_and_links_the_open_poll(build) -> None:
    world = await build()
    members(world.state, STARTER, HOLDER_A)
    await world.flow.start(press(world.card_id))
    await world.post.drain()
    poll = await open_poll(world)
    assert poll is not None

    await world.flow.start(press(world.card_id, user=HOLDER_A))
    await world.post.drain()

    assert len(posts_to(world.state, POLL_POSTS)) == 1
    assert len(await world.db.run(lambda c: polls.ballots(c, poll.id))) == 2
    assert text.POLL_ALREADY_OPEN.format(link=link_to(world, PLAY, poll.message_id)) in dm_contents(world.state,
                                                                                                    HOLDER_A)


# AC2.8: a failed member listing -------------------------------------------------------------------------

async def test_a_failed_member_listing_posts_nothing_stores_nothing_and_tells_the_starter_to_try_later(
        build, caplog: pytest.LogCaptureFixture) -> None:
    world = await build()
    caplog.set_level(logging.DEBUG)
    members(world.state, STARTER, HOLDER_A)
    world.state.overrides[MEMBERS_GET] = Override(500, b'{"code":"INTERNAL","message":"x"}')

    await world.flow.start(press(world.card_id))
    await world.post.drain()

    assert posts_to(world.state, POLL_POSTS) == []
    assert await open_poll(world) is None
    assert dm_contents(world.state, STARTER) == [text.POLL_TRY_LATER]
    assert all(STARTER not in fields.values() for fields in bot_named_fields(caplog))


# AC2.9: a press on a far copy ----------------------------------------------------------------------------

async def test_a_press_on_a_far_copy_dms_the_card_link_and_starts_nothing(build) -> None:
    world = await build()
    members(world.state, STARTER, HOLDER_A)

    await world.flow.start(press(world.card_id, home=False))
    await world.post.drain()

    assert posts_to(world.state, POLL_POSTS) == []
    assert await open_poll(world) is None
    link = render.jump_link(text.JUMP_LABEL_CARD, world.config.instance_url, GUILD, GAMES, CARD_MSG)
    assert dm_contents(world.state, STARTER) == [text.POLL_ELSEWHERE.format(link=link)]


# Routing: the interactions handler ----------------------------------------------------------------------

async def test_a_poll_press_on_a_play_now_post_starts_nothing(build) -> None:
    world = await build()
    members(world.state, STARTER, HOLDER_A)
    surface = Surface(kind="play_now", channel_id=PLAY, message_id="5301", guild_id=GUILD, home_community=True)

    await world.interactions.handle(Press(card_id=world.card_id, action=Action.POLL, user_id=STARTER,
                                          member_role_ids=None, surface=surface))
    await world.post.drain()

    assert world.state.requests == []


async def test_a_poll_press_on_the_card_reaches_the_flow_through_the_handler(build) -> None:
    world = await build()
    members(world.state, STARTER)

    await world.interactions.handle(press(world.card_id))
    await world.post.drain()

    assert len(posts_to(world.state, POLL_POSTS)) == 1


# The race: a poll stored while the provisional message is in flight -----------------------------------

async def test_a_poll_stored_during_the_post_deletes_the_provisional_message_and_links_the_presser(build) -> None:
    world = await build()
    members(world.state, STARTER, HOLDER_A)
    gate_key = f"POST {POLL_POSTS}"
    world.state.gates[gate_key] = asyncio.Event()

    task = asyncio.create_task(world.flow.start(press(world.card_id)))
    await wait_until(lambda: gate_key in world.state.gated, waiting_for="the poll message POST to reach its gate")

    def racing_poll(conn: Any) -> int:
        poll_id = polls.create(conn, card_id=world.card_id, guild_id=GUILD, channel_id=PLAY, message_id="5999",
                               starter_id=ADMIN, kind="approval", parent_poll_id=None, now=int(NOW),
                               closes_at=int(NOW) + 86400, ballot_size=6, options=[(int(NOW) + 7200, "offered")],
                               recipients=[])
        assert poll_id is not None
        return poll_id

    await world.db.run(racing_poll)
    world.state.gates[gate_key].set()
    await asyncio.wait_for(task, timeout=5)
    await world.post.drain()

    assert (PLAY, FIRST_POLL_MESSAGE) in world.state.deleted
    polls_stored = await world.db.run(lambda c: [p.message_id for p in polls.open_polls(c)])
    assert polls_stored == ["5999"]
    assert dm_contents(world.state, STARTER) == [text.POLL_ALREADY_OPEN.format(link=link_to(world, PLAY, "5999"))]
