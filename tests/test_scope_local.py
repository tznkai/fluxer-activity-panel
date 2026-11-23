# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A home scenario and a local scenario on one card, against the fake Fluxer REST.

A home 🙋, 📅 and 🗳️ (with a vote and a close) must write nothing into the far local channel, and only the copy's own
message in the far redirect copy. A local 🙋, 📅 and 🗳️ must land in the local channel, and nothing from them may reach
the home play-now channel or the games channel. Every component shares one stub clock and one CardLocks, wired as
`__main__.run` wires them. The test moves the clock, and the pacer never sleeps."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.roles import RoleCache
from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, play_now, polls, sessions, subscriptions
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.dm_router import DmRouter, NonAdminLimiter
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poll_close import PollCloser
from fluxer_panel.poll_commands import PollCommands
from fluxer_panel.poll_flow import PollFlow
from fluxer_panel.poll_jobs import PollJobs
from fluxer_panel.poll_post import PollPost
from fluxer_panel.poller import Poller
from fluxer_panel.presses import PLAY_NOW_EMOJI, POLL_EMOJI, SESSION_EMOJI, Presses
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_flow import SessionFlow
from fluxer_panel.session_post import SessionPost
from fluxer_panel.sources.registry import Sources
from tests.fakes import FakeFluxerState, make_rest_app
from tests.test_board_poller import GAMES, TOKEN, FakeClock

BOT_ID = "1900"
ADMIN = "1001"
HOME_PRESSER = "6001"        # presses on the card in the games channel; holds no follow role
HOME_HOLDERS = ("6002", "6003")  # the home follow role's holders: they get the home ballots
LOCAL_PRESSER = "6101"       # presses 🙋 on the local copy
LOCAL_SESSION = "6102"       # proposes a session on the local copy
LOCAL_POLL = "6103"          # starts a poll on the local copy
LOCAL_HOLDERS = ("6104", "6105")  # the local follow role's holders in the far community
GUILD = "2001"               # the games community
OTHER = "2002"               # the far community: the redirect copy, the local copy and their channels
PLAY = "3200"                # the home play-now channel (PLAY_NOW_CHANNEL_ID), in GUILD
FAR_COPY = "3005"            # a redirect copy in OTHER
FAR_LOCAL = "3004"           # a local copy in OTHER, with its own channel
HOME_ROLE = "7001"           # the card's follow role in GUILD
SUB_ROLE = "7002"            # the local subscription's follow role in OTHER
COPY_MSG = "5003"            # the redirect copy's own message
LOCAL_MSG = "5004"           # the local copy's own message
INSTANCE = "https://fluxer.invalid"
PLAY_MESSAGES = f"/api/v1/channels/{PLAY}/messages"
GAMES_PATH = f"/api/v1/channels/{GAMES}/"
NOW = datetime(2026, 11, 11, 8, 0, tzinfo=UTC).timestamp()   # a Wednesday, 08:00 UTC
GOING = "✅"


def keycap(n: int) -> str:
    return f"{n}️⃣"


class Scenario:
    """Every component under test, wired as `__main__.run` wires them, on one clock and one CardLocks."""

    def __init__(self, **parts: Any) -> None:
        self.__dict__.update(parts)


@pytest.fixture
async def scenario(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[Scenario]:
    database = Database.open(tmp_path / "panel.db")
    state = FakeFluxerState(bot_user_id=BOT_ID)
    state.guilds[GUILD] = {"id": GUILD}
    state.guilds[OTHER] = {"id": OTHER}
    state.channels[GAMES] = {"id": GAMES, "name": "games", "type": 0, "guild_id": GUILD}
    state.channels[PLAY] = {"id": PLAY, "name": "play-now", "type": 0, "guild_id": GUILD}
    state.channels[FAR_COPY] = {"id": FAR_COPY, "name": "lounge", "type": 0, "guild_id": OTHER}
    state.channels[FAR_LOCAL] = {"id": FAR_LOCAL, "name": "local", "type": 0, "guild_id": OTHER}
    state.messages[(FAR_COPY, COPY_MSG)] = {"embeds": []}
    state.messages[(FAR_LOCAL, LOCAL_MSG)] = {"embeds": []}
    for uid in HOME_HOLDERS:
        state.members[(GUILD, uid)] = {"user": {"username": "home", "bot": False}, "roles": [HOME_ROLE]}
    for uid in LOCAL_HOLDERS:
        state.members[(OTHER, uid)] = {"user": {"username": "local", "bot": False}, "roles": [SUB_ROLE]}
    try:
        async with aiohttp.ClientSession() as client:
            server = await aiohttp_server(make_rest_app(state))
            rest = FluxerREST(client, f"{str(server.make_url('/')).rstrip('/')}/api", TOKEN)
            clock = FakeClock()
            clock.now = NOW
            config = Config.from_env({
                "FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": INSTANCE,
                "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES, "PLAY_NOW_CHANNEL_ID": PLAY,
                "POLL_INTERVAL_SECONDS": "3600",
            })
            locks = CardLocks()
            pacer = ChannelPacer(max_calls=1000)
            identity = BotIdentity(user_id=BOT_ID)
            games_guild = GamesGuild(rest, config)
            backoff = DmBackoff(config.dm_backoff_seconds, clock=clock)
            limiter = NonAdminLimiter(config, clock=clock)
            seeder = Seeder(rest=rest, pacer=pacer, db=database)
            session_post = SessionPost(db=database, rest=rest, config=config, pacer=pacer, backoff=backoff)
            session_flow = SessionFlow(db=database, rest=rest, config=config, locks=locks,
                                       session_post=session_post, games_guild=games_guild, pacer=pacer,
                                       identity=identity, backoff=backoff, limiter=limiter, clock=clock)
            poll_post = PollPost(db=database, rest=rest, config=config, pacer=pacer, identity=identity,
                                 backoff=backoff, edit_delay=0)
            poll_flow = PollFlow(db=database, rest=rest, config=config, locks=locks, poll_post=poll_post,
                                 backoff=backoff, games_guild=games_guild, clock=clock, session_flow=session_flow)
            poll_closer = PollCloser(db=database, locks=locks, poll_post=poll_post, clock=clock)
            poll_post.set_on_gone(poll_closer.on_message_gone)
            poll_commands = PollCommands(db=database, rest=rest, config=config, locks=locks, poll_post=poll_post,
                                         poll_flow=poll_flow, closer=poll_closer, clock=clock)
            poll_jobs = PollJobs(db=database, rest=rest, config=config, closer=poll_closer, backoff=backoff,
                                 clock=clock)
            interactions = Interactions(db=database, rest=rest, config=config, locks=locks, games_guild=games_guild,
                                        seeder=seeder, pacer=pacer, identity=identity, roles=RoleCache(rest),
                                        clock=clock, session_post=session_post, backoff=backoff,
                                        session_flow=session_flow, poll_flow=poll_flow)
            poller = Poller(db=database, rest=rest, sources=Sources(managers={}, probes={}), config=config,
                            locks=locks, clock=clock, games_guild=games_guild, seeder=seeder)
            poller.set_poll_jobs(poll_jobs)
            poller.set_interactions(interactions)
            presses = Presses(db=database, rest=rest, config=config, handler=interactions, games_guild=games_guild,
                              pacer=pacer, identity=identity, clock=clock, backoff=backoff)
            router = DmRouter(config=config, rest=rest, card_ctx=None, limiter=limiter, session_flow=session_flow,
                              poll_flow=poll_flow, poll_commands=poll_commands.handle)

            def make_card(conn: Any) -> tuple[int, int, int]:
                card_id = cards.create_card(conn, slug="mc", title="Minecraft", author_id=ADMIN, now=1)
                cards.set_state(conn, card_id, "live", 1)
                cards.set_seats(conn, card_id, 1)
                cards.set_follow_role(conn, card_id, GUILD, HOME_ROLE)
                redirect = subscriptions.add(conn, card_id, OTHER, FAR_COPY)
                subscriptions.set_message(conn, card_id, FAR_COPY, COPY_MSG, "a" * 64)
                local = subscriptions.add(conn, card_id, OTHER, FAR_LOCAL)
                subscriptions.set_message(conn, card_id, FAR_LOCAL, LOCAL_MSG, "a" * 64)
                subscriptions.set_mode_if(conn, local, "redirect", "local")
                cards.set_subscription_role(conn, local, SUB_ROLE)
                assert redirect != local
                return card_id, redirect, local

            card_id, redirect_id, local_id = await database.run(make_card)
            yield Scenario(db=database, state=state, rest=rest, clock=clock, config=config, poller=poller,
                           presses=presses, router=router, poll_post=poll_post, session_flow=session_flow,
                           card_id=card_id,
                           redirect_id=redirect_id, local_id=local_id)
    finally:
        database.close()


# Helpers ---------------------------------------------------------------------------------------------------

def dm_channel(s: Scenario, user: str) -> str:
    channel = s.state.dm_channels.get(user)
    assert channel is not None, "the user has no DM channel yet"
    return channel


def last_dm(s: Scenario, user: str, needle: str) -> str:
    """The id of the newest DM to the user whose text contains `needle`."""
    channel = dm_channel(s, user)
    ids = [mid for (cid, mid), payload in s.state.messages.items()
           if cid == channel and needle in str(payload.get("content", ""))]
    assert ids, f"no DM containing {needle!r}"
    return ids[-1]


def reaction(user: str, channel: str, message: str, name: str, *, guild: str | None) -> events.ReactionAdd:
    return events.ReactionAdd(user_id=user, channel_id=channel, message_id=message, guild_id=guild,
                              emoji=events.Emoji(name=name, id=None), member_role_ids=None)


async def say(s: Scenario, user: str, content: str) -> None:
    """A DM from the user to the bot, through the router as the gateway delivers it."""
    await s.router.route(events.MessageCreate(message_id="7001", channel_id=dm_channel(s, user), guild_id=None,
                                              author_id=user, author_is_bot=False, member_role_ids=None,
                                              content=content))


def writes_since(s: Scenario, mark: int, channel: str) -> list[Any]:
    """Every POST, PATCH or PUT the fake received under the channel since request `mark`."""
    prefix = f"/api/v1/channels/{channel}/"
    return [r for r in s.state.requests[mark:]
            if r.method in ("POST", "PATCH", "PUT") and r.path.startswith(prefix)]


def foreign_writes(s: Scenario, mark: int, channel: str, own_message: str) -> list[str]:
    """The writes under the channel that do not target the copy's own message: a POST is never its own, and a PATCH or
    PUT counts only when it is the message itself or one of its reactions. Anything else is a leak."""
    own = (f"/api/v1/channels/{channel}/messages/{own_message}",)
    leaks = []
    for request in writes_since(s, mark, channel):
        if request.method != "POST" and request.path.startswith(own) and (
                request.path == own[0] or request.path.startswith(own[0] + "/reactions/")):
            continue
        leaks.append(f"{request.method} {request.path}")
    return leaks


def posts_to(s: Scenario, channel: str) -> list[Any]:
    path = f"/api/v1/channels/{channel}/messages"
    return [r for r in s.state.requests if r.method == "POST" and r.path == path]


# The scenario ----------------------------------------------------------------------------------------------

async def test_home_objects_never_reach_a_local_channel_and_local_objects_never_reach_home(scenario: Scenario) -> None:
    s = scenario

    # The games channel gets the card and the board. The copies are edited, not posted: the copies' messages exist.
    await s.poller.tick()
    card = await s.db.run(lambda c: cards.get_by_id(c, s.card_id))
    assert card is not None and card.message_id is not None
    card_message = card.message_id

    # 1. Home 🙋 on the card: a play-now post in the home play channel, and nothing in the far copies.
    home_mark = len(s.state.requests)
    await s.presses.on_reaction(reaction(HOME_PRESSER, GAMES, card_message, PLAY_NOW_EMOJI, guild=GUILD))
    play_rows = await s.db.run(lambda c: play_now.live_for_card(c, s.card_id))
    assert [row.channel_id for row in play_rows] == [PLAY], "the home 🙋 posts in the home play channel"

    # 2. Home 📅 on the card: the DM flow, then the session in the home play channel.
    s.clock.now += 61
    await s.presses.on_reaction(reaction(HOME_PRESSER, GAMES, card_message, SESSION_EMOJI, guild=GUILD))
    channel = dm_channel(s, HOME_PRESSER)
    await s.session_flow.on_dm_reaction(reaction(HOME_PRESSER, channel, last_dm(s, HOME_PRESSER, "Pick a time"),
                                         keycap(1), guild=None))
    await s.session_flow.on_dm_reaction(reaction(HOME_PRESSER, channel, last_dm(s, HOME_PRESSER, "to post it"), GOING,
                                         guild=None))
    home_sessions = await s.db.run(lambda c: sessions.upcoming_for_scope(c, s.card_id, None))
    assert [row.channel_id for row in home_sessions] == [PLAY], "the home 📅 posts in the home play channel"

    # 3. Home 🗳️ on the card: a poll in the home play channel, ballots to the home holders, a vote, then the close.
    s.clock.now += 61
    await s.presses.on_reaction(reaction(HOME_PRESSER, GAMES, card_message, POLL_EMOJI, guild=GUILD))
    await s.poll_post.drain()
    home_poll = await s.db.run(lambda c: polls.open_for_card(c, s.card_id))
    assert home_poll is not None and home_poll.channel_id == PLAY and home_poll.scope_sub_id is None
    await say(s, HOME_HOLDERS[0], "a")
    s.clock.now = home_poll.closes_at
    await s.poller.tick()
    await s.poll_post.drain()
    closed = await s.db.run(lambda c: polls.get(c, home_poll.id))
    assert closed is not None and closed.state == "closed"

    # Positive control: the home objects were written to the home play channel.
    assert len(posts_to(s, PLAY)) >= 3, "the home 🙋, 📅 and 🗳️ should each post in the home play channel"

    # AC2.6, home side: nothing from the home objects reaches the far local channel. The local copy's own message may
    # be edited and seeded by the card, and the redirect copy's own message likewise. Any other write is a leak.
    assert foreign_writes(s, home_mark, FAR_LOCAL, LOCAL_MSG) == [], "a home object reached the far local channel"
    assert foreign_writes(s, home_mark, FAR_COPY, COPY_MSG) == [], "a home object reached the far redirect copy"

    # 4. Local 🙋 on the local copy: its play-now post is in the local channel, not in the home play channel.
    local_mark = len(s.state.requests)
    home_play_before = len(posts_to(s, PLAY))
    s.clock.now += 61
    await s.presses.on_reaction(reaction(LOCAL_PRESSER, FAR_LOCAL, LOCAL_MSG, PLAY_NOW_EMOJI, guild=OTHER))
    local_play = await s.db.run(lambda c: play_now.latest_for_scope(c, s.card_id, s.local_id))
    assert local_play is not None and local_play.channel_id == FAR_LOCAL, "the local 🙋 posts in the local channel"

    # 5. Local 📅 on the local copy: the DM flow, and a session in the local channel.
    s.clock.now += 61
    await s.presses.on_reaction(reaction(LOCAL_SESSION, FAR_LOCAL, LOCAL_MSG, SESSION_EMOJI, guild=OTHER))
    local_channel = dm_channel(s, LOCAL_SESSION)
    await s.session_flow.on_dm_reaction(reaction(LOCAL_SESSION, local_channel,
                                         last_dm(s, LOCAL_SESSION, "Pick a time"), keycap(1), guild=None))
    await s.session_flow.on_dm_reaction(reaction(LOCAL_SESSION, local_channel,
                                         last_dm(s, LOCAL_SESSION, "to post it"), GOING, guild=None))
    local_sessions = await s.db.run(lambda c: sessions.upcoming_for_scope(c, s.card_id, s.local_id))
    assert [row.channel_id for row in local_sessions] == [FAR_LOCAL], "the local 📅 posts in the local channel"

    # 6. Local 🗳️ on the local copy: a poll in the local channel, ballots to the local holders only.
    s.clock.now += 61
    await s.presses.on_reaction(reaction(LOCAL_POLL, FAR_LOCAL, LOCAL_MSG, POLL_EMOJI, guild=OTHER))
    await s.poll_post.drain()
    local_poll = await s.db.run(lambda c: polls.open_for_scope(c, s.card_id, s.local_id))
    assert local_poll is not None and local_poll.channel_id == FAR_LOCAL
    assert local_poll.scope_sub_id == s.local_id
    assert set(s.state.dm_channels) >= {LOCAL_POLL, *LOCAL_HOLDERS}

    # Positive control: the local objects were written to the local channel.
    assert len(posts_to(s, FAR_LOCAL)) >= 3, "the local 🙋, 📅 and 🗳️ should each post in the local channel"

    # AC2.6, local side: none of the local objects' writes reach the home play channel or the games channel.
    assert len(posts_to(s, PLAY)) == home_play_before, "a local object reached the home play channel"
    local_writes = s.state.requests[local_mark:]
    assert not [r for r in local_writes if r.path.startswith(PLAY_MESSAGES)], "a local object wrote to the play channel"
    assert not [r for r in local_writes if r.path.startswith(GAMES_PATH)], "a local object wrote to the games channel"

    # The local objects' rows all name the local channel. No request in a home channel names their messages.
    local_rows = await s.db.run(lambda c: (
        [row.channel_id for row in sessions.list_for_scope(c, s.local_id)],
        [row.channel_id for row in polls.list_for_scope(c, s.local_id)],
        [row.channel_id for row in play_now.list_for_scope(c, s.local_id)]))
    assert all(channel == FAR_LOCAL for rows in local_rows for channel in rows)
    local_message_ids = {LOCAL_MSG, local_sessions[0].message_id, local_poll.message_id, local_play.message_id}
    for request in s.state.requests[local_mark:]:
        if request.path.startswith((PLAY_MESSAGES, GAMES_PATH)):
            assert not any(f"/messages/{mid}" in request.path for mid in local_message_ids), request.path
