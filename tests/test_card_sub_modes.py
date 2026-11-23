# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`!card subscribe ... mode=` and `!card unsubscribe` through the admin path: the mode transitions, the subscription's
follow role in its own community, and the cleanup of a scope. The fake Fluxer REST answers every call; the database is
real SQLite. Fake ids only."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.roles import RoleCache
from fluxer_panel import cards, mirrors, play_now, polls, sessions, subscriptions, text
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poll_close import PollCloser
from fluxer_panel.poll_post import PollPost
from fluxer_panel.presses import POLL_EMOJI, SESSION_EMOJI
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_commands import SessionCommands
from fluxer_panel.session_post import SessionPost
from tests.fakes import FakeFluxerState, Override, make_rest_app
from tests.test_card_commands import World, build

GAMES = "1101"
GAMES_CH = "2100"
FAR = "1103"
FAR_COPY = "2201"
FAR_SECOND = "2202"
FAR_LOCAL = "2203"
FAR_PLAY = "2204"
BOT = "1900"
BOT_ROLE = "5000"
ADMIN = "1001"
ATTENDEE = "1005"
PLAYER = "1201"
NEW_ROLE = "7001"          # the id the fake gives the next role it creates
NOW = 1_800_000_000
STARTS = NOW + 86400
HOME_MSG = "5001"
SCOPE_SESSION_MSG = "5102"
SCOPE_POLL_MSG = "5202"
PLAY_NOW_MSG = "5301"
MIRROR_MSG = "5401"
COPY_MSG = "5501"
ROLES = f"/api/v1/guilds/{FAR}/roles"
FORBIDDEN = b'{"code": "FORBIDDEN", "message": "no", "global": false}'
SESSION_CANCEL_TEXT = "cancel"


def role(rid: str, name: str, *, permissions: str = "0", position: int = 1, mentionable: bool = True) -> dict[str, Any]:
    return {"id": rid, "name": name, "permissions": permissions, "position": position, "mentionable": mentionable}


def far_roles() -> list[dict[str, Any]]:
    everyone = {"id": FAR, "name": "@everyone", "permissions": "0", "position": 0, "mentionable": False}
    return [everyone, role(BOT_ROLE, "bot", permissions="8", position=10, mentionable=False)]


def clock() -> float:
    return float(NOW)


async def never(_seconds: float) -> None:
    await asyncio.Event().wait()


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def modes_world(db, session, aiohttp_server) -> AsyncIterator[World]:
    """The games community with its channel, and a far community with two copy channels, a local channel and a play-now
    channel. Following is wired as the poller's context has it: roles, identity, the games lookup, the seeder, and the
    session, poll and play-now commands that end a scope."""
    state = FakeFluxerState(bot_user_id=BOT)
    state.guilds[GAMES] = {"id": GAMES}
    state.channels[GAMES_CH] = {"id": GAMES_CH, "type": 0, "guild_id": GAMES, "name": "games"}
    state.guilds[FAR] = {"id": FAR}
    for channel in (FAR_COPY, FAR_SECOND, FAR_LOCAL, FAR_PLAY):
        state.channels[channel] = {"id": channel, "type": 0, "guild_id": FAR, "name": "lounge"}
    for channel, message in ((FAR_COPY, SCOPE_SESSION_MSG), (FAR_COPY, SCOPE_POLL_MSG), (FAR_COPY, MIRROR_MSG),
                             (FAR_COPY, COPY_MSG), (FAR_PLAY, PLAY_NOW_MSG), (FAR_COPY, HOME_MSG)):
        state.messages[(channel, message)] = {"embeds": []}
    state.reactions[(FAR_PLAY, PLAY_NOW_MSG)] = {}
    state.roles = {FAR: far_roles()}
    state.members = {(FAR, BOT): {"roles": [BOT_ROLE]}}
    state.next_role_id = int(NEW_ROLE)
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    world = await build(db, state, base, session, GAMES_CHANNEL_ID=GAMES_CH)
    rest = world.rest
    locks = world.ctx.locks
    pacer = ChannelPacer(max_calls=1000)
    backoff = DmBackoff(world.config.dm_backoff_seconds, clock=clock)
    session_post = SessionPost(db=db, rest=rest, config=world.config, pacer=pacer, backoff=backoff)
    commands = SessionCommands(db=db, rest=rest, config=world.config, locks=locks, session_post=session_post,
                               clock=clock, kick=lambda: None)
    session_post.set_on_gone(commands.on_message_gone)
    poll_post = PollPost(db=db, rest=rest, config=world.config, pacer=ChannelPacer(max_calls=1000),
                         identity=BotIdentity(user_id=BOT), backoff=backoff, edit_delay=0, sleep=never)
    world.ctx.session_commands = commands
    world.ctx.poll_closer = PollCloser(db=db, locks=locks, poll_post=poll_post, clock=clock)
    world.ctx.play_now_ender = Interactions(db=db, rest=rest, config=world.config, locks=locks,
                                            games_guild=GamesGuild(rest, world.config),
                                            seeder=Seeder(rest=rest, pacer=pacer, db=db), pacer=pacer,
                                            identity=BotIdentity(user_id=BOT), roles=RoleCache(rest), clock=clock)
    world.ctx.roles = RoleCache(rest)
    world.ctx.identity = BotIdentity(user_id=BOT)
    world.ctx.games_guild = GamesGuild(rest, world.config)
    world.ctx.seeder = Seeder(rest=rest, pacer=pacer, db=db)
    try:
        yield world
    finally:
        await poll_post.aclose(timeout=0.05)


async def make_card(world: World, slug: str = "mc") -> cards.Card:
    await world.run(f"!card new {slug} Minecraft Survival")
    card = await world.card(slug)
    assert card is not None
    await world.db.run(lambda c: cards.set_state(c, card.id, "live", NOW))
    return card


async def sub_of(world: World, card_id: int, channel: str = FAR_COPY) -> subscriptions.Subscription | None:
    return await world.db.run(lambda c: subscriptions.get(c, card_id, channel))


async def sub_role(world: World, sub_id: int) -> cards.FollowRole | None:
    return await world.db.run(lambda c: cards.get_subscription_role(c, sub_id))


def role_ids_in(world: World) -> list[str]:
    return [str(r["id"]) for r in world.state.roles.get(FAR, [])]


def role_by_name(world: World, name: str) -> dict[str, Any] | None:
    return next((r for r in world.state.roles.get(FAR, []) if r["name"] == name), None)


def deletes_to(world: World, fragment: str) -> list[str]:
    return [r.path for r in world.state.requests if r.method == "DELETE" and fragment in r.path]


def reaction_deletes(world: World, emoji: str) -> list[str]:
    return [r.path for r in world.state.requests if r.method == "DELETE" and "/reactions/" in r.path
            and emoji in r.path and r.path.startswith(f"/api/v1/channels/{FAR_COPY}/messages/{COPY_MSG}")]


def dm_texts(world: World, user_id: str) -> list[str]:
    """The content of every DM the fake received for this user."""
    channel = world.state.dm_channels.get(user_id)
    if channel is None:
        return []
    path = f"/api/v1/channels/{channel}/messages"
    return [r.payload["content"] for r in world.state.requests if r.method == "POST" and r.path == path]


async def seed_scope(world: World, card_id: int, sub_id: int) -> dict[str, int]:
    """Rows the scope owns: an upcoming session with one attendee going, an open poll, a live play-now post, and one
    mirror of a home session. Each is set up through the Phase 1 stores."""
    def seed(conn: Any) -> dict[str, int]:
        home = sessions.add(conn, card_id=card_id, guild_id=GAMES, channel_id=GAMES_CH, message_id=HOME_MSG,
                            starts_at=STARTS, ends_at=STARTS + 3600, proposer_id=ADMIN, now=NOW,
                            location_text="Hall A")
        scope_session = sessions.add(conn, card_id=card_id, guild_id=FAR, channel_id=FAR_COPY,
                                     message_id=SCOPE_SESSION_MSG, starts_at=STARTS, ends_at=STARTS + 3600,
                                     proposer_id=ADMIN, now=NOW, location_text="Hall B", scope_sub_id=sub_id)
        for session_id in (home, scope_session):
            row = sessions.get(conn, session_id)
            assert row is not None
            sessions.add_presence(conn, row, ADMIN, "going", NOW)
            sessions.add_presence(conn, row, ATTENDEE, "going", NOW)
            sessions.recompute(conn, row, None, NOW)
        poll = polls.create(conn, card_id=card_id, guild_id=FAR, channel_id=FAR_COPY, message_id=SCOPE_POLL_MSG,
                            starter_id=ADMIN, kind="approval", parent_poll_id=None, now=NOW, closes_at=NOW + 86400,
                            ballot_size=6, options=[(STARTS, "offered"), (STARTS + 3600, "offered")],
                            recipients=[ATTENDEE], scope_sub_id=sub_id)
        assert poll is not None
        live = play_now.add(conn, card_id=card_id, guild_id=FAR, channel_id=FAR_PLAY, message_id=PLAY_NOW_MSG,
                            presser_id=PLAYER, offers_wake=False, now=NOW, expires_at=NOW + 3600,
                            scope_sub_id=sub_id)
        mirrors.add(conn, sub_id=sub_id, channel_id=FAR_COPY, message_id=MIRROR_MSG, kind="session",
                    object_id=home, content_hash=None)
        subscriptions.set_message(conn, card_id, FAR_COPY, COPY_MSG, "c" * 64)
        return {"home": home, "scope_session": scope_session, "poll": poll, "play_now": live}
    return await world.db.run(seed)


# subscribe with mode=local or mode=both: the role in the far community -----------------------------------

async def test_mode_local_creates_a_mentionable_follow_role_in_the_far_community(modes_world: World) -> None:
    card = await make_card(modes_world)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    sub = await sub_of(modes_world, card.id)
    assert sub is not None and sub.mode == "local"
    role_row = await sub_role(modes_world, sub.id)
    assert role_row is not None and role_row.role_id == NEW_ROLE and role_row.guild_id == FAR
    created = role_by_name(modes_world, "card_subscription_mc")
    assert created is not None and created["mentionable"] is True
    assert modes_world.last_reply().splitlines()[-1] == text.SUBSCRIPTION_ROLE_OK.format(role_id=NEW_ROLE)
    assert await modes_world.db.run(lambda c: cards.get_follow_role(c, card.id)) is None   # the card's role untouched


async def test_mode_both_creates_the_follow_role_the_same_way(modes_world: World) -> None:
    card = await make_card(modes_world)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=both")
    sub = await sub_of(modes_world, card.id)
    assert sub is not None and sub.mode == "both"
    role_row = await sub_role(modes_world, sub.id)
    assert role_row is not None and role_row.role_id == NEW_ROLE


async def test_an_unmanaged_role_with_the_follow_name_is_adopted(modes_world: World) -> None:
    card = await make_card(modes_world)
    modes_world.state.roles[FAR].append(role("7050", "card_subscription_mc", mentionable=False))
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    sub = await sub_of(modes_world, card.id)
    assert sub is not None
    role_row = await sub_role(modes_world, sub.id)
    assert role_row is not None and role_row.role_id == "7050"
    assert role_by_name(modes_world, "card_subscription_mc")["mentionable"] is True   # type: ignore[index]
    assert modes_world.last_reply().splitlines()[-1] == text.SUBSCRIPTION_ROLE_OK.format(role_id="7050")


# AC1.6: a clash on the name, and role=

async def test_a_second_subscription_with_the_same_name_is_asked_for_a_role_and_keeps_its_mode(
        modes_world: World) -> None:
    card = await make_card(modes_world)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_SECOND} mode=local")
    second = await sub_of(modes_world, card.id, FAR_SECOND)
    assert second is not None and second.mode == "local"
    assert await sub_role(modes_world, second.id) is None
    assert modes_world.last_reply().splitlines()[-1] == text.SUBSCRIPTION_ROLE_ASK


async def test_role_id_of_another_role_in_the_community_is_stored_for_the_second_subscription(
        modes_world: World) -> None:
    card = await make_card(modes_world)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    modes_world.state.roles[FAR].append(role("9001", "something else"))
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_SECOND} mode=local")
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_SECOND} role=9001")
    second = await sub_of(modes_world, card.id, FAR_SECOND)
    assert second is not None
    role_row = await sub_role(modes_world, second.id)
    assert role_row is not None and role_row.role_id == "9001"
    assert modes_world.last_reply().splitlines()[-1] == text.SUBSCRIPTION_ROLE_OK.format(role_id="9001")


async def test_role_above_the_bot_is_refused_and_the_mode_is_kept(modes_world: World) -> None:
    card = await make_card(modes_world)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_SECOND} mode=local")
    modes_world.state.roles[FAR].append(role("9002", "senior", position=20))
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_SECOND} role=9002")
    second = await sub_of(modes_world, card.id, FAR_SECOND)
    assert second is not None and second.mode == "local"
    assert await sub_role(modes_world, second.id) is None
    assert modes_world.last_reply().splitlines()[-1] == text.SUBSCRIPTION_ROLE_FAILED.format(
        reason=text.SUBSCRIPTION_ROLE_REASON["hierarchy"])


async def test_role_with_permissions_is_refused_and_the_mode_is_kept(modes_world: World) -> None:
    card = await make_card(modes_world)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_SECOND} mode=local")
    modes_world.state.roles[FAR].append(role("9003", "powerful", permissions="8"))
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_SECOND} role=9003")
    second = await sub_of(modes_world, card.id, FAR_SECOND)
    assert second is not None and second.mode == "local"
    assert await sub_role(modes_world, second.id) is None
    assert modes_world.last_reply().splitlines()[-1] == text.SUBSCRIPTION_ROLE_FAILED.format(
        reason=text.SUBSCRIPTION_ROLE_REASON["role_permissions"])


# AC2.8: a failed role step keeps the mode and stores nothing, and a retry after the fix stores the role

async def test_a_refused_role_create_keeps_the_mode_and_stores_no_role(modes_world: World) -> None:
    card = await make_card(modes_world)
    modes_world.state.overrides[f"POST {ROLES}"] = Override(403, FORBIDDEN)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    sub = await sub_of(modes_world, card.id)
    assert sub is not None and sub.mode == "local"
    assert await sub_role(modes_world, sub.id) is None
    assert modes_world.last_reply().splitlines()[-1] == text.SUBSCRIPTION_ROLE_FAILED.format(
        reason=text.SUBSCRIPTION_ROLE_REASON["create_failed"])


async def test_repeating_the_mode_after_a_failed_role_step_retries_it(modes_world: World) -> None:
    card = await make_card(modes_world)
    modes_world.state.overrides[f"POST {ROLES}"] = Override(403, FORBIDDEN)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    del modes_world.state.overrides[f"POST {ROLES}"]
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    sub = await sub_of(modes_world, card.id)
    assert sub is not None
    role_row = await sub_role(modes_world, sub.id)
    assert role_row is not None and role_row.role_id == NEW_ROLE
    assert modes_world.last_reply().splitlines()[-1] == text.SUBSCRIPTION_ROLE_OK.format(role_id=NEW_ROLE)


# Mode transitions and cleanup ------------------------------------------------------------------------------

async def test_local_then_redirect_then_unsubscribe_succeeds_with_no_integrity_error(modes_world: World) -> None:
    card = await make_card(modes_world)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    sub = await sub_of(modes_world, card.id)
    assert sub is not None
    await seed_scope(modes_world, card.id, sub.id)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=redirect")
    await modes_world.run(f"!card unsubscribe mc {FAR} {FAR_COPY}")
    assert modes_world.last_reply() == text.UNSUBSCRIBED
    assert await sub_of(modes_world, card.id) is None
    remaining = await modes_world.db.run(lambda c: [
        c.execute(f"SELECT COUNT(*) FROM {table} WHERE scope_sub_id = ?", (sub.id,)).fetchone()[0]
        for table in ("sessions", "polls", "play_now")])
    assert remaining == [0, 0, 0]


async def test_leaving_local_for_redirect_ends_the_scope_and_keeps_the_role(modes_world: World) -> None:
    card = await make_card(modes_world)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    sub = await sub_of(modes_world, card.id)
    assert sub is not None
    ids = await seed_scope(modes_world, card.id, sub.id)
    modes_world.clear_requests()
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=redirect")
    after = await sub_of(modes_world, card.id)
    assert after is not None and (after.mode, after.local_channel_id) == ("redirect", None)
    assert await sub_role(modes_world, sub.id) is not None
    scope_session = await modes_world.db.run(lambda c: sessions.get(c, ids["scope_session"]))
    assert scope_session is not None and scope_session.state == "cancelled"
    home = await modes_world.db.run(lambda c: sessions.get(c, ids["home"]))
    assert home is not None and home.state == "upcoming"
    poll = await modes_world.db.run(lambda c: polls.get(c, ids["poll"]))
    assert poll is not None and (poll.state, poll.outcome) == ("closed", "none")
    assert await modes_world.db.run(lambda c: play_now.get_live_by_message(c, PLAY_NOW_MSG)) is None
    assert await modes_world.db.run(lambda c: mirrors.get_by_message_id(c, MIRROR_MSG)) is None
    assert (FAR_COPY, MIRROR_MSG) in modes_world.state.deleted
    assert dm_texts(modes_world, ATTENDEE)          # the attendee of the cancelled session is told
    assert dm_texts(modes_world, ADMIN) == []       # the actor is not
    assert reaction_deletes(modes_world, SESSION_EMOJI) and reaction_deletes(modes_world, POLL_EMOJI)
    assert deletes_to(modes_world, "/roles") == []  # the follow role is kept, and never deleted


async def test_leaving_local_for_redirect_clears_the_copy_s_session_and_poll_reactions(modes_world: World) -> None:
    card = await make_card(modes_world)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    sub = await sub_of(modes_world, card.id)
    assert sub is not None
    await seed_scope(modes_world, card.id, sub.id)
    modes_world.clear_requests()
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=redirect")
    assert reaction_deletes(modes_world, SESSION_EMOJI)
    assert reaction_deletes(modes_world, POLL_EMOJI)


async def test_unsubscribe_cleans_the_scope_deletes_its_history_and_keeps_the_role_out_of_fluxer(
        modes_world: World) -> None:
    card = await make_card(modes_world)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    sub = await sub_of(modes_world, card.id)
    assert sub is not None
    ids = await seed_scope(modes_world, card.id, sub.id)
    modes_world.clear_requests()
    await modes_world.run(f"!card unsubscribe mc {FAR} {FAR_COPY}")
    assert modes_world.last_reply() == text.UNSUBSCRIBED
    assert dm_texts(modes_world, ATTENDEE)
    assert (FAR_COPY, MIRROR_MSG) in modes_world.state.deleted
    assert (FAR_COPY, COPY_MSG) in modes_world.state.deleted
    assert await modes_world.db.run(lambda c: sessions.get(c, ids["scope_session"])) is None
    assert await modes_world.db.run(lambda c: polls.get(c, ids["poll"])) is None
    assert await modes_world.db.run(lambda c: play_now.get_live_by_message(c, PLAY_NOW_MSG)) is None
    assert await sub_of(modes_world, card.id) is None
    assert await modes_world.db.run(lambda c: sessions.get(c, ids["home"])) is not None   # home rows untouched
    assert deletes_to(modes_world, "/roles") == []
    assert not any(r.method == "DELETE" and "/roles" in r.path for r in modes_world.state.requests)


async def test_unsubscribing_a_copy_that_was_local_and_is_now_redirect_deletes_its_history(
        modes_world: World) -> None:
    card = await make_card(modes_world)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    sub = await sub_of(modes_world, card.id)
    assert sub is not None
    ids = await seed_scope(modes_world, card.id, sub.id)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=redirect")
    modes_world.clear_requests()
    await modes_world.run(f"!card unsubscribe mc {FAR} {FAR_COPY}")
    assert modes_world.last_reply() == text.UNSUBSCRIBED
    assert await modes_world.db.run(lambda c: sessions.get(c, ids["scope_session"])) is None
    assert await sub_of(modes_world, card.id) is None


async def test_both_to_local_deletes_the_mirrors_and_keeps_the_scope_session_upcoming(modes_world: World) -> None:
    card = await make_card(modes_world)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=both")
    sub = await sub_of(modes_world, card.id)
    assert sub is not None
    ids = await seed_scope(modes_world, card.id, sub.id)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    after = await sub_of(modes_world, card.id)
    assert after is not None and after.mode == "local"
    assert await modes_world.db.run(lambda c: mirrors.get_by_message_id(c, MIRROR_MSG)) is None
    assert (FAR_COPY, MIRROR_MSG) in modes_world.state.deleted
    scope_session = await modes_world.db.run(lambda c: sessions.get(c, ids["scope_session"]))
    assert scope_session is not None and scope_session.state == "upcoming"
    assert reaction_deletes(modes_world, SESSION_EMOJI) == []   # the copy keeps its calendar: still a scope


async def test_local_to_both_changes_the_mode_only_and_posts_nothing_yet(modes_world: World) -> None:
    card = await make_card(modes_world)
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=local")
    sub = await sub_of(modes_world, card.id)
    assert sub is not None
    await seed_scope(modes_world, card.id, sub.id)
    modes_world.clear_requests()
    await modes_world.run(f"!card subscribe mc {FAR} {FAR_COPY} mode=both")
    after = await sub_of(modes_world, card.id)
    assert after is not None and after.mode == "both"
    posts = [r for r in modes_world.state.requests if r.method == "POST" and "/messages" in r.path
             and f"/channels/{FAR_COPY}/" in r.path]
    assert posts == []
