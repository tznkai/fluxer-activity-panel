# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The poll flow: starting a poll from 🗳️ (AC2) and, from Task 3 on, voting by typed reply (AC3, AC4). A real PollFlow,
PollPost and Interactions run against the fake REST server and a SQLite file. The clock moves only when a test moves
it, and the edits are debounced with no delay. The emoji are written as escapes."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.roles import RoleCache
from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, poll_letters, polls, render, text
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
from fluxer_panel import interaction_settings
from fluxer_panel.presses import POLL_EMOJI, Action, Press, Presses, Surface
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
    locks: CardLocks
    backoff: DmBackoff


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
                         interactions=interactions, card_id=card_id, locks=locks, backoff=backoff)

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


def message_posts(state: FakeFluxerState) -> list[Any]:
    """Every POST that sends a message to a guild channel: not the DM channel creation, nor a DM to a user."""
    dms = set(state.dm_channels.values())
    return [r for r in state.requests
            if r.method == "POST" and r.path.endswith("/messages") and r.path.split("/")[4] not in dms]


def dm_contents(state: FakeFluxerState, user_id: str) -> list[str]:
    """The text of every DM message the bot sent to one user, in order."""
    channel = state.dm_channels.get(user_id)
    if channel is None:
        return []
    path = f"/api/v1/channels/{channel}/messages"
    return [r.payload["content"] for r in state.requests if r.method == "POST" and r.path == path]


BALLOT_PREFIX = text.POLL_BALLOT_HEAD.split("{n}")[0]     # "Time poll ": every ballot opens with it, then the poll id


def ballot_contents(state: FakeFluxerState, user_id: str) -> list[str]:
    return [content for content in dm_contents(state, user_id) if content.startswith(BALLOT_PREFIX)]


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
    assert ballot.startswith(f"Time poll {poll.id} for Lobby.")
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


# The error paths of PollFlow.start ----------------------------------------------------------------------

def failures_logged(caplog: pytest.LogCaptureFixture, name: str) -> list[Any]:
    return [getattr(r, FIELDS_ATTR, {}).get("reason") for r in caplog.records if r.getMessage() == name]


def set_card_channel(conn: Any, card_id: int, channel_id: str) -> None:
    """Gives the card its own verified play-now channel, which the poll posts to instead of PLAY_NOW_CHANNEL_ID."""
    cards.set_play_now_channel(conn, card_id, cards.ChannelLink(guild_id=GUILD, channel_id=channel_id,
                                                                channel_name=None, verified=True))


async def test_a_failed_insert_deletes_the_provisional_message_and_tells_the_presser(build, monkeypatch) -> None:
    world = await build()
    members(world.state, STARTER, HOLDER_A)

    def broken_create(conn: Any, **_: Any) -> int:
        raise sqlite3.IntegrityError("injected by the test")

    monkeypatch.setattr(polls, "create", broken_create)
    await world.flow.start(press(world.card_id))
    await world.post.drain()

    assert len(posts_to(world.state, POLL_POSTS)) == 1
    assert (PLAY, FIRST_POLL_MESSAGE) in world.state.deleted
    assert await open_poll(world) is None
    assert dm_contents(world.state, STARTER) == [text.POLL_POST_FAILED]
    assert ballot_contents(world.state, HOLDER_A) == []


async def test_a_poll_message_with_a_bad_id_is_not_stored_and_nothing_is_deleted(build, caplog) -> None:
    world = await build()
    members(world.state, STARTER, HOLDER_A)
    caplog.set_level(logging.DEBUG)
    world.state.overrides[f"POST {POLL_POSTS}"] = Override(200, b'{"id": "not-a-snowflake"}')

    await world.flow.start(press(world.card_id))
    await world.post.drain()

    assert await open_poll(world) is None
    assert [r for r in world.state.requests if r.method == "DELETE"] == []
    assert dm_contents(world.state, STARTER) == [text.POLL_POST_FAILED]
    assert failures_logged(caplog, "poll_post_failed") == ["bad_message_id"]


async def test_a_poll_post_answering_500_stores_nothing_and_tells_the_presser(build) -> None:
    world = await build()
    members(world.state, STARTER, HOLDER_A)
    world.state.overrides[f"POST {POLL_POSTS}"] = Override(500, b'{"code":"INTERNAL","message":"x"}')

    await world.flow.start(press(world.card_id))
    await world.post.drain()

    assert len(posts_to(world.state, POLL_POSTS)) == 1
    assert await open_poll(world) is None
    assert dm_contents(world.state, STARTER) == [text.POLL_POST_FAILED]
    assert ballot_contents(world.state, HOLDER_A) == []


async def test_a_card_whose_channel_changes_while_the_post_waits_for_the_lock_gets_no_poll(build) -> None:
    world = await build()
    members(world.state, STARTER, HOLDER_A)
    lock = world.locks.get(world.card_id)
    await lock.acquire()
    try:
        task = asyncio.create_task(world.flow.start(press(world.card_id)))
        # The target and the member list are read before the lock. Once the member list is requested, the flow is only
        # left to wait on the lock, and the change below is made under the lock, so the flow reads the card after it.
        await wait_until(lambda: any(r.method == "GET" and r.path == MEMBERS for r in world.state.requests),
                         waiting_for="the member list request")
        await world.db.run(lambda c: set_card_channel(c, world.card_id, "3300"))
    finally:
        lock.release()
    await asyncio.wait_for(task, timeout=5)
    await world.post.drain()

    assert posts_to(world.state, POLL_POSTS) == []
    assert await open_poll(world) is None
    assert dm_contents(world.state, STARTER) == [text.POLL_POST_FAILED]


async def test_a_card_whose_own_channel_is_the_games_channel_gets_no_poll(build) -> None:
    world = await build()
    members(world.state, STARTER, HOLDER_A)
    await world.db.run(lambda c: set_card_channel(c, world.card_id, GAMES))

    await world.flow.start(press(world.card_id))
    await world.post.drain()

    assert message_posts(world.state) == []
    assert await open_poll(world) is None
    assert dm_contents(world.state, STARTER) == [text.POLL_POST_FAILED]


async def test_a_card_with_no_channel_is_told_so_and_nothing_is_posted(build) -> None:
    world = await build(PLAY_NOW_CHANNEL_ID="")
    members(world.state, STARTER, HOLDER_A)

    await world.flow.start(press(world.card_id))
    await world.post.drain()

    assert message_posts(world.state) == []
    assert await open_poll(world) is None
    assert dm_contents(world.state, STARTER) == [text.POLL_NO_CHANNEL]


async def test_a_press_in_backoff_starts_nothing_and_sends_nothing(build) -> None:
    world = await build()
    members(world.state, STARTER, HOLDER_A)
    world.backoff.failed(STARTER)

    await world.flow.start(press(world.card_id))
    await world.post.drain()

    assert world.state.requests == []
    assert await open_poll(world) is None


async def test_a_far_copy_press_without_an_instance_url_says_so_without_a_link(build) -> None:
    world = await build(FLUXER_INSTANCE_URL="", FLUXER_API_URL="https://fluxer.invalid/api")
    members(world.state, STARTER, HOLDER_A)

    await world.flow.start(press(world.card_id, home=False))
    await world.post.drain()

    assert posts_to(world.state, POLL_POSTS) == []
    assert dm_contents(world.state, STARTER) == [text.POLL_ELSEWHERE_NO_LINK]


async def test_a_second_press_without_an_instance_url_says_the_poll_is_open_without_a_link(build) -> None:
    world = await build(FLUXER_INSTANCE_URL="", FLUXER_API_URL="https://fluxer.invalid/api")
    members(world.state, STARTER, HOLDER_A)
    await world.flow.start(press(world.card_id))
    await world.post.drain()

    await world.flow.start(press(world.card_id, user=HOLDER_A))
    await world.post.drain()

    assert len(posts_to(world.state, POLL_POSTS)) == 1
    assert last_reply(world.state, HOLDER_A) == text.POLL_ALREADY_OPEN_NO_LINK


# Voting by typed reply (AC3, AC4) ------------------------------------------------------------------------

def reply_event(world: World, user: str, content: str) -> events.MessageCreate:
    return events.MessageCreate(message_id="7100", channel_id=world.state.dm_channels[user], guild_id=None,
                                author_id=user, author_is_bot=False, member_role_ids=None, content=content)


async def hand_reply(flow: PollFlow, world: World, user: str, content: str) -> None:
    """What the router does with a non-command DM: one open-ballot lookup, then the reply with the polls it found."""
    open_polls = await flow.open_ballots(user)
    if open_polls:
        await flow.on_reply(reply_event(world, user, content), open_polls)


async def reply(world: World, user: str, content: str) -> None:
    await hand_reply(world.flow, world, user, content)
    await world.post.drain()


async def started(world: World, *holders: str) -> polls.Poll:
    members(world.state, STARTER, *holders)
    await world.flow.start(press(world.card_id))
    await world.post.drain()
    poll = await open_poll(world)
    assert poll is not None
    return poll


async def picks_of(world: World, poll_id: int, user: str) -> list[int]:
    return await world.db.run(lambda c: polls.picks(c, poll_id, user))


async def letters_of(world: World, poll_id: int, user: str) -> list[str]:
    options = await world.db.run(lambda c: polls.options(c, poll_id))
    by_id = {o.id: poll_letters.letter(o.position) for o in options}
    return [by_id[option_id] for option_id in await picks_of(world, poll_id, user)]


async def option_count(world: World, poll_id: int) -> int:
    return len(await world.db.run(lambda c: polls.options(c, poll_id)))


def last_reply(state: FakeFluxerState, user: str) -> str:
    return dm_contents(state, user)[-1]


async def ballot_of(world: World, poll_id: int, user: str) -> polls.Ballot | None:
    return await world.db.run(lambda c: polls.ballot(c, poll_id, user))


async def ballot_message_text(world: World, poll_id: int, user: str) -> str:
    ballot = await world.db.run(lambda c: polls.ballot(c, poll_id, user))
    assert ballot is not None and ballot.dm_channel_id is not None and ballot.dm_message_id is not None
    return world.state.messages[(ballot.dm_channel_id, ballot.dm_message_id)]["content"]


def debounced_flow(world: World, sleep: Callable[[float], Awaitable[None]]) -> tuple[PollFlow, PollPost]:
    """A flow whose poll post debounces its edits through `sleep`, for the votes only (the start uses world.flow)."""
    clock = world.clock
    post = PollPost(db=world.db, rest=world.rest, config=world.config, pacer=ChannelPacer(max_calls=1000),
                    identity=BotIdentity(user_id=BOT), backoff=DmBackoff(world.config.dm_backoff_seconds, clock=clock),
                    edit_delay=5, sleep=sleep)
    flow = PollFlow(db=world.db, rest=world.rest, config=world.config, locks=CardLocks(), poll_post=post,
                    backoff=DmBackoff(world.config.dm_backoff_seconds, clock=clock),
                    games_guild=GamesGuild(world.rest, world.config), clock=clock)
    return flow, post


# AC3.1: a reply sets the picks, and the bot says so -----------------------------------------------------

async def test_a_reply_sets_the_picks_to_exactly_the_letters_given_and_says_so(build) -> None:
    world = await build()
    poll = await started(world, HOLDER_A)

    await reply(world, STARTER, "a, c")

    assert await letters_of(world, poll.id, STARTER) == ["a", "c"]
    assert last_reply(world.state, STARTER) == text.POLL_YOUR_PICKS.format(letters="a, c")


# AC3.2: a reply replaces the earlier picks; clear and all -------------------------------------------------

async def test_a_reply_replaces_earlier_picks_clear_removes_them_and_all_picks_the_ballot(build) -> None:
    world = await build(POLL_BALLOT_SIZE="2")
    poll = await started(world, HOLDER_A)
    assert await option_count(world, poll.id) > 2

    await reply(world, STARTER, "a, c")
    await reply(world, STARTER, "b")
    assert await letters_of(world, poll.id, STARTER) == ["b"]

    await reply(world, STARTER, "clear")
    assert await letters_of(world, poll.id, STARTER) == []
    assert last_reply(world.state, STARTER) == text.POLL_NO_PICKS

    await reply(world, STARTER, "all")
    assert await letters_of(world, poll.id, STARTER) == ["a", "b"]
    assert last_reply(world.state, STARTER) == text.POLL_YOUR_PICKS.format(letters="a, b")


# AC3.3: a typed time that matches no option adds one, and every open ballot shows it -----------------------

async def test_a_typed_time_matching_no_option_adds_one_picks_it_and_shows_on_every_open_ballot(build) -> None:
    world = await build(POLL_BALLOT_SIZE="20")   # the new option must fit on the ballot to be shown there
    poll = await started(world, HOLDER_A)
    offered = offered_slot_times(world.config, NOW)
    typed_at = int(datetime(2026, 11, 14, 14, 7, tzinfo=timezone.utc).timestamp())
    assert typed_at not in offered

    await reply(world, STARTER, "a, 2026-11-14 14:07")

    stored = await world.db.run(lambda c: polls.options(c, poll.id))
    assert len(stored) == len(offered) + 1
    added = stored[-1]
    assert (added.starts_at, added.origin, added.position) == (typed_at, "typed", len(offered))
    letter = poll_letters.letter(added.position)
    assert await letters_of(world, poll.id, STARTER) == ["a", letter]
    line = f"{letter}. <t:{typed_at}:F>"
    assert line in await ballot_message_text(world, poll.id, HOLDER_A)
    assert line in json.dumps(world.state.messages[(PLAY, poll.message_id)])


# AC3.4: a typed time equal to an option picks it and adds nothing ----------------------------------------

async def test_a_typed_time_equal_to_an_option_picks_that_option_and_adds_none(build) -> None:
    world = await build()
    poll = await started(world)
    offered = offered_slot_times(world.config, NOW)
    same = datetime.fromtimestamp(offered[1], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

    await reply(world, STARTER, same)

    assert await letters_of(world, poll.id, STARTER) == ["b"]
    assert await option_count(world, poll.id) == len(offered)


# AC3.5: a letter past the ballot is still a valid pick --------------------------------------------------

async def test_a_letter_not_shown_on_the_ballot_picks_that_option(build) -> None:
    world = await build(POLL_BALLOT_SIZE="2")
    poll = await started(world, HOLDER_A)
    offered = offered_slot_times(world.config, NOW)
    assert len(offered) == 6
    last = poll_letters.letter(len(offered) - 1)
    assert last == "f"

    await reply(world, STARTER, last)

    assert await letters_of(world, poll.id, STARTER) == ["f"]
    assert last_reply(world.state, STARTER) == text.POLL_YOUR_PICKS.format(letters="f")
    assert f"f. <t:{offered[5]}:F>" not in await ballot_message_text(world, poll.id, STARTER)


# AC3.3: a full ballot counts the new option in its "+K more" line -----------------------------------------

async def test_a_typed_time_on_a_full_ballot_raises_its_more_count_on_every_open_ballot(build) -> None:
    world = await build(POLL_BALLOT_SIZE="2")   # the ballot shows two options, so the rest are counted
    poll = await started(world, HOLDER_A)
    offered = offered_slot_times(world.config, NOW)
    typed_at = int(datetime(2026, 11, 14, 14, 7, tzinfo=timezone.utc).timestamp())
    assert typed_at not in offered
    link = link_to(world, PLAY, poll.message_id)
    before = await ballot_message_text(world, poll.id, HOLDER_A)
    assert text.POLL_BALLOT_MORE.format(k=len(offered) - 2, link=link) in before

    await reply(world, STARTER, "a, 2026-11-14 14:07")

    after = await ballot_message_text(world, poll.id, HOLDER_A)
    assert text.POLL_BALLOT_MORE.format(k=len(offered) - 1, link=link) in after
    assert after.count("more options in the poll message") == 1
    assert f"{poll_letters.letter(len(offered))}. <t:{typed_at}:F>" not in after


# AC3.6: an unreadable item changes nothing and names its position -----------------------------------------

@pytest.mark.parametrize("bad, reason", [
    ("a, sat 2pm", text.SESSION_TIME_REFUSED["format"]),
    ("a, zz", text.POLL_REPLY_REFUSED["no_option"]),
    ("a, today 08:30", text.POLL_REPLY_REFUSED["too_soon"]),
])
async def test_an_unreadable_item_changes_nothing_and_is_refused_by_position(build, bad: str, reason: str) -> None:
    world = await build()
    poll = await started(world, HOLDER_A)
    await reply(world, STARTER, "a")
    before = await option_count(world, poll.id)

    await reply(world, STARTER, bad)

    assert await letters_of(world, poll.id, STARTER) == ["a"]
    assert await option_count(world, poll.id) == before
    refusal = last_reply(world.state, STARTER)
    assert refusal == (text.POLL_REPLY_ITEM.format(index=2, reason=reason) + "\n"
                       + text.POLL_FORMAT + " " + text.SESSION_TIME_EXAMPLE)
    assert refusal.count("Reply") == 1      # the format line and the example line read as one paragraph


# AC3.7: a reply to a closed poll is refused as closed ----------------------------------------------------

async def test_a_vote_on_a_closed_poll_is_refused_as_closed_and_changes_nothing(build) -> None:
    world = await build()
    poll = await started(world, HOLDER_A)
    await reply(world, STARTER, "a")
    await world.db.run(lambda c: polls.close_if_open(c, poll.id, now=int(NOW) + 1, outcome="none",
                                                     winner_option_id=None))

    await world.flow.vote(STARTER, poll.id, "b", reply_channel_id=world.state.dm_channels[STARTER])
    await world.post.drain()

    assert await letters_of(world, poll.id, STARTER) == ["a"]
    assert last_reply(world.state, STARTER) == text.POLL_REPLY_CLOSED


async def test_a_vote_by_someone_with_no_ballot_is_refused_and_changes_nothing(build) -> None:
    world = await build()
    poll = await started(world, HOLDER_A)

    await world.flow.vote(HOLDER_B, poll.id, "a", reply_channel_id=world.state.dm_channels[STARTER])
    await world.post.drain()

    assert await picks_of(world, poll.id, HOLDER_B) == []
    assert await world.db.run(lambda c: polls.ballot(c, poll.id, HOLDER_B)) is None
    assert last_reply(world.state, STARTER) == text.POLL_NOT_RECIPIENT


# AC4.4: several replies in one debounce window give one edit per message --------------------------------

async def test_replies_from_three_voters_in_one_window_give_one_poll_edit_and_one_edit_per_ballot(build) -> None:
    world = await build()
    poll = await started(world, HOLDER_A, HOLDER_B)
    gate = asyncio.Event()

    async def hold(_delay: float) -> None:
        await gate.wait()

    flow, post = debounced_flow(world, hold)
    before = len(world.state.requests)
    await hand_reply(flow, world, STARTER, "a")
    await hand_reply(flow, world, HOLDER_A, "b")
    await hand_reply(flow, world, HOLDER_B, "a, c")
    gate.set()
    await post.drain()

    patches = Counter(r.path for r in world.state.requests[before:] if r.method == "PATCH")
    assert patches[f"/api/v1/channels/{PLAY}/messages/{poll.message_id}"] == 1
    for user in (STARTER, HOLDER_A, HOLDER_B):
        ballot = await ballot_of(world, poll.id, user)
        assert ballot is not None and ballot.dm_channel_id is not None and ballot.dm_message_id is not None
        assert patches[f"/api/v1/channels/{ballot.dm_channel_id}/messages/{ballot.dm_message_id}"] == 1
    assert max(patches.values()) == 1


# AC4.5: positions and letters never change as votes come in ----------------------------------------------

async def test_options_keep_their_positions_and_letters_through_votes_and_a_typed_option(build) -> None:
    world = await build()
    poll = await started(world, HOLDER_A)
    offered = offered_slot_times(world.config, NOW)
    before = await world.db.run(lambda c: [(o.id, o.position) for o in polls.options(c, poll.id)])

    await reply(world, STARTER, "a, c")
    await reply(world, HOLDER_A, "b")
    await reply(world, HOLDER_A, "2026-11-14 14:07, a")

    after = await world.db.run(lambda c: [(o.id, o.position) for o in polls.options(c, poll.id)])
    assert after[:len(before)] == before
    starter_ballot = await ballot_message_text(world, poll.id, STARTER)
    for position, starts in enumerate(offered[: world.config.poll_ballot_size]):
        assert f"{poll_letters.letter(position)}. <t:{starts}:F>" in starter_ballot


# Several open ballots: the reply changes nothing and names the open polls ---------------------------------

def seed_second_card(conn: Any) -> int:
    card_id = cards.create_card(conn, slug="md", title="Arena", author_id=ADMIN, now=1)
    cards.set_state(conn, card_id, "live", 1)
    cards.set_message(conn, card_id, GAMES, "5102", HASH)
    return card_id


async def test_with_two_open_ballots_a_reply_changes_nothing_and_names_the_open_polls(build) -> None:
    world = await build()
    first = await started(world, HOLDER_A)
    second_card = await world.db.run(seed_second_card)
    await world.flow.start(press(second_card))
    await world.post.drain()
    second = await world.db.run(lambda c: polls.open_for_card(c, second_card))
    assert second is not None

    await reply(world, STARTER, "a")

    assert last_reply(world.state, STARTER) == text.POLL_WHICH.format(polls=f"{first.id}, {second.id}")
    assert await picks_of(world, first.id, STARTER) == []
    assert await picks_of(world, second.id, STARTER) == []


# Full poll: a typed time is refused, and a reply that adds some options before running out adds none -------

def fill_options(conn: Any, poll_id: int, count: int) -> None:
    while len(polls.options(conn, poll_id)) < count:
        polls.add_option(conn, poll_id, starts_at=int(NOW) + 10**6 + len(polls.options(conn, poll_id)) * 60,
                         origin="typed")


async def test_a_full_poll_refuses_a_typed_time_and_adds_nothing(build) -> None:
    world = await build()
    poll = await started(world, HOLDER_A)
    await world.db.run(lambda c: fill_options(c, poll.id, polls.MAX_OPTIONS))

    await reply(world, STARTER, "2026-11-14 14:07")

    assert await option_count(world, poll.id) == polls.MAX_OPTIONS
    assert last_reply(world.state, STARTER) == text.POLL_FULL


async def test_a_reply_that_runs_out_of_room_partway_rolls_back_the_options_it_added(build) -> None:
    world = await build()
    poll = await started(world, HOLDER_A)
    await world.db.run(lambda c: fill_options(c, poll.id, polls.MAX_OPTIONS - 1))

    await reply(world, STARTER, "a, 2026-11-14 14:07, 2026-11-14 14:37")

    assert await option_count(world, poll.id) == polls.MAX_OPTIONS - 1
    assert await picks_of(world, poll.id, STARTER) == []
    assert last_reply(world.state, STARTER) == text.POLL_FULL


# A failed answer: the vote is stored and the poll and ballots are still refreshed --------------------------

async def test_a_failed_answer_still_stores_the_vote_and_refreshes_the_poll_and_ballots(build, caplog) -> None:
    world = await build()
    poll = await started(world, HOLDER_A)
    caplog.set_level(logging.DEBUG)
    dm = world.state.dm_channels[STARTER]
    world.state.overrides[f"POST /api/v1/channels/{dm}/messages"] = Override(
        403, b'{"code":"FORBIDDEN","message":"x"}')
    before = len(world.state.requests)

    await reply(world, STARTER, "a, c")

    assert await letters_of(world, poll.id, STARTER) == ["a", "c"]
    patches = Counter(r.path for r in world.state.requests[before:] if r.method == "PATCH")
    assert patches[f"/api/v1/channels/{PLAY}/messages/{poll.message_id}"] == 1
    for user in (STARTER, HOLDER_A):
        ballot = await ballot_of(world, poll.id, user)
        assert ballot is not None and ballot.dm_channel_id is not None and ballot.dm_message_id is not None
        assert patches[f"/api/v1/channels/{ballot.dm_channel_id}/messages/{ballot.dm_message_id}"] == 1
    failures = [getattr(r, FIELDS_ATTR, {}) for r in caplog.records if r.getMessage() == "poll_answer_failed"]
    assert [fields.get("reason") for fields in failures] == ["FluxerHTTPError"]


# Logs: the vote's log names the poll and the outcome, never the user or the reply -------------------------

async def test_a_vote_logs_the_poll_and_outcome_but_never_the_user_or_the_reply(build, caplog) -> None:
    world = await build()
    poll = await started(world, HOLDER_A)
    caplog.set_level(logging.DEBUG)

    await reply(world, STARTER, "a, c")

    vote_logs = [r for r in caplog.records if r.getMessage() == "poll_vote"]
    assert vote_logs, "a vote logs poll_vote"
    for record in vote_logs:
        fields = getattr(record, FIELDS_ATTR, {})
        assert fields.get("poll_id") == poll.id
        assert fields.get("outcome") == "voted"
        assert set(fields) <= {"poll_id", "outcome", "reason", "count"}
        assert STARTER not in fields.values() and "a, c" not in fields.values()


# The press end to end: a real Presses, Interactions and PollFlow -----------------------------------------

def presses_for(world: World) -> Presses:
    return Presses(db=world.db, rest=world.rest, config=world.config, handler=world.interactions,
                   games_guild=GamesGuild(world.rest, world.config), pacer=ChannelPacer(max_calls=1000),
                   identity=BotIdentity(user_id=BOT), clock=world.clock)


def poll_reaction(user: str = STARTER, *, channel: str = GAMES, message: str = CARD_MSG) -> events.ReactionAdd:
    return events.ReactionAdd(user_id=user, channel_id=channel, message_id=message, guild_id=GUILD,
                              emoji=events.Emoji(name=POLL_EMOJI, id=None), member_role_ids=None)


async def test_a_poll_press_removes_the_presser_reaction_posts_one_poll_and_sends_the_ballots(build) -> None:
    world = await build()
    members(world.state, STARTER, HOLDER_A)

    await presses_for(world).on_reaction(poll_reaction())
    await world.post.drain()

    removals = [r.raw_path for r in world.state.requests if r.method == "DELETE"]
    assert removals == [f"/api/v1/channels/{GAMES}/messages/{CARD_MSG}/reactions/%F0%9F%97%B3%EF%B8%8F/{STARTER}"]
    assert len(posts_to(world.state, POLL_POSTS)) == 1
    poll = await open_poll(world)
    assert poll is not None
    assert await ballot_users(world, poll.id) == {STARTER, HOLDER_A}


async def test_with_the_kill_switch_off_a_poll_press_does_nothing(build) -> None:
    world = await build()
    members(world.state, STARTER, HOLDER_A)
    await world.db.run(lambda c: interaction_settings.set_enabled(c, False))

    await presses_for(world).on_reaction(poll_reaction())
    await world.post.drain()

    assert world.state.requests == []
    assert await open_poll(world) is None
