# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scheduling a poll's winner (📅) and starting a ranked runoff (🔁), through the production wiring: Presses, Interactions,
PollFlow, SessionFlow, PollPost, PollCloser and PollJobs, against the fake REST server and a SQLite file. The clock moves
only when a test moves it. The emoji are written as escapes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.roles import RoleCache
from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, polls, render, sessions, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.dm_router import NonAdminLimiter
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poll_close import PollCloser
from fluxer_panel.poll_flow import PollFlow
from fluxer_panel.poll_jobs import PollJobs
from fluxer_panel.poll_post import PollPost
from fluxer_panel.presses import Presses
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_flow import SessionFlow
from fluxer_panel.session_post import SessionPost
from tests.fakes import FakeFluxerState, make_rest_app

TOKEN = "SENTINEL-TOKEN-poll-schedule"
BOT = "1900"
ADMIN = "1001"
STARTER = "6001"
VOTER_A = "6002"
VOTER_B = "6003"
VOTER_C = "6004"
VOTER_D = "6005"
VOTER_E = "6006"
VOTER_F = "6007"
VOTER_G = "6008"
STRANGER = "6099"
GUILD = "2001"          # the games community
GAMES = "3100"          # the games channel, in GUILD
PLAY = "3200"           # the card's session channel and poll channel (PLAY_NOW_CHANNEL_ID), in GUILD
CARD_MSG = "5101"
POLL_MSG = "5301"       # the closed approval poll's message, in PLAY
ROLE = "7001"
HASH = "a" * 64
URL = "https://fluxer.invalid"
NOW = 1_800_000_000
CLOSES = NOW + 7200                  # two hours after the start
ONE = CLOSES + 7200                  # usable at close: two hours away
TWO = CLOSES + 10800
THREE = CLOSES + 14400
DAY = 86400
BACKOFF = 300
SESSION = "\U0001F4C5"               # the calendar
RUNOFF = "\U0001F501"                # the repeat button
POLL_POSTS = f"/api/v1/channels/{PLAY}/messages"
POLL_PATH = f"/api/v1/channels/{PLAY}/messages/{POLL_MSG}"


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def config_for() -> Config:
    env = {"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": URL, "ADMIN_USER_IDS": ADMIN,
           "GAMES_CHANNEL_ID": GAMES, "PLAY_NOW_CHANNEL_ID": PLAY, "POLL_INTERVAL_SECONDS": "3600",
           "DM_BACKOFF_SECONDS": str(BACKOFF)}
    return Config.from_env(env)


@dataclass
class World:
    db: Database
    state: FakeFluxerState
    rest: FluxerREST
    config: Config
    clock: Clock
    post: PollPost
    closer: PollCloser
    jobs: PollJobs
    flow: PollFlow
    presses: Presses
    card_id: int


def _card(conn: Any) -> int:
    card_id = cards.create_card(conn, slug="lobby", title="Lobby", author_id=ADMIN, now=1)
    cards.set_state(conn, card_id, "live", 1)
    cards.set_message(conn, card_id, GAMES, CARD_MSG, HASH)
    cards.set_follow_role(conn, card_id, GUILD, ROLE)
    return card_id


@pytest.fixture
async def world(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[World]:
    database = Database.open(tmp_path / "poll_schedule.db")
    state = FakeFluxerState(bot_user_id=BOT, next_message_id=9001)
    state.guilds.update({GUILD: {"id": GUILD}})
    state.channels.update({
        GAMES: {"id": GAMES, "guild_id": GUILD, "type": 0},
        PLAY: {"id": PLAY, "guild_id": GUILD, "type": 0},
    })
    state.messages[(PLAY, POLL_MSG)] = {"embeds": []}
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    config = config_for()
    clock = Clock(float(NOW))
    kicks: list[int] = []
    post: PollPost | None = None
    try:
        async with aiohttp.ClientSession() as client:
            rest = FluxerREST(client, f"{base}/api", TOKEN)
            pacer = ChannelPacer(max_calls=1000)
            identity = BotIdentity(user_id=BOT)
            games_guild = GamesGuild(rest, config)
            locks = CardLocks()
            backoff = DmBackoff(config.dm_backoff_seconds, clock=clock)
            limiter = NonAdminLimiter(config, clock=clock)
            post = PollPost(db=database, rest=rest, config=config, pacer=pacer, identity=identity, backoff=backoff,
                            edit_delay=0)
            closer = PollCloser(db=database, locks=locks, poll_post=post, clock=clock)
            session_post = SessionPost(db=database, rest=rest, config=config, pacer=pacer, backoff=backoff)
            session_flow = SessionFlow(db=database, rest=rest, config=config, locks=locks, session_post=session_post,
                                       games_guild=games_guild, pacer=pacer, identity=identity, backoff=backoff,
                                       limiter=limiter, clock=clock, kick=lambda: kicks.append(1))
            flow = PollFlow(db=database, rest=rest, config=config, locks=locks, poll_post=post, backoff=backoff,
                            games_guild=games_guild, clock=clock, kick=lambda: kicks.append(1),
                            session_flow=session_flow)
            jobs = PollJobs(db=database, rest=rest, config=config, closer=closer, backoff=backoff, clock=clock,
                            kick=lambda: kicks.append(1))
            interactions = Interactions(db=database, rest=rest, config=config, locks=locks, games_guild=games_guild,
                                        seeder=Seeder(rest=rest, pacer=pacer, db=database), pacer=pacer,
                                        identity=identity, roles=RoleCache(rest), clock=clock, backoff=backoff,
                                        session_flow=session_flow, session_post=session_post, poll_flow=flow)
            presses = Presses(db=database, rest=rest, config=config, handler=interactions, games_guild=games_guild,
                              pacer=pacer, identity=identity, backoff=backoff, clock=clock)
            card_id = await database.run(_card)
            yield World(db=database, state=state, rest=rest, config=config, clock=clock, post=post, closer=closer,
                        jobs=jobs, flow=flow, presses=presses, card_id=card_id)
    finally:
        if post is not None:
            await post.aclose(timeout=0.05)
        database.close()


# Helpers --------------------------------------------------------------------------------------------------

def members(state: FakeFluxerState, *user_ids: str) -> None:
    """Members of the games community who hold the follow role. Replaces the list each call."""
    state.members.clear()
    for uid in user_ids:
        state.members[(GUILD, uid)] = {"user": {"id": uid}, "roles": [ROLE]}


async def store(world: World, *, starts: list[int], users: tuple[str, ...], kind: str = "approval",
                parent: int | None = None, message: str = POLL_MSG) -> int:
    def tx(conn: Any) -> int:
        poll_id = polls.create(conn, card_id=world.card_id, guild_id=GUILD, channel_id=PLAY, message_id=message,
                               starter_id=STARTER, kind=kind, parent_poll_id=parent, now=NOW, closes_at=CLOSES,
                               ballot_size=6, options=[(s, "offered") for s in starts], recipients=list(users))
        assert poll_id is not None
        return poll_id

    return await world.db.run(tx)


async def option_ids(world: World, poll_id: int) -> dict[int, int]:
    """Option id by its start time."""
    options = await world.db.run(lambda c: polls.options(c, poll_id))
    return {option.starts_at: option.id for option in options}


async def vote(world: World, poll_id: int, user: str, starts: list[int], *, ranked: bool = False) -> None:
    ids = await option_ids(world, poll_id)
    assert await world.db.run(
        lambda c: polls.replace_picks_if_open(c, poll_id, user, [ids[s] for s in starts], ranked=ranked))


async def close_at_closes(world: World, poll_id: int) -> None:
    world.clock.now = CLOSES
    closed = await world.closer.close(poll_id)
    assert closed is not None and closed.outcome == "winner"
    await world.post.drain()


async def closed_winner(world: World) -> int:
    """An approval poll that closed at CLOSES with ONE the winner: A and B voted ONE, C voted TWO. The starter and the
    other voters hold no vote for ONE."""
    poll_id = await store(world, starts=[ONE, TWO], users=(STARTER, VOTER_A, VOTER_B, VOTER_C))
    await vote(world, poll_id, VOTER_A, [ONE])
    await vote(world, poll_id, VOTER_B, [ONE])
    await vote(world, poll_id, VOTER_C, [TWO])
    await close_at_closes(world, poll_id)
    return poll_id


async def closed_three_way(world: World) -> int:
    """An approval poll that closed at CLOSES: ONE has three votes (A, B, C), TWO has two (D, E), THREE has one (F).
    The starter and every voter hold a ballot."""
    users = (STARTER, VOTER_A, VOTER_B, VOTER_C, VOTER_D, VOTER_E, VOTER_F)
    poll_id = await store(world, starts=[ONE, TWO, THREE], users=users)
    for user in (VOTER_A, VOTER_B, VOTER_C):
        await vote(world, poll_id, user, [ONE])
    for user in (VOTER_D, VOTER_E):
        await vote(world, poll_id, user, [TWO])
    await vote(world, poll_id, VOTER_F, [THREE])
    await close_at_closes(world, poll_id)
    return poll_id


async def react(world: World, user: str, name: str, *, message: str = POLL_MSG) -> None:
    await world.presses.on_reaction(events.ReactionAdd(
        user_id=user, channel_id=PLAY, message_id=message, guild_id=GUILD,
        emoji=events.Emoji(name=name, id=None), member_role_ids=None, user_is_bot=False))
    await world.post.drain()


def dm_contents(state: FakeFluxerState, user_id: str) -> list[str]:
    channel = state.dm_channels.get(user_id)
    if channel is None:
        return []
    path = f"/api/v1/channels/{channel}/messages"
    return [r.payload["content"] for r in state.requests if r.method == "POST" and r.path == path]


def session_posts(state: FakeFluxerState) -> list[Any]:
    return [r for r in state.requests if r.method == "POST" and r.path == POLL_POSTS]


def reaction_removals(state: FakeFluxerState) -> list[tuple[str, str]]:
    """(emoji, user id) for each reaction DELETE on the poll message, in order."""
    out = []
    for r in state.requests:
        if r.method == "DELETE" and f"/messages/{POLL_MSG}/reactions/" in r.path:
            out.append((unquote(r.path.split("/")[-2]), r.path.split("/")[-1]))
    return out


async def sessions_all(world: World) -> list[tuple[int, str, int, str]]:
    def read(conn: Any) -> list[tuple[int, str, int, str]]:
        return [tuple(row) for row in conn.execute(
            "SELECT id, proposer_id, starts_at, message_id FROM sessions ORDER BY id").fetchall()]

    return await world.db.run(read)


async def rsvp_rows(world: World, session_id: int) -> set[tuple[str, str, int]]:
    def read(conn: Any) -> set[tuple[str, str, int]]:
        return {tuple(row) for row in conn.execute(
            "SELECT user_id, choice, implicit FROM rsvp_reaction WHERE session_id = ?", (session_id,)).fetchall()}

    return await world.db.run(read)


async def stored(world: World, poll_id: int) -> polls.Poll:
    poll = await world.db.run(lambda c: polls.get(c, poll_id))
    assert poll is not None
    return poll


async def runoff_of(world: World, parent: int) -> polls.Poll | None:
    return await world.db.run(lambda c: polls.child_of(c, parent))


def link_to(world: World, channel: str, message: str) -> str:
    return render.jump_link(text.POLL_LINK_LABEL, URL, GUILD, channel, message)


def session_link(message: str) -> str:
    return render.jump_link(text.SESSION_LINK_LABEL, URL, GUILD, PLAY, message)


# AC7.1: 📅 by the starter schedules the winner --------------------------------------------------------

async def test_the_starter_schedules_the_winner_with_its_voters_as_maybes(world: World) -> None:
    poll_id = await closed_winner(world)

    await react(world, STARTER, SESSION)

    [(session_id, proposer, starts_at, message_id)] = await sessions_all(world)
    assert (proposer, starts_at) == (STARTER, ONE)
    assert len(session_posts(world.state)) == 1
    assert await rsvp_rows(world, session_id) == {(STARTER, "going", 1), (VOTER_A, "maybe", 1), (VOTER_B, "maybe", 1)}
    assert (await stored(world, poll_id)).session_id == session_id
    link = session_link(message_id)
    for user in (STARTER, VOTER_A, VOTER_B):
        assert dm_contents(world.state, user) == [text.POLL_SCHEDULED_DM.format(card="Lobby", link=link)]
    assert dm_contents(world.state, VOTER_C) == []


async def test_the_poll_message_links_the_session_and_the_bot_removes_its_actions(world: World) -> None:
    await closed_winner(world)

    await react(world, STARTER, SESSION)

    [(session_id, _proposer, _starts, message_id)] = await sessions_all(world)
    edits = [r for r in world.state.requests if r.method == "PATCH" and r.path == POLL_PATH]
    assert session_link(message_id) in str(edits[-1].payload)
    removed = reaction_removals(world.state)
    assert (SESSION, STARTER) in removed                    # the presser's reaction
    assert (SESSION, BOT) in removed and (RUNOFF, BOT) in removed   # the bot's seeded actions


async def test_a_second_schedule_creates_no_second_session(world: World) -> None:
    await closed_winner(world)
    await react(world, STARTER, SESSION)

    await react(world, STARTER, SESSION)

    assert len(await sessions_all(world)) == 1
    assert len(session_posts(world.state)) == 1


async def test_two_schedules_in_flight_at_once_create_one_session(world: World) -> None:
    await closed_winner(world)
    world.clock.now = CLOSES
    first = events.ReactionAdd(user_id=STARTER, channel_id=PLAY, message_id=POLL_MSG, guild_id=GUILD,
                               emoji=events.Emoji(name=SESSION, id=None), member_role_ids=None, user_is_bot=False)

    await asyncio.gather(world.presses.on_reaction(first), world.presses.on_reaction(first))
    await world.post.drain()

    assert len(await sessions_all(world)) == 1
    assert len(session_posts(world.state)) == 1


async def test_a_winner_under_an_hour_away_is_not_scheduled(world: World) -> None:
    await closed_winner(world)
    world.clock.now = ONE - 1800

    await react(world, STARTER, SESSION)

    assert dm_contents(world.state, STARTER) == [text.POLL_TOO_LATE]
    assert await sessions_all(world) == []


async def test_a_starter_at_the_session_cap_gets_the_cap_text_and_no_session(world: World) -> None:
    await closed_winner(world)

    def fill(conn: Any) -> None:
        for index in range(3):   # MAX_PER_PROPOSER
            sessions.add(conn, card_id=world.card_id, guild_id=GUILD, channel_id=PLAY, message_id=f"98{index:02d}",
                         starts_at=ONE + index * DAY, ends_at=ONE + index * DAY + 3600, proposer_id=STARTER, now=NOW)

    await world.db.run(fill)

    await react(world, STARTER, SESSION)

    assert dm_contents(world.state, STARTER) == [text.SESSION_CAP["proposer"].format(card="Lobby")]
    assert len(await sessions_all(world)) == 3
    assert session_posts(world.state) == []


# AC7.7: 📅 and 🔁 from anyone else are removed and do nothing ------------------------------------------

async def test_schedule_and_runoff_from_a_stranger_are_removed_and_do_nothing(world: World) -> None:
    poll_id = await closed_winner(world)

    await react(world, STRANGER, SESSION)
    await react(world, STRANGER, RUNOFF)

    removed = reaction_removals(world.state)
    assert (SESSION, STRANGER) in removed and (RUNOFF, STRANGER) in removed
    assert await sessions_all(world) == []
    assert await runoff_of(world, poll_id) is None
    assert dm_contents(world.state, STRANGER) == []


# AC7.3: 🔁 by an admin starts a runoff of the top three ---------------------------------------------

async def test_an_admin_starts_a_runoff_of_the_top_three_times_in_rank_order(world: World) -> None:
    parent = await closed_three_way(world)
    members(world.state, STARTER, VOTER_A, VOTER_B, VOTER_C, VOTER_D, VOTER_E, VOTER_F)
    members(world.state, STARTER, VOTER_A, VOTER_G)   # the role as it is now: B is no longer a holder

    await react(world, ADMIN, RUNOFF)

    runoff = await runoff_of(world, parent)
    assert runoff is not None
    assert (runoff.kind, runoff.parent_poll_id, runoff.starter_id) == ("runoff", parent, STARTER)
    options = await world.db.run(lambda c: polls.options(c, runoff.id))
    assert [(o.position, o.starts_at) for o in options] == [(0, ONE), (1, TWO), (2, THREE)]
    assert runoff.closes_at == CLOSES + world.config.poll_length_seconds
    ballots = await world.db.run(lambda c: polls.ballots(c, runoff.id))
    assert {b.user_id for b in ballots} == {STARTER, VOTER_A, VOTER_G}
    assert dm_contents(world.state, VOTER_A) and dm_contents(world.state, VOTER_G)
    assert dm_contents(world.state, VOTER_B) == []
    assert dm_contents(world.state, ADMIN) == [text.POLL_RUNOFF_STARTED.format(
        link=link_to(world, PLAY, runoff.message_id))]
    assert (SESSION, BOT) in reaction_removals(world.state)


async def test_a_second_runoff_links_the_one_already_started(world: World) -> None:
    parent = await closed_three_way(world)
    await react(world, ADMIN, RUNOFF)
    runoff = await runoff_of(world, parent)
    assert runoff is not None

    await react(world, ADMIN, RUNOFF)

    assert dm_contents(world.state, ADMIN)[-1] == text.POLL_RUNOFF_EXISTS.format(
        link=link_to(world, PLAY, runoff.message_id))
    count = await world.db.run(lambda c: c.execute("SELECT COUNT(*) FROM polls WHERE parent_poll_id = ?",
                                                   (parent,)).fetchone()[0])
    assert count == 1


# AC7.4: 🔁 with fewer than two usable voted times starts nothing ------------------------------------

async def test_a_runoff_with_fewer_than_two_usable_voted_times_starts_nothing(world: World) -> None:
    poll_id = await store(world, starts=[ONE, TWO], users=(STARTER, VOTER_A))
    await vote(world, poll_id, VOTER_A, [ONE])
    await close_at_closes(world, poll_id)

    await react(world, STARTER, RUNOFF)

    assert dm_contents(world.state, STARTER) == [text.POLL_RUNOFF_NEEDS_TWO]
    assert await runoff_of(world, poll_id) is None
