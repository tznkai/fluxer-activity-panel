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
from fluxer_client.rest import FluxerREST, encode_emoji
from fluxer_panel import cards, poll_members, polls, render, sessions, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.dm_router import NonAdminLimiter
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poll_close import PollCloser
from fluxer_panel.poll_flow import RUNOFF_CLOSE_MARGIN, PollFlow
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
RUNOFF_LENGTH = 1800
SESSION = "\U0001F4C5"               # the calendar
RUNOFF = "\U0001F501"                # the repeat button
POLL_POSTS = f"/api/v1/channels/{PLAY}/messages"
POLL_PATH = f"/api/v1/channels/{PLAY}/messages/{POLL_MSG}"


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def config_for(*, instance_url: bool = True, length: int | None = RUNOFF_LENGTH) -> Config:
    """The test config. `length` is the poll length; None keeps the default (24 hours). The short length makes the older
    tests close quickly. A runoff is capped before its earliest time whatever the length, so the tests with the default
    length cover the cap itself. `instance_url=False` leaves FLUXER_INSTANCE_URL unset, for the no-link texts."""
    env = {"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "ADMIN_USER_IDS": ADMIN,
           "GAMES_CHANNEL_ID": GAMES, "PLAY_NOW_CHANNEL_ID": PLAY, "POLL_INTERVAL_SECONDS": "3600",
           "DM_BACKOFF_SECONDS": str(BACKOFF)}
    if instance_url:
        env["FLUXER_INSTANCE_URL"] = URL
    else:
        env["FLUXER_API_URL"] = URL      # the config needs one of the two URLs
    if length is not None:
        env["POLL_LENGTH_SECONDS"] = str(length)
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
async def world(tmp_path: Path, aiohttp_server: Any, request: pytest.FixtureRequest) -> AsyncIterator[World]:
    """A test may pass `config_for` options with `@pytest.mark.parametrize("world", [{...}], indirect=True)`."""
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
    config = config_for(**getattr(request, "param", {}))
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


def reaction_removals(state: FakeFluxerState, message: str = POLL_MSG) -> list[tuple[str, str]]:
    """(emoji, user id) for each reaction DELETE on a poll message (the closed approval poll's by default), in order."""
    out = []
    for r in state.requests:
        if r.method == "DELETE" and f"/messages/{message}/reactions/" in r.path:
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


# AC7.5 and AC7.6: a runoff end to end ----------------------------------------------------------------------

async def option_of(world: World, poll_id: int, starts_at: int) -> int:
    return (await option_ids(world, poll_id))[starts_at]


async def ranks_of(world: World, poll_id: int, user: str) -> list[tuple[int, int | None]]:
    votes = await world.db.run(lambda c: polls.votes(c, poll_id))
    return [(vote.option_id, vote.rank) for vote in votes if vote.user_id == user]


async def open_runoff(world: World) -> tuple[int, int]:
    """A closed three-way poll, then a runoff started by an admin. Returns (parent id, runoff id). The runoff's ballots
    go to the starter and to VOTER_A and VOTER_G, the holders of the role as it is then."""
    parent = await closed_three_way(world)
    members(world.state, STARTER, VOTER_A, VOTER_G)
    await react(world, ADMIN, RUNOFF)
    runoff = await runoff_of(world, parent)
    assert runoff is not None
    return parent, runoff.id


async def reply(world: World, user: str, content: str) -> None:
    open_polls = await world.flow.open_ballots(user)
    if open_polls:
        await world.flow.on_reply(events.MessageCreate(
            message_id="7100", channel_id=world.state.dm_channels[user], guild_id=None, author_id=user,
            author_is_bot=False, member_role_ids=None, content=content), open_polls)
    await world.post.drain()


def refusal_for(index: int, reason: str) -> str:
    return (text.POLL_REPLY_ITEM.format(index=index, reason=text.POLL_REPLY_REFUSED[reason]) + "\n"
            + text.POLL_FORMAT + " " + text.SESSION_TIME_EXAMPLE)


@pytest.mark.parametrize(("content", "index", "reason"), [
    ("fri 20:00", 1, "letters_only"),
    ("all", 1, "no_all"),
    ("a, b, a", 3, "repeated"),
])
async def test_a_runoff_refuses_a_typed_time_all_and_a_repeated_letter_and_changes_nothing(
        world: World, content: str, index: int, reason: str) -> None:
    _parent, runoff_id = await open_runoff(world)
    await reply(world, VOTER_A, "b")
    before = await ranks_of(world, runoff_id, VOTER_A)
    options_before = len(await world.db.run(lambda c: polls.options(c, runoff_id)))

    await reply(world, VOTER_A, content)

    assert await ranks_of(world, runoff_id, VOTER_A) == before
    assert len(await world.db.run(lambda c: polls.options(c, runoff_id))) == options_before
    assert dm_contents(world.state, VOTER_A)[-1] == refusal_for(index, reason)


async def test_a_runoff_accepts_one_letter_and_a_partial_ranking_in_rank_order(world: World) -> None:
    _parent, runoff_id = await open_runoff(world)

    await reply(world, VOTER_A, "b")
    assert await ranks_of(world, runoff_id, VOTER_A) == [(await option_of(world, runoff_id, TWO), 1)]

    await reply(world, VOTER_G, "c, a")
    assert await ranks_of(world, runoff_id, VOTER_G) == [
        (await option_of(world, runoff_id, THREE), 1), (await option_of(world, runoff_id, ONE), 2)]
    assert dm_contents(world.state, VOTER_G)[-1] == text.POLL_YOUR_PICKS.format(letters="c, a")


async def test_a_closed_runoff_shows_its_rounds_gets_only_the_calendar_and_its_winner_schedules(world: World) -> None:
    _parent, runoff_id = await open_runoff(world)
    await reply(world, STARTER, "a")     # first choice: a
    await reply(world, VOTER_A, "b")     # first choice: b
    await reply(world, VOTER_G, "c, a")  # first choice: c, dropped in round one; then a

    runoff = await stored(world, runoff_id)
    world.clock.now = runoff.closes_at
    await world.jobs.run()
    await world.post.drain()

    closed = await stored(world, runoff_id)
    assert (closed.state, closed.outcome) == ("closed", "winner")
    assert closed.winner_option_id == await option_of(world, runoff_id, ONE)
    path = f"/api/v1/channels/{PLAY}/messages/{runoff.message_id}"
    shown = str([r for r in world.state.requests if r.method == "PATCH" and r.path == path][-1].payload)
    assert "Round 1:" in shown and "Round 2:" in shown
    assert text.POLL_DROPPED.format(letter="c") in shown
    assert text.POLL_WINNER.format(letter="a", ts=ONE) in shown
    assert set(world.state.reactions.get((PLAY, runoff.message_id), {})) == {SESSION}

    await react(world, STARTER, SESSION, message=runoff.message_id)

    [(session_id, proposer, starts_at, _message)] = await sessions_all(world)
    assert (proposer, starts_at) == (STARTER, ONE)
    assert await rsvp_rows(world, session_id) == {(STARTER, "going", 1), (VOTER_G, "maybe", 1)}


# The runoff's close cap: it closes an hour before its earliest time at the latest, and is refused when that leaves
# under fifteen minutes --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("world", [{"length": None}], indirect=True)
async def test_a_runoff_closes_an_hour_before_its_earliest_time_with_the_default_length(world: World) -> None:
    parent = await closed_three_way(world)
    members(world.state, STARTER, VOTER_A, VOTER_G)
    assert world.config.poll_length_seconds == DAY

    await react(world, ADMIN, RUNOFF)

    runoff = await runoff_of(world, parent)
    assert runoff is not None
    assert runoff.closes_at == ONE - 3600 - RUNOFF_CLOSE_MARGIN   # capped: the default length would close it a day out
    await reply(world, STARTER, "a")
    world.clock.now = runoff.closes_at
    await world.jobs.run()
    await world.post.drain()
    closed = await stored(world, runoff.id)
    assert (closed.state, closed.outcome) == ("closed", "winner")
    assert closed.winner_option_id == await option_of(world, runoff.id, ONE)


@pytest.mark.parametrize("world", [{"length": None}], indirect=True)
async def test_a_runoff_whose_times_are_all_far_away_runs_the_full_poll_length(world: World) -> None:
    far = [CLOSES + 2 * DAY, CLOSES + 2 * DAY + 3600, CLOSES + 3 * DAY]
    parent = await store(world, starts=far, users=(STARTER, VOTER_A, VOTER_B))
    await vote(world, parent, VOTER_A, [far[0]])
    await vote(world, parent, VOTER_B, [far[1]])
    await close_at_closes(world, parent)
    members(world.state, STARTER, VOTER_A, VOTER_B)

    await react(world, ADMIN, RUNOFF)

    runoff = await runoff_of(world, parent)
    assert runoff is not None
    assert runoff.closes_at == CLOSES + DAY         # now + POLL_LENGTH_SECONDS: the cap does not apply


@pytest.mark.parametrize("world", [{"length": None}], indirect=True)
async def test_a_runoff_with_under_fifteen_minutes_to_run_starts_nothing_and_says_so(world: World) -> None:
    parent = await closed_three_way(world)
    members(world.state, STARTER, VOTER_A, VOTER_G)
    world.clock.now = ONE - 3600 - 600              # the earliest time is 70 minutes away: no room once the margin is taken

    await react(world, ADMIN, RUNOFF)

    assert await runoff_of(world, parent) is None
    assert dm_contents(world.state, ADMIN) == [text.POLL_RUNOFF_TOO_SOON.format(ts=ONE)]
    assert session_posts(world.state) == []         # no poll message was posted
    assert (RUNOFF, ADMIN) in reaction_removals(world.state)


@pytest.mark.parametrize("world", [{"length": None}], indirect=True)
async def test_a_runoff_with_exactly_fifteen_minutes_to_run_starts(world: World) -> None:
    parent = await closed_three_way(world)
    members(world.state, STARTER, VOTER_A, VOTER_G)
    world.clock.now = ONE - 3600 - RUNOFF_CLOSE_MARGIN - 900   # exactly 15 minutes between now and the close

    await react(world, ADMIN, RUNOFF)

    runoff = await runoff_of(world, parent)
    assert runoff is not None
    assert runoff.closes_at == ONE - 3600 - RUNOFF_CLOSE_MARGIN


@pytest.mark.parametrize("world", [{"length": None}], indirect=True)
async def test_a_runoff_closed_by_a_tick_five_minutes_late_still_ranks_its_earliest_time(world: World) -> None:
    parent = await closed_three_way(world)
    members(world.state, STARTER, VOTER_A, VOTER_G)
    await react(world, ADMIN, RUNOFF)
    runoff = await runoff_of(world, parent)
    assert runoff is not None
    await reply(world, STARTER, "a")                # the only ballot ranks the earliest time
    world.clock.now = runoff.closes_at + 300        # the tick that closes it runs five minutes after closes_at
    await world.jobs.run()
    await world.post.drain()
    closed = await stored(world, runoff.id)
    assert (closed.state, closed.outcome) == ("closed", "winner")
    assert closed.winner_option_id == await option_of(world, runoff.id, ONE)


@pytest.mark.parametrize("world", [{"length": None}], indirect=True)
async def test_a_runoff_one_second_short_of_the_minimum_starts_nothing(world: World) -> None:
    parent = await closed_three_way(world)
    members(world.state, STARTER, VOTER_A, VOTER_G)
    world.clock.now = ONE - 3600 - RUNOFF_CLOSE_MARGIN - 899   # 899 seconds to run: one short of the 900 minimum

    await react(world, ADMIN, RUNOFF)

    assert await runoff_of(world, parent) is None
    assert dm_contents(world.state, ADMIN) == [text.POLL_RUNOFF_TOO_SOON.format(ts=ONE)]
    assert session_posts(world.state) == []


# The variation selector, a closed runoff, refusals to an admin presser, and the no-link texts ------------------

async def fill_sessions(world: World, proposers: tuple[str, ...]) -> None:
    """One upcoming session per proposer, each at its own time, so the card and proposer counts are as given."""
    def fill(conn: Any) -> None:
        for index, proposer in enumerate(proposers):
            sessions.add(conn, card_id=world.card_id, guild_id=GUILD, channel_id=PLAY, message_id=f"98{index:02d}",
                         starts_at=ONE + index * DAY, ends_at=ONE + index * DAY + 3600, proposer_id=proposer, now=NOW)

    await world.db.run(fill)


async def test_a_schedule_sent_with_the_variation_selector_is_removed_under_that_spelling_and_still_schedules(
        world: World) -> None:
    await closed_winner(world)

    await react(world, STARTER, SESSION + "\ufe0f")

    [(_session_id, proposer, starts_at, _message)] = await sessions_all(world)
    assert (proposer, starts_at) == (STARTER, ONE)
    [segment] = [r.path.split("/")[-2] for r in world.state.requests
                 if r.method == "DELETE" and f"/messages/{POLL_MSG}/reactions/" in r.path
                 and r.path.endswith(f"/{STARTER}")]
    assert segment == SESSION + "\ufe0f"             # the fake records the decoded path
    assert "%EF%B8%8F" in encode_emoji(segment)      # and this is the spelling sent on the wire


async def test_a_runoff_reaction_on_a_closed_runoff_is_removed_and_starts_nothing(world: World) -> None:
    parent = await closed_three_way(world)
    members(world.state, STARTER, VOTER_A, VOTER_G)
    await react(world, ADMIN, RUNOFF)
    runoff = await runoff_of(world, parent)
    assert runoff is not None
    # Three voted times, so that a runoff of the runoff would have candidates and reach the guard under test.
    await reply(world, STARTER, "a")
    await reply(world, VOTER_A, "b")
    await reply(world, VOTER_G, "c, a")
    world.clock.now = runoff.closes_at
    await world.jobs.run()
    await world.post.drain()
    assert (await stored(world, runoff.id)).state == "closed"
    posts_before = len(session_posts(world.state))

    await react(world, STARTER, RUNOFF, message=runoff.message_id)

    assert await runoff_of(world, runoff.id) is None
    assert len(session_posts(world.state)) == posts_before
    assert (RUNOFF, STARTER) in reaction_removals(world.state, runoff.message_id)


async def test_an_admin_runoff_while_the_card_has_an_open_poll_links_that_poll_to_the_admin_only(
        world: World) -> None:
    parent = await closed_three_way(world)
    members(world.state, STARTER, VOTER_A, VOTER_G)
    open_message = "5302"
    await store(world, starts=[THREE + DAY], users=(STARTER,), message=open_message)

    await react(world, ADMIN, RUNOFF)

    assert await runoff_of(world, parent) is None
    assert dm_contents(world.state, ADMIN) == [text.POLL_ALREADY_OPEN.format(
        link=link_to(world, PLAY, open_message))]
    assert dm_contents(world.state, STARTER) == []
    assert (RUNOFF, ADMIN) in reaction_removals(world.state)


async def test_an_admin_runoff_when_the_member_list_cannot_be_read_says_try_later_to_the_admin_only(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = await closed_three_way(world)

    async def unavailable(*_args: Any, **_kwargs: Any) -> list[str]:
        raise poll_members.MembersUnavailable

    monkeypatch.setattr(poll_members, "role_holders", unavailable)

    await react(world, ADMIN, RUNOFF)

    assert await runoff_of(world, parent) is None
    assert dm_contents(world.state, ADMIN) == [text.POLL_TRY_LATER]
    assert dm_contents(world.state, STARTER) == []


@pytest.mark.parametrize("world", [{"instance_url": False}], indirect=True)
async def test_without_an_instance_url_the_schedule_text_names_no_link(world: World) -> None:
    await closed_winner(world)

    await react(world, STARTER, SESSION)

    for user in (STARTER, VOTER_A, VOTER_B):
        assert dm_contents(world.state, user) == [text.POLL_SCHEDULED_DM_NO_LINK.format(card="Lobby")]


@pytest.mark.parametrize("world", [{"instance_url": False}], indirect=True)
async def test_without_an_instance_url_the_runoff_texts_name_no_link(world: World) -> None:
    parent = await closed_three_way(world)
    members(world.state, STARTER, VOTER_A, VOTER_G)

    await react(world, ADMIN, RUNOFF)
    assert dm_contents(world.state, ADMIN) == [text.POLL_RUNOFF_STARTED_NO_LINK]

    await react(world, ADMIN, RUNOFF)
    assert dm_contents(world.state, ADMIN)[-1] == text.POLL_RUNOFF_EXISTS_NO_LINK
    assert await runoff_of(world, parent) is not None


async def test_an_admin_scheduling_when_the_starter_is_at_the_session_cap_is_told_about_the_starter(
        world: World) -> None:
    await closed_winner(world)
    await fill_sessions(world, (STARTER, STARTER, STARTER))

    await react(world, ADMIN, SESSION)

    assert dm_contents(world.state, ADMIN) == [text.POLL_STARTER_AT_CAP.format(card="Lobby")]
    assert dm_contents(world.state, STARTER) == []
    assert len(await sessions_all(world)) == 3
    assert session_posts(world.state) == []


async def test_an_admin_scheduling_when_the_card_is_at_its_cap_gets_the_card_text(world: World) -> None:
    await closed_winner(world)
    await fill_sessions(world, (VOTER_A, VOTER_A, VOTER_A, VOTER_B, VOTER_B, VOTER_B, VOTER_C, VOTER_C, VOTER_C, VOTER_D))

    await react(world, ADMIN, SESSION)

    assert dm_contents(world.state, ADMIN) == [text.SESSION_CAP["card"].format(card="Lobby")]
    assert len(await sessions_all(world)) == 10
    assert session_posts(world.state) == []


# 📅 and 🔁 on one poll exclude each other ---------------------------------------------------------------------

async def test_a_runoff_started_first_blocks_scheduling_and_the_starter_is_linked_to_the_runoff(world: World) -> None:
    parent = await closed_winner(world)
    members(world.state, STARTER, VOTER_A, VOTER_B, VOTER_C)
    await react(world, STARTER, RUNOFF)
    runoff = await runoff_of(world, parent)
    assert runoff is not None
    posts_before = len(session_posts(world.state))   # the runoff's own poll message is one of these

    await react(world, STARTER, SESSION)

    assert await sessions_all(world) == []
    assert len(session_posts(world.state)) == posts_before
    assert (await stored(world, parent)).session_id is None
    assert dm_contents(world.state, STARTER)[-1] == text.POLL_SCHEDULE_RUNOFF_EXISTS.format(
        link=link_to(world, PLAY, runoff.message_id))
    assert await runoff_of(world, runoff.id) is None


async def test_a_session_scheduled_first_blocks_a_runoff_and_the_starter_is_linked_to_the_session(world: World) -> None:
    parent = await closed_winner(world)
    members(world.state, STARTER, VOTER_A, VOTER_B, VOTER_C)
    await react(world, STARTER, SESSION)
    [(_session_id, _proposer, _starts, message_id)] = await sessions_all(world)

    await react(world, STARTER, RUNOFF)

    assert await runoff_of(world, parent) is None
    assert len(session_posts(world.state)) == 1
    assert dm_contents(world.state, STARTER)[-1] == text.POLL_RUNOFF_SCHEDULED.format(link=session_link(message_id))


async def test_a_runoff_blocks_scheduling_with_no_instance_url_and_names_no_link(world: World) -> None:
    parent = await closed_winner(world)
    members(world.state, STARTER, VOTER_A, VOTER_B, VOTER_C)
    await react(world, STARTER, RUNOFF)

    await react(world, STARTER, SESSION)

    assert await sessions_all(world) == []
    assert dm_contents(world.state, STARTER)[-1] == text.POLL_SCHEDULE_RUNOFF_EXISTS.format(
        link=link_to(world, PLAY, (await runoff_of(world, parent)).message_id))


@pytest.mark.parametrize("world", [{"instance_url": False}], indirect=True)
async def test_a_scheduled_winner_blocks_a_runoff_with_no_instance_url_and_names_no_link(world: World) -> None:
    await closed_winner(world)
    await react(world, STARTER, SESSION)

    await react(world, STARTER, RUNOFF)

    assert dm_contents(world.state, STARTER)[-1] == text.POLL_RUNOFF_SCHEDULED_NO_LINK


async def test_the_calendar_on_a_closed_runoff_still_schedules_the_runoff_winner_beside_its_parent(world: World) -> None:
    parent = await closed_winner(world)
    members(world.state, STARTER, VOTER_A, VOTER_B, VOTER_C)
    await react(world, STARTER, RUNOFF)
    runoff = await runoff_of(world, parent)
    assert runoff is not None
    await reply(world, STARTER, "a")
    world.clock.now = runoff.closes_at
    await world.jobs.run()
    await world.post.drain()
    assert (await stored(world, runoff.id)).outcome == "winner"

    await react(world, STARTER, SESSION, message=runoff.message_id)

    [(_session_id, proposer, starts_at, _message)] = await sessions_all(world)
    assert (proposer, starts_at) == (STARTER, ONE)
    assert (await stored(world, runoff.id)).session_id is not None


async def test_a_schedule_and_a_runoff_at_once_on_one_closed_poll_let_exactly_one_act(world: World) -> None:
    parent = await closed_winner(world)
    members(world.state, STARTER, VOTER_A, VOTER_B, VOTER_C)
    world.clock.now = CLOSES

    def press(name: str) -> Any:
        return world.presses.on_reaction(events.ReactionAdd(
            user_id=STARTER, channel_id=PLAY, message_id=POLL_MSG, guild_id=GUILD,
            emoji=events.Emoji(name=name, id=None), member_role_ids=None, user_is_bot=False))

    await asyncio.gather(press(SESSION), press(RUNOFF))
    await world.post.drain()

    sessions = await sessions_all(world)
    runoff = await runoff_of(world, parent)
    assert len(sessions) + (runoff is not None) == 1
    if sessions:
        assert runoff is None
    else:
        assert runoff is not None and (await stored(world, parent)).session_id is None
