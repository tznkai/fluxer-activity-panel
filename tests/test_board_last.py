# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The board check and the debounced re-post, against a real SQLite file and the fake Fluxer REST: the board stays the
games channel's newest message, one re-post per interval, and a failed re-post is retried after the interval."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.events import MessageDelete
from fluxer_client.rest import FluxerREST
from fluxer_panel import cards
from fluxer_panel.db import Database
from tests.fakes import FakeFluxerState, Override, Recorded, make_rest_app
from tests.test_board_poller import (
    GAMES,
    GAMES_MESSAGES,
    OTHER_GAMES,
    OTHER_MESSAGES,
    TOKEN,
    FakeClock,
    StubProbe,
    World,
    board_id,
    board_posts,
    posted,
    seed,
)


class RecordingWake:
    """Stands in for the wake requests: counts the ticks that closed what was due."""

    def __init__(self) -> None:
        self.closes = 0

    async def close_due(self) -> None:
        self.closes += 1


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def world(db: Database, session: aiohttp.ClientSession, aiohttp_server: Any) -> AsyncIterator[World]:
    fluxer = FakeFluxerState()
    fluxer.channels[GAMES] = {"id": GAMES, "name": "games", "type": 0, "guild_id": "1001"}
    server = await aiohttp_server(make_rest_app(fluxer))
    rest = FluxerREST(session, f"{str(server.make_url('/')).rstrip('/')}/api", TOKEN)
    yield World(db=db, rest=rest, fluxer=fluxer, probe=StubProbe(), clock=FakeClock())


def chat(world: World) -> str:
    """A message from someone else in the games channel, sent after every message the fake holds."""
    mid = str(world.fluxer.next_message_id)
    world.fluxer.next_message_id += 1
    world.fluxer.messages[(GAMES, mid)] = {"content": "chat"}
    return mid


def gets(world: World, path: str = GAMES_MESSAGES) -> list[Recorded]:
    return [r for r in world.fluxer.requests if r.method == "GET" and r.path == path]


def deletes_of(world: World, message_id: str, channel: str = GAMES) -> list[Recorded]:
    path = f"/api/v1/channels/{channel}/messages/{message_id}"
    return [r for r in world.fluxer.requests if r.method == "DELETE" and r.path == path]


def patches(world: World) -> list[Recorded]:
    return [r for r in world.fluxer.requests if r.method == "PATCH"]


# The check and the arming ---------------------------------------------------------------------------------------


async def test_a_tick_with_nothing_armed_asks_for_no_newest_message(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha")
    poller = world.poller()

    await poller.tick()
    assert len(gets(world)) == 1          # the first tick's check, armed by the card and the board posts
    await poller.tick()

    assert len(gets(world)) == 1


async def test_edits_in_place_neither_arm_the_check_nor_repost_the_board(world: World, db: Database) -> None:
    card_id = await seed(db, "alpha", title="Alpha")
    poller = world.poller()
    await poller.tick()
    gets_before = len(gets(world))
    posts_before = len(board_posts(world.fluxer))

    await db.run(lambda c: cards.update_fields(c, card_id, 0, title="Beta"))
    await poller.tick()

    assert len(patches(world)) == 2       # the card and the board, both edited in place
    assert len(gets(world)) == gets_before
    assert len(board_posts(world.fluxer)) == posts_before


async def test_a_games_message_only_arms_the_check_and_does_not_kick(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha")
    poller = world.poller()
    await poller.tick()
    requests_before = len(world.fluxer.requests)

    for _ in range(5):
        poller.on_games_message(GAMES)

    assert not poller._kick.is_set()
    assert len(world.fluxer.requests) == requests_before


async def test_a_moved_games_channel_leaves_the_board_where_it_is(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha")
    await world.poller().tick()          # the board is stored in GAMES
    board_message = await board_id(world)
    poller = world.poller(GAMES_CHANNEL_ID=OTHER_GAMES)

    await poller.tick()
    poller.on_games_message(OTHER_GAMES)
    await poller.tick()

    assert gets(world, OTHER_MESSAGES) == []
    assert posted(world.fluxer, OTHER_MESSAGES) == []
    assert deletes_of(world, board_message) == []
    assert len(board_posts(world.fluxer)) == 1


# The re-post -----------------------------------------------------------------------------------------------------


async def test_chat_below_the_board_reposts_it_and_then_deletes_the_old_one(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha")
    poller = world.poller()
    await poller.tick()
    old = await board_id(world)
    chat(world)
    poller.on_games_message(GAMES)

    await poller.tick()

    new = await board_id(world)
    assert len(board_posts(world.fluxer)) == 2
    assert new != old
    assert len(deletes_of(world, old)) == 1
    # The old message's delete is not a reason to post again.
    await poller.on_message_delete(MessageDelete(old, GAMES, None))
    await poller.tick()
    assert len(board_posts(world.fluxer)) == 2
    assert await board_id(world) == new


async def test_several_changes_below_the_board_within_the_interval_cause_one_repost(world: World,
                                                                                    db: Database) -> None:
    await seed(db, "alpha", title="Alpha")
    poller = world.poller()
    world.clock.now = 0.0
    await poller.tick()
    chat(world)
    poller.on_games_message(GAMES)
    await poller.tick()                   # the first re-post is at once: nothing has reposted before it
    assert len(board_posts(world.fluxer)) == 2

    chat(world)                           # below the re-posted board, within the interval
    for now in (10.0, 20.0, 30.0):
        world.clock.now = now
        poller.on_games_message(GAMES)
        await poller.tick()
    assert len(board_posts(world.fluxer)) == 2

    world.clock.now = 299.0
    await poller.tick()
    assert len(board_posts(world.fluxer)) == 2

    world.clock.now = 300.0               # the interval after the first re-post
    await poller.tick()
    assert len(board_posts(world.fluxer)) == 3


def test_the_wait_is_cut_short_to_the_due_repost(world: World) -> None:
    poller = world.poller(POLL_INTERVAL_SECONDS="60")
    world.clock.now = 100.0
    poller._repost_due = 105.0
    assert poller._wait_timeout() == 5

    poller._repost_due = None
    assert poller._wait_timeout() == 60


async def test_a_failed_repost_is_logged_keeps_the_tick_going_and_is_retried_after_the_interval(
        world: World, db: Database, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    await seed(db, "alpha", title="Alpha")
    poller = world.poller()
    wake = RecordingWake()
    poller.set_wake(wake)                 # the rest of the tick still runs after a failed re-post
    world.clock.now = 0.0
    await poller.tick()
    old = await board_id(world)
    world.fluxer.overrides[f"POST {GAMES_MESSAGES}"] = Override(500, b'{"code": "INTERNAL", "message": "x"}')
    chat(world)
    poller.on_games_message(GAMES)

    world.clock.now = 10.0
    await poller.tick()

    assert [r.getMessage() for r in caplog.records].count("board_repost_failed") == 1
    assert wake.closes == 2               # both ticks ran to the end, the failing re-post's tick included
    assert await board_id(world) == old
    assert deletes_of(world, old) == []

    # The debounce holds after a failure: the check re-arms, but the re-post waits out the interval.
    world.clock.now = 20.0
    await poller.tick()
    assert [r.status for r in board_posts(world.fluxer)] == [200, 500]

    world.clock.now = 310.0               # 10 + BOARD_REPOST_MIN_INTERVAL_SECONDS
    del world.fluxer.overrides[f"POST {GAMES_MESSAGES}"]
    await poller.tick()

    assert [r.status for r in board_posts(world.fluxer)] == [200, 500, 200]
    assert await board_id(world) != old
    assert len(deletes_of(world, old)) == 1


# A card below the board, and the delete of the board it replaces -------------------------------------------------


def old_board_path(message_id: str) -> str:
    return f"DELETE {GAMES_MESSAGES}/{message_id}"


async def test_a_card_posted_below_a_stored_board_arms_the_check(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha")
    poller = world.poller()
    await poller.tick()                   # consumes the startup check; the board is the newest message
    old = await board_id(world)
    gets_before = len(gets(world))
    posts_before = len(board_posts(world.fluxer))

    await seed(db, "beta", title="Beta")  # a new live card is posted in this tick, below the board
    await poller.tick()

    assert len(gets(world)) - gets_before == 1
    assert len(board_posts(world.fluxer)) - posts_before == 1
    assert await board_id(world) != old
    assert len(deletes_of(world, old)) == 1


async def test_a_failed_old_board_delete_is_retried_by_the_next_check(world: World, db: Database,
                                                                      caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    await seed(db, "alpha", title="Alpha")
    poller = world.poller()
    world.clock.now = 0.0
    await poller.tick()
    old = await board_id(world)
    world.fluxer.overrides[old_board_path(old)] = Override(500, b'{"code": "INTERNAL", "message": "x"}')
    chat(world)
    poller.on_games_message(GAMES)

    await poller.tick()

    new = await board_id(world)
    assert new != old
    assert [r.getMessage() for r in caplog.records].count("board_old_delete_failed") == 1
    assert poller._stale_boards == [(GAMES, old)]
    assert len(deletes_of(world, old)) == 1

    del world.fluxer.overrides[old_board_path(old)]
    await poller.tick()

    assert len(deletes_of(world, old)) == 2
    assert poller._stale_boards == []
    assert await board_id(world) == new
    assert len(board_posts(world.fluxer)) == 2


async def test_an_old_board_already_gone_when_retried_is_dropped_from_the_list(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha")
    poller = world.poller()
    world.clock.now = 0.0
    await poller.tick()
    old = await board_id(world)
    world.fluxer.overrides[old_board_path(old)] = Override(500, b'{"code": "INTERNAL", "message": "x"}')
    chat(world)
    poller.on_games_message(GAMES)
    await poller.tick()
    assert poller._stale_boards == [(GAMES, old)]

    world.fluxer.overrides[old_board_path(old)] = Override(404, b'{"code": "UNKNOWN_MESSAGE", "message": "x"}')
    await poller.tick()

    assert poller._stale_boards == []
    assert len(deletes_of(world, old)) == 2
    await poller.tick()                   # nothing left to retry
    assert len(deletes_of(world, old)) == 2
