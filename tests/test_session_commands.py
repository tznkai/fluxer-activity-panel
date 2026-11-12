# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`!session` in DMs, end to end: a real DmRouter, SessionCommands and SessionPost against the fake REST server. Replies
are the POSTs to the command's DM channel, and notification DMs are the POSTs to each recipient's own DM channel. The
clock is a stub, so the time parser and every timestamp are deterministic."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.rest import FluxerREST
from fluxer_panel import card_fields, cards, render, sessions, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import CardFieldError
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.dm_router import DmRouter, NonAdminLimiter
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.session_commands import SessionCommands
from fluxer_panel.session_post import SessionPost
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "session-commands-token"
URL = "https://fluxer.invalid"
ADMIN = "1001"
PROPOSER = "1002"
GOING = "1003"
WAITING = "1004"
MAYBE = "1005"
STRANGER = "1006"
GUILD = "2001"
CHANNEL = "2101"
MSG = "5101"
GAMES = "3100"
DM = "7700"
STARTS = 1_800_000_000
NOW = int(datetime(2026, 11, 12, 10, 0, tzinfo=UTC).timestamp())
MOVE_TIME = "2026-11-14 14:00"
MOVE_TS = int(datetime(2026, 11, 14, 14, 0, tzinfo=UTC).timestamp())
LONG_LOCATION = "x" * 201
PATCH = f"/api/v1/channels/{CHANNEL}/messages/{MSG}"
REPLIES = f"/api/v1/channels/{DM}/messages"


def config_for() -> Config:
    return Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": URL,
                            "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES, "POLL_INTERVAL_SECONDS": "3600"})


def fixed_clock() -> float:
    return float(NOW)


@pytest.fixture
async def world(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[SimpleNamespace]:
    database = Database.open(tmp_path / "session_commands.db")
    state = FakeFluxerState(bot_user_id="1900")
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    config = config_for()
    kicks: list[int] = []
    try:
        async with aiohttp.ClientSession() as client:
            rest = FluxerREST(client, f"{base}/api", TOKEN)
            backoff = DmBackoff(3600, clock=fixed_clock)
            post = SessionPost(db=database, rest=rest, config=config, pacer=ChannelPacer(), backoff=backoff)
            locks = CardLocks()
            commands = SessionCommands(db=database, rest=rest, config=config, locks=locks, session_post=post,
                                       clock=fixed_clock, kick=lambda: kicks.append(1))
            post.set_on_gone(commands.on_message_gone)
            router = DmRouter(config=config, rest=rest, card_ctx=None,
                              limiter=NonAdminLimiter(config, clock=fixed_clock),
                              session_commands=commands.handle)
            yield SimpleNamespace(db=database, state=state, rest=rest, post=post, backoff=backoff, locks=locks,
                                  commands=commands, router=router, kicks=kicks)
    finally:
        database.close()


def seed_card(conn: Any, *, slug: str = "mc", title: str = "Lobby") -> int:
    card = cards.create_card(conn, slug=slug, title=title, author_id=ADMIN, now=1)
    cards.set_state(conn, card, "live", 1)
    return card


def seed_session(conn: Any, card_id: int, *, message: str = MSG, seats: int | None = None, starts: int = STARTS,
                 proposer_cap: bool = True) -> int:
    return sessions.add(conn, card_id=card_id, guild_id=GUILD, channel_id=CHANNEL, message_id=message,
                        starts_at=starts, ends_at=starts + 3600, proposer_id=PROPOSER, now=1, location_text="Hall B",
                        seats=seats, proposer_cap=proposer_cap)


async def session_with(world: SimpleNamespace, *, seats: int | None = None,
                       rows: Sequence[tuple[str, str]] = ()) -> int:
    """One live card with one upcoming session (the proposer going), plus the given reactions in order. Each reaction
    goes through the store's own presence API and the engine, so a going reaction past the seats is waitlisted."""
    def build(conn: Any) -> int:
        session_id = seed_session(conn, seed_card(conn), seats=seats)
        session = sessions.get(conn, session_id)
        assert session is not None
        for user_id, choice in rows:
            sessions.add_presence(conn, session, user_id, choice, 1)
            sessions.recompute(conn, session, seats, 1)
        return session_id

    return await world.db.run(build)


async def say(world: SimpleNamespace, content: str, author: str = PROPOSER, *, guild: bool = False) -> None:
    await world.router.route(events.MessageCreate(
        message_id="8001", channel_id=DM, guild_id=GUILD if guild else None, author_id=author,
        author_is_bot=False, member_role_ids=None, content=content))


def replies(world: SimpleNamespace) -> list[str]:
    return [r.payload["content"] for r in world.state.requests
            if r.method == "POST" and r.path == REPLIES]


def sent_to(world: SimpleNamespace, user_id: str) -> list[str]:
    channel = world.state.dm_channels.get(user_id)
    if channel is None:
        return []
    path = f"/api/v1/channels/{channel}/messages"
    return [r.payload["content"] for r in world.state.requests if r.method == "POST" and r.path == path]


def patches(world: SimpleNamespace) -> list[Any]:
    return [r for r in world.state.requests if r.method == "PATCH" and r.path == PATCH]


def status_of(patch: Any) -> str:
    return next(f["value"] for f in patch.payload["embeds"][0]["fields"] if f["name"] == "Status")


async def read_session(world: SimpleNamespace, session_id: int) -> sessions.Session:
    session = await world.db.run(lambda conn: sessions.get(conn, session_id))
    assert session is not None
    return session


def panel_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "fluxer_panel.session_commands"]


# AC6.1: list. A non-admin sees their own state; the admin sees every upcoming session; chunks stay under the limit.

async def test_the_proposer_list_shows_their_own_state(world: SimpleNamespace) -> None:
    session_id = await session_with(world)
    await say(world, "!session list")
    expected = text.SESSION_LIST_MINE.format(n=session_id, card="Lobby", ts=STARTS, state="going")
    assert replies(world) == [expected]


async def test_a_waitlisted_user_sees_their_waitlist_position(world: SimpleNamespace) -> None:
    await session_with(world, seats=1, rows=[(GOING, "going")])
    await say(world, "!session list", author=GOING)
    [reply] = replies(world)
    assert "you: waitlist #1" in reply


async def test_a_user_with_no_sessions_is_told_so(world: SimpleNamespace) -> None:
    await session_with(world)
    await say(world, "!session list", author=STRANGER)
    assert replies(world) == [text.SESSION_LIST_EMPTY]


async def test_the_admin_list_shows_every_upcoming_session_with_counts(world: SimpleNamespace) -> None:
    session_id = await session_with(world, seats=2, rows=[(GOING, "going"), (WAITING, "going")])
    await say(world, "!session list", author=ADMIN)
    [reply] = replies(world)
    counts = text.SESSION_COUNTS.format(going=2, maybe=0) + text.SESSION_COUNTS_WAITING.format(waiting=1)
    assert reply == text.SESSION_LIST_ALL.format(n=session_id, card="Lobby", ts=STARTS, counts=counts)


async def test_the_admin_list_arrives_in_messages_under_the_chunk_limit(world: SimpleNamespace) -> None:
    def build(conn: Any) -> None:
        for index in range(6):
            card_id = seed_card(conn, slug=f"c{index}", title=f"Card {index}")
            for step in range(10):
                message = str(5000 + index * 10 + step)
                seed_session(conn, card_id, message=message, starts=STARTS + (index * 10 + step) * 3600,
                             proposer_cap=False)

    await world.db.run(build)
    await say(world, "!session list", author=ADMIN)
    messages = replies(world)
    assert sum(len(m.split("\n")) for m in messages) == 60
    assert len(messages) >= 2
    assert all(render.units(m) <= 1900 for m in messages)


# AC6.2: each command works for the proposer and for the admin, by number and by message link.

async def test_the_proposer_can_cancel_by_session_number(world: SimpleNamespace) -> None:
    session_id = await session_with(world)
    await say(world, f"!session cancel {session_id}")
    assert (await read_session(world, session_id)).state == "cancelled"
    assert replies(world) == [text.SESSION_CANCEL_DONE.format(n=session_id)]
    assert world.kicks == [1]


async def test_the_admin_can_cancel_by_message_link(world: SimpleNamespace) -> None:
    session_id = await session_with(world)
    link = f"https://fluxer.invalid/channels/{GUILD}/{CHANNEL}/{MSG}"
    await say(world, f"!session cancel {link}", author=ADMIN)
    assert (await read_session(world, session_id)).state == "cancelled"


async def test_repeat_weekly_then_off_sets_and_clears_the_series_recurrence(world: SimpleNamespace) -> None:
    session_id = await session_with(world)
    await say(world, f"!session repeat {session_id} weekly")
    assert (await read_session(world, session_id)).recurrence == "weekly"
    await say(world, f"!session repeat {session_id} off")
    assert (await read_session(world, session_id)).recurrence is None
    assert replies(world) == [text.SESSION_REPEAT_ON.format(n=session_id),
                              text.SESSION_REPEAT_OFF.format(n=session_id)]


async def test_location_sets_then_clears_the_session_location(world: SimpleNamespace) -> None:
    session_id = await session_with(world)
    await say(world, f"!session location {session_id} Hall C")
    assert (await read_session(world, session_id)).location_text == "Hall C"
    assert patches(world)
    await say(world, f"!session location {session_id} clear")
    assert (await read_session(world, session_id)).location_text is None
    assert replies(world) == [text.SESSION_LOCATION_DONE.format(n=session_id)] * 2


async def test_seats_set_then_clear(world: SimpleNamespace) -> None:
    session_id = await session_with(world)
    await say(world, f"!session seats {session_id} 4")
    assert (await read_session(world, session_id)).seats == 4
    await say(world, f"!session seats {session_id} clear")
    assert (await read_session(world, session_id)).seats is None
    assert replies(world) == [text.SESSION_SEATS_DONE.format(n=session_id)] * 2
    assert world.kicks == [1, 1]


async def test_move_changes_the_time_and_keeps_the_length_and_the_rsvps(world: SimpleNamespace) -> None:
    session_id = await session_with(world, rows=[(GOING, "going")])
    before = await world.db.run(lambda conn: (sessions.get(conn, session_id), sessions.rsvps_of(conn, session_id)))
    await say(world, f"!session move {session_id} {MOVE_TIME}")
    after = await read_session(world, session_id)
    assert after.starts_at == MOVE_TS
    assert after.ends_at - after.starts_at == before[0].ends_at - before[0].starts_at
    assert await world.db.run(lambda conn: sessions.rsvps_of(conn, session_id)) == before[1]
    assert replies(world) == [text.SESSION_MOVE_DONE.format(n=session_id, ts=MOVE_TS)]
    assert world.kicks == [1]


# AC6.3: cancel and move DM each going and waiting user except the actor, and edit the message.

async def test_cancel_dms_each_going_and_waiting_user_except_the_actor_and_edits_the_message(
        world: SimpleNamespace) -> None:
    session_id = await session_with(world, seats=2, rows=[(GOING, "going"), (WAITING, "going"),
                                                         (MAYBE, "maybe")])
    await say(world, f"!session cancel {session_id}")
    assert sent_to(world, GOING) == [text.SESSION_CANCELLED_DM.format(card="Lobby", ts=STARTS)]
    assert sent_to(world, WAITING) == [text.SESSION_CANCELLED_DM.format(card="Lobby", ts=STARTS)]
    assert sent_to(world, PROPOSER) == []
    assert sent_to(world, MAYBE) == []
    [patch] = patches(world)
    assert status_of(patch) == "Cancelled"
    assert "Location" not in [f["name"] for f in patch.payload["embeds"][0]["fields"]]


async def test_move_dms_each_going_and_waiting_user_with_the_new_time_and_edits_the_message(
        world: SimpleNamespace) -> None:
    session_id = await session_with(world, seats=2, rows=[(GOING, "going"), (WAITING, "going"),
                                                         (MAYBE, "maybe")])
    await say(world, f"!session move {session_id} {MOVE_TIME}")
    link = render.jump_link(text.SESSION_LINK_LABEL, URL, GUILD, CHANNEL, MSG)
    expected = [text.SESSION_MOVED_DM.format(card="Lobby", ts=MOVE_TS, link=link)]
    assert sent_to(world, GOING) == expected
    assert sent_to(world, WAITING) == expected
    assert sent_to(world, PROPOSER) == []
    assert sent_to(world, MAYBE) == []
    [patch] = patches(world)
    assert status_of(patch) == text.SESSION_MOVED


async def test_a_cancel_dm_is_attempted_even_for_a_user_in_dm_backoff(world: SimpleNamespace) -> None:
    session_id = await session_with(world, rows=[(GOING, "going")])
    world.backoff.failed(GOING)
    await say(world, f"!session cancel {session_id}")
    assert sent_to(world, GOING) == [text.SESSION_CANCELLED_DM.format(card="Lobby", ts=STARTS)]


# AC6.4: refusals.

async def test_a_stranger_is_refused_and_nothing_changes(world: SimpleNamespace) -> None:
    session_id = await session_with(world, rows=[(GOING, "going")])
    await say(world, f"!session cancel {session_id}", author=STRANGER)
    assert replies(world) == [text.SESSION_NOT_ALLOWED]
    assert (await read_session(world, session_id)).state == "upcoming"
    assert patches(world) == []


async def test_an_unknown_session_number_gets_no_such_session(world: SimpleNamespace) -> None:
    await session_with(world)
    await say(world, "!session cancel 999")
    assert replies(world) == [text.SESSION_UNKNOWN]


async def test_a_change_to_an_ended_session_is_refused(world: SimpleNamespace) -> None:
    session_id = await session_with(world)
    await world.db.run(lambda conn: sessions.end_if(conn, session_id, 2))
    await say(world, f"!session cancel {session_id}")
    assert replies(world) == [text.SESSION_NOT_UPCOMING]
    assert patches(world) == []


async def test_a_bad_move_time_gets_the_time_refusal_and_the_example(world: SimpleNamespace) -> None:
    session_id = await session_with(world)
    await say(world, f"!session move {session_id} sat 2pm")
    assert replies(world) == [text.SESSION_TIME_REFUSED["format"] + " " + text.SESSION_TIME_EXAMPLE]


async def test_a_long_location_gets_the_card_field_refusal(world: SimpleNamespace) -> None:
    session_id = await session_with(world)
    await say(world, f"!session location {session_id} {LONG_LOCATION}")
    assert replies(world) == [card_fields.refusal_reply(CardFieldError("location", "length"))]


async def test_seats_out_of_range_gets_the_card_field_refusal(world: SimpleNamespace) -> None:
    session_id = await session_with(world)
    await say(world, f"!session seats {session_id} 0")
    assert replies(world) == [card_fields.refusal_reply(CardFieldError("seats", "range"))]


async def test_usage_is_given_for_a_missing_reference(world: SimpleNamespace) -> None:
    await session_with(world)
    await say(world, "!session cancel")
    assert replies(world) == [text.SESSION_USAGE]


# AC6.5: never answered in a guild channel.

async def test_a_guild_session_command_gets_no_reply(world: SimpleNamespace) -> None:
    await session_with(world)
    await say(world, "!session list", author=ADMIN, guild=True)
    assert [r for r in world.state.requests if r.method == "POST"] == []


async def test_the_handler_itself_refuses_a_guild_event(world: SimpleNamespace) -> None:
    await session_with(world)
    guild_event = events.MessageCreate(message_id="8001", channel_id=DM, guild_id=GUILD, author_id=ADMIN,
                                       author_is_bot=False, member_role_ids=None, content="!session list")
    await world.commands.handle("list", guild_event)
    assert world.state.requests == []


# Seats: a raised limit promotes a waiting user, with one DM.

async def test_raising_seats_promotes_a_waiting_user_with_one_dm(world: SimpleNamespace) -> None:
    session_id = await session_with(world, seats=1, rows=[(WAITING, "going")])
    assert (await world.db.run(lambda conn: sessions.rsvps_of(conn, session_id)))[WAITING] == ("waiting", 1)
    await say(world, f"!session seats {session_id} 2")
    promoted = await world.db.run(lambda conn: sessions.rsvps_of(conn, session_id))
    assert promoted[WAITING] == ("going", None)
    link = render.jump_link(text.SESSION_LINK_LABEL, URL, GUILD, CHANNEL, MSG)
    assert sent_to(world, WAITING) == [text.SESSION_PROMOTED.format(card="Lobby", link=link)]
    assert world.kicks == [1]


# on_message_gone: a 404 on the session message cancels the session and DMs the attendees.

async def test_a_404_during_a_seats_change_cancels_the_session_and_dms_the_attendees(world: SimpleNamespace) -> None:
    session_id = await session_with(world, rows=[(GOING, "going")])
    world.state.overrides[f"PATCH {PATCH}"] = Override(404, b'{"code":"UNKNOWN_MESSAGE","message":"x"}')
    await say(world, f"!session seats {session_id} 3")
    assert (await read_session(world, session_id)).state == "cancelled"
    assert sent_to(world, GOING) == [text.SESSION_CANCELLED_DM.format(card="Lobby", ts=STARTS)]
    assert replies(world) == [text.SESSION_NOT_UPCOMING]
    assert world.kicks == [1]


async def test_a_404_during_a_move_cancels_once_and_sends_no_moved_dm(world: SimpleNamespace) -> None:
    session_id = await session_with(world, seats=2, rows=[(GOING, "going"), (WAITING, "going")])
    world.state.overrides[f"PATCH {PATCH}"] = Override(404, b'{"code":"UNKNOWN_MESSAGE","message":"x"}')
    await say(world, f"!session move {session_id} {MOVE_TIME}")
    assert (await read_session(world, session_id)).state == "cancelled"
    # The move is stored before the edit, so the cancel DM carries the moved time.
    cancel = [text.SESSION_CANCELLED_DM.format(card="Lobby", ts=MOVE_TS)]
    assert sent_to(world, GOING) == cancel
    assert sent_to(world, WAITING) == cancel
    assert replies(world) == [text.SESSION_NOT_UPCOMING]
    assert world.kicks == [1]


async def test_a_404_during_a_location_change_cancels_and_says_not_upcoming(world: SimpleNamespace) -> None:
    session_id = await session_with(world, rows=[(GOING, "going")])
    world.state.overrides[f"PATCH {PATCH}"] = Override(404, b'{"code":"UNKNOWN_MESSAGE","message":"x"}')
    await say(world, f"!session location {session_id} Hall C")
    assert (await read_session(world, session_id)).state == "cancelled"
    assert sent_to(world, GOING) == [text.SESSION_CANCELLED_DM.format(card="Lobby", ts=STARTS)]
    assert replies(world) == [text.SESSION_NOT_UPCOMING]
    assert world.kicks == [1]


async def test_on_message_gone_takes_no_card_lock(world: SimpleNamespace) -> None:
    session_id = await session_with(world, rows=[(GOING, "going")])
    card_id = (await read_session(world, session_id)).card_id
    async with world.locks.get(card_id):   # held by the caller, as an archive holds it
        await asyncio.wait_for(world.commands.on_message_gone(session_id), 5)
    assert (await read_session(world, session_id)).state == "cancelled"
    assert world.kicks == [1]


async def test_on_message_gone_does_nothing_for_an_ended_session(world: SimpleNamespace) -> None:
    session_id = await session_with(world, rows=[(GOING, "going")])
    await world.db.run(lambda conn: sessions.end_if(conn, session_id, 2))
    await world.commands.on_message_gone(session_id)
    assert sent_to(world, GOING) == []
    assert world.kicks == []


async def test_cancel_card_sessions_locked_runs_with_the_card_lock_held(world: SimpleNamespace) -> None:
    session_id = await session_with(world, rows=[(GOING, "going")])
    card_id = (await read_session(world, session_id)).card_id
    async with world.locks.get(card_id):
        count = await asyncio.wait_for(world.commands.cancel_card_sessions_locked(card_id, ADMIN), 5)
    assert count == 1
    assert (await read_session(world, session_id)).state == "cancelled"
    assert sent_to(world, GOING) == [text.SESSION_CANCELLED_DM.format(card="Lobby", ts=STARTS)]


# Races: the session ends or is cancelled after a command's pre-lock check and before the command takes the card lock.
# The transaction's own state guard refuses the change, so the command replies not-upcoming and writes nothing.

RACE_COMMANDS = {
    "cancel": lambda session_id: f"!session cancel {session_id}",
    "move": lambda session_id: f"!session move {session_id} {MOVE_TIME}",
    "repeat": lambda session_id: f"!session repeat {session_id} weekly",
    "location": lambda session_id: f"!session location {session_id} Hall C",
    "seats": lambda session_id: f"!session seats {session_id} 4",
}
RACE_ENDINGS = {"ended": sessions.end_if, "cancelled": sessions.cancel_if}


def _signal_when_blocked(lock: asyncio.Lock) -> asyncio.Event:
    """Sets the event when something starts to acquire the lock: the command has then passed its pre-lock checks and is
    about to wait on the lock the caller holds. Install it after the caller has acquired, so the caller's own acquire is
    not counted."""
    entered = asyncio.Event()
    acquire = lock.acquire

    async def signalling_acquire() -> bool:
        entered.set()
        return await acquire()

    lock.acquire = signalling_acquire  # type: ignore[method-assign]
    return entered


async def _stored_fields(world: SimpleNamespace, session_id: int) -> tuple[object, ...]:
    session = await read_session(world, session_id)
    return (session.state, session.starts_at, session.ends_at, session.seats, session.location_text,
            session.recurrence)


@pytest.mark.parametrize("ending", sorted(RACE_ENDINGS))
@pytest.mark.parametrize("command", sorted(RACE_COMMANDS))
async def test_a_change_that_loses_a_race_with_the_session_ending_is_refused_and_writes_nothing(
        world: SimpleNamespace, command: str, ending: str) -> None:
    session_id = await session_with(world, rows=[(GOING, "going")])
    card_id = (await read_session(world, session_id)).card_id
    lock = world.locks.get(card_id)
    await lock.acquire()
    entered = _signal_when_blocked(lock)
    task = asyncio.create_task(say(world, RACE_COMMANDS[command](session_id)))
    try:
        await asyncio.wait_for(entered.wait(), 5)   # the command is now waiting on the card lock
        end = RACE_ENDINGS[ending]
        await world.db.run(lambda conn: end(conn, session_id, NOW))
        before = await _stored_fields(world, session_id)
    finally:
        lock.release()
    await asyncio.wait_for(task, 5)
    assert replies(world) == [text.SESSION_NOT_UPCOMING]
    # The only request is the reply: no edit, no DM, no DM channel.
    assert [(r.method, r.path) for r in world.state.requests if r.method != "GET"] == [("POST", REPLIES)]
    assert await _stored_fields(world, session_id) == before
    assert world.kicks == []


# Logs: session_command never carries a user id.

async def test_session_command_logs_carry_no_user_id(world: SimpleNamespace, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.session_commands")
    session_id = await session_with(world, rows=[(GOING, "going")])
    await say(world, f"!session cancel {session_id}")
    [logged] = [r for r in panel_records(caplog) if r.getMessage() == "session_command"]
    fields = getattr(logged, FIELDS_ATTR)
    assert "user_id" not in fields
    assert fields["session_id"] == session_id
    assert fields["outcome"] == "done"
