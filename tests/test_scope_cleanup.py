# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ending and deleting a subscription's scope against a real database and the fake REST server. A scope owns the
sessions, poll and play-now posts that carry its scope_sub_id, plus its mirrors. The home rows must never change."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import cards, mirrors, play_now, polls, scope_cleanup, sessions, subscriptions
from fluxer_panel.card_commands import CardContext, CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poll_close import PollCloser
from fluxer_panel.poll_post import PollPost
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_commands import SessionCommands
from fluxer_panel.session_post import SessionPost
from fluxer_panel.sources.registry import build_sources
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "scope-cleanup-token"
URL = "https://fluxer.invalid"
BOT = "1900"
ADMIN = "1001"
PROPOSER = "1002"
ATTENDEE = "1003"
PLAYER = "1201"
HOME_GUILD = "2001"
FAR_GUILD = "2002"
GAMES = "3001"
HOME_CH = "2101"
FAR_CH = "2102"
PLAY_CH = "2103"
NOW = 1_800_000_000
STARTS = NOW + 86400
HOME_SESSION_MSG = "5101"
SCOPE_SESSION_MSG = "5102"
HOME_POLL_MSG = "5201"
SCOPE_POLL_MSG = "5202"
PLAY_NOW_MSG = "5301"
MIRROR_OK = "5401"
MIRROR_FORBIDDEN = "5402"
LOCATION = "Hall B"
FORBIDDEN_DELETE = f"DELETE /api/v1/channels/{FAR_CH}/messages/{MIRROR_FORBIDDEN}"


def config_for() -> Config:
    return Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": URL,
                            "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES, "POLL_INTERVAL_SECONDS": "3600"})


def clock() -> float:
    return float(NOW)


async def never(_seconds: float) -> None:
    await asyncio.Event().wait()


@dataclass
class World:
    db: Database
    state: FakeFluxerState
    ctx: CardContext
    locks: CardLocks
    interactions: Interactions
    card_id: int
    sub_id: int
    home_session: int
    scope_session: int
    home_poll: int
    scope_poll: int
    play_now_id: int
    session_post: SessionPost


@pytest.fixture
async def world(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[World]:
    database = Database.open(tmp_path / "scope_cleanup.db")
    state = FakeFluxerState(bot_user_id=BOT)
    for channel, message in ((HOME_CH, HOME_SESSION_MSG), (FAR_CH, SCOPE_SESSION_MSG), (HOME_CH, HOME_POLL_MSG),
                             (FAR_CH, SCOPE_POLL_MSG), (PLAY_CH, PLAY_NOW_MSG), (FAR_CH, MIRROR_OK),
                             (FAR_CH, MIRROR_FORBIDDEN)):
        state.messages[(channel, message)] = {"embeds": []}
    state.reactions[(PLAY_CH, PLAY_NOW_MSG)] = {}
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    config = config_for()
    try:
        async with aiohttp.ClientSession() as client:
            rest = FluxerREST(client, f"{base}/api", TOKEN)
            pacer = ChannelPacer(max_calls=1000)
            backoff = DmBackoff(config.dm_backoff_seconds, clock=clock)
            locks = CardLocks()
            session_post = SessionPost(db=database, rest=rest, config=config, pacer=pacer, backoff=backoff)
            commands = SessionCommands(db=database, rest=rest, config=config, locks=locks, session_post=session_post,
                                       clock=clock, kick=lambda: None)
            session_post.set_on_gone(commands.on_message_gone)
            poll_post = PollPost(db=database, rest=rest, config=config, pacer=ChannelPacer(max_calls=1000),
                                 identity=BotIdentity(user_id=BOT), backoff=backoff, edit_delay=0, sleep=never)
            closer = PollCloser(db=database, locks=locks, poll_post=poll_post, clock=clock)
            interactions = Interactions(db=database, rest=rest, config=config, locks=locks,
                                        games_guild=GamesGuild(rest, config),
                                        seeder=Seeder(rest=rest, pacer=pacer, db=database), pacer=pacer,
                                        identity=BotIdentity(user_id=BOT), roles=RoleCache(rest), clock=clock)
            ctx = CardContext(db=database, rest=rest, config=config, sources=build_sources(client, config),
                              locks=locks, session_commands=commands, poll_closer=closer,
                              play_now_ender=interactions)
            try:
                ids = await database.run(seed)
                yield World(db=database, state=state, ctx=ctx, locks=locks, interactions=interactions,
                            session_post=session_post, **ids)
            finally:
                await poll_post.aclose(timeout=0.05)
    finally:
        database.close()


def seed(conn: Any) -> dict[str, int]:
    """A live card with a local subscription. Each scope owns one upcoming session (the actor going, an attendee going)
    and one open poll, plus a live play-now post and a mirror. The home rows are the same kinds, with no scope."""
    card_id = cards.create_card(conn, slug="mc", title="Lobby", author_id=ADMIN, now=NOW)
    cards.set_state(conn, card_id, "live", NOW)
    sub_id = subscriptions.add(conn, card_id, FAR_GUILD, FAR_CH)
    assert subscriptions.set_mode_if(conn, sub_id, "redirect", "local")

    home_session = sessions.add(conn, card_id=card_id, guild_id=HOME_GUILD, channel_id=HOME_CH,
                                message_id=HOME_SESSION_MSG, starts_at=STARTS, ends_at=STARTS + 3600,
                                proposer_id=PROPOSER, now=NOW, location_text="Hall A")
    scope_session = sessions.add(conn, card_id=card_id, guild_id=FAR_GUILD, channel_id=FAR_CH,
                                 message_id=SCOPE_SESSION_MSG, starts_at=STARTS, ends_at=STARTS + 3600,
                                 proposer_id=PROPOSER, now=NOW, location_text=LOCATION, scope_sub_id=sub_id)
    for session_id in (home_session, scope_session):
        session = sessions.get(conn, session_id)
        assert session is not None
        sessions.add_presence(conn, session, ADMIN, "going", NOW)
        sessions.add_presence(conn, session, ATTENDEE, "going", NOW)
        sessions.recompute(conn, session, None, NOW)

    home_poll = polls.create(conn, card_id=card_id, guild_id=HOME_GUILD, channel_id=HOME_CH,
                             message_id=HOME_POLL_MSG, starter_id=ADMIN, kind="approval", parent_poll_id=None,
                             now=NOW, closes_at=NOW + 86400, ballot_size=6,
                             options=[(STARTS, "offered"), (STARTS + 3600, "offered")], recipients=[ATTENDEE])
    scope_poll = polls.create(conn, card_id=card_id, guild_id=FAR_GUILD, channel_id=FAR_CH,
                              message_id=SCOPE_POLL_MSG, starter_id=ADMIN, kind="approval", parent_poll_id=None,
                              now=NOW, closes_at=NOW + 86400, ballot_size=6,
                              options=[(STARTS, "offered"), (STARTS + 3600, "offered")], recipients=[ATTENDEE],
                              scope_sub_id=sub_id)
    assert home_poll is not None and scope_poll is not None

    play_now_id = play_now.add(conn, card_id=card_id, guild_id=HOME_GUILD, channel_id=PLAY_CH,
                               message_id=PLAY_NOW_MSG, presser_id=PLAYER, offers_wake=False, now=NOW,
                               expires_at=NOW + 3600, scope_sub_id=sub_id)
    mirrors.add(conn, sub_id=sub_id, channel_id=FAR_CH, message_id=MIRROR_OK, kind="session",
                object_id=home_session, content_hash=None)
    mirrors.add(conn, sub_id=sub_id, channel_id=FAR_CH, message_id=MIRROR_FORBIDDEN, kind="poll",
                object_id=home_poll, content_hash=None)
    return {"card_id": card_id, "sub_id": sub_id, "home_session": home_session, "scope_session": scope_session,
            "home_poll": home_poll, "scope_poll": scope_poll, "play_now_id": play_now_id}


async def end_scope(world: World) -> scope_cleanup.ScopeEnded:
    """The caller's protocol: count the live play-now posts before the lock, then end the scope under it, then clear
    the reactions of the ended posts after it."""
    live = await world.db.run(lambda c: play_now.live_for_scope(c, world.card_id, world.sub_id))
    counts = await world.interactions.count_play_now(live)
    async with world.locks.get(world.card_id):
        ended = await scope_cleanup.end_scope_locked(world.ctx, world.card_id, world.sub_id, ADMIN, counts=counts)
    await scope_cleanup.end_scope_after_lock(world.ctx, ended)
    return ended


async def delete_rows(world: World) -> None:
    async with world.locks.get(world.card_id):
        await scope_cleanup.delete_scope_rows_locked(world.ctx, world.sub_id)


def sent_to(world: World, user_id: str) -> list[str]:
    channel = world.state.dm_channels.get(user_id)
    if channel is None:
        return []
    path = f"/api/v1/channels/{channel}/messages"
    return [r.payload["content"] for r in world.state.requests if r.method == "POST" and r.path == path]


def requests(world: World, method: str, suffix: str) -> list[Any]:
    return [r for r in world.state.requests if r.method == method and r.path.endswith(suffix)]


async def session_state(world: World, session_id: int) -> str:
    session = await world.db.run(lambda c: sessions.get(c, session_id))
    assert session is not None
    return session.state


async def poll_row(world: World, poll_id: int) -> polls.Poll:
    poll = await world.db.run(lambda c: polls.get(c, poll_id))
    assert poll is not None
    return poll


async def test_end_scope_cancels_only_the_scopes_session_and_dms_its_attendees_but_not_the_actor(world: World) -> None:
    ended = await end_scope(world)

    assert ended.sessions == 1
    assert await session_state(world, world.scope_session) == "cancelled"
    assert await session_state(world, world.home_session) == "upcoming"
    assert len(sent_to(world, ATTENDEE)) == 1
    assert sent_to(world, ADMIN) == []


async def test_end_scope_closes_the_scope_poll_with_no_winner_and_keeps_the_home_poll_open(world: World) -> None:
    ended = await end_scope(world)

    scope = await poll_row(world, world.scope_poll)
    assert (scope.state, scope.outcome) == ("closed", "none")
    assert (await poll_row(world, world.home_poll)).state == "open"
    assert ended.poll_closed is True


async def test_end_scope_expires_its_play_now_post_and_edits_and_clears_its_reactions(world: World) -> None:
    ended = await end_scope(world)

    rows = await world.db.run(lambda c: play_now.list_for_scope(c, world.sub_id))
    assert [row.state for row in rows] == ["expired"]
    assert [row.id for row in ended.play_now] == [world.play_now_id]
    assert requests(world, "PATCH", f"/{PLAY_NOW_MSG}")
    assert requests(world, "DELETE", f"/{PLAY_NOW_MSG}/reactions")


async def test_end_scope_deletes_the_mirrors_and_keeps_a_forbidden_one_in_mirrors_left(world: World) -> None:
    world.state.overrides[FORBIDDEN_DELETE] = Override(403, b'{"code":"MISSING_PERMISSIONS","message":"no"}')

    ended = await end_scope(world)

    assert (FAR_CH, MIRROR_OK) in world.state.deleted
    assert (FAR_CH, MIRROR_FORBIDDEN) not in world.state.deleted
    assert ended.mirrors_left == (FAR_CH,)
    assert await world.db.run(lambda c: mirrors.for_sub(c, world.sub_id)) == []


async def test_end_scope_leaves_the_home_session_mirror_and_home_rows_alone(world: World) -> None:
    await end_scope(world)

    home = await world.db.run(lambda c: sessions.get(c, world.home_session))
    assert home is not None and home.state == "upcoming"
    assert (await poll_row(world, world.home_poll)).state == "open"


async def test_delete_scope_rows_removes_every_state_so_the_subscription_can_be_removed(world: World) -> None:
    await end_scope(world)

    await delete_rows(world)

    def remaining(conn: Any) -> tuple[int, int, int]:
        return (len(sessions.list_for_scope(conn, world.sub_id)), len(polls.list_for_scope(conn, world.sub_id)),
                len(play_now.list_for_scope(conn, world.sub_id)))

    assert await world.db.run(remaining) == (0, 0, 0)
    assert await world.db.run(lambda c: subscriptions.remove_by_id(c, world.sub_id)) is True
    assert await session_state(world, world.home_session) == "upcoming"
    assert (await poll_row(world, world.home_poll)).state == "open"


async def test_delete_scope_rows_scrubs_each_session_message_before_the_delete(world: World) -> None:
    await delete_rows(world)

    patches = requests(world, "PATCH", f"/{SCOPE_SESSION_MSG}")
    assert patches, "the scope's session message was not edited"
    assert all(LOCATION not in json.dumps(r.payload) for r in patches)
    assert await world.db.run(lambda c: sessions.list_for_scope(c, world.sub_id)) == []
