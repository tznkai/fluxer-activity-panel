# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One scope isolation scenario against the fake Fluxer REST: a home scope, a local subscription (L) and a both
subscription (B) on one card, driven through the production components, then the card's subscriptions changed and
archived.

Home objects never reach a far channel except as mirrors in B, L and B objects never reach the home play channel, a
B-started object is never mirrored, every write is edit-safe (`allowed_mentions` parses nothing, a play-now post names
only its own scope's role), the scope and mirror log records carry no user id or location, and the role writes touch
only the stored roles. The test moves the clock, and the pacer never sleeps."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.roles import RoleCache
from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, mirrors, play_now, polls, sessions, subscriptions
from fluxer_panel.card_commands import CardContext, CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.dm_router import DmRouter, NonAdminLimiter
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.logs import FIELDS_ATTR, ID_KEYS, JsonFormatter
from fluxer_panel.mirror_post import MirrorPost
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poll_close import PollCloser
from fluxer_panel.poll_commands import PollCommands
from fluxer_panel.poll_flow import PollFlow
from fluxer_panel.poll_jobs import PollJobs
from fluxer_panel.poll_post import PollPost
from fluxer_panel.poller import Poller
from fluxer_panel.presses import PLAY_NOW_EMOJI, POLL_EMOJI, SESSION_EMOJI, Presses
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_commands import SessionCommands
from fluxer_panel.session_flow import SessionFlow
from fluxer_panel.session_jobs import SessionJobs
from fluxer_panel.session_post import SessionPost
from fluxer_panel.sources.registry import Sources
from tests.fakes import FakeFluxerState, make_rest_app
from tests.test_board_poller import GAMES, GAMES_MESSAGES, TOKEN, FakeClock, board_posts, card_posts

BOT_ID = "1900"
ADMIN = "1001"
ADMIN_DM = "9100"
GUILD = "2001"                   # the games community
OTHER = "2002"                   # the far community: L's and B's copies and channels
PLAY = "3200"                    # the home play-now channel (PLAY_NOW_CHANNEL_ID), in GUILD
FAR_L = "3004"                   # L's local channel, in OTHER
FAR_B = "3005"                   # B's copy channel, in OTHER: B's objects and the mirrors go here
LOCAL_COPY = "3006"              # L's copy channel, in OTHER: the copy's message lives here
LOCAL_MSG = "5004"               # L's copy message
B_MSG = "5005"                   # B's copy message
HOME_ROLE = "7001"               # the card's follow role in GUILD
ROLE_L = "7002"                  # L's follow role in OTHER
ROLE_B = "7003"                  # B's follow role in OTHER
HOME_PRESSER = "6001"            # presses on the card; starts the home objects
HOME_HOLDERS = ("6002", "6003")  # the home follow role's holders in GUILD
LOCAL_PRESSER = "6101"           # presses on L's copy
LOCAL_SESSION = "6102"           # proposes a session on L's copy
LOCAL_POLL = "6103"              # starts a poll on L's copy
LOCAL_HOLDERS = ("6104", "6105")  # L's follow role's holders in OTHER
FAR_VOTER = "6201"               # holds B's role in OTHER: a home ballot arrives by B, and reacts on B's mirror
B_PRESSER = "6202"               # presses on B's copy: starts B's own objects
SENTINEL_LOC = "SENTINEL LOC 7a1f"
GOING = "✅"
PLAY_MESSAGES = f"/api/v1/channels/{PLAY}/messages"
NOW = 1_800_000_000
ALL_USERS = (HOME_PRESSER, *HOME_HOLDERS, LOCAL_PRESSER, LOCAL_SESSION, LOCAL_POLL, *LOCAL_HOLDERS, FAR_VOTER,
             B_PRESSER)
STORED_ROLES = {HOME_ROLE, ROLE_L, ROLE_B}
# The channel each scope's play-now posts may mention its own role in.
ROLE_OF_CHANNEL = {PLAY: HOME_ROLE, FAR_L: ROLE_L, FAR_B: ROLE_B}
SCOPE_EVENT_PREFIXES = ("scope_", "mirror_", "subscription_")
SCOPE_LOG_FIELDS = ID_KEYS | {"sub_id", "mode", "count", "reason", "card_id", "kind", "mirror_id", "status", "outcome",
                              "session_id", "poll_id", "scope_sub_id", "via_sub_id"}
OBJECT_ROWS_SQL = ("SELECT 'session', id, scope_sub_id, channel_id, message_id FROM sessions UNION ALL "
                   "SELECT 'poll', id, scope_sub_id, channel_id, message_id FROM polls UNION ALL "
                   "SELECT 'play_now', id, scope_sub_id, channel_id, message_id FROM play_now")
MIRROR_ROWS_SQL = (
    "SELECT CASE WHEN session_id IS NOT NULL THEN 'session' WHEN poll_id IS NOT NULL THEN 'poll' ELSE 'play_now' END, "
    "COALESCE(session_id, poll_id, play_now_id), sub_id, channel_id, message_id FROM mirror_message ORDER BY id")


def keycap(n: int) -> str:
    return f"{n}️⃣"


class Scenario:
    """Every component under test, wired as `__main__.run` wires them, on one clock and one CardLocks."""

    def __init__(self, **parts: Any) -> None:
        self.__dict__.update(parts)


def _member(state: FakeFluxerState, guild: str, user: str, role: str) -> None:
    state.members[(guild, user)] = {"user": {"username": "member", "bot": False}, "roles": [role]}


@pytest.fixture
async def scenario(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[Scenario]:
    database = Database.open(tmp_path / "panel.db")
    state = FakeFluxerState(bot_user_id=BOT_ID, next_message_id=9001)
    state.guilds[GUILD] = {"id": GUILD}
    state.guilds[OTHER] = {"id": OTHER}
    state.channels[GAMES] = {"id": GAMES, "name": "games", "type": 0, "guild_id": GUILD}
    state.channels[PLAY] = {"id": PLAY, "name": "play-now", "type": 0, "guild_id": GUILD}
    state.channels[FAR_L] = {"id": FAR_L, "name": "local", "type": 0, "guild_id": OTHER}
    state.channels[FAR_B] = {"id": FAR_B, "name": "both", "type": 0, "guild_id": OTHER}
    state.channels[LOCAL_COPY] = {"id": LOCAL_COPY, "name": "local-copy", "type": 0, "guild_id": OTHER}
    state.messages[(LOCAL_COPY, LOCAL_MSG)] = {"embeds": []}
    state.messages[(FAR_B, B_MSG)] = {"embeds": []}
    for user in HOME_HOLDERS:
        _member(state, GUILD, user, HOME_ROLE)
    for user in LOCAL_HOLDERS:
        _member(state, OTHER, user, ROLE_L)
    for user in (FAR_VOTER, B_PRESSER):
        _member(state, OTHER, user, ROLE_B)
    try:
        async with aiohttp.ClientSession() as client:
            server = await aiohttp_server(make_rest_app(state))
            rest = FluxerREST(client, f"{str(server.make_url('/')).rstrip('/')}/api", TOKEN)
            clock = FakeClock()
            clock.now = NOW
            config = Config.from_env({
                "FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
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
            mirror_post = MirrorPost(db=database, rest=rest, config=config)
            session_post = SessionPost(db=database, rest=rest, config=config, pacer=pacer, backoff=backoff,
                                       mirrors=mirror_post)
            session_flow = SessionFlow(db=database, rest=rest, config=config, locks=locks,
                                       session_post=session_post, games_guild=games_guild, pacer=pacer,
                                       identity=identity, backoff=backoff, limiter=limiter, clock=clock)
            poll_post = PollPost(db=database, rest=rest, config=config, pacer=pacer, identity=identity,
                                 backoff=backoff, edit_delay=0, mirror_post=mirror_post)
            poll_flow = PollFlow(db=database, rest=rest, config=config, locks=locks, poll_post=poll_post,
                                 backoff=backoff, games_guild=games_guild, clock=clock, session_flow=session_flow,
                                 mirror_post=mirror_post)
            poll_closer = PollCloser(db=database, locks=locks, poll_post=poll_post, clock=clock)
            poll_post.set_on_gone(poll_closer.on_message_gone)
            poll_commands = PollCommands(db=database, rest=rest, config=config, locks=locks, poll_post=poll_post,
                                         poll_flow=poll_flow, closer=poll_closer, clock=clock)
            poll_jobs = PollJobs(db=database, rest=rest, config=config, closer=poll_closer, backoff=backoff,
                                 clock=clock)
            session_commands = SessionCommands(db=database, rest=rest, config=config, locks=locks,
                                               session_post=session_post, clock=clock)
            session_post.set_on_gone(session_commands.on_message_gone)
            session_jobs = SessionJobs(db=database, rest=rest, config=config, locks=locks, session_post=session_post,
                                       session_flow=session_flow, status_of=lambda _card_id: None, clock=clock,
                                       backoff=backoff)
            interactions = Interactions(db=database, rest=rest, config=config, locks=locks, games_guild=games_guild,
                                        seeder=seeder, pacer=pacer, identity=identity, roles=RoleCache(rest),
                                        clock=clock, session_post=session_post, backoff=backoff,
                                        session_flow=session_flow, poll_flow=poll_flow, mirror_post=mirror_post)
            presses = Presses(db=database, rest=rest, config=config, handler=interactions, games_guild=games_guild,
                              pacer=pacer, identity=identity, clock=clock, backoff=backoff)
            poller = Poller(db=database, rest=rest, sources=Sources(managers={}, probes={}), config=config,
                            locks=locks, clock=clock, games_guild=games_guild, seeder=seeder)
            poller.set_session_jobs(session_jobs)
            poller.set_poll_jobs(poll_jobs)
            poller.set_interactions(interactions)
            card_ctx = CardContext(db=database, rest=rest, config=config, sources=Sources(managers={}, probes={}),
                                   locks=locks, roles=RoleCache(rest), games_guild=games_guild, seeder=seeder,
                                   identity=identity, session_commands=session_commands, poll_closer=poll_closer,
                                   play_now_ender=interactions)
            router = DmRouter(config=config, rest=rest, card_ctx=card_ctx, limiter=limiter, session_flow=session_flow,
                              poll_flow=poll_flow, poll_commands=poll_commands.handle,
                              session_commands=session_commands.handle)

            def make_card(conn: Any) -> tuple[int, int, int]:
                card_id = cards.create_card(conn, slug="mc", title="Minecraft", author_id=ADMIN, now=1)
                cards.set_state(conn, card_id, "live", 1)
                cards.set_seats(conn, card_id, 3)
                cards.set_location(conn, card_id, cards.Location(text=SENTINEL_LOC))
                cards.set_follow_role(conn, card_id, GUILD, HOME_ROLE)
                local = subscriptions.add(conn, card_id, OTHER, LOCAL_COPY)
                subscriptions.set_message(conn, card_id, LOCAL_COPY, LOCAL_MSG, "a" * 64)
                assert subscriptions.set_mode_if(conn, local, "redirect", "local")
                subscriptions.set_local_channel(conn, local, FAR_L)
                cards.set_subscription_role(conn, local, ROLE_L)
                both = subscriptions.add(conn, card_id, OTHER, FAR_B)
                subscriptions.set_message(conn, card_id, FAR_B, B_MSG, "b" * 64)
                assert subscriptions.set_mode_if(conn, both, "redirect", "both")
                cards.set_subscription_role(conn, both, ROLE_B)
                return card_id, local, both

            card_id, local_id, both_id = await database.run(make_card)
            yield Scenario(db=database, state=state, rest=rest, clock=clock, config=config, poller=poller,
                           presses=presses, router=router, poll_post=poll_post, session_flow=session_flow,
                           poll_flow=poll_flow, poll_closer=poll_closer, card_ctx=card_ctx, card_id=card_id,
                           local_id=local_id, both_id=both_id)
    finally:
        database.close()


@pytest.fixture
def captured(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


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


async def command(s: Scenario, content: str) -> None:
    """An admin command in the admin's DM, through the router."""
    await s.router.route(events.MessageCreate(message_id="7002", channel_id=ADMIN_DM, guild_id=None,
                                              author_id=ADMIN, author_is_bot=False, member_role_ids=None,
                                              content=content))


def message_id_of(path: str) -> str | None:
    """The message id a path names after `/messages/`, or None for a path with no message."""
    if "/messages/" not in path:
        return None
    return path.split("/messages/", 1)[1].split("/", 1)[0]


def writes_since(s: Scenario, mark: int, channel: str) -> list[Any]:
    """Every POST, PATCH or PUT the fake received under the channel since request `mark`."""
    prefix = f"/api/v1/channels/{channel}/"
    return [r for r in s.state.requests[mark:]
            if r.method in ("POST", "PATCH", "PUT") and r.path.startswith(prefix)]


def writes_outside(s: Scenario, mark: int, channel: str, own: set[str]) -> list[str]:
    """The non-POST writes under the channel since `mark` that do not target one of the `own` messages (or their
    reactions). A POST is never its own: a send under the channel is what the caller counts."""
    leaks = []
    for request in writes_since(s, mark, channel):
        if request.method == "POST" or message_id_of(request.path) in own:
            continue
        leaks.append(f"{request.method} {request.path}")
    return leaks


def posts_to(s: Scenario, channel: str) -> list[Any]:
    path = f"/api/v1/channels/{channel}/messages"
    return [r for r in s.state.requests if r.method == "POST" and r.path == path]


def object_rows(conn: Any) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in conn.execute(OBJECT_ROWS_SQL).fetchall()]


def mirror_rows(conn: Any) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in conn.execute(MIRROR_ROWS_SQL).fetchall()]


# The scenario ----------------------------------------------------------------------------------------------

async def test_home_local_and_both_scopes_stay_apart_and_their_writes_and_logs_are_safe(
        scenario: Scenario, captured: pytest.LogCaptureFixture) -> None:
    s = scenario

    # The card and the copies' messages exist; the poller posts the card and the board to the games channel.
    await s.poller.tick()
    card = await s.db.run(lambda c: cards.get_by_id(c, s.card_id))
    assert card is not None and card.message_id is not None
    card_message = card.message_id

    # 1. Home 🙋, 📅 and 🗳️ on the card. The home 🗳️ reaches the far holder of B by B's route, and the mirrors go to B.
    home_mark = len(s.state.requests)
    await s.presses.on_reaction(reaction(HOME_PRESSER, GAMES, card_message, PLAY_NOW_EMOJI, guild=GUILD))
    home_play = await s.db.run(lambda c: play_now.live_for_card(c, s.card_id))
    assert [row.channel_id for row in home_play] == [PLAY], "the home 🙋 posts in the home play channel"

    s.clock.now += 61
    await s.presses.on_reaction(reaction(HOME_PRESSER, GAMES, card_message, SESSION_EMOJI, guild=GUILD))
    channel = dm_channel(s, HOME_PRESSER)
    await s.session_flow.on_dm_reaction(reaction(HOME_PRESSER, channel, last_dm(s, HOME_PRESSER, "Pick a time"),
                                                keycap(1), guild=None))
    await s.session_flow.on_dm_reaction(reaction(HOME_PRESSER, channel, last_dm(s, HOME_PRESSER, "to post it"),
                                                 GOING, guild=None))
    home_session_rows = await s.db.run(lambda c: sessions.upcoming_for_scope(c, s.card_id, None))
    assert [row.channel_id for row in home_session_rows] == [PLAY], "the home 📅 posts in the home play channel"
    home_session = home_session_rows[0].id

    s.clock.now += 61
    await s.presses.on_reaction(reaction(HOME_PRESSER, GAMES, card_message, POLL_EMOJI, guild=GUILD))
    await s.poll_post.drain()
    home_poll = await s.db.run(lambda c: polls.open_for_scope(c, s.card_id, None))
    assert home_poll is not None and home_poll.channel_id == PLAY and home_poll.scope_sub_id is None
    ballots = await s.db.run(lambda c: polls.ballots(c, home_poll.id))
    # Positive control: the far holder of B gets the home ballot by B's route.
    assert {b.user_id: b.via_sub_id for b in ballots}.get(FAR_VOTER) == s.both_id
    await say(s, FAR_VOTER, "a")
    # The home poll stays open: closing it would move the clock past the session's start (step 3 needs it upcoming).
    # The archive in step 5 closes it through the same close path.
    assert home_poll.closes_at > s.clock.now + 3600

    # The home objects were mirrored into B: one mirror per home session, poll and play-now post, and nothing else.
    home_mirrors = await s.db.run(lambda c: mirror_rows(c))
    assert sorted(kind for kind, _, _, _, _ in home_mirrors) == ["play_now", "poll", "session"]
    assert {sub for _, _, sub, _, _ in home_mirrors} == {s.both_id}
    mirror_message_ids = {message for _, _, _, _, message in home_mirrors}

    # AC5.1, home side: the home objects write into the home play channel and the far ones only as their mirrors.
    assert len(posts_to(s, PLAY)) >= 3, "the home 🙋, 📅 and 🗳️ should each post in the home play channel"
    assert writes_since(s, home_mark, FAR_L) == [], "a home object reached the local channel"
    assert writes_outside(s, home_mark, LOCAL_COPY, {LOCAL_MSG}) == [], "a home object reached L's copy"
    assert posts_to(s, LOCAL_COPY) == [], "a home object was posted into L's copy channel"
    far_b_posts = [r for r in writes_since(s, home_mark, FAR_B) if r.method == "POST"]
    assert len(far_b_posts) == 3, "each home object should be mirrored into B exactly once"
    assert writes_outside(s, home_mark, FAR_B, {B_MSG, *mirror_message_ids}) == [], "a home write reached B's copy"

    # 2. Local 🙋, 📅 and 🗳️ on L's copy, then B's 🙋, 📅 and 🗳️ on B's copy.
    local_mark = len(s.state.requests)
    play_before = len(posts_to(s, PLAY))
    s.clock.now += 61
    await s.presses.on_reaction(reaction(LOCAL_PRESSER, LOCAL_COPY, LOCAL_MSG, PLAY_NOW_EMOJI, guild=OTHER))
    local_play = await s.db.run(lambda c: play_now.latest_for_scope(c, s.card_id, s.local_id))
    assert local_play is not None and local_play.channel_id == FAR_L, "L's 🙋 posts in L's channel"

    s.clock.now += 61
    await s.presses.on_reaction(reaction(LOCAL_SESSION, LOCAL_COPY, LOCAL_MSG, SESSION_EMOJI, guild=OTHER))
    local_channel = dm_channel(s, LOCAL_SESSION)
    await s.session_flow.on_dm_reaction(reaction(LOCAL_SESSION, local_channel,
                                                 last_dm(s, LOCAL_SESSION, "Pick a time"), keycap(1), guild=None))
    await s.session_flow.on_dm_reaction(reaction(LOCAL_SESSION, local_channel,
                                                 last_dm(s, LOCAL_SESSION, "to post it"), GOING, guild=None))
    local_sessions = await s.db.run(lambda c: sessions.upcoming_for_scope(c, s.card_id, s.local_id))
    assert [row.channel_id for row in local_sessions] == [FAR_L], "L's 📅 posts in L's channel"

    s.clock.now += 61
    await s.presses.on_reaction(reaction(LOCAL_POLL, LOCAL_COPY, LOCAL_MSG, POLL_EMOJI, guild=OTHER))
    await s.poll_post.drain()
    local_poll = await s.db.run(lambda c: polls.open_for_scope(c, s.card_id, s.local_id))
    assert local_poll is not None and local_poll.channel_id == FAR_L and local_poll.scope_sub_id == s.local_id

    s.clock.now += 61
    await s.presses.on_reaction(reaction(B_PRESSER, FAR_B, B_MSG, PLAY_NOW_EMOJI, guild=OTHER))
    both_play = await s.db.run(lambda c: play_now.latest_for_scope(c, s.card_id, s.both_id))
    assert both_play is not None and both_play.channel_id == FAR_B, "B's 🙋 posts in B's channel"

    s.clock.now += 61
    await s.presses.on_reaction(reaction(B_PRESSER, FAR_B, B_MSG, SESSION_EMOJI, guild=OTHER))
    both_channel = dm_channel(s, B_PRESSER)
    await s.session_flow.on_dm_reaction(reaction(B_PRESSER, both_channel,
                                                 last_dm(s, B_PRESSER, "Pick a time"), keycap(1), guild=None))
    await s.session_flow.on_dm_reaction(reaction(B_PRESSER, both_channel,
                                                 last_dm(s, B_PRESSER, "to post it"), GOING, guild=None))
    both_sessions = await s.db.run(lambda c: sessions.upcoming_for_scope(c, s.card_id, s.both_id))
    assert [row.channel_id for row in both_sessions] == [FAR_B], "B's 📅 posts in B's channel"

    s.clock.now += 61
    await s.presses.on_reaction(reaction(B_PRESSER, FAR_B, B_MSG, POLL_EMOJI, guild=OTHER))
    await s.poll_post.drain()
    both_poll = await s.db.run(lambda c: polls.open_for_scope(c, s.card_id, s.both_id))
    assert both_poll is not None and both_poll.channel_id == FAR_B and both_poll.scope_sub_id == s.both_id

    # Positive controls: L's and B's objects were written to their own channels.
    assert len(posts_to(s, FAR_L)) >= 3, "L's 🙋, 📅 and 🗳️ should each post in L's channel"
    assert len(posts_to(s, FAR_B)) >= 6, "B's objects and the mirrors should post in B's channel"

    # AC5.1, local and both side: none of their writes reach the home play channel or the games channel, and a B
    # object is never mirrored: the mirror rows stay the three home ones.
    assert len(posts_to(s, PLAY)) == play_before, "an L or B object reached the home play channel"
    local_writes = s.state.requests[local_mark:]
    assert not [r for r in local_writes if r.path.startswith(f"/api/v1/channels/{GAMES}/")], "a write hit games"
    assert await s.db.run(lambda c: mirror_rows(c)) == home_mirrors, "a B-started object was mirrored"
    assert len(await s.db.run(lambda c: mirrors.for_sub(c, s.both_id))) == 3

    # 3. ✅ on the home session's mirror in B, and Me Too on the home play-now mirror in B.
    session_mirror = await s.db.run(lambda c: mirrors.get(c, "session", home_session, s.both_id))
    assert session_mirror is not None
    s.clock.now += 61
    await s.presses.on_reaction(reaction(FAR_VOTER, FAR_B, session_mirror.message_id, GOING, guild=OTHER))
    rsvps = await s.db.run(lambda c: sessions.rsvps_of(c, home_session))
    assert rsvps.get(FAR_VOTER, ("",))[0] == "going", "the ✅ on B's mirror is the home session's RSVP"

    play_mirror = await s.db.run(lambda c: mirrors.for_object(c, "play_now", home_play[0].id))
    assert [row.sub_id for row in play_mirror] == [s.both_id]
    s.clock.now += 61
    await s.presses.on_reaction(reaction(FAR_VOTER, FAR_B, play_mirror[0].message_id, PLAY_NOW_EMOJI, guild=OTHER))

    # Every object and mirror that exists now, with the message it lives in. The consistency check below uses it.
    objects = await s.db.run(object_rows)
    message_channel: dict[str, str] = {}
    message_scope: dict[str, int | None] = {}
    for _, _, scope_sub, channel, message in objects:
        if message is not None:
            message_channel[message] = channel
            message_scope[message] = scope_sub
    for _, _, _, channel, message in await s.db.run(mirror_rows):
        message_channel[message] = channel
        message_scope[message] = None   # a mirror is a home object's copy: it is written in B's channel only
    home_objects = {m for m, sub in message_scope.items() if sub is None and m not in mirror_message_ids}
    assert home_objects and any(sub is not None for sub in message_scope.values()), \
        "the scenario should hold home and scope objects"

    # 4. Subscribe B back to redirect (B's scope ends), then unsubscribe L.
    await command(s, f"!card subscribe mc {OTHER} {FAR_B} mode=redirect")
    await command(s, f"!card unsubscribe mc {OTHER} {LOCAL_COPY}")

    # 5. Archive the card.
    await command(s, "!card archive mc")
    archived = await s.db.run(lambda c: cards.get_by_slug(c, "mc"))
    assert archived is not None and archived.state == "archived"

    # AC5.1, consistency over every request: an object's message is only ever written in its own channel, and a
    # mirror only in B's channel. A home object therefore never reaches L's or B's channel, and an L or B object never
    # reaches the home play channel.
    for request in s.state.requests:
        message = message_id_of(request.path)
        if message is None or message not in message_channel:
            continue
        channel = request.path.split("/channels/", 1)[1].split("/", 1)[0]
        assert channel == message_channel[message], f"{request.method} {request.path}: an object outside its channel"

    # AC5.1, the games channel takes only the card and board posts, and no write there names an object's message.
    game_posts = [r for r in s.state.requests if r.method == "POST" and r.path == GAMES_MESSAGES]
    assert len(game_posts) == len(card_posts(s.state)) + len(board_posts(s.state))
    for request in s.state.requests:
        if request.method in ("PATCH", "PUT", "DELETE") and request.path.startswith(f"/api/v1/channels/{GAMES}/"):
            assert message_id_of(request.path) not in message_channel, f"{request.method} {request.path}: an object"

    # AC5.1 (both): B's objects were never mirrored, and once B is redirect again no mirror of B remains.
    assert await s.db.run(lambda c: mirrors.for_sub(c, s.both_id)) == []

    # The write payloads. An edit never parses a mention. A post under a play, far or DM channel parses none, except
    # a play-now post, which may name only its own scope's role.
    dm_channels = set(s.state.dm_channels.values())
    for request in s.state.requests:
        if request.method == "PATCH":
            assert request.payload is not None
            assert request.payload.get("allowed_mentions") == {"parse": []}, f"PATCH {request.path} mentions"
            continue
        if request.method != "POST" or not request.path.endswith("/messages"):
            continue
        channel = request.path.split("/channels/", 1)[1].split("/", 1)[0]
        if channel not in ROLE_OF_CHANNEL and channel not in dm_channels:
            continue
        allowed = request.payload.get("allowed_mentions")
        assert allowed is not None, f"POST {request.path} carries no allowed_mentions"
        if "roles" in allowed:
            assert allowed["roles"] == [ROLE_OF_CHANNEL[channel]], f"POST {request.path} mentions another role"
        else:
            assert allowed.get("parse") == [], f"POST {request.path} parses mentions"

    # AC5.3: every scope, mirror and subscription record carries ids and reasons only, and no user or location.
    checked = 0
    for record in captured.records:
        if not record.getMessage().startswith(SCOPE_EVENT_PREFIXES):
            continue
        fields = getattr(record, FIELDS_ATTR, {})
        assert set(fields) <= SCOPE_LOG_FIELDS, f"{record.getMessage()} logs {sorted(set(fields) - SCOPE_LOG_FIELDS)}"
        assert "user_id" not in fields, record.getMessage()
        rendered = JsonFormatter().format(record)
        for user in ALL_USERS:
            assert user not in rendered, record.getMessage()
        assert SENTINEL_LOC not in rendered, record.getMessage()
        checked += 1
    assert checked > 0, "the scenario should produce scope, mirror or subscription records"

    # AC5.2: the role writes touch only the stored roles (the home role, L's and B's), and the bot never creates one.
    role_writes = [r for r in s.state.requests
                   if (r.method in ("PUT", "DELETE") and "/roles/" in r.path)
                   or (r.method == "PATCH" and "/roles/" in r.path)]
    for request in role_writes:
        role = request.path.rsplit("/roles/", 1)[1].split("/", 1)[0]
        assert role in STORED_ROLES, f"{request.method} {request.path} writes a role that is not stored"
    assert not [r for r in s.state.requests if r.method == "POST" and r.path.endswith("/roles")]
