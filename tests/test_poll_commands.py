# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The `!poll` commands in DMs, end to end: a real PollCommands, PollFlow, PollCloser and PollPost over a SQLite file and
the fake REST server. Each change is a compare-and-set under the card lock. The race tests close the poll from inside
that lock, after the command has read the poll, so only the transaction can refuse the change. The clock is a stub and
the edits run with no debounce delay."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, poll_letters, polls, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poll_close import PollCloser
from fluxer_panel.poll_commands import PollCommands
from fluxer_panel.poll_flow import PollFlow
from fluxer_panel.poll_post import PollPost
from tests.fakes import FakeFluxerState, make_rest_app

TOKEN = "SENTINEL-TOKEN-poll-commands"
BOT = "1900"
ADMIN = "1001"
STARTER = "6001"
HOLDER = "6002"
OTHER = "6003"
STRANGER = "6004"
GUILD = "2001"
GAMES = "3001"
CHANNEL = "3010"
POLL_MSG = "5001"
POLL_MSG2 = "5002"
CMD_DM = "8800"                 # the DM channel the commands are sent from and answered in
NOW = 1_800_000_000
ONE = NOW + 7200
TWO = NOW + 10800
THREE = NOW + 14400
LENGTH = 86400                  # POLL_LENGTH_SECONDS default
CLOSES = NOW + LENGTH
CAP = 604800                    # POLL_MAX_LENGTH_SECONDS default
POLL_PATH = f"/api/v1/channels/{CHANNEL}/messages/{POLL_MSG}"
REPLIES = f"/api/v1/channels/{CMD_DM}/messages"
LINK = f"https://fluxer.invalid/channels/{GUILD}/{CHANNEL}/{POLL_MSG}"
RELETTERED = text.POLL_RELETTERED.split("{")[0]
COMMANDS = ("list", "vote", "sort", "shown", "extend", "close", "help")


def config_for() -> Config:
    return Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC",
                            "FLUXER_INSTANCE_URL": "https://fluxer.invalid", "ADMIN_USER_IDS": ADMIN,
                            "GAMES_CHANNEL_ID": GAMES, "POLL_INTERVAL_SECONDS": "3600"})


class Clock:
    def __init__(self) -> None:
        self.now = float(NOW)

    def __call__(self) -> float:
        return self.now


class RaceLocks:
    """A card lock registry that, once armed with a poll id, closes that poll inside the first lock taken after the
    command read the poll: a close that wins the race. It then takes the real lock."""

    def __init__(self, db: Database) -> None:
        self._inner = CardLocks()
        self._db = db
        self._armed: int | None = None

    def arm(self, poll_id: int) -> None:
        self._armed = poll_id

    def get(self, card_id: int) -> AbstractAsyncContextManager[None]:
        return _gated(self._inner.get(card_id), self._win)

    async def _win(self) -> None:
        poll_id, self._armed = self._armed, None
        if poll_id is not None:
            await self._db.run(lambda c: polls.close_if_open(c, poll_id, now=NOW, outcome="none",
                                                             winner_option_id=None))


@asynccontextmanager
async def _gated(inner: Any, before: Callable[[], Awaitable[None]]) -> AsyncIterator[None]:
    await before()
    async with inner:
        yield


@dataclass
class World:
    db: Database
    state: FakeFluxerState
    rest: FluxerREST
    config: Config
    clock: Clock
    post: PollPost
    closer: PollCloser
    commands: PollCommands
    locks: RaceLocks
    kicks: list[int] = field(default_factory=list)


@pytest.fixture
async def build(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[Callable[[], Awaitable[World]]]:
    opened: list[Database] = []
    async with aiohttp.ClientSession() as client:

        async def make() -> World:
            state = FakeFluxerState(bot_user_id=BOT, next_message_id=9001)
            state.messages[(CHANNEL, POLL_MSG)] = {"embeds": []}
            state.messages[(CHANNEL, POLL_MSG2)] = {"embeds": []}
            server = await aiohttp_server(make_rest_app(state))
            base = str(server.make_url("/")).rstrip("/")
            rest = FluxerREST(client, f"{base}/api", TOKEN)
            database = Database.open(tmp_path / f"commands{len(opened)}.db")
            opened.append(database)
            config = config_for()
            clock = Clock()
            backoff = DmBackoff(config.dm_backoff_seconds, clock=clock)
            post = PollPost(db=database, rest=rest, config=config, pacer=ChannelPacer(max_calls=1000),
                            identity=BotIdentity(user_id=BOT), backoff=backoff, edit_delay=0)
            locks = RaceLocks(database)
            flow = PollFlow(db=database, rest=rest, config=config, locks=locks, poll_post=post, backoff=backoff,
                            games_guild=GamesGuild(rest, config), clock=clock)
            closer = PollCloser(db=database, locks=locks, poll_post=post, clock=clock)
            kicks: list[int] = []
            commands = PollCommands(db=database, rest=rest, config=config, locks=locks, poll_post=post,
                                    poll_flow=flow, closer=closer, clock=clock, kick=lambda: kicks.append(1))
            return World(db=database, state=state, rest=rest, config=config, clock=clock, post=post,
                         closer=closer, commands=commands, locks=locks, kicks=kicks)

        yield make
    for database in opened:
        database.close()


def _store(conn: Any, *, slug: str, title: str, starts: tuple[int, ...], users: tuple[str, ...], message: str) -> int:
    card_id = cards.create_card(conn, slug=slug, title=title, author_id=ADMIN, now=1)
    cards.set_state(conn, card_id, "live", 1)
    poll_id = polls.create(conn, card_id=card_id, guild_id=GUILD, channel_id=CHANNEL, message_id=message,
                           starter_id=STARTER, kind="approval", parent_poll_id=None, now=NOW, closes_at=CLOSES,
                           ballot_size=6, options=[(s, "offered") for s in starts], recipients=list(users))
    assert poll_id is not None
    return poll_id


async def started(world: World, *, slug: str = "mc", title: str = "Lobby",
                  starts: tuple[int, ...] = (ONE, TWO, THREE), users: tuple[str, ...] = (STARTER, HOLDER),
                  message: str = POLL_MSG) -> int:
    """A stored poll with a ballot DM sent to each recipient, drained so every edit has landed."""
    poll_id = await world.db.run(lambda c: _store(c, slug=slug, title=title, starts=starts, users=users,
                                                  message=message))
    for user in users:
        assert await world.post.send_ballot(poll_id, user)
    await world.post.drain()
    return poll_id


async def run(world: World, user: str, args: str, *, guild: bool = False) -> None:
    event = events.MessageCreate(message_id="7100", channel_id=CMD_DM, guild_id=GUILD if guild else None,
                                 author_id=user, author_is_bot=False, member_role_ids=None,
                                 content=f"!poll {args}".rstrip())
    await world.commands.handle(args, event)
    await world.post.drain()


async def poll_row(world: World, poll_id: int) -> polls.Poll:
    poll = await world.db.run(lambda c: polls.get(c, poll_id))
    assert poll is not None
    return poll


async def option_at(world: World, poll_id: int, starts_at: int) -> int:
    return next(o.id for o in await world.db.run(lambda c: polls.options(c, poll_id)) if o.starts_at == starts_at)


async def order_of(world: World, poll_id: int) -> list[int]:
    """The starts of the options in position order: the lettering as a user sees it."""
    return [o.starts_at for o in await world.db.run(lambda c: polls.options(c, poll_id))]


async def vote(world: World, poll_id: int, user: str, starts: list[int]) -> None:
    ids = [await option_at(world, poll_id, s) for s in starts]
    assert await world.db.run(lambda c: polls.replace_picks_if_open(c, poll_id, user, ids, ranked=False))


def replies(world: World) -> list[str]:
    return [r.payload["content"] for r in world.state.requests if r.method == "POST" and r.path == REPLIES]


def last_reply(world: World) -> str:
    return replies(world)[-1]


def mark(world: World) -> int:
    return len(world.state.requests)


def patches_since(world: World, start: int) -> list[Any]:
    return [r for r in world.state.requests[start:] if r.method == "PATCH"]


def ballot_channel(world: World, user: str) -> str:
    return world.state.dm_channels[user]


async def ballot_text(world: World, poll_id: int, user: str) -> str:
    """The stored text of a user's ballot DM, as the fake holds it."""
    ballot = await world.db.run(lambda c: polls.ballot(c, poll_id, user))
    assert ballot is not None and ballot.dm_channel_id is not None and ballot.dm_message_id is not None
    return world.state.messages[(ballot.dm_channel_id, ballot.dm_message_id)]["content"]


# AC5.1: sort ---------------------------------------------------------------------------------------------

async def test_the_starter_sort_orders_by_votes_then_soonest_and_says_so_on_the_message_and_ballots(build) -> None:
    world = await build()
    poll_id = await started(world, users=(STARTER, HOLDER, OTHER))
    await vote(world, poll_id, STARTER, [TWO])
    await vote(world, poll_id, HOLDER, [TWO])
    await vote(world, poll_id, OTHER, [THREE])

    await run(world, STARTER, "sort 1")

    assert await order_of(world, poll_id) == [TWO, THREE, ONE]
    assert last_reply(world) == text.POLL_SORTED
    assert RELETTERED in json.dumps(world.state.messages[(CHANNEL, POLL_MSG)])
    for user in (STARTER, HOLDER, OTHER):
        assert RELETTERED in await ballot_text(world, poll_id, user)
    assert (await poll_row(world, poll_id)).sorted_at == NOW


async def test_an_admin_can_sort_a_poll_they_did_not_start(build) -> None:
    world = await build()
    poll_id = await started(world)
    await vote(world, poll_id, HOLDER, [THREE])

    await run(world, ADMIN, "sort 1")

    assert await order_of(world, poll_id) == [THREE, ONE, TWO]
    assert (await poll_row(world, poll_id)).sorted_at == NOW


# AC5.2: shown -------------------------------------------------------------------------------------------

async def test_shown_stores_a_pending_size_and_changes_nothing_until_the_next_sort(build) -> None:
    world = await build()
    poll_id = await started(world)
    start = mark(world)

    await run(world, STARTER, "shown 1 3")

    poll = await poll_row(world, poll_id)
    assert poll.pending_ballot_size == 3 and poll.ballot_size == 6
    assert patches_since(world, start) == []
    assert last_reply(world) == text.POLL_SHOWN_PENDING.format(n=3)

    await run(world, STARTER, "sort 1")

    poll = await poll_row(world, poll_id)
    assert poll.ballot_size == 3 and poll.pending_ballot_size is None


async def test_shown_accepts_the_bounds_one_and_twenty(build) -> None:
    world = await build()
    poll_id = await started(world)

    await run(world, STARTER, "shown 1 1")
    assert (await poll_row(world, poll_id)).pending_ballot_size == 1
    await run(world, STARTER, "shown 1 20")
    assert (await poll_row(world, poll_id)).pending_ballot_size == 20


# AC5.3: extend ------------------------------------------------------------------------------------------

async def test_extend_moves_closes_at_by_the_duration_and_edits_the_message_and_every_ballot(build) -> None:
    world = await build()
    poll_id = await started(world)
    start = mark(world)

    await run(world, STARTER, "extend 1 12h")

    assert (await poll_row(world, poll_id)).closes_at == CLOSES + 43200
    assert last_reply(world) == text.POLL_EXTENDED.format(ts=CLOSES + 43200)
    edited = {r.path for r in patches_since(world, start)}
    assert POLL_PATH in edited
    for user in (STARTER, HOLDER):
        assert any(path.startswith(f"/api/v1/channels/{ballot_channel(world, user)}/") for path in edited)
    assert world.kicks == [1]


async def test_extend_without_a_duration_adds_the_poll_length_and_two_day_and_ninety_minute_work(build) -> None:
    world = await build()
    poll_id = await started(world)

    await run(world, STARTER, "extend 1")
    assert (await poll_row(world, poll_id)).closes_at == CLOSES + LENGTH
    await run(world, STARTER, "extend 1 2d")
    assert (await poll_row(world, poll_id)).closes_at == CLOSES + LENGTH + 172800
    await run(world, STARTER, "extend 1 90m")
    assert (await poll_row(world, poll_id)).closes_at == CLOSES + LENGTH + 172800 + 5400


async def test_a_duration_that_is_not_a_number_and_unit_answers_usage_and_changes_nothing(build) -> None:
    world = await build()
    poll_id = await started(world)

    await run(world, STARTER, "extend 1 soon")

    assert last_reply(world) == text.POLL_USAGE
    assert (await poll_row(world, poll_id)).closes_at == CLOSES


# AC5.4: the cap -----------------------------------------------------------------------------------------

async def test_an_extension_past_the_cap_from_creation_is_refused_and_changes_nothing(build) -> None:
    world = await build()
    poll_id = await started(world)
    start = mark(world)

    await run(world, STARTER, "extend 1 7d")

    assert last_reply(world) == text.POLL_EXTEND_CAP
    assert (await poll_row(world, poll_id)).closes_at == CLOSES
    assert patches_since(world, start) == []
    assert world.kicks == []


async def test_an_extension_to_exactly_the_cap_is_allowed_and_one_more_hour_is_not(build) -> None:
    world = await build()
    poll_id = await started(world)

    await run(world, STARTER, "extend 1 6d")
    assert (await poll_row(world, poll_id)).closes_at == NOW + CAP

    await run(world, STARTER, "extend 1 1h")
    assert last_reply(world) == text.POLL_EXTEND_CAP
    assert (await poll_row(world, poll_id)).closes_at == NOW + CAP


# AC5.5: close -------------------------------------------------------------------------------------------

async def test_close_closes_the_poll_at_once_through_the_close_path_with_its_edits_and_seeds(build) -> None:
    world = await build()
    poll_id = await started(world)
    await vote(world, poll_id, STARTER, [TWO])
    start = mark(world)

    await run(world, STARTER, "close 1")

    poll = await poll_row(world, poll_id)
    assert poll.state == "closed" and poll.outcome == "winner"
    assert last_reply(world) == text.POLL_CLOSED_DONE
    assert POLL_PATH in {r.path for r in patches_since(world, start)}
    assert not [r for r in world.state.requests[start:] if r.method == "POST" and r.path == "/api/v1/users/@me/channels"]
    assert set(world.state.reactions.get((CHANNEL, POLL_MSG), {})) >= {"\U0001F4C5", "\U0001F501"}


# AC5.6: list, vote and help -----------------------------------------------------------------------------

async def test_list_shows_a_member_the_polls_they_were_sent_or_started(build) -> None:
    world = await build()
    await started(world)
    await started(world, slug="other", title="Other", starts=(ONE,), users=(OTHER,), message=POLL_MSG2)

    await run(world, HOLDER, "list")

    lines = last_reply(world).splitlines()
    assert len(lines) == 1 and "Lobby" in lines[0] and "Other" not in lines[0]


async def test_list_for_a_stranger_says_there_are_no_polls(build) -> None:
    world = await build()
    await started(world)

    await run(world, STRANGER, "list")

    assert last_reply(world) == text.POLL_LIST_EMPTY


async def test_list_for_an_admin_shows_every_open_poll(build) -> None:
    world = await build()
    await started(world)
    await started(world, slug="other", title="Other", starts=(ONE,), users=(OTHER,), message=POLL_MSG2)

    await run(world, ADMIN, "list")

    lines = last_reply(world).splitlines()
    assert len(lines) == 2
    assert "Lobby" in lines[0] and "Other" in lines[1]


async def test_vote_sets_the_picks_like_a_reply_and_answers_in_the_command_channel(build) -> None:
    world = await build()
    poll_id = await started(world)

    await run(world, HOLDER, "vote 1 a, c")

    ids = await world.db.run(lambda c: polls.picks(c, poll_id, HOLDER))
    positions = {o.id: poll_letters.letter(o.position) for o in await world.db.run(lambda c: polls.options(c, poll_id))}
    assert [positions[i] for i in ids] == ["a", "c"]
    assert last_reply(world) == text.POLL_YOUR_PICKS.format(letters="a, c")


async def test_help_and_a_bare_poll_answer_the_usage_and_the_usage_names_every_command(build) -> None:
    world = await build()

    await run(world, STRANGER, "help")
    await run(world, STRANGER, "")

    assert replies(world) == [text.POLL_USAGE, text.POLL_USAGE]
    for command in COMMANDS:
        assert command in text.POLL_USAGE


async def test_an_unknown_subcommand_and_a_change_with_no_poll_answer_the_usage(build) -> None:
    world = await build()
    await started(world)

    await run(world, STARTER, "frobnicate 1")
    await run(world, STARTER, "sort")

    assert replies(world) == [text.POLL_USAGE, text.POLL_USAGE]


# AC5.7: starter or admin, and n from 1 to 20 ----------------------------------------------------------

@pytest.mark.parametrize("args", ["sort 1", "shown 1 3", "extend 1 1d", "close 1"])
async def test_a_stranger_is_refused_every_change_and_nothing_changes(build, args: str) -> None:
    world = await build()
    poll_id = await started(world)
    before = await poll_row(world, poll_id)
    start = mark(world)

    await run(world, STRANGER, args)

    assert last_reply(world) == text.POLL_NOT_ALLOWED
    assert await poll_row(world, poll_id) == before
    assert patches_since(world, start) == []
    assert world.kicks == []


@pytest.mark.parametrize("n", ["0", "21", "100", "x"])
async def test_shown_outside_one_to_twenty_is_refused_and_changes_nothing(build, n: str) -> None:
    world = await build()
    poll_id = await started(world)

    await run(world, STARTER, f"shown 1 {n}")

    assert last_reply(world) == text.POLL_SHOWN_RANGE
    assert (await poll_row(world, poll_id)).pending_ballot_size is None


# AC5.8: a change to a closed poll, and a change that loses the race to a close ----------------------------

async def test_every_change_to_a_closed_poll_is_refused_and_changes_nothing(build) -> None:
    world = await build()
    poll_id = await started(world)
    assert await world.closer.close(poll_id) is not None
    before = await poll_row(world, poll_id)
    start = mark(world)

    for args in ("sort 1", "shown 1 3", "extend 1 1d", "close 1"):
        await run(world, STARTER, args)
        assert last_reply(world) == text.POLL_NOT_OPEN

    assert await poll_row(world, poll_id) == before
    assert patches_since(world, start) == []


@pytest.mark.parametrize("args", ["sort 1", "shown 1 3", "extend 1 1d", "close 1"])
async def test_a_change_that_loses_the_race_to_a_close_answers_not_open_and_changes_nothing(build, args: str) -> None:
    world = await build()
    poll_id = await started(world)
    world.locks.arm(poll_id)                       # the close lands after the command read the open poll
    start = mark(world)

    await run(world, STARTER, args)

    assert last_reply(world) == text.POLL_NOT_OPEN
    poll = await poll_row(world, poll_id)
    assert poll.state == "closed" and poll.outcome == "none"     # closed by the race, not by the command
    assert poll.sorted_at is None and poll.pending_ballot_size is None and poll.closes_at == CLOSES
    assert patches_since(world, start) == []
    assert world.kicks == []


# Addressing: the message link, unknown polls and guild messages ------------------------------------------

async def test_a_poll_named_by_its_message_link_resolves(build) -> None:
    world = await build()
    poll_id = await started(world)

    await run(world, STARTER, f"sort {LINK}")

    assert last_reply(world) == text.POLL_SORTED
    assert (await poll_row(world, poll_id)).sorted_at == NOW


async def test_an_unknown_poll_answers_unknown_and_changes_nothing(build) -> None:
    world = await build()
    await started(world)

    await run(world, STARTER, "sort 999")
    await run(world, HOLDER, "vote 999 a")

    assert replies(world) == [text.POLL_UNKNOWN, text.POLL_UNKNOWN]


async def test_a_guild_message_is_never_answered_and_changes_nothing(build) -> None:
    world = await build()
    poll_id = await started(world)

    await run(world, STARTER, "sort 1", guild=True)

    assert replies(world) == []
    assert (await poll_row(world, poll_id)).sorted_at is None


# The log rule ----------------------------------------------------------------------------------------------

async def test_a_command_logs_its_command_outcome_and_poll_id_and_never_a_user_or_the_reply(build, caplog) -> None:
    caplog.set_level(logging.INFO)
    world = await build()
    poll_id = await started(world)

    await run(world, STARTER, "sort 1")

    records = [r for r in caplog.records if r.getMessage() == "poll_command"]
    assert len(records) == 1
    assert getattr(records[0], FIELDS_ATTR) == {"command": "poll_sort", "outcome": "done", "poll_id": poll_id}
