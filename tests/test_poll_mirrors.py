# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Poll fan-out into both subscriptions (AC3.5, AC3.6, AC3.7 and far-origin links): a home poll reaches the holders of
each both copy's role, its message is mirrored once per both copy, a far ballot, reminder and closed notice link only to
that copy's mirror, a scope poll started by a both copy is never mirrored, and a failed member list in a far community
does not fail the home poll. Runs the production PollFlow, PollPost, PollCloser, PollJobs, SessionFlow and MirrorPost
against the fake REST server and a SQLite file."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, mirrors, polls, sessions, subscriptions
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.dm_router import NonAdminLimiter
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.mirror_backfill import MirrorBackfill
from fluxer_panel.mirror_post import MirrorPost
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poll_close import PollCloser
from fluxer_panel.poll_flow import PollFlow
from fluxer_panel.poll_jobs import PollJobs
from fluxer_panel.poll_post import PollPost
from fluxer_panel.session_flow import SessionFlow
from fluxer_panel.session_post import SessionPost
from fluxer_panel.text import POLL_SESSION_PLAIN
from tests.fakes import FakeFluxerState, make_rest_app

TOKEN = "SENTINEL-TOKEN-poll-mirrors"
BOT = "1900"
ADMIN = "1001"
STARTER = "6001"
HOME_H1 = "6101"          # a holder of the home role, and of B's role in the far community
HOME_H2 = "6102"          # a holder of the home role only
FAR_F1 = "6201"           # a holder of B's role only
LOCAL_L1 = "6301"         # a holder of local subscription L's role only
GUILD = "2001"            # the games community
OTHER = "4000"            # the far community of both copy B and local copy L
GAMES = "3100"            # the games channel, in GUILD
PLAY = "3200"             # the home poll and session channel, in GUILD
FAR_B = "4102"            # both copy B's channel (its scope target)
FAR_L = "4103"            # local copy L's channel
CARD_MSG = "5101"
ROLE = "7001"             # the home follow role
ROLE_B = "7101"           # B's follow role, in OTHER
ROLE_L = "7102"           # L's follow role, in OTHER
HASH = "a" * 64
URL = "https://fluxer.invalid"
NOW = 1_800_000_000
CLOSES = NOW + 7200
ONE = CLOSES + 7200       # usable at close, two hours away
TWO = CLOSES + 10800
HALFWAY = NOW + 3600
DAY = 86400
TYPED = "2027-03-01 14:00"


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def config_for() -> Config:
    return Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "ADMIN_USER_IDS": ADMIN,
                            "GAMES_CHANNEL_ID": GAMES, "PLAY_NOW_CHANNEL_ID": PLAY,
                            "FLUXER_INSTANCE_URL": URL, "POLL_INTERVAL_SECONDS": "3600",
                            "DM_BACKOFF_SECONDS": "300", "POLL_LENGTH_SECONDS": str(CLOSES - NOW)})


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
    mirror: MirrorPost
    session_post: SessionPost
    card_id: int
    sub_b: int
    sub_l: int


def _card(conn: Any) -> tuple[int, int, int]:
    card_id = cards.create_card(conn, slug="lobby", title="Lobby", author_id=ADMIN, now=1)
    cards.set_state(conn, card_id, "live", 1)
    cards.set_message(conn, card_id, GAMES, CARD_MSG, HASH)
    cards.set_follow_role(conn, card_id, GUILD, ROLE)
    sub_b = subscriptions.add(conn, card_id, OTHER, FAR_B)
    assert subscriptions.set_mode_if(conn, sub_b, "redirect", "both")
    cards.set_subscription_role(conn, sub_b, ROLE_B)
    sub_l = subscriptions.add(conn, card_id, OTHER, FAR_L)
    assert subscriptions.set_mode_if(conn, sub_l, "redirect", "local")
    cards.set_subscription_role(conn, sub_l, ROLE_L)
    return card_id, sub_b, sub_l


def _member(state: FakeFluxerState, guild: str, user: str, *roles: str) -> None:
    state.members[(guild, user)] = {"user": {"id": user}, "roles": list(roles)}


@pytest.fixture
async def world(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[World]:
    database = Database.open(tmp_path / "poll_mirrors.db")
    state = FakeFluxerState(bot_user_id=BOT, next_message_id=9001)
    state.guilds.update({GUILD: {"id": GUILD}, OTHER: {"id": OTHER}})
    state.channels.update({
        GAMES: {"id": GAMES, "guild_id": GUILD, "type": 0},
        PLAY: {"id": PLAY, "guild_id": GUILD, "type": 0},
        FAR_B: {"id": FAR_B, "guild_id": OTHER, "type": 0},
        FAR_L: {"id": FAR_L, "guild_id": OTHER, "type": 0},
    })
    state.messages[(GAMES, CARD_MSG)] = {"embeds": []}
    _member(state, GUILD, HOME_H1, ROLE)
    _member(state, GUILD, HOME_H2, ROLE)
    _member(state, OTHER, HOME_H1, ROLE_B)
    _member(state, OTHER, FAR_F1, ROLE_B)
    _member(state, OTHER, LOCAL_L1, ROLE_L)
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    config = config_for()
    clock = Clock(float(NOW))
    post: PollPost | None = None
    try:
        async with aiohttp.ClientSession() as client:
            rest = FluxerREST(client, f"{base}/api", TOKEN)
            pacer = ChannelPacer(max_calls=1000)
            identity = BotIdentity(user_id=BOT)
            games_guild = GamesGuild(rest, config)
            locks = CardLocks()
            backoff = DmBackoff(config.dm_backoff_seconds, clock=clock)
            mirror = MirrorPost(db=database, rest=rest, config=config)
            post = PollPost(db=database, rest=rest, config=config, pacer=pacer, identity=identity, backoff=backoff,
                            edit_delay=0, mirror_post=mirror)
            closer = PollCloser(db=database, locks=locks, poll_post=post, clock=clock)
            session_post = SessionPost(db=database, rest=rest, config=config, pacer=pacer, backoff=backoff,
                                       mirrors=mirror)
            session_flow = SessionFlow(db=database, rest=rest, config=config, locks=locks,
                                       session_post=session_post, games_guild=games_guild, pacer=pacer,
                                       identity=identity, backoff=backoff,
                                       limiter=NonAdminLimiter(config, clock=clock), clock=clock,
                                       kick=lambda: None)
            flow = PollFlow(db=database, rest=rest, config=config, locks=locks, poll_post=post, backoff=backoff,
                            games_guild=games_guild, clock=clock, kick=lambda: None, session_flow=session_flow,
                            mirror_post=mirror)
            jobs = PollJobs(db=database, rest=rest, config=config, closer=closer, backoff=backoff, clock=clock,
                            kick=lambda: None)
            card_id, sub_b, sub_l = await database.run(_card)
            yield World(db=database, state=state, rest=rest, config=config, clock=clock, post=post, closer=closer,
                        jobs=jobs, flow=flow, mirror=mirror, session_post=session_post, card_id=card_id,
                        sub_b=sub_b, sub_l=sub_l)
    finally:
        if post is not None:
            await post.aclose(timeout=0.05)
        database.close()


# Helpers ----------------------------------------------------------------------------------------------------

async def open_home(world: World, options: int = 2) -> int:
    """Opens the home poll with `options` offered times, one hour apart from ONE."""
    times = [(ONE + 3600 * i, "offered") for i in range(options)]
    poll_id = await world.flow._open_poll(world.card_id, starter_id=STARTER, presser_id=STARTER, kind="approval",
                                          parent_poll_id=None, options=times)
    assert poll_id is not None
    return poll_id


async def ballot(world: World, poll_id: int, user: str) -> polls.Ballot:
    found = await world.db.run(lambda c: polls.ballot(c, poll_id, user))
    assert found is not None
    return found


async def mirror_of(world: World, poll_id: int, sub_id: int) -> mirrors.Mirror | None:
    return await world.db.run(lambda c: mirrors.get(c, "poll", poll_id, sub_id))


async def ballot_text(world: World, poll_id: int, user: str) -> str:
    found = await ballot(world, poll_id, user)
    assert found.dm_channel_id is not None and found.dm_message_id is not None
    return str(world.state.messages[(found.dm_channel_id, found.dm_message_id)]["content"])


async def home_ids(world: World, poll_id: int) -> tuple[str, str]:
    """The home poll's (channel id, message id)."""
    poll = await world.db.run(lambda c: polls.get(c, poll_id))
    assert poll is not None
    return poll.channel_id, poll.message_id


def post_count(world: World, channel: str) -> int:
    path = f"/api/v1/channels/{channel}/messages"
    return sum(1 for r in world.state.requests if r.method == "POST" and r.path == path)


def dm_posts(world: World, user: str) -> list[str]:
    channel = world.state.dm_channels.get(user)
    if channel is None:
        return []
    path = f"/api/v1/channels/{channel}/messages"
    return [str(r.payload.get("content", "")) for r in world.state.requests if r.method == "POST" and r.path == path]


def last_embed_description(world: World, channel: str, message: str) -> str:
    embeds = world.state.messages[(channel, message)]["embeds"]
    return str(embeds[0].get("description", ""))


def leaves_out(text: str, *forbidden: str) -> None:
    """The text names none of the forbidden home ids."""
    for value in forbidden:
        assert value not in text


# AC3.5: the home poll reaches the far holders, and its message is mirrored once per both copy ------------------

async def test_a_home_poll_reaches_far_holders_with_their_route_and_is_mirrored_to_both_only(
        world: World) -> None:
    poll_id = await open_home(world)
    recipients = {b.user_id: b.via_sub_id for b in await world.db.run(lambda c: polls.ballots(c, poll_id))}
    assert set(recipients) == {STARTER, HOME_H1, HOME_H2, FAR_F1}
    assert recipients[HOME_H1] is None
    assert recipients[FAR_F1] == world.sub_b
    assert LOCAL_L1 not in recipients
    row = await mirror_of(world, poll_id, world.sub_b)
    assert row is not None and row.channel_id == FAR_B
    assert await mirror_of(world, poll_id, world.sub_l) is None
    assert post_count(world, FAR_L) == 0
    assert post_count(world, FAR_B) == 1
    [sent] = [r for r in world.state.requests if r.method == "POST" and r.path == f"/api/v1/channels/{FAR_B}/messages"]
    assert sent.payload["allowed_mentions"] == {"parse": []}


async def test_a_far_vote_counts_in_the_home_tally_and_the_mirror_shows_the_same_counts(world: World) -> None:
    poll_id = await open_home(world)
    channel = world.state.dm_channels[FAR_F1]
    await world.flow.vote(FAR_F1, poll_id, f"a, {TYPED}", reply_channel_id=channel)
    await world.post.flush(poll_id)
    row = await mirror_of(world, poll_id, world.sub_b)
    assert row is not None
    home_channel, home_message = await home_ids(world, poll_id)
    assert last_embed_description(world, row.channel_id, row.message_id) == last_embed_description(
        world, home_channel, home_message)
    picks = await world.db.run(lambda c: polls.picks(c, poll_id, FAR_F1))
    assert len(picks) == 2


# AC3.6: far ballots, reminders and closed notices link only to their mirror --------------------------------------

async def test_a_far_ballot_links_to_its_mirror_and_a_home_ballot_to_the_home_poll(world: World) -> None:
    # Sixty options make the open ballot too long for its list, so it carries the poll link ("N more options in ...").
    poll_id = await open_home(world, options=60)
    await world.post.send_ballot(poll_id, FAR_F1)
    await world.post.send_ballot(poll_id, HOME_H1)
    row = await mirror_of(world, poll_id, world.sub_b)
    assert row is not None
    home_channel, home_message = await home_ids(world, poll_id)
    far_text = await ballot_text(world, poll_id, FAR_F1)
    assert f"/{OTHER}/{FAR_B}/{row.message_id}" in far_text
    leaves_out(far_text, home_message, home_channel, GUILD)
    home_text = await ballot_text(world, poll_id, HOME_H1)
    assert f"/{GUILD}/{PLAY}/{home_message}" in home_text


async def test_a_far_reminder_and_the_closed_notice_link_only_to_the_mirror(world: World) -> None:
    poll_id = await open_home(world)
    await world.post.send_ballot(poll_id, FAR_F1)
    await world.post.send_ballot(poll_id, HOME_H1)
    row = await mirror_of(world, poll_id, world.sub_b)
    assert row is not None
    home_channel, home_message = await home_ids(world, poll_id)
    world.clock.now = HALFWAY
    await world.jobs.run()
    [reminder] = [text for text in dm_posts(world, FAR_F1) if "time poll" in text]
    assert f"/{OTHER}/{FAR_B}/{row.message_id}" in reminder
    leaves_out(reminder, home_message, home_channel)
    world.clock.now = CLOSES + 1
    await world.closer.close(poll_id)
    closed = await ballot_text(world, poll_id, FAR_F1)
    assert f"/{OTHER}/{FAR_B}/{row.message_id}" in closed
    leaves_out(closed, home_message, home_channel)


async def test_a_far_ballot_has_no_link_once_its_mirror_is_gone(world: World) -> None:
    poll_id = await open_home(world)
    await world.post.send_ballot(poll_id, FAR_F1)
    row = await mirror_of(world, poll_id, world.sub_b)
    assert row is not None
    home_channel, home_message = await home_ids(world, poll_id)
    assert await world.mirror.delete(row)
    world.clock.now = CLOSES + 1
    await world.closer.close(poll_id)
    text = await ballot_text(world, poll_id, FAR_F1)
    leaves_out(text, row.message_id, home_message, home_channel)
    assert "https://" not in text


# The scheduled DM links the voter's own session mirror ------------------------------------------------------

async def test_the_scheduled_dm_links_the_far_voters_session_mirror_and_the_home_voters_the_home_session(
        world: World) -> None:
    poll_id = await open_home(world)
    await world.flow.vote(FAR_F1, poll_id, "a", reply_channel_id=await _dm(world, FAR_F1))
    await world.flow.vote(HOME_H1, poll_id, "a", reply_channel_id=await _dm(world, HOME_H1))
    world.clock.now = CLOSES + 1
    await world.closer.close(poll_id)
    await world.flow._schedule(poll_id, presser=STARTER)
    session_id = await world.db.run(lambda c: await_poll_session(c, poll_id))
    session = await world.db.run(lambda c: sessions.get(c, session_id))
    assert session is not None
    session_mirror = await world.db.run(lambda c: mirrors.get(c, "session", session.id, world.sub_b))
    assert session_mirror is not None
    far = [t for t in dm_posts(world, FAR_F1) if "scheduled" in t]
    home = [t for t in dm_posts(world, HOME_H1) if "scheduled" in t]
    assert len(far) == 1 and len(home) == 1
    assert session_mirror.message_id in far[0]
    assert session.message_id not in far[0]
    assert session.message_id in home[0]


def await_poll_session(conn: Any, poll_id: int) -> int:
    poll = polls.get(conn, poll_id)
    assert poll is not None and poll.session_id is not None
    return poll.session_id


async def _dm(world: World, user: str) -> str:
    """The DM channel the fake gave a user when their first ballot was sent."""
    channel = world.state.dm_channels.get(user)
    assert channel is not None
    return channel


# AC3.7: a poll a both copy starts is posted in its own channel only -----------------------------------------

async def test_a_poll_started_in_a_both_scope_is_mirrored_nowhere(world: World) -> None:
    poll_id = await world.flow._open_poll(world.card_id, starter_id=STARTER, presser_id=STARTER, kind="approval",
                                          parent_poll_id=None, options=[(ONE, "offered")],
                                          scope_sub_id=world.sub_b)
    assert poll_id is not None
    assert await mirror_of(world, poll_id, world.sub_b) is None
    assert post_count(world, PLAY) == 0
    assert post_count(world, FAR_B) == 1
    recipients = {b.user_id for b in await world.db.run(lambda c: polls.ballots(c, poll_id))}
    assert recipients == {STARTER, HOME_H1, FAR_F1}


# A member-list failure in a far community ----------------------------------------------------------------

async def test_a_member_list_failure_in_a_far_community_still_opens_the_home_poll(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    real = world.rest.list_members

    async def failing(guild_id: str, **kwargs: Any) -> Any:
        if guild_id == OTHER:
            raise RuntimeError("member list unavailable")
        return await real(guild_id, **kwargs)

    monkeypatch.setattr(world.rest, "list_members", failing)
    poll_id = await open_home(world)
    recipients = {b.user_id for b in await world.db.run(lambda c: polls.ballots(c, poll_id))}
    assert recipients == {STARTER, HOME_H1, HOME_H2}
    assert await mirror_of(world, poll_id, world.sub_b) is not None


# AC3.8 for polls: a 404 on a mirror drops that mirror and never closes the home poll ------------------------

async def test_a_mirror_404_drops_its_row_and_the_home_poll_stays_open(world: World) -> None:
    poll_id = await open_home(world)
    row = await mirror_of(world, poll_id, world.sub_b)
    assert row is not None
    world.state.deleted.add((row.channel_id, row.message_id))
    await world.flow.vote(HOME_H2, poll_id, "a", reply_channel_id=await _dm(world, HOME_H2))
    await world.post.flush(poll_id)
    assert await mirror_of(world, poll_id, world.sub_b) is None
    poll = await world.db.run(lambda c: polls.get(c, poll_id))
    assert poll is not None and poll.state == "open"


# The session link of a poll mirror: its own copy's session mirror, never the home session (AC3.6) ---------

async def schedule_home_poll(world: World) -> tuple[int, sessions.Session]:
    """A home poll with two votes for its first time, closed and scheduled. Returns the poll id and its session."""
    poll_id = await open_home(world)
    await world.flow.vote(FAR_F1, poll_id, "a", reply_channel_id=await _dm(world, FAR_F1))
    await world.flow.vote(HOME_H1, poll_id, "a", reply_channel_id=await _dm(world, HOME_H1))
    world.clock.now = CLOSES + 1
    await world.closer.close(poll_id)
    await world.flow._schedule(poll_id, presser=STARTER)
    poll = await world.db.run(lambda c: polls.get(c, poll_id))
    assert poll is not None and poll.session_id is not None
    session = await world.db.run(lambda c: sessions.get(c, poll.session_id))
    assert session is not None
    return poll_id, session


def poll_mirror_patches(world: World, channel: str, message: str) -> list[str]:
    """The JSON of every PATCH to one message, in order."""
    path = f"/api/v1/channels/{channel}/messages/{message}"
    return [json.dumps(r.payload, sort_keys=True) for r in world.state.requests
            if r.method == "PATCH" and r.path == path]


async def test_a_scheduled_poll_mirror_links_its_own_copys_session_mirror_and_never_home(world: World) -> None:
    poll_id, session = await schedule_home_poll(world)
    session_mirror = await world.db.run(lambda c: mirrors.get(c, "session", session.id, world.sub_b))
    assert session_mirror is not None
    row = await mirror_of(world, poll_id, world.sub_b)
    assert row is not None
    await world.post.drain()
    await world.post.flush(poll_id)
    patches = poll_mirror_patches(world, FAR_B, row.message_id)
    assert patches, "the scheduled poll's mirror is edited with its session link"
    last = patches[-1]
    assert f"/{OTHER}/{FAR_B}/{session_mirror.message_id}" in last
    home_channel, home_message = await home_ids(world, poll_id)
    leaves_out(last, session.message_id, home_message, home_channel)


async def test_a_scheduled_poll_mirror_with_no_session_mirror_shows_plain_text_and_no_link(world: World) -> None:
    poll_id, session = await schedule_home_poll(world)
    session_mirror = await world.db.run(lambda c: mirrors.get(c, "session", session.id, world.sub_b))
    assert session_mirror is not None
    await world.post.drain()
    assert await world.mirror.delete(session_mirror)
    row = await mirror_of(world, poll_id, world.sub_b)
    assert row is not None
    world.state.requests.clear()
    world.post.changed(poll_id)
    await world.post.flush(poll_id)
    patches = poll_mirror_patches(world, FAR_B, row.message_id)
    assert patches, "the poll mirror is edited once its session mirror is gone"
    assert POLL_SESSION_PLAIN in patches[-1]
    assert "/channels/" not in patches[-1]
    assert session.message_id not in patches[-1]


async def test_a_backfill_of_an_open_poll_beside_a_scheduled_session_links_nothing_home(world: World) -> None:
    # The schema allows a session link only on a winning, closed poll (CHECK polls.session_id), so an open poll never
    # shows a session field. Its back-fill, with the card's home session scheduled beside it, must post no link at all.
    _closed_id, session = await schedule_home_poll(world)
    open_id = await open_home(world)
    row = await mirror_of(world, open_id, world.sub_b)
    assert row is not None
    assert await world.mirror.delete(row)
    home_channel, home_message = await home_ids(world, open_id)
    card = await world.db.run(lambda c: cards.get_by_id(c, world.card_id))
    assert card is not None
    backfill = MirrorBackfill(db=world.db, config=world.config, session_post=world.session_post,
                              mirror_post=world.mirror)
    world.state.requests.clear()
    await backfill.backfill_locked(card, world.sub_b)
    text_of_poll = [json.dumps(r.payload, sort_keys=True) for r in world.state.requests
                    if r.method == "POST" and r.path == f"/api/v1/channels/{FAR_B}/messages"]
    assert text_of_poll, "the back-fill posts the open poll's mirror"
    assert all("/channels/" not in body for body in text_of_poll)
    for body in text_of_poll:
        leaves_out(body, session.message_id, home_message, home_channel)
