# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`!card archive` ends every scope of a card: the home scope, each local subscription's scope, and each both
subscription's mirrors. It also expires every live play-now post and deletes the mirrors, the copies and the card's
message. The real command path runs against the fake Fluxer REST and a real SQLite database."""

from __future__ import annotations

import asyncio
import dataclasses
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
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_commands import SessionCommands
from fluxer_panel.session_post import SessionPost
from tests.fakes import FakeFluxerState, Override, make_rest_app
from tests.test_card_commands import World, build

GUILD = "1001"
GAMES = "2200"
HOME_CH = "2101"
PLAY_CH = "2103"
LOCAL_CH = "2111"
BOTH_CH = "2121"
BOT = "1900"
ADMIN = "1002"
ATTENDEE = "1003"
PLAYER = "1201"
NOW = 1_800_000_000
STARTS = NOW + 86400
MAIN_MESSAGE = "5100"
HOME_SESSION_MSG = "5101"
HOME_POLL_MSG = "5102"
HOME_PLAY_NOW_MSG = "5103"
LOCAL_COPY_MSG = "5110"
LOCAL_SESSION_MSG = "5111"
LOCAL_POLL_MSG = "5112"
LOCAL_PLAY_NOW_MSG = "5113"
BOTH_COPY_MSG = "5120"
MIRROR_SESSION_MSG = "5121"
MIRROR_POLL_MSG = "5122"
MIRROR_PLAY_NOW_MSG = "5123"
REDIRECT_CH = "2131"
REDIRECT_SESSION_MSG = "5131"
REDIRECT_POLL_MSG = "5132"


def clock() -> float:
    return float(NOW)


async def never(_seconds: float) -> None:
    await asyncio.Event().wait()


def channel_path(channel: str, message: str) -> str:
    return f"/api/v1/channels/{channel}/messages/{message}"


@dataclasses.dataclass
class Scenario:
    world: World
    poll_post: PollPost
    local_sub: int
    both_sub: int
    home_session: int
    local_session: int
    home_poll: int
    local_poll: int
    home_play_now: int
    local_play_now: int


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def scenario(db: Database, aiohttp_server: Any) -> AsyncIterator[Scenario]:
    state = FakeFluxerState(bot_user_id=BOT)
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    async with aiohttp.ClientSession() as client:
        world = await build(db, state, base, client, GAMES_CHANNEL_ID=GAMES, POLL_INTERVAL_SECONDS="3600")
        config = world.config
        backoff = DmBackoff(config.dm_backoff_seconds, clock=clock)
        pacer = ChannelPacer(max_calls=1000)
        session_post = SessionPost(db=db, rest=world.rest, config=config, pacer=pacer, backoff=backoff)
        commands = SessionCommands(db=db, rest=world.rest, config=config, locks=world.ctx.locks,
                                   session_post=session_post, clock=clock, kick=lambda: None)
        session_post.set_on_gone(commands.on_message_gone)
        poll_post = PollPost(db=db, rest=world.rest, config=config, pacer=ChannelPacer(max_calls=1000),
                             identity=BotIdentity(user_id=BOT), backoff=backoff, edit_delay=0, sleep=never)
        closer = PollCloser(db=db, locks=world.ctx.locks, poll_post=poll_post, clock=clock)
        interactions = Interactions(db=db, rest=world.rest, config=config, locks=world.ctx.locks,
                                    games_guild=GamesGuild(world.rest, config),
                                    seeder=Seeder(rest=world.rest, pacer=pacer, db=db), pacer=pacer,
                                    identity=BotIdentity(user_id=BOT), roles=RoleCache(world.rest), clock=clock)
        world.ctx = dataclasses.replace(world.ctx, session_commands=commands, poll_closer=closer,
                                        play_now_ender=interactions)
        try:
            ids = await db.run(lambda conn: seed(conn, state))
            yield Scenario(world=world, poll_post=poll_post, **ids)
        finally:
            await poll_post.aclose(timeout=0.05)


def seed(conn: Any, state: FakeFluxerState) -> dict[str, int]:
    """A live card with its main message in the games channel. Home owns one session, one poll and one play-now post.
    A local subscription owns the same three in its own channel and has a copy. A both subscription has a copy and
    mirrors of the home session, the home poll and the home play-now post."""
    card_id = cards.create_card(conn, slug="mc", title="Lobby", author_id=ADMIN, now=NOW)
    cards.set_state(conn, card_id, "live", NOW)
    cards.set_message(conn, card_id, GAMES, MAIN_MESSAGE, "c" * 64)
    local_sub = subscriptions.add(conn, card_id, GUILD, LOCAL_CH)
    assert subscriptions.set_mode_if(conn, local_sub, "redirect", "local")
    both_sub = subscriptions.add(conn, card_id, GUILD, BOTH_CH)
    assert subscriptions.set_mode_if(conn, both_sub, "redirect", "both")
    subscriptions.set_message(conn, card_id, LOCAL_CH, LOCAL_COPY_MSG, "d" * 64)
    subscriptions.set_message(conn, card_id, BOTH_CH, BOTH_COPY_MSG, "e" * 64)

    home_session = sessions.add(conn, card_id=card_id, guild_id=GUILD, channel_id=HOME_CH,
                                message_id=HOME_SESSION_MSG, starts_at=STARTS, ends_at=STARTS + 3600,
                                proposer_id=ADMIN, now=NOW, location_text="Hall A")
    local_session = sessions.add(conn, card_id=card_id, guild_id=GUILD, channel_id=LOCAL_CH,
                                 message_id=LOCAL_SESSION_MSG, starts_at=STARTS, ends_at=STARTS + 3600,
                                 proposer_id=ADMIN, now=NOW, location_text="Hall B", scope_sub_id=local_sub)
    for session_id in (home_session, local_session):
        session = sessions.get(conn, session_id)
        assert session is not None
        sessions.add_presence(conn, session, ATTENDEE, "going", NOW)
        sessions.recompute(conn, session, None, NOW)

    options = [(STARTS, "offered"), (STARTS + 3600, "offered")]
    home_poll = polls.create(conn, card_id=card_id, guild_id=GUILD, channel_id=HOME_CH, message_id=HOME_POLL_MSG,
                             starter_id=ADMIN, kind="approval", parent_poll_id=None, now=NOW, closes_at=NOW + 86400,
                             ballot_size=6, options=options, recipients=[ATTENDEE])
    local_poll = polls.create(conn, card_id=card_id, guild_id=GUILD, channel_id=LOCAL_CH,
                              message_id=LOCAL_POLL_MSG, starter_id=ADMIN, kind="approval", parent_poll_id=None,
                              now=NOW, closes_at=NOW + 86400, ballot_size=6, options=options, recipients=[ATTENDEE],
                              scope_sub_id=local_sub)
    assert home_poll is not None and local_poll is not None

    home_play_now = play_now.add(conn, card_id=card_id, guild_id=GUILD, channel_id=PLAY_CH,
                                 message_id=HOME_PLAY_NOW_MSG, presser_id=PLAYER, offers_wake=False, now=NOW,
                                 expires_at=NOW + 3600)
    local_play_now = play_now.add(conn, card_id=card_id, guild_id=GUILD, channel_id=LOCAL_CH,
                                  message_id=LOCAL_PLAY_NOW_MSG, presser_id=PLAYER, offers_wake=False, now=NOW,
                                  expires_at=NOW + 3600, scope_sub_id=local_sub)
    mirrors.add(conn, sub_id=both_sub, channel_id=BOTH_CH, message_id=MIRROR_SESSION_MSG, kind="session",
                object_id=home_session, content_hash=None)
    mirrors.add(conn, sub_id=both_sub, channel_id=BOTH_CH, message_id=MIRROR_POLL_MSG, kind="poll",
                object_id=home_poll, content_hash=None)
    mirrors.add(conn, sub_id=both_sub, channel_id=BOTH_CH, message_id=MIRROR_PLAY_NOW_MSG, kind="play_now",
                object_id=home_play_now, content_hash=None)

    for channel, message in ((GAMES, MAIN_MESSAGE), (HOME_CH, HOME_SESSION_MSG), (HOME_CH, HOME_POLL_MSG),
                             (PLAY_CH, HOME_PLAY_NOW_MSG), (LOCAL_CH, LOCAL_COPY_MSG), (LOCAL_CH, LOCAL_SESSION_MSG),
                             (LOCAL_CH, LOCAL_POLL_MSG), (LOCAL_CH, LOCAL_PLAY_NOW_MSG), (BOTH_CH, BOTH_COPY_MSG),
                             (BOTH_CH, MIRROR_SESSION_MSG), (BOTH_CH, MIRROR_POLL_MSG),
                             (BOTH_CH, MIRROR_PLAY_NOW_MSG)):
        state.messages[(channel, message)] = {"embeds": []}
        state.reactions[(channel, message)] = {}
    return {"local_sub": local_sub, "both_sub": both_sub, "home_session": home_session,
            "local_session": local_session, "home_poll": home_poll, "local_poll": local_poll,
            "home_play_now": home_play_now, "local_play_now": local_play_now}


def sent_to(world: World, user_id: str) -> list[str]:
    channel = world.state.dm_channels.get(user_id)
    if channel is None:
        return []
    path = f"/api/v1/channels/{channel}/messages"
    return [r.payload["content"] for r in world.state.requests if r.method == "POST" and r.path == path]


def deletes(world: World) -> list[str]:
    return [r.path for r in world.state.requests if r.method == "DELETE"]


def refuse_delete(world: World, channel: str, message: str) -> None:
    world.state.overrides[f"DELETE {channel_path(channel, message)}"] = Override(
        403, b'{"code": "X", "message": "no"}')


async def session_state(scenario: Scenario, session_id: int) -> str:
    session = await scenario.world.db.run(lambda c: sessions.get(c, session_id))
    assert session is not None
    return session.state


async def play_now_state(scenario: Scenario, play_now_id: int) -> str:
    row = await scenario.world.db.run(lambda c: play_now.get_by_id(c, play_now_id))
    assert row is not None
    return row.state


async def poll_row(scenario: Scenario, poll_id: int) -> polls.Poll:
    poll = await scenario.world.db.run(lambda c: polls.get(c, poll_id))
    assert poll is not None
    return poll


async def test_archive_cancels_the_home_session_and_every_scope_session(scenario: Scenario) -> None:
    await scenario.world.run("!card archive mc")

    for session_id in (scenario.home_session, scenario.local_session):
        assert await session_state(scenario, session_id) == "cancelled"
    # Each cancelled session's attendee is DMed; the admin who archived is not.
    assert len(sent_to(scenario.world, ATTENDEE)) == 2
    assert sent_to(scenario.world, "1001") == []


async def test_archive_closes_every_poll_with_no_winner(scenario: Scenario) -> None:
    await scenario.world.run("!card archive mc")

    for poll_id in (scenario.home_poll, scenario.local_poll):
        poll = await poll_row(scenario, poll_id)
        assert (poll.state, poll.outcome) == ("closed", "none")


async def test_archive_expires_every_play_now_post_of_every_scope(scenario: Scenario) -> None:
    await scenario.world.run("!card archive mc")

    for play_now_id in (scenario.home_play_now, scenario.local_play_now):
        assert await play_now_state(scenario, play_now_id) == "expired"


async def test_archive_deletes_every_mirror_copy_and_the_card_message_and_no_mirror_row_remains(
        scenario: Scenario) -> None:
    world = scenario.world
    await world.run("!card archive mc")

    expected = {channel_path(BOTH_CH, MIRROR_SESSION_MSG), channel_path(BOTH_CH, MIRROR_POLL_MSG),
                channel_path(BOTH_CH, MIRROR_PLAY_NOW_MSG), channel_path(BOTH_CH, BOTH_COPY_MSG),
                channel_path(LOCAL_CH, LOCAL_COPY_MSG), channel_path(GAMES, MAIN_MESSAGE)}
    assert expected <= set(deletes(world))
    assert await world.db.run(lambda c: mirrors.for_sub(c, scenario.both_sub)) == []
    archived = await world.card("mc")
    assert archived is not None and archived.state == "archived" and archived.message_id is None


async def test_archive_reply_names_a_mirror_the_bot_is_refused_and_says_nothing_else_is_left(
        scenario: Scenario) -> None:
    world = scenario.world
    refuse_delete(world, BOTH_CH, MIRROR_POLL_MSG)

    await world.run("!card archive mc")

    assert world.last_reply() == text.CARD_ARCHIVED_SOME_LEFT + "\n" + text.ARCHIVE_LEFT_LINE.format(channel_id=BOTH_CH)
    assert await world.db.run(lambda c: mirrors.for_sub(c, scenario.both_sub)) == []


async def test_archive_keeps_subscriptions_modes_and_roles_as_they_were(scenario: Scenario) -> None:
    world = scenario.world
    await world.run("!card archive mc")

    local = await world.db.run(lambda c: subscriptions.get_by_id(c, scenario.local_sub))
    both = await world.db.run(lambda c: subscriptions.get_by_id(c, scenario.both_sub))
    assert local is not None and local.mode == "local"
    assert both is not None and both.mode == "both"


def reactions_path(channel: str, message: str) -> str:
    return channel_path(channel, message) + "/reactions"


def reaction_requests(world: World) -> list[tuple[int, str]]:
    """(index, method) of every reaction request, in the order the fake received them. Reactor reads are GETs and the
    remove-all clears are DELETEs; both live under .../reactions."""
    return [(i, r.method) for i, r in enumerate(world.state.requests) if "/reactions" in r.path]


async def test_archive_clears_the_reactions_of_the_home_and_scope_play_now_posts(scenario: Scenario) -> None:
    world = scenario.world
    await world.run("!card archive mc")

    cleared = deletes(world)
    assert reactions_path(PLAY_CH, HOME_PLAY_NOW_MSG) in cleared
    assert reactions_path(LOCAL_CH, LOCAL_PLAY_NOW_MSG) in cleared


async def test_a_failed_copy_delete_still_clears_the_expired_play_now_reactions_and_the_card_stays_live(
        scenario: Scenario) -> None:
    world = scenario.world
    world.state.overrides[f"DELETE {channel_path(LOCAL_CH, LOCAL_COPY_MSG)}"] = Override(
        500, b'{"code": "X", "message": "boom"}')
    await world.run("!card archive mc")

    cleared = deletes(world)
    assert reactions_path(PLAY_CH, HOME_PLAY_NOW_MSG) in cleared
    assert reactions_path(LOCAL_CH, LOCAL_PLAY_NOW_MSG) in cleared
    assert world.last_reply() == text.ARCHIVE_RETRY
    card = await world.card("mc")
    assert card is not None and card.state == "live"


async def test_no_reaction_request_runs_between_the_archive_lock_and_the_card_message_delete(
        scenario: Scenario) -> None:
    world = scenario.world
    await world.run("!card archive mc")

    requests = world.state.requests
    card_delete = next(i for i, r in enumerate(requests)
                       if r.method == "DELETE" and r.path == channel_path(GAMES, MAIN_MESSAGE))
    # The first request made under the card lock: a cancel edit, a DM or a play-now edit, never a reaction request.
    first_locked = min(i for i, r in enumerate(requests) if r.method != "GET" and "/reactions" not in r.path)
    calls = reaction_requests(world)
    # Non-vacuous: the reactor reads (before the lock) and the clears (after it) both happened.
    assert any(method == "GET" for _, method in calls)
    assert any(method == "DELETE" for _, method in calls)
    assert first_locked < card_delete
    for index, method in calls:
        assert index < first_locked or index > card_delete, (index, method, first_locked, card_delete)


async def test_no_reaction_request_is_answered_while_the_card_lock_is_held(scenario: Scenario) -> None:
    """Each reaction request is checked as the fake answers it: the reactor reads come before the lock, and the clears
    come after it, so none is answered with the card lock held."""
    world = scenario.world
    card = await world.card("mc")
    assert card is not None
    lock = world.ctx.locks.get(card.id)
    held: list[tuple[str, str, bool]] = []

    def note_lock_state(request: Any) -> None:
        if "/reactions" in request.path:
            held.append((request.method, request.path, lock.locked()))

    world.state.observers.append(note_lock_state)
    await world.run("!card archive mc")

    assert any(method == "GET" for method, _, _ in held)   # reactor reads, before the lock
    assert any(method == "DELETE" for method, _, _ in held)   # clears, after the lock
    assert [(method, path) for method, path, locked in held if locked] == []


async def test_a_refused_play_now_mirror_still_has_its_reactions_cleared_after_the_lock(scenario: Scenario) -> None:
    world = scenario.world
    refuse_delete(world, BOTH_CH, MIRROR_PLAY_NOW_MSG)
    await world.run("!card archive mc")

    assert reactions_path(BOTH_CH, MIRROR_PLAY_NOW_MSG) in deletes(world)
    assert world.last_reply() == text.CARD_ARCHIVED_SOME_LEFT + "\n" + text.ARCHIVE_LEFT_LINE.format(channel_id=BOTH_CH)


async def test_a_reaction_clear_answering_404_on_a_gone_mirror_is_harmless(scenario: Scenario,
                                                                          caplog: pytest.LogCaptureFixture) -> None:
    world = scenario.world
    gone = reactions_path(BOTH_CH, MIRROR_PLAY_NOW_MSG)
    world.state.overrides[f"DELETE {gone}"] = Override(404, b'{"code": "X", "message": "gone"}')
    await world.run("!card archive mc")

    assert gone in deletes(world)   # the clear was made, and answered 404
    assert [r for r in caplog.records if r.getMessage() == "play_now_reactions_not_removed"] == []
    assert world.last_reply() == text.CARD_ARCHIVED
    card = await world.card("mc")
    assert card is not None and card.state == "archived"


async def test_archive_closes_and_cancels_the_objects_of_a_redirect_subscription(scenario: Scenario) -> None:
    world = scenario.world
    card = await world.card("mc")
    assert card is not None

    def add_redirect_objects(conn: Any) -> tuple[int, int]:
        """A redirect copy is not a scope, but a poll and a session can still carry its scope_sub_id: the archive must
        close and cancel them too."""
        redirect_sub = subscriptions.add(conn, card.id, GUILD, REDIRECT_CH)
        session_id = sessions.add(conn, card_id=card.id, guild_id=GUILD, channel_id=REDIRECT_CH,
                                  message_id=REDIRECT_SESSION_MSG, starts_at=STARTS, ends_at=STARTS + 3600,
                                  proposer_id=ADMIN, now=NOW, location_text="Hall C", scope_sub_id=redirect_sub)
        poll_id = polls.create(conn, card_id=card.id, guild_id=GUILD, channel_id=REDIRECT_CH,
                               message_id=REDIRECT_POLL_MSG, starter_id=ADMIN, kind="approval", parent_poll_id=None,
                               now=NOW, closes_at=NOW + 86400, ballot_size=6,
                               options=[(STARTS, "offered"), (STARTS + 3600, "offered")], recipients=[ATTENDEE],
                               scope_sub_id=redirect_sub)
        assert poll_id is not None
        return session_id, poll_id

    session_id, poll_id = await world.db.run(add_redirect_objects)
    world.state.messages[(REDIRECT_CH, REDIRECT_SESSION_MSG)] = {"embeds": []}
    world.state.messages[(REDIRECT_CH, REDIRECT_POLL_MSG)] = {"embeds": []}
    await world.run("!card archive mc")

    assert await session_state(scenario, session_id) == "cancelled"
    poll = await poll_row(scenario, poll_id)
    assert (poll.state, poll.outcome) == ("closed", "none")
