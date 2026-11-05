# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`!request list|clear <slug>` end to end: `admin.handle` on a DM, a real SQLite file, the fake Fluxer REST server and a
WakeRequests built over them."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest

from fluxer_panel import cards, wake_requests
from fluxer_panel.db import Database
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.request_commands import _age
from fluxer_panel.wake import WakeRequests
from tests.fakes import FakeFluxerState, make_rest_app
from tests.test_card_commands import World, build

ALICE = "1101"
BOB = "1102"
CAROL = "1103"
NOW = 10_000
TITLE = "Minecraft"
REQUESTED_DM = "Nobody could start Minecraft right now."
DM = "9100"
DM_MESSAGES = f"/api/v1/channels/{DM}/messages"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState()
    server = await aiohttp_server(make_rest_app(state))
    return state, str(server.make_url("/")).rstrip("/")


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def world(db: Database, fake, session: aiohttp.ClientSession) -> World:
    state, base = fake
    built = await build(db, state, base, session)
    built.ctx.wake = WakeRequests(db=db, rest=built.rest, config=built.config, sources=built.ctx.sources,
                                  locks=built.ctx.locks, status_of=lambda _card_id: None,
                                  clock=lambda: float(NOW))
    return built


async def seed_card(db: Database, slug: str = "mc") -> int:
    return await db.run(lambda c: cards.create_card(c, slug=slug, title=TITLE, author_id="1001", now=1))


async def seed_request(db: Database, card_id: int, requester: str, created_at: int) -> None:
    await db.run(lambda c: wake_requests.add_open(c, card_id, requester, created_at))


def dm_texts(state: FakeFluxerState, user_id: str) -> list[str]:
    """The contents posted to a user's DM channel, the requester's or an admin's."""
    channel = state.dm_channels.get(user_id)
    if channel is None:
        return []
    path = f"/api/v1/channels/{channel}/messages"
    return [r.payload["content"] for r in state.requests if r.method == "POST" and r.path == path]


def replies(world: World) -> list[str]:
    return [payload["content"] for payload in world.payloads()]


# --- the list ----------------------------------------------------------------------------------

async def test_list_with_nothing_open_says_no_open_requests(world: World) -> None:
    await world.run("!request list")
    assert replies(world) == ["No open requests."]


async def test_list_gives_slug_count_and_age_per_card_in_slug_order(world: World, db: Database) -> None:
    mc = await seed_card(db, "mc")
    ark = await seed_card(db, "ark")
    await seed_request(db, mc, ALICE, NOW - 300)
    await seed_request(db, mc, BOB, NOW - 60)
    await seed_request(db, ark, CAROL, NOW - 3900)

    await world.run("!request list")

    assert replies(world) == ["ark: 1 requested, open for 1 h 5 min\nmc: 2 requested, open for 5 min"]


async def test_list_is_sent_as_a_message_with_no_mentions(world: World, db: Database) -> None:
    card = await seed_card(db)
    await seed_request(db, card, ALICE, NOW - 10)

    await world.run("!request list")

    [payload] = world.payloads()
    assert payload["allowed_mentions"] == {"parse": []}


# --- clear -------------------------------------------------------------------------------------

async def test_clear_closes_the_rows_as_cleared_and_dms_each_requester_once(world: World, db: Database) -> None:
    card = await seed_card(db)
    await seed_request(db, card, ALICE, NOW - 10)
    await seed_request(db, card, BOB, NOW - 5)

    await world.run("!request clear mc")

    assert replies(world) == ["Cleared the request for mc (2 people)."]
    assert dm_texts(world.state, ALICE) == [REQUESTED_DM]
    assert dm_texts(world.state, BOB) == [REQUESTED_DM]
    assert await db.run(lambda c: wake_requests.open_counts(c)) == {}


async def test_clear_with_nothing_open_says_no_open_request_and_sends_no_dm(world: World, db: Database) -> None:
    await seed_card(db)

    await world.run("!request clear mc")

    [reply] = replies(world)
    assert "no open request" in reply
    assert dm_texts(world.state, ALICE) == []


async def test_clearing_twice_sends_the_requester_one_dm(world: World, db: Database) -> None:
    card = await seed_card(db)
    await seed_request(db, card, ALICE, NOW - 10)

    await world.run("!request clear mc")
    assert dm_texts(world.state, ALICE) == [REQUESTED_DM]

    await world.run("!request clear mc")

    assert "no open request" in replies(world)[-1]
    assert dm_texts(world.state, ALICE) == [REQUESTED_DM]


async def test_clear_of_an_unknown_slug_says_no_such_card(world: World) -> None:
    await world.run("!request clear nope")
    assert replies(world) == ["No such card."]


async def test_the_subcommand_is_case_insensitive(world: World, db: Database) -> None:
    card = await seed_card(db)
    await seed_request(db, card, ALICE, NOW - 10)

    await world.run("!request CLEAR mc")

    assert replies(world) == ["Cleared the request for mc (1 people)."]


# --- usage and access --------------------------------------------------------------------------

@pytest.mark.parametrize("content", ["!request", "!request dance", "!request list mc", "!request clear", "!request clear a b"])
async def test_bad_arguments_get_the_usage_line(world: World, content: str) -> None:
    await world.run(content)
    assert replies(world) == ["Usage: !request list | !request clear <slug>"]


async def test_non_admin_gets_no_reply_and_logs_a_denial(world: World, db: Database,
                                                         caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    card = await seed_card(db)
    await seed_request(db, card, ALICE, NOW - 10)

    await world.run("!request clear mc", author="2002")

    assert world.state.requests == []
    assert await db.run(lambda c: wake_requests.open_counts(c)) == {card: 1}
    denials = [r for r in caplog.records if r.getMessage() == "admin_denied"]
    assert len(denials) == 1


async def test_request_head_without_a_wake_is_ignored(world: World, db: Database) -> None:
    world.ctx.wake = None
    await world.run("!request list")
    assert world.state.requests == []


async def test_clear_logs_the_card_and_action_only(world: World, db: Database,
                                                   caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.request_commands")
    card = await seed_card(db)
    await seed_request(db, card, ALICE, NOW - 10)

    await world.run("!request clear mc")

    [record] = [r for r in caplog.records if r.getMessage() == "admin_command"]
    assert getattr(record, FIELDS_ATTR)["command"] == "request"
    assert getattr(record, FIELDS_ATTR)["action"] == "clear"
    assert getattr(record, FIELDS_ATTR)["card_id"] == card
    assert ALICE not in caplog.text


# --- age ---------------------------------------------------------------------------------------

@pytest.mark.parametrize(("seconds", "expected"), [
    (0, "<1 min"),
    (59, "<1 min"),
    (60, "1 min"),
    (3599, "59 min"),
    (3600, "1 h 0 min"),
    (3900, "1 h 5 min"),
])
def test_age_text(seconds: int, expected: str) -> None:
    assert _age(seconds) == expected
