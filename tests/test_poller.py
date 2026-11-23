# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The card poller against a real SQLite file, the fake Fluxer REST and gateway, the fake Panel and stub probes.

Each test name states the behaviour it checks. The `AC` comments name the acceptance criteria for the card poller.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.events import MessageCreate, MessageDelete
from fluxer_client.gateway import Gateway
from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import (
    admin,
    board,
    cards,
    mirrors,
    play_now,
    polls,
    render,
    sessions,
    subscriptions,
    text,
    wake_requests,
)
from fluxer_panel.bot import Bot
from fluxer_panel.card_commands import CardContext, CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.dm_router import NonAdminLimiter
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.logs import FIELDS_ATTR, setup_logging
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poll_close import PollCloser
from fluxer_panel.poll_jobs import PollJobs
from fluxer_panel.poll_post import PollPost
from fluxer_panel import poller as poller_module
from fluxer_panel.poller import _DELETED_CAP, Poller
from fluxer_panel.presses import WAKE_EMOJI, Presses
from fluxer_panel.resolver import StatusKind
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_flow import SessionFlow
from fluxer_panel.session_jobs import SessionJobs
from fluxer_panel.session_post import SessionPost
from fluxer_panel.sources.base import PlayerCounts, ProbeDetails, ProbeFailed, ProbeResult
from fluxer_panel.sources.pterodactyl import PterodactylManager
from fluxer_panel.sources.registry import Sources
from fluxer_panel.wake import WakeRequests
from tests.fake_ptero import FakePanelState, FakeServer, Scripted, make_panel_app
from tests.fake_ptero import schedule as panel_schedule
from tests.fake_ptero import task as panel_task
from tests.fakes import (
    FakeFluxerState,
    Override,
    Recorded,
    expect,
    hold,
    make_gateway_app,
    make_rest_app,
    message_delete,
    send,
    settle,
    wait_until,
)

TOKEN = "SENTINEL-TOKEN-7f3a9c"
PANEL_KEY = "SENTINEL-PANEL-KEY-91c2"
AUTHOR = "1001"
GUILD = "1001"
GAMES = "3001"
VOICE = "2001"
TEXT = "2002"                                  # a text channel in the same community
SERVER = "abcd1234"
HOST = "127.0.0.1"
PORT = 25565
GAMES_MESSAGES = f"/api/v1/channels/{GAMES}/messages"


def channel_messages(channel: str) -> str:
    return f"/api/v1/channels/{channel}/messages"


POSTED = f"{GAMES_MESSAGES}/5001"
# The summary board already exists in these tests, so card message ids start at 5001 as they always did. The board's
# own posting and sync are covered by tests/test_board_poller.py. Its edits are filtered out of `patches`.
# kept above every fake id, so the stored board stays the newest message and never triggers a re-post
BOARD_MESSAGE = "8999999"
SCHEDULES = f"/api/client/servers/{SERVER}/schedules"
OPEN_DM = "/api/v1/users/@me/channels"
FIRST_DM = "/api/v1/channels/9000/messages"    # the first DM channel the fake opens: admin 1001
SECOND_DM = "/api/v1/channels/9001/messages"   # admin 1002
ADMIN_DM = "9100"                              # where `!card` replies arrive in these tests

RUNNING = ProbeResult(PlayerCounts(1, 4), ProbeDetails())


class FakeClock:
    """The poller's clock: `now` is set by the test and read by the poller."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class StubProbe:
    """A probe plugin double behind the Probe contract. It records calls, then returns `reply` or raises it."""

    reply: ProbeResult | ProbeFailed | BaseException = RUNNING
    gate: asyncio.Event | None = None
    calls: list[tuple[str, int, float]] = field(default_factory=list)

    async def probe(self, host: str, port: int, timeout: float) -> ProbeResult | ProbeFailed:
        self.calls.append((host, port, timeout))
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


async def no_sleep(_seconds: float) -> None:
    return None


def config_for(**overrides: str) -> Config:
    env = {
        "FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC",
        "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
        "ADMIN_USER_IDS": "1001,1002",
        "GAMES_CHANNEL_ID": GAMES,
        "POLL_INTERVAL_SECONDS": "3600",
        "SCHEDULE_REFRESH_SECONDS": "600",
        "PROBE_GRACE_SECONDS": "600",
        "PTERODACTYL_URL": "https://panel.invalid",
        "PTERODACTYL_API_KEY": PANEL_KEY,
    }
    return Config.from_env({**env, **overrides})


@dataclass
class World:
    db: Database
    rest: FluxerREST
    fluxer: FakeFluxerState
    panel: FakePanelState
    clock: FakeClock
    config: Config
    sources: Sources
    locks: CardLocks
    probe: StubProbe

    def make_poller(self, *, config: Config | None = None, sources: Sources | None = None) -> Poller:
        return Poller(db=self.db, rest=self.rest, sources=sources or self.sources, config=config or self.config,
                      locks=self.locks, clock=self.clock)


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
    fluxer_server = await aiohttp_server(make_rest_app(fluxer))
    panel = FakePanelState(servers={SERVER: FakeServer()})
    panel_server = await aiohttp_server(make_panel_app(panel))
    panel_base = str(panel_server.make_url("/")).rstrip("/")
    rest = FluxerREST(session, f"{str(fluxer_server.make_url('/')).rstrip('/')}/api", TOKEN)
    probe = StubProbe()
    fluxer.messages[(GAMES, BOARD_MESSAGE)] = {"embeds": []}
    await db.run(lambda c: board.set_message(c, GAMES, BOARD_MESSAGE, "0" * 64))
    sources = Sources(
        managers={"pterodactyl": PterodactylManager(session, panel_base, PANEL_KEY, sleep=no_sleep)},
        probes={"stub": probe, "boom": StubProbe(reply=RuntimeError("SENTINEL-ERR-5d1e"))},
    )
    yield World(db=db, rest=rest, fluxer=fluxer, panel=panel, clock=FakeClock(), config=config_for(),
                sources=sources, locks=CardLocks(), probe=probe)


@pytest.fixture
async def poller(world: World) -> Poller:
    return world.make_poller()


@pytest.fixture
def root_logging():
    """setup_logging replaces the root handlers and level; restore both so pytest's capture keeps working."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    logging.captureWarnings(False)


# Helpers --------------------------------------------------------------------------------------------

_SLOTS: dict[str, Callable[..., None]] = {
    "voice": lambda c, card_id, value: cards.set_voice(c, card_id, value, slot=1),
    "voice2": lambda c, card_id, value: cards.set_voice(c, card_id, value, slot=2),
    "voice3": lambda c, card_id, value: cards.set_voice(c, card_id, value, slot=3),
    "text_channel": lambda c, card_id, value: cards.set_text_channel(c, card_id, value[0], value[1]),
    "address": lambda c, card_id, value: cards.set_address(c, card_id, value, slot=1),
    "location": cards.set_location,
    "seats": cards.set_seats,
    "manager": cards.set_manager,
    "probe": cards.set_probe,
}


async def seed(db: Database, slug: str, *, title: str = "Card", state: str = "live", **slots: Any) -> int:
    def tx(conn: Any) -> int:
        card_id = cards.create_card(conn, slug=slug, title=title, author_id=AUTHOR, now=0)
        for name, value in slots.items():
            _SLOTS[name](conn, card_id, value)
        cards.set_state(conn, card_id, state, 0)
        return card_id

    return await db.run(tx)


async def load(db: Database, card_id: int) -> cards.Card:
    card = await db.run(lambda c: cards.get_by_id(c, card_id))
    assert card is not None
    return card


async def wait_for_card(db: Database, card_id: int, predicate: Callable[[cards.Card], bool],
                        timeout: float = 5.0) -> cards.Card:
    """Waits for a stored row to satisfy `predicate`. A POST is recorded before the poller stores its id."""
    deadline = time.monotonic() + timeout
    while True:
        card = await load(db, card_id)
        if predicate(card):
            return card
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the card row")
        await asyncio.sleep(0.01)


async def set_title(db: Database, card_id: int, title: str) -> None:
    await db.run(lambda c: cards.update_fields(c, card_id, 0, title=title))


def posts(state: FakeFluxerState) -> list[Recorded]:
    return [r for r in state.requests if r.method == "POST" and r.path == GAMES_MESSAGES]


def patches(state: FakeFluxerState) -> list[Recorded]:
    return [r for r in state.requests if r.method == "PATCH" and r.path != f"{GAMES_MESSAGES}/{BOARD_MESSAGE}"]


def dm_messages(state: FakeFluxerState) -> list[Recorded]:
    return [r for r in state.requests
            if r.method == "POST" and r.path.startswith("/api/v1/channels/") and r.path.endswith("/messages")
            and r.path != GAMES_MESSAGES]


def requests_for(state: FakeFluxerState, method: str, path: str) -> list[Recorded]:
    return [r for r in state.requests if r.method == method and r.path == path]


def schedule_requests(panel: FakePanelState) -> list[Any]:
    return [r for r in panel.requests if r.path == SCHEDULES]


def fields_of(embed: dict[str, Any]) -> dict[str, str]:
    return {f["name"]: f["value"] for f in embed.get("fields", [])}


def records(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == event]


def assert_card_writes_safe(state: FakeFluxerState) -> None:
    """Every message write, to the games channel or a DM, carries an empty `parse` list."""
    writes = [r for r in state.requests if r.method in ("POST", "PATCH") and "/messages" in r.path]
    assert writes, "expected at least one message write"
    for request in writes:
        assert request.payload["allowed_mentions"] == {"parse": []}


def voice_channel(*, name: str = "New", kind: int = 2, guild: str = GUILD) -> dict[str, Any]:
    return {"id": VOICE, "name": name, "type": kind, "guild_id": guild}


def text_channel(*, name: str = "general", kind: int = 0, guild: str = GUILD) -> dict[str, Any]:
    """A text channel as Fluxer reports it: type 0 is a text channel."""
    return {"id": TEXT, "name": name, "type": kind, "guild_id": guild}


async def track_text_channel_writes(db: Database) -> None:
    def tx(conn: Any) -> None:
        conn.execute("CREATE TABLE text_writes (n INTEGER)")
        conn.execute("CREATE TRIGGER count_text_writes AFTER UPDATE ON card_text_channel "
                     "BEGIN INSERT INTO text_writes VALUES (1); END")

    await db.run(tx)


async def text_writes(db: Database) -> int:
    return await db.run(lambda c: c.execute("SELECT COUNT(*) FROM text_writes").fetchone()[0])


async def track_voice_writes(db: Database) -> None:
    def tx(conn: Any) -> None:
        conn.execute("CREATE TABLE voice_writes (n INTEGER)")
        conn.execute("CREATE TRIGGER count_voice_writes AFTER UPDATE ON card_voice "
                     "BEGIN INSERT INTO voice_writes VALUES (1); END")

    await db.run(tx)


async def voice_writes(db: Database) -> int:
    return await db.run(lambda c: c.execute("SELECT COUNT(*) FROM voice_writes").fetchone()[0])


# AC6.1: publishing ---------------------------------------------------------------------------------

async def test_a_live_card_is_posted_and_its_id_and_hash_are_stored(world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", title="Alpha")

    await poller.tick()

    [sent] = posts(world.fluxer)
    card = await load(db, card_id)
    assert card.message_id == "5001"
    assert card.channel_id == GAMES
    assert card.runtime.rendered_hash == render.rendered_hash(sent.payload["embeds"][0])
    assert_card_writes_safe(world.fluxer)


# AC6.2 and AC6.3: edits happen only on a change ------------------------------------------------------

async def test_an_unchanged_rendering_makes_no_edit_request(world: World, poller: Poller, db: Database):
    await seed(db, "alpha")

    await poller.tick()
    await poller.tick()

    assert len(posts(world.fluxer)) == 1
    assert patches(world.fluxer) == []
    assert_card_writes_safe(world.fluxer)


async def test_a_changed_rendering_produces_exactly_one_edit_without_a_content_key(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", title="Alpha")
    await poller.tick()

    await set_title(db, card_id, "Beta")
    await poller.tick()

    [edit] = patches(world.fluxer)
    assert edit.path == POSTED
    assert edit.payload["embeds"][0]["title"] == "Beta"
    assert edit.payload["allowed_mentions"] == {"parse": []}
    assert "content" not in edit.payload
    assert_card_writes_safe(world.fluxer)


# AC6.4: an edit that finds the message gone -------------------------------------------------------

async def test_an_edit_that_returns_404_posts_a_new_message_and_stores_its_id(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger="fluxer_panel.poller")
    card_id = await seed(db, "alpha", title="Alpha")
    await poller.tick()
    world.fluxer.deleted.add((GAMES, "5001"))
    await set_title(db, card_id, "Beta")

    await poller.tick()

    assert [r.status for r in patches(world.fluxer)] == [404]
    assert len(posts(world.fluxer)) == 2
    assert (await load(db, card_id)).message_id == "5002"
    [recreated] = records(caplog, "card_message_recreated")
    assert recreated.panel_fields == {"card_id": card_id}
    assert_card_writes_safe(world.fluxer)


@pytest.mark.parametrize("status", [500, 403], ids=["server-error", "forbidden"])
async def test_an_edit_that_fails_for_another_reason_keeps_the_message_and_posts_nothing(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture, status: int):
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poller")
    card_id = await seed(db, "alpha", title="Alpha")
    await poller.tick()
    await set_title(db, card_id, "Beta")
    world.fluxer.overrides[f"PATCH {POSTED}"] = Override(status, b'{"code": "X", "message": "no"}')

    await poller.tick()

    assert [r.status for r in patches(world.fluxer)] == [status]
    assert len(posts(world.fluxer)) == 1           # no second message is posted over the first
    card = await load(db, card_id)
    assert card.message_id == "5001"
    [failed] = records(caplog, "card_poll_failed")
    assert failed.panel_fields == {"card_id": card_id, "reason": "FluxerHTTPError"}
    assert_card_writes_safe(world.fluxer)


# AC6.5: reconciling stored messages at startup --------------------------------------------------

async def test_startup_recreates_a_stored_message_that_no_longer_exists(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha")
    await db.run(lambda c: cards.set_message(c, card_id, GAMES, "5999", "0" * 64))

    run_task = asyncio.create_task(poller.run())
    try:
        await wait_until(lambda: len(posts(world.fluxer)) == 1)
        card = await wait_for_card(db, card_id, lambda c: c.message_id == "5001")
    finally:
        run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert card.message_id == "5001"
    assert [r.status for r in requests_for(world.fluxer, "GET", "/api/v1/channels/3001/messages/5999")] == [404]


async def test_reconcile_keeps_a_message_that_still_exists(world: World, poller: Poller, db: Database):
    await seed(db, "alpha")
    await poller.tick()
    world.fluxer.requests.clear()

    await poller.reconcile()
    await poller.tick()

    # The board's own lookup is checked after the cards, and it is not a card request.
    card_requests = [r for r in world.fluxer.requests if r.path == POSTED]
    assert [(r.method, r.status) for r in card_requests] == [("GET", 200)]


async def test_reconcile_leaves_the_message_alone_when_the_lookup_fails_for_another_reason(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poller")
    card_id = await seed(db, "alpha")
    await poller.tick()
    world.fluxer.overrides[f"GET {POSTED}"] = Override(500, b'{"code": "X", "message": "no"}')

    await poller.reconcile()

    assert (await load(db, card_id)).message_id == "5001"
    [failed] = records(caplog, "card_reconcile_failed")
    assert failed.panel_fields == {"card_id": card_id, "reason": "FluxerHTTPError"}
    await poller.tick()
    assert len(posts(world.fluxer)) == 1
    assert patches(world.fluxer) == []


async def test_a_transport_error_on_one_lookup_does_not_stop_the_reconcile_of_later_cards(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poller")
    first = await seed(db, "alpha")
    second = await seed(db, "beta")
    await db.run(lambda c: cards.set_message(c, first, GAMES, "5101", "0" * 64))
    await db.run(lambda c: cards.set_message(c, second, GAMES, "5102", "0" * 64))   # unknown to the fake: a 404
    world.fluxer.drops.add(f"GET {GAMES_MESSAGES}/5101")                           # the connection closes

    await poller.reconcile()

    [failed] = records(caplog, "card_reconcile_failed")
    assert failed.panel_fields == {"card_id": first, "reason": "ServerDisconnectedError"}
    assert (await load(db, first)).message_id == "5101"       # a failed lookup leaves the message alone
    assert (await load(db, second)).message_id is None        # the later card was still checked and forgotten
    gets = [(r.path, r.status) for r in world.fluxer.requests
            if r.method == "GET" and r.path != f"{GAMES_MESSAGES}/{BOARD_MESSAGE}"]
    assert gets[-1] == (f"{GAMES_MESSAGES}/5102", 404)     # the later card's lookup ran after the failed one


# AC6.6: MESSAGE_DELETE recreates the card without the poll --------------------------------------------

async def test_a_message_delete_recreates_the_card_without_waiting_for_the_poll(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha")
    run_task = asyncio.create_task(poller.run())
    try:
        await wait_until(lambda: len(posts(world.fluxer)) == 1)
        await poller.on_message_delete(MessageDelete("5001", GAMES, None))
        await wait_until(lambda: len(posts(world.fluxer)) == 2, timeout=2.0)
        await wait_for_card(db, card_id, lambda c: c.message_id == "5002")
    finally:
        run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert_card_writes_safe(world.fluxer)


async def test_a_message_delete_for_another_message_causes_no_post(world: World, poller: Poller, db: Database):
    await seed(db, "alpha")
    await poller.tick()

    await poller.on_message_delete(MessageDelete("9999", GAMES, None))
    await poller.tick()

    assert len(posts(world.fluxer)) == 1


async def test_a_message_delete_over_the_gateway_recreates_the_card(
        world: World, poller: Poller, db: Database, aiohttp_server: Any, session: aiohttp.ClientSession):
    card_id = await seed(db, "alpha")
    deleted = asyncio.Event()

    async def gateway_script(ws: Any, index: int, state: Any) -> None:
        await send(ws, 10, {"heartbeat_interval": 30000})
        await expect(ws, 2)
        await send(ws, 0, {"session_id": "S1", "user": {"id": "5000"}}, t="READY", s=1)
        await deleted.wait()
        await send(ws, 0, message_delete("5001", GAMES), t="MESSAGE_DELETE", s=2)
        await hold(ws)

    gateway_server = await aiohttp_server(make_gateway_app(gateway_script))
    gateway_url = str(gateway_server.make_url("/gateway").with_scheme("ws"))
    bot = Bot(rest=world.rest, config=world.config, roles=RoleCache(world.rest),
              on_message_delete=poller.on_message_delete)
    gateway = Gateway(session, gateway_url, TOKEN, bot.on_dispatch, rng=random.Random(0))
    run_task = asyncio.create_task(poller.run())
    gateway_task = asyncio.create_task(gateway.run())
    try:
        await wait_until(lambda: len(posts(world.fluxer)) == 1)
        world.fluxer.deleted.add((GAMES, "5001"))
        deleted.set()
        await wait_until(lambda: len(posts(world.fluxer)) == 2, timeout=5.0)
        await wait_for_card(db, card_id, lambda c: c.message_id == "5002")
    finally:
        await gateway.stop()
        await asyncio.wait_for(gateway_task, 5.0)
        run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert_card_writes_safe(world.fluxer)


async def test_a_message_delete_during_an_edit_is_not_written_back_and_the_card_is_reposted(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", title="Alpha")
    run_task = asyncio.create_task(poller.run())
    try:
        await wait_until(lambda: len(posts(world.fluxer)) == 1)
        await wait_for_card(db, card_id, lambda c: c.message_id == "5001")
        await set_title(db, card_id, "Beta")
        world.fluxer.delays[f"PATCH {POSTED}"] = 0.3
        poller.kick()
        await asyncio.sleep(0.1)     # the edit is delayed 0.3 s, so it is still in flight here
        await poller.on_message_delete(MessageDelete("5001", GAMES, None))
        await wait_until(lambda: len(posts(world.fluxer)) == 2, timeout=2.0)
        card = await wait_for_card(db, card_id, lambda c: c.message_id == "5002")
    finally:
        run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert card.runtime.rendered_hash is not None
    assert len(patches(world.fluxer)) == 1
    assert_card_writes_safe(world.fluxer)


async def test_a_message_delete_for_a_message_still_being_posted_causes_a_repost(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha")
    world.fluxer.delays[f"POST {GAMES_MESSAGES}"] = 0.3
    run_task = asyncio.create_task(poller.run())
    try:
        await asyncio.sleep(0.1)     # the first post is delayed 0.3 s, so it is still in flight here
        await poller.on_message_delete(MessageDelete("5001", GAMES, None))   # the id the fake hands out first
        await wait_until(lambda: len(posts(world.fluxer)) == 2, timeout=2.0)
        await wait_for_card(db, card_id, lambda c: c.message_id == "5002")
    finally:
        run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert_card_writes_safe(world.fluxer)


@pytest.mark.parametrize("later_deletes, reposts", [(_DELETED_CAP - 1, True), (_DELETED_CAP, False)],
                         ids=["still-remembered", "evicted"])
async def test_a_delete_is_remembered_for_the_most_recent_deletes_only(
        world: World, poller: Poller, db: Database, later_deletes: int, reposts: bool):
    card_id = await seed(db, "alpha")
    world.fluxer.delays[f"POST {GAMES_MESSAGES}"] = 0.5
    run_task = asyncio.create_task(poller.run())
    try:
        await asyncio.sleep(0.1)     # the first post (id 5001) is still in flight for the next 0.5 s
        await poller.on_message_delete(MessageDelete("5001", GAMES, None))
        for index in range(later_deletes):
            await poller.on_message_delete(MessageDelete(str(9000 + index), GAMES, None))
        await wait_for_card(db, card_id, lambda c: c.message_id is not None)
        await asyncio.sleep(0.3)     # a repost, if any, follows the store at once
    finally:
        run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert len(posts(world.fluxer)) == (2 if reposts else 1)
    assert_card_writes_safe(world.fluxer)


# AC6.11: a kick shows a change without waiting for the poll --------------------------------------

async def test_a_kick_publishes_a_change_without_waiting_for_the_poll(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", title="Alpha")
    run_task = asyncio.create_task(poller.run())
    try:
        await wait_until(lambda: len(posts(world.fluxer)) == 1)
        await wait_for_card(db, card_id, lambda c: c.message_id == "5001")
        await set_title(db, card_id, "Beta")
        poller.kick()
        await wait_until(lambda: len(patches(world.fluxer)) == 1, timeout=2.0)
    finally:
        run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert_card_writes_safe(world.fluxer)


# AC6.6: a late delete for a message the poller has replaced clears nothing ------------------------

async def test_a_late_delete_of_a_replaced_message_clears_nothing_and_kicks_nothing(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", title="Alpha")
    run_task = asyncio.create_task(poller.run())
    try:
        await wait_until(lambda: len(posts(world.fluxer)) == 1)
        await wait_for_card(db, card_id, lambda c: c.message_id == "5001")
        world.fluxer.deleted.add((GAMES, "5001"))
        await set_title(db, card_id, "Beta")
        poller.kick()
        await wait_until(lambda: len(posts(world.fluxer)) == 2, timeout=2.0)
        await wait_for_card(db, card_id, lambda c: c.message_id == "5002")

        # A title change with no kick: only a tick would edit, so a kick from the late delete shows as a PATCH.
        await set_title(db, card_id, "Gamma")
        await poller.on_message_delete(MessageDelete("5001", GAMES, None))
        await asyncio.sleep(0.3)
    finally:
        run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert len(patches(world.fluxer)) == 1          # only the 404 edit from the replaced message
    assert (await load(db, card_id)).message_id == "5002"


# AC6.7: one failing card does not stop the others --------------------------------------------------

async def test_a_failing_card_does_not_stop_the_other_cards(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poller")
    failing = await seed(db, "alpha", probe=cards.ProbeRef("boom", HOST, PORT))
    other = await seed(db, "beta")

    await poller.tick()

    assert (await load(db, other)).message_id == "5001"
    [record] = records(caplog, "card_poll_failed")
    assert record.panel_fields == {"card_id": failing, "reason": "RuntimeError"}
    assert "SENTINEL-ERR" not in caplog.text
    assert_card_writes_safe(world.fluxer)


async def test_a_failed_tick_is_logged_and_the_loop_keeps_running(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.ERROR, logger="fluxer_panel.poller")
    await seed(db, "alpha")
    # A real database failure: the card's runtime table is unreadable, so every tick's card load raises.
    await db.run(lambda c: c.execute("ALTER TABLE card_runtime RENAME TO card_runtime_away"))
    run_task = asyncio.create_task(poller.run())
    try:
        await wait_until(lambda: bool(records(caplog, "poller_tick_failed")))
        await db.run(lambda c: c.execute("ALTER TABLE card_runtime_away RENAME TO card_runtime"))
        poller.kick()
        await wait_until(lambda: len(posts(world.fluxer)) == 1)
        assert not run_task.done()
    finally:
        run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert records(caplog, "poller_reconcile_failed")


# AC6.8: draft, archived and none/none cards -------------------------------------------------------

async def test_draft_and_archived_cards_are_never_posted_and_none_cards_make_no_source_calls(
        world: World, poller: Poller, db: Database):
    manager = cards.ManagerRef("pterodactyl", SERVER)
    probe = cards.ProbeRef("stub", HOST, PORT)
    await seed(db, "draft", state="draft", manager=manager, probe=probe)
    await seed(db, "old", state="archived", manager=manager, probe=probe)
    plain = await seed(db, "plain")

    await poller.tick()

    assert len(posts(world.fluxer)) == 1
    assert (await load(db, plain)).message_id == "5001"
    assert world.probe.calls == []
    assert world.panel.requests == []
    assert_card_writes_safe(world.fluxer)


# AC6.9: schedules and probes ------------------------------------------------------------------------

async def test_schedules_are_requested_once_per_refresh_and_probed_only_while_running(
        world: World, poller: Poller, db: Database):
    await seed(db, "alpha", manager=cards.ManagerRef("pterodactyl", SERVER), probe=cards.ProbeRef("stub", HOST, PORT))
    server = world.panel.servers[SERVER]

    for now, state in [(0, "offline"), (10, "running"), (599, "offline"), (600, "running")]:
        server.current_state = state
        world.clock.now = now
        await poller.tick()

    assert [r.path for r in schedule_requests(world.panel)] == [SCHEDULES, SCHEDULES]
    assert len(world.probe.calls) == 2
    assert_card_writes_safe(world.fluxer)


async def test_a_probe_only_card_probes_on_every_tick(world: World, poller: Poller, db: Database):
    await seed(db, "alpha", probe=cards.ProbeRef("stub", HOST, PORT))

    for now in (0, 10, 20):
        world.clock.now = now
        await poller.tick()

    assert len(world.probe.calls) == 3


async def test_an_archived_card_forgets_its_schedule_and_voice_check_so_a_republish_fetches_them_again(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", voice=cards.ChannelLink(GUILD, VOICE, "Old", True),
                         manager=cards.ManagerRef("pterodactyl", SERVER))
    world.panel.servers[SERVER].current_state = "running"
    world.fluxer.channels[VOICE] = voice_channel(name="Old")

    world.clock.now = 0
    await poller.tick()                        # the schedule and the voice name are fetched at 0
    await db.run(lambda c: cards.set_state(c, card_id, "archived", 0))
    world.clock.now = 10
    await poller.tick()                        # the archived card is not loaded, so its cache entries are dropped
    await db.run(lambda c: cards.set_state(c, card_id, "live", 0))
    world.clock.now = 20                       # inside both refresh intervals: only the pruning can cause a fetch
    await poller.tick()

    assert len(schedule_requests(world.panel)) == 2
    assert len(requests_for(world.fluxer, "GET", f"/api/v1/channels/{VOICE}")) == 2


# AC4.7 (poller side): a schedule failure hides only the Next line ----------------------------------

async def test_a_failed_schedule_request_hides_next_and_leaves_status_unchanged(
        world: World, poller: Poller, db: Database):
    await seed(db, "alpha", manager=cards.ManagerRef("pterodactyl", SERVER))
    world.panel.servers[SERVER].current_state = "running"
    world.panel.servers[SERVER].schedules = [
        panel_schedule("restart", next_run_at=_iso(700 + 3600), tasks=(panel_task(1, "power", "restart"),)),
    ]
    world.panel.scripted[f"GET {SCHEDULES}"] = [Scripted(status=500) for _ in range(3)]

    await poller.tick()

    [first] = posts(world.fluxer)
    failed = fields_of(first.payload["embeds"][0])
    assert failed[text.FIELD_STATUS] == text.STATUS_READY
    assert text.FIELD_NEXT not in failed

    world.clock.now = 700
    await poller.tick()

    [edit] = patches(world.fluxer)
    working = fields_of(edit.payload["embeds"][0])
    assert working[text.FIELD_STATUS] == failed[text.FIELD_STATUS]
    assert working[text.FIELD_NEXT].startswith(text.NEXT_RESTART)


async def test_a_cached_action_that_has_passed_is_not_shown_until_the_next_refresh(
        world: World, poller: Poller, db: Database):
    await seed(db, "alpha", manager=cards.ManagerRef("pterodactyl", SERVER))
    world.panel.servers[SERVER].current_state = "running"
    world.panel.servers[SERVER].schedules = [
        panel_schedule("restart", next_run_at=_iso(100), tasks=(panel_task(1, "power", "restart"),)),
    ]

    await poller.tick()
    [first] = posts(world.fluxer)
    assert fields_of(first.payload["embeds"][0])[text.FIELD_NEXT].startswith(text.NEXT_RESTART)

    world.clock.now = 200
    await poller.tick()

    [edit] = patches(world.fluxer)
    assert text.FIELD_NEXT not in fields_of(edit.payload["embeds"][0])
    assert len(schedule_requests(world.panel)) == 1


async def test_a_changed_manager_server_is_asked_for_its_own_schedule_at_once(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", manager=cards.ManagerRef("pterodactyl", SERVER))
    other_server = "ef012345"
    world.panel.servers[SERVER].current_state = "running"
    world.panel.servers[SERVER].schedules = [
        panel_schedule("restart", next_run_at=_iso(3600), tasks=(panel_task(1, "power", "restart"),)),
    ]
    world.panel.servers[other_server] = FakeServer(current_state="running", schedules=[
        panel_schedule("stop", next_run_at=_iso(1800), tasks=(panel_task(1, "power", "stop"),)),
    ])

    await poller.tick()
    [first] = posts(world.fluxer)
    assert fields_of(first.payload["embeds"][0])[text.FIELD_NEXT].startswith(text.NEXT_RESTART)

    await db.run(lambda c: cards.set_manager(c, card_id, cards.ManagerRef("pterodactyl", other_server)))
    world.clock.now = 10       # well inside the refresh interval: only the changed manager can cause a request
    await poller.tick()

    schedule_paths = [r.path for r in world.panel.requests if r.path.endswith("/schedules")]
    assert schedule_paths == [SCHEDULES, f"/api/client/servers/{other_server}/schedules"]
    [edit] = patches(world.fluxer)
    assert fields_of(edit.payload["embeds"][0])[text.FIELD_NEXT].startswith(text.NEXT_SLEEP)
    assert_card_writes_safe(world.fluxer)


async def test_an_unconfigured_manager_kind_shows_unknown_without_a_panel_request(
        world: World, db: Database):
    await seed(db, "alpha", manager=cards.ManagerRef("pterodactyl", SERVER))
    world.panel.servers[SERVER].current_state = "running"
    poller = world.make_poller(sources=Sources(managers={}, probes={"stub": world.probe}))

    await poller.tick()

    [sent] = posts(world.fluxer)
    assert fields_of(sent.payload["embeds"][0])[text.FIELD_STATUS] == text.STATUS_UNKNOWN
    assert world.panel.requests == []


async def test_an_unknown_probe_kind_reads_as_a_failed_probe(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poller")
    await seed(db, "alpha", probe=cards.ProbeRef("nope", HOST, PORT))

    await poller.tick()

    [sent] = posts(world.fluxer)
    assert fields_of(sent.payload["embeds"][0])[text.FIELD_STATUS] == text.STATUS_NOT_RESPONDING
    assert records(caplog, "card_poll_failed") == []


# AC6.10: the probe-failure DM ------------------------------------------------------------------------

async def _failing_running_card(world: World, db: Database) -> int:
    world.panel.servers[SERVER].current_state = "running"
    world.probe.reply = ProbeFailed("timeout")
    return await seed(db, "alpha", manager=cards.ManagerRef("pterodactyl", SERVER),
                      probe=cards.ProbeRef("stub", HOST, PORT))


def _alert_texts(state: FakeFluxerState) -> list[str]:
    return [r.payload["content"] for r in dm_messages(state)]


async def test_a_probe_failure_alerts_each_admin_once_and_not_again_after_a_restart(
        world: World, poller: Poller, db: Database):
    card_id = await _failing_running_card(world, db)
    for now in (0, 300, 600):
        world.clock.now = now
        await poller.tick()

    opened = requests_for(world.fluxer, "POST", OPEN_DM)
    assert len(opened) == 2
    assert _alert_texts(world.fluxer) == [text.PROBE_ALERT.format(slug="alpha")] * 2
    assert [r.path for r in dm_messages(world.fluxer)] == [FIRST_DM, SECOND_DM]

    world.clock.now = 700
    await poller.tick()
    world.clock.now = 800
    await world.make_poller().tick()
    assert len(dm_messages(world.fluxer)) == 2
    assert (await load(db, card_id)).runtime.probe_alert_sent is True
    assert_card_writes_safe(world.fluxer)


async def test_a_probe_success_clears_the_alert_flag(world: World, db: Database):
    card_id = await _failing_running_card(world, db)
    for now in (0, 300, 600):
        world.clock.now = now
        await world.make_poller().tick()

    world.probe.reply = RUNNING
    world.clock.now = 900
    await world.make_poller().tick()

    card = await load(db, card_id)
    assert card.runtime.probe_alert_sent is False
    assert card.runtime.probe_failing_since is None


async def test_the_alert_flag_is_saved_before_the_dm_is_sent(world: World, poller: Poller, db: Database):
    card_id = await _failing_running_card(world, db)
    for now in (0, 300):
        world.clock.now = now
        await poller.tick()
    world.fluxer.delays[f"POST {FIRST_DM}"] = 1.0

    world.clock.now = 600
    tick = asyncio.create_task(poller.tick())
    await wait_for_card(db, card_id, lambda c: c.runtime.probe_alert_sent is True)
    assert dm_messages(world.fluxer) == []     # the first DM is still in flight, and the flag is already saved
    await tick
    assert len(dm_messages(world.fluxer)) == 2


async def test_one_admin_dm_failure_does_not_stop_the_other_alert(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poller")
    await _failing_running_card(world, db)
    world.fluxer.overrides[f"POST {FIRST_DM}"] = Override(500, b'{"code": "X", "message": "no"}')
    for now in (0, 600):
        world.clock.now = now
        await poller.tick()

    assert [r.status for r in dm_messages(world.fluxer)] == [500, 200]
    [failed] = records(caplog, "probe_alert_failed")
    assert failed.panel_fields == {"user_id": "1001", "reason": "FluxerHTTPError"}


# Commands racing the poller ------------------------------------------------------------------------

async def test_an_archive_during_a_probe_stops_the_post(world: World, poller: Poller, db: Database):
    world.probe.gate = asyncio.Event()
    card_id = await seed(db, "alpha", probe=cards.ProbeRef("stub", HOST, PORT))
    tick = asyncio.create_task(poller.tick())
    await wait_until(lambda: len(world.probe.calls) == 1)

    ctx = CardContext(db=db, rest=world.rest, config=world.config, sources=world.sources, locks=world.locks)
    await admin.handle(_admin_message("!card archive alpha"), world.config, world.rest, ctx)
    world.probe.gate.set()
    await tick

    assert posts(world.fluxer) == []
    card = await load(db, card_id)
    assert card.message_id is None
    assert card.state == "archived"


async def test_an_archive_during_a_post_waits_for_the_post_and_deletes_the_message(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha")
    world.fluxer.delays[f"POST {GAMES_MESSAGES}"] = 0.3
    tick = asyncio.create_task(poller.tick())
    await asyncio.sleep(0.1)     # the POST is delayed 0.3 s, so it is still in flight here
    assert not tick.done()

    ctx = CardContext(db=db, rest=world.rest, config=world.config, sources=world.sources, locks=world.locks)
    await admin.handle(_admin_message("!card archive alpha"), world.config, world.rest, ctx)
    await tick

    deletes = [r for r in world.fluxer.requests if r.method == "DELETE" and r.path == POSTED]
    assert [r.status for r in deletes] == [204]     # the archive removed the message the post stored
    card = await load(db, card_id)
    assert card.state == "archived"
    assert card.message_id is None
    assert_card_writes_safe(world.fluxer)


def _admin_message(content: str) -> MessageCreate:
    return MessageCreate(message_id="8001", channel_id=ADMIN_DM, guild_id=None, author_id=AUTHOR,
                         author_is_bot=False, member_role_ids=None, content=content)


# Logging, games channel and voice ------------------------------------------------------------------

async def test_a_missing_games_channel_is_logged_once_and_nothing_is_posted(
        world: World, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poller")
    await seed(db, "alpha")
    poller = world.make_poller(config=config_for(GAMES_CHANNEL_ID=""))

    await poller.tick()
    await poller.tick()

    assert len(records(caplog, "games_channel_unset")) == 1
    assert posts(world.fluxer) == []


async def test_the_poller_logs_no_card_text_or_probe_detail(
        world: World, db: Database, root_logging: None):
    stream = io.StringIO()
    setup_logging("DEBUG", [TOKEN], stream)
    sentinels = ["SENTINEL-TITLE-5d1e", "SENTINEL-LOC-5d1e", "sentinel-host.invalid", "SENTINEL-VOICE-5d1e",
                 "SENTINEL-MAP-5d1e", "SENTINEL-TEXT-5d1e"]
    world.fluxer.channels[VOICE] = voice_channel(name="SENTINEL-VOICE-5d1e")
    world.fluxer.channels[TEXT] = text_channel(name="SENTINEL-TEXT-5d1e")
    world.probe.reply = ProbeResult(PlayerCounts(1, 4), ProbeDetails(map="SENTINEL-MAP-5d1e"))
    card_id = await seed(db, "alpha", title="SENTINEL-TITLE-5d1e",
                         location=cards.Location("SENTINEL-LOC-5d1e"),
                         voice=cards.ChannelLink(GUILD, VOICE, "SENTINEL-VOICE-5d1e", True),
                         text_channel=(1, cards.ChannelLink(GUILD, TEXT, "SENTINEL-TEXT-5d1e", True)),
                         probe=cards.ProbeRef("stub", "sentinel-host.invalid", PORT))
    await db.run(lambda c: cards.update_fields(c, card_id, 0, show_details=("map",)))
    await seed(db, "beta", probe=cards.ProbeRef("boom", "sentinel-host.invalid", PORT))

    await world.make_poller().tick()

    logged = stream.getvalue()
    assert "card_poll_failed" in logged
    for sentinel in sentinels:
        assert sentinel not in logged


async def test_an_unchanged_voice_name_is_not_written(world: World, poller: Poller, db: Database):
    await track_voice_writes(db)
    await seed(db, "alpha", voice=cards.ChannelLink(GUILD, VOICE, "Old", True))
    world.fluxer.channels[VOICE] = voice_channel(name="Old")

    await poller.tick()

    assert await voice_writes(db) == 0


async def test_a_changed_voice_name_is_refreshed_and_shown(world: World, poller: Poller, db: Database):
    await track_voice_writes(db)
    card_id = await seed(db, "alpha", voice=cards.ChannelLink(GUILD, VOICE, "Old", True))
    world.fluxer.channels[VOICE] = voice_channel(name="New")

    await poller.tick()

    assert (await load(db, card_id)).voice.channel_name == "New"
    assert await voice_writes(db) == 1
    [sent] = posts(world.fluxer)
    assert "#New" in fields_of(sent.payload["embeds"][0])[text.FIELD_VOICE]


@pytest.mark.parametrize("slot", ["voice2", "voice3"])
async def test_a_changed_name_in_voice_slots_two_and_three_is_refreshed_and_shown(
        world: World, poller: Poller, db: Database, slot: str):
    await track_voice_writes(db)
    card_id = await seed(db, "alpha", **{slot: cards.ChannelLink(GUILD, VOICE, "Old", True, suffix="Raid")})
    world.fluxer.channels[VOICE] = voice_channel(name="New")

    await poller.tick()

    card = await load(db, card_id)
    assert getattr(card, slot) == cards.ChannelLink(GUILD, VOICE, "New", True, suffix="Raid")
    assert [v for v in card.voices if v is not None] == [getattr(card, slot)]
    assert await voice_writes(db) == 1
    [sent] = posts(world.fluxer)
    assert "#New" in fields_of(sent.payload["embeds"][0])["Voice (Raid)"]


async def test_every_voice_slot_is_refreshed_in_one_check(world: World, poller: Poller, db: Database):
    await track_voice_writes(db)
    card_id = await seed(db, "alpha", voice=cards.ChannelLink(GUILD, VOICE, "Old", True),
                         voice2=cards.ChannelLink(GUILD, VOICE, "Older", True),
                         voice3=cards.ChannelLink(GUILD, VOICE, "Oldest", True))
    world.fluxer.channels[VOICE] = voice_channel(name="New")

    await poller.tick()

    assert [v.channel_name for v in (await load(db, card_id)).voices] == ["New", "New", "New"]
    assert await voice_writes(db) == 3


async def test_a_voice_slot_cleared_during_a_name_refresh_stays_cleared(world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", voice3=cards.ChannelLink(GUILD, VOICE, "Old", True))
    world.fluxer.channels[VOICE] = voice_channel(name="New")
    world.fluxer.delays[f"GET /api/v1/channels/{VOICE}"] = 0.3
    tick = asyncio.create_task(poller.tick())
    await asyncio.sleep(0.1)     # the admin's clear of slot 3 lands during the lookup
    await db.run(lambda c: cards.clear_voice(c, card_id, 3))
    await tick

    assert (await load(db, card_id)).voices == (None, None, None)
    assert_card_writes_safe(world.fluxer)


async def test_a_voice_cleared_during_a_name_refresh_stays_cleared(world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", voice=cards.ChannelLink(GUILD, VOICE, "Old", True))
    world.fluxer.channels[VOICE] = voice_channel(name="New")
    world.fluxer.delays[f"GET /api/v1/channels/{VOICE}"] = 0.3
    tick = asyncio.create_task(poller.tick())
    await asyncio.sleep(0.1)     # the channel lookup is delayed 0.3 s, so the admin's clear lands during it
    await db.run(lambda c: cards.clear_voice(c, card_id, 1))
    await tick

    assert (await load(db, card_id)).voice is None
    [sent] = posts(world.fluxer)
    assert text.FIELD_VOICE not in fields_of(sent.payload["embeds"][0])
    assert_card_writes_safe(world.fluxer)


async def test_a_voice_set_during_a_name_refresh_stays_set(world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", voice=cards.ChannelLink(GUILD, VOICE, "Old", True))
    world.fluxer.channels[VOICE] = voice_channel(name="New")
    world.fluxer.delays[f"GET /api/v1/channels/{VOICE}"] = 0.3
    tick = asyncio.create_task(poller.tick())
    await asyncio.sleep(0.1)
    admin_choice = cards.ChannelLink(GUILD, "2002", "Admin", True)
    await db.run(lambda c: cards.set_voice(c, card_id, admin_choice, slot=1))
    await tick

    assert (await load(db, card_id)).voice == admin_choice
    [sent] = posts(world.fluxer)
    assert "#Admin" in fields_of(sent.payload["embeds"][0])[text.FIELD_VOICE]
    assert_card_writes_safe(world.fluxer)


@pytest.mark.parametrize("change", [{"name": "   "}, {"kind": 0}, {"guild": "9999"}],
                         ids=["blank-name", "not-voice", "other-community"])
async def test_a_voice_refresh_keeps_the_stored_name_when_fluxer_does_not_confirm_it(
        world: World, poller: Poller, db: Database, change: dict[str, Any]):
    await track_voice_writes(db)
    card_id = await seed(db, "alpha", voice=cards.ChannelLink(GUILD, VOICE, "Old", True))
    world.fluxer.channels[VOICE] = voice_channel(**{"name": "New", **change})

    await poller.tick()

    assert await voice_writes(db) == 0
    assert (await load(db, card_id)).voice.channel_name == "Old"


async def test_a_voice_name_is_checked_once_per_refresh_interval(world: World, poller: Poller, db: Database):
    await seed(db, "alpha", voice=cards.ChannelLink(GUILD, VOICE, "Old", True))
    world.fluxer.channels[VOICE] = voice_channel(name="Old")

    for now in (0, 10, 599, 600):
        world.clock.now = now
        await poller.tick()

    assert len(requests_for(world.fluxer, "GET", f"/api/v1/channels/{VOICE}")) == 2


async def test_an_unverified_voice_is_never_checked(world: World, poller: Poller, db: Database):
    await seed(db, "alpha", voice=cards.ChannelLink(GUILD, VOICE, None, False))
    world.fluxer.channels[VOICE] = voice_channel(name="New")

    await poller.tick()

    assert requests_for(world.fluxer, "GET", f"/api/v1/channels/{VOICE}") == []


async def test_a_failed_voice_check_is_logged_and_the_card_still_posts(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poller")
    card_id = await seed(db, "alpha", voice=cards.ChannelLink(GUILD, VOICE, "Old", True))
    world.fluxer.overrides[f"GET /api/v1/channels/{VOICE}"] = Override(500, b'{"code": "X", "message": "no"}')

    await poller.tick()

    [failed] = records(caplog, "voice_refresh_failed")
    assert failed.panel_fields == {"card_id": card_id, "reason": "FluxerHTTPError"}
    assert len(posts(world.fluxer)) == 1


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


# Text channel names: refreshed on the same schedule and with the same checks as the voice name ------------

async def test_a_changed_text_channel_name_is_refreshed_and_shown(world: World, poller: Poller, db: Database):
    await track_text_channel_writes(db)
    card_id = await seed(db, "alpha", text_channel=(1, cards.ChannelLink(GUILD, TEXT, "Old", True)))
    world.fluxer.channels[TEXT] = text_channel(name="New")

    await poller.tick()

    assert (await load(db, card_id)).text_channels[0].channel_name == "New"
    assert await text_writes(db) == 1
    [sent] = posts(world.fluxer)
    assert "#New" in fields_of(sent.payload["embeds"][0])[text.FIELD_TEXT_CHANNEL]


async def test_an_unchanged_text_channel_name_is_not_written(world: World, poller: Poller, db: Database):
    await track_text_channel_writes(db)
    await seed(db, "alpha", text_channel=(2, cards.ChannelLink(GUILD, TEXT, "general", True)))
    world.fluxer.channels[TEXT] = text_channel(name="general")

    await poller.tick()

    assert await text_writes(db) == 0


async def test_each_verified_channel_link_is_refreshed_in_its_own_slot(world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", voice=cards.ChannelLink(GUILD, VOICE, "Old", True),
                         text_channel=(1, cards.ChannelLink(GUILD, TEXT, "Old", True)))
    await db.run(lambda c: cards.set_text_channel(c, card_id, 3, cards.ChannelLink(GUILD, "2003", "Third", True)))
    world.fluxer.channels[VOICE] = voice_channel(name="NewVoice")
    world.fluxer.channels[TEXT] = text_channel(name="NewText")
    world.fluxer.channels["2003"] = {"id": "2003", "name": "Third", "type": 0, "guild_id": GUILD}

    await poller.tick()

    card = await load(db, card_id)
    assert card.voice.channel_name == "NewVoice"
    assert card.text_channels == (cards.ChannelLink(GUILD, TEXT, "NewText", True), None,
                                  cards.ChannelLink(GUILD, "2003", "Third", True))


@pytest.mark.parametrize("change", [{"kind": 2}, {"guild": "9999"}], ids=["not-text", "other-community"])
async def test_a_text_channel_refresh_keeps_the_stored_name_when_fluxer_does_not_confirm_it(
        world: World, poller: Poller, db: Database, change: dict[str, Any]):
    await track_text_channel_writes(db)
    card_id = await seed(db, "alpha", text_channel=(1, cards.ChannelLink(GUILD, TEXT, "Old", True)))
    world.fluxer.channels[TEXT] = text_channel(**{"name": "New", **change})

    await poller.tick()

    assert await text_writes(db) == 0
    assert (await load(db, card_id)).text_channels[0].channel_name == "Old"


async def test_a_text_channel_name_is_checked_once_per_refresh_interval(world: World, poller: Poller, db: Database):
    await seed(db, "alpha", text_channel=(1, cards.ChannelLink(GUILD, TEXT, "general", True)))
    world.fluxer.channels[TEXT] = text_channel(name="general")

    for now in (0, 10, 599, 600):
        world.clock.now = now
        await poller.tick()

    assert len(requests_for(world.fluxer, "GET", f"/api/v1/channels/{TEXT}")) == 2


async def test_an_unverified_text_channel_is_never_checked(world: World, poller: Poller, db: Database):
    await seed(db, "alpha", text_channel=(1, cards.ChannelLink(GUILD, TEXT, None, False)))
    world.fluxer.channels[TEXT] = text_channel(name="general")

    await poller.tick()

    assert requests_for(world.fluxer, "GET", f"/api/v1/channels/{TEXT}") == []


async def test_a_failed_text_channel_check_is_logged_by_card_and_reason_and_the_card_still_posts(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poller")
    card_id = await seed(db, "alpha", text_channel=(1, cards.ChannelLink(GUILD, TEXT, "Old", True)))
    world.fluxer.overrides[f"GET /api/v1/channels/{TEXT}"] = Override(500, b'{"code": "X", "message": "no"}')

    await poller.tick()

    [failed] = records(caplog, "text_channel_refresh_failed")
    assert failed.panel_fields == {"card_id": card_id, "reason": "FluxerHTTPError"}
    assert len(posts(world.fluxer)) == 1


async def test_a_text_channel_cleared_during_a_name_refresh_stays_cleared(world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", text_channel=(1, cards.ChannelLink(GUILD, TEXT, "Old", True)))
    world.fluxer.channels[TEXT] = text_channel(name="New")
    world.fluxer.delays[f"GET /api/v1/channels/{TEXT}"] = 0.3
    tick = asyncio.create_task(poller.tick())
    await asyncio.sleep(0.1)
    await db.run(lambda c: cards.clear_text_channel(c, card_id, 1))
    await tick

    assert (await load(db, card_id)).text_channels == (None, None, None)
    [sent] = posts(world.fluxer)
    assert text.FIELD_TEXT_CHANNEL not in fields_of(sent.payload["embeds"][0])
    assert_card_writes_safe(world.fluxer)


async def test_a_text_channel_set_during_a_name_refresh_stays_set(world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", text_channel=(1, cards.ChannelLink(GUILD, TEXT, "Old", True)))
    world.fluxer.channels[TEXT] = text_channel(name="New")
    world.fluxer.delays[f"GET /api/v1/channels/{TEXT}"] = 0.3
    tick = asyncio.create_task(poller.tick())
    await asyncio.sleep(0.1)
    admin_choice = cards.ChannelLink(GUILD, "2004", "Admin", True)
    await db.run(lambda c: cards.set_text_channel(c, card_id, 1, admin_choice))
    await tick

    assert (await load(db, card_id)).text_channels[0] == admin_choice
    [sent] = posts(world.fluxer)
    assert "#Admin" in fields_of(sent.payload["embeds"][0])[text.FIELD_TEXT_CHANNEL]
    assert_card_writes_safe(world.fluxer)


# Card subscriptions: a copy of the card in another text channel ----------------------------------------

SUB = "2003"                # a text channel the card is subscribed to
SUB2 = "2004"               # a second subscribed channel
SUB_MESSAGES = f"/api/v1/channels/{SUB}/messages"
SUB_MESSAGE = "5002"        # the copy's first id: the card's own message took 5001 in the same tick


async def subscribe(db: Database, card_id: int, channel: str, *, paused: bool = False) -> None:
    def tx(conn: Any) -> None:
        subscriptions.add(conn, card_id, GUILD, channel)
        if paused:
            subscriptions.pause(conn, card_id, channel)

    await db.run(tx)


async def subscription(db: Database, card_id: int, channel: str) -> subscriptions.Subscription:
    sub = await db.run(lambda c: subscriptions.get(c, card_id, channel))
    assert sub is not None
    return sub


async def _wait_for_subscription(db: Database, card_id: int, channel: str,
                                 predicate: Callable[[subscriptions.Subscription], bool]) -> None:
    deadline = time.monotonic() + 5.0
    while True:
        sub = await db.run(lambda c: subscriptions.get(c, card_id, channel))
        if sub is not None and predicate(sub):
            return
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the subscription row")
        await asyncio.sleep(0.01)


def copy_posts(state: FakeFluxerState, channel: str) -> list[Recorded]:
    return [r for r in state.requests if r.method == "POST" and r.path == f"/api/v1/channels/{channel}/messages"]


def copy_patches(state: FakeFluxerState, channel: str) -> list[Recorded]:
    return [r for r in state.requests
            if r.method == "PATCH" and r.path.startswith(f"/api/v1/channels/{channel}/messages/")]


def admin_dms(state: FakeFluxerState) -> list[Recorded]:
    return [r for r in state.requests if r.method == "POST" and r.path in (FIRST_DM, SECOND_DM)]


async def test_a_subscription_gets_a_copy_of_the_card_with_the_same_embed_and_no_mentions(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", title="Alpha")
    await subscribe(db, card_id, SUB)

    await poller.tick()

    [main] = posts(world.fluxer)
    [copy] = copy_posts(world.fluxer, SUB)
    assert copy.payload == main.payload
    sub = await subscription(db, card_id, SUB)
    assert (sub.message_id, sub.content_hash) == (SUB_MESSAGE, render.rendered_hash(main.payload["embeds"][0]))
    assert_card_writes_safe(world.fluxer)


async def test_a_changed_card_edits_each_copy_once_and_an_unchanged_card_edits_none(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", title="Alpha")
    await subscribe(db, card_id, SUB)
    await poller.tick()
    await poller.tick()
    assert copy_patches(world.fluxer, SUB) == []

    await set_title(db, card_id, "Beta")
    await poller.tick()

    [edit] = copy_patches(world.fluxer, SUB)
    assert edit.path == f"{SUB_MESSAGES}/{SUB_MESSAGE}"
    assert edit.payload["embeds"][0]["title"] == "Beta"
    assert "content" not in edit.payload
    edited = await subscription(db, card_id, SUB)
    assert (edited.message_id, edited.content_hash) == (SUB_MESSAGE, render.rendered_hash(edit.payload["embeds"][0]))

    await poller.tick()     # the stored hash now matches the card, so this tick edits nothing
    assert len(copy_patches(world.fluxer, SUB)) == 1
    assert_card_writes_safe(world.fluxer)


async def test_a_copy_deleted_while_its_edit_is_in_flight_is_not_written_back(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", title="Alpha")
    await subscribe(db, card_id, SUB)
    await poller.tick()
    await set_title(db, card_id, "Beta")
    key = f"PATCH {SUB_MESSAGES}/{SUB_MESSAGE}"
    gate = asyncio.Event()
    world.fluxer.gates[key] = gate
    tick = asyncio.create_task(poller.tick())
    await wait_until(lambda: key in world.fluxer.gated)

    await poller.on_message_delete(MessageDelete(SUB_MESSAGE, SUB, None))
    gate.set()
    await tick

    # The edit that was in flight still succeeds, but the row no longer holds that message, so nothing is written back.
    assert [r.status for r in copy_patches(world.fluxer, SUB)] == [200]
    forgotten = await subscription(db, card_id, SUB)
    assert (forgotten.message_id, forgotten.content_hash) == (None, None)
    await poller.tick()     # so the next tick posts the copy again
    assert len(copy_posts(world.fluxer, SUB)) == 2
    assert (await subscription(db, card_id, SUB)).message_id is not None


async def test_a_copy_gone_when_edited_is_posted_again_and_its_new_id_is_stored(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO, logger="fluxer_panel.poller")
    card_id = await seed(db, "alpha", title="Alpha")
    await subscribe(db, card_id, SUB)
    await poller.tick()
    world.fluxer.deleted.add((SUB, SUB_MESSAGE))
    await set_title(db, card_id, "Beta")

    await poller.tick()

    assert [r.status for r in copy_patches(world.fluxer, SUB)] == [404]
    assert len(copy_posts(world.fluxer, SUB)) == 2
    assert (await subscription(db, card_id, SUB)).message_id == "5003"
    [recreated] = records(caplog, "subscription_message_recreated")
    assert recreated.panel_fields == {"card_id": card_id, "channel_id": SUB}
    assert_card_writes_safe(world.fluxer)


async def test_a_paused_subscription_is_neither_posted_nor_edited(world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha")
    await subscribe(db, card_id, SUB, paused=True)

    await poller.tick()

    assert copy_posts(world.fluxer, SUB) == []


async def test_a_forbidden_post_pauses_the_copy_and_alerts_each_admin_once(world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha")
    await subscribe(db, card_id, SUB)
    world.fluxer.overrides[f"POST {SUB_MESSAGES}"] = Override(403, b'{"code": "X", "message": "no"}')

    await poller.tick()
    await poller.tick()

    assert len(copy_posts(world.fluxer, SUB)) == 1
    sub = await subscription(db, card_id, SUB)
    assert sub.paused is True and sub.message_id is None
    alert = text.SUBSCRIPTION_PAUSED_ALERT.format(slug="alpha", channel_id=SUB)
    assert [r.payload["content"] for r in admin_dms(world.fluxer)] == [alert, alert]
    assert len(posts(world.fluxer)) == 1          # the card's own message is still posted


async def test_a_copy_whose_channel_is_gone_is_paused_and_its_admins_told_once(world: World, poller: Poller,
                                                                              db: Database):
    card_id = await seed(db, "alpha")
    await subscribe(db, card_id, SUB)
    world.fluxer.overrides[f"POST {SUB_MESSAGES}"] = Override(404, b'{"code": "UNKNOWN_CHANNEL", "message": "no"}')

    await poller.tick()
    await poller.tick()

    assert len(copy_posts(world.fluxer, SUB)) == 1     # paused, so the next tick does not post it again
    sub = await subscription(db, card_id, SUB)
    assert sub.paused is True and sub.message_id is None
    alert = text.SUBSCRIPTION_CHANNEL_GONE_ALERT.format(slug="alpha", channel_id=SUB)
    assert [r.payload["content"] for r in admin_dms(world.fluxer)] == [alert, alert]


async def test_a_forbidden_edit_pauses_the_copy_and_keeps_its_message_and_hash(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", title="Alpha")
    await subscribe(db, card_id, SUB)
    await poller.tick()
    stored_hash = (await subscription(db, card_id, SUB)).content_hash
    world.fluxer.overrides[f"PATCH {SUB_MESSAGES}/{SUB_MESSAGE}"] = Override(403, b'{"code": "X", "message": "no"}')

    await set_title(db, card_id, "Beta")
    await poller.tick()
    await poller.tick()

    assert [r.status for r in copy_patches(world.fluxer, SUB)] == [403]
    sub = await subscription(db, card_id, SUB)
    assert (sub.paused, sub.message_id, sub.content_hash) == (True, SUB_MESSAGE, stored_hash)
    assert len(admin_dms(world.fluxer)) == 2


async def test_a_resumed_copy_is_posted_on_the_next_tick(world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha")
    await subscribe(db, card_id, SUB, paused=True)
    await poller.tick()
    assert copy_posts(world.fluxer, SUB) == []

    await db.run(lambda c: subscriptions.resume(c, card_id, SUB))
    await poller.tick()

    assert len(copy_posts(world.fluxer, SUB)) == 1
    assert (await subscription(db, card_id, SUB)).message_id == SUB_MESSAGE


async def test_a_failed_copy_post_is_logged_by_ids_and_type_and_retried_on_the_next_tick(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poller")
    card_id = await seed(db, "alpha")
    await subscribe(db, card_id, SUB)
    world.fluxer.overrides[f"POST {SUB_MESSAGES}"] = Override(500, b'{"code": "X", "message": "boom"}')

    await poller.tick()

    sub = await subscription(db, card_id, SUB)
    assert (sub.message_id, sub.paused) == (None, False)
    [failed] = records(caplog, "subscription_failed")
    assert failed.panel_fields == {"card_id": card_id, "channel_id": SUB, "reason": "FluxerHTTPError"}

    del world.fluxer.overrides[f"POST {SUB_MESSAGES}"]
    await poller.tick()
    assert (await subscription(db, card_id, SUB)).message_id == SUB_MESSAGE


async def test_a_failing_copy_does_not_stop_the_card_or_the_other_copies(world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha")
    await subscribe(db, card_id, SUB)
    await subscribe(db, card_id, SUB2)
    world.fluxer.overrides[f"POST {SUB_MESSAGES}"] = Override(500, b'{"code": "X", "message": "boom"}')

    await poller.tick()

    assert len(posts(world.fluxer)) == 1
    assert len(copy_posts(world.fluxer, SUB2)) == 1
    assert (await subscription(db, card_id, SUB2)).message_id is not None


async def test_a_failing_card_post_still_posts_its_copy_and_is_logged_by_ids_and_type(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poller")
    card_id = await seed(db, "alpha")
    await subscribe(db, card_id, SUB)
    world.fluxer.overrides[f"POST {GAMES_MESSAGES}"] = Override(500, b'{"code": "X", "message": "boom"}')

    await poller.tick()

    assert (await load(db, card_id)).message_id is None
    [copy] = copy_posts(world.fluxer, SUB)
    assert copy.status == 200
    assert (await subscription(db, card_id, SUB)).message_id is not None
    [failed] = records(caplog, "card_poll_failed")
    assert failed.panel_fields == {"card_id": card_id, "reason": "FluxerHTTPError"}


async def test_a_copy_deleted_by_hand_is_posted_again_at_once_through_the_kick(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha")
    await subscribe(db, card_id, SUB)
    run_task = asyncio.create_task(poller.run())
    try:
        await wait_until(lambda: len(copy_posts(world.fluxer, SUB)) == 1)
        await _wait_for_subscription(db, card_id, SUB, lambda s: s.message_id == SUB_MESSAGE)
        await poller.on_message_delete(MessageDelete(SUB_MESSAGE, SUB, None))
        await _wait_for_subscription(db, card_id, SUB, lambda s: s.message_id == "5003")
    finally:
        run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert len(copy_posts(world.fluxer, SUB)) == 2


async def test_a_copy_deleted_while_its_post_is_in_flight_is_not_stored_and_is_posted_again(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha")
    await subscribe(db, card_id, SUB)
    gate_key = f"POST {SUB_MESSAGES}"
    gate = asyncio.Event()
    world.fluxer.gates[gate_key] = gate
    tick = asyncio.create_task(poller.tick())
    await wait_until(lambda: gate_key in world.fluxer.gated)

    await poller.on_message_delete(MessageDelete(SUB_MESSAGE, SUB, None))
    gate.set()
    await tick

    assert (await subscription(db, card_id, SUB)).message_id is None
    del world.fluxer.gates[gate_key]
    await poller.tick()
    assert (await subscription(db, card_id, SUB)).message_id == "5003"


async def test_publishing_again_posts_every_active_copy_and_not_a_paused_one(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", state="draft")
    await subscribe(db, card_id, SUB)
    await subscribe(db, card_id, SUB2, paused=True)

    await poller.tick()
    assert copy_posts(world.fluxer, SUB) == [] and copy_posts(world.fluxer, SUB2) == []

    await db.run(lambda c: cards.set_state(c, card_id, "live", 0))
    await poller.tick()

    assert len(copy_posts(world.fluxer, SUB)) == 1
    assert copy_posts(world.fluxer, SUB2) == []


async def test_startup_keeps_a_copy_whose_lookup_fails_with_a_server_error(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poller")
    card_id = await seed(db, "alpha")
    await subscribe(db, card_id, SUB)
    await db.run(lambda c: subscriptions.set_message(c, card_id, SUB, SUB_MESSAGE, "0" * 64))
    world.fluxer.overrides[f"GET {SUB_MESSAGES}/{SUB_MESSAGE}"] = Override(500, b'{"code": "X", "message": "boom"}')

    await poller.reconcile()

    kept = await subscription(db, card_id, SUB)
    assert (kept.message_id, kept.content_hash) == (SUB_MESSAGE, "0" * 64)
    [failed] = records(caplog, "subscription_reconcile_failed")
    assert failed.panel_fields == {"card_id": card_id, "channel_id": SUB, "reason": "FluxerHTTPError"}


async def test_startup_keeps_a_copy_whose_lookup_loses_its_connection(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poller")
    card_id = await seed(db, "alpha")
    await subscribe(db, card_id, SUB)
    await db.run(lambda c: subscriptions.set_message(c, card_id, SUB, SUB_MESSAGE, "0" * 64))
    world.fluxer.drops.add(f"GET {SUB_MESSAGES}/{SUB_MESSAGE}")

    await poller.reconcile()

    kept = await subscription(db, card_id, SUB)
    assert (kept.message_id, kept.content_hash) == (SUB_MESSAGE, "0" * 64)
    [failed] = records(caplog, "subscription_reconcile_failed")
    assert failed.panel_fields["card_id"] == card_id and failed.panel_fields["channel_id"] == SUB
    assert failed.panel_fields["reason"] == "ServerDisconnectedError"


async def test_startup_recreates_a_copy_that_no_longer_exists(world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha")
    await subscribe(db, card_id, SUB)
    await db.run(lambda c: subscriptions.set_message(c, card_id, SUB, "5999", "0" * 64))

    run_task = asyncio.create_task(poller.run())
    try:
        await wait_until(lambda: len(copy_posts(world.fluxer, SUB)) == 1)
        await _wait_for_subscription(db, card_id, SUB, lambda s: s.message_id == SUB_MESSAGE)
    finally:
        run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task
    assert [r.status for r in requests_for(world.fluxer, "GET", f"{SUB_MESSAGES}/5999")] == [404]


async def test_subscriptions_are_posted_when_no_games_channel_is_set(world: World, db: Database):
    card_id = await seed(db, "alpha")
    await subscribe(db, card_id, SUB)
    poller = world.make_poller(config=replace(config_for(), games_channel_id=None))

    await poller.tick()

    assert posts(world.fluxer) == []
    assert len(copy_posts(world.fluxer, SUB)) == 1


async def test_the_board_links_the_card_message_and_never_a_copy(world: World, poller: Poller, db: Database):
    world.fluxer.channels[GAMES] = {"id": GAMES, "type": 0, "guild_id": GUILD, "name": "games"}
    card_id = await seed(db, "alpha", probe=cards.ProbeRef("stub", HOST, PORT))
    await subscribe(db, card_id, SUB)

    await poller.tick()

    board_edits = requests_for(world.fluxer, "PATCH", f"{GAMES_MESSAGES}/{BOARD_MESSAGE}")
    assert board_edits
    links = [match.groups()[1:] for match in render.JUMP_LINK_RE.finditer(json.dumps(board_edits[-1].payload))]
    card_message_id = (await load(db, card_id)).message_id
    assert card_message_id is not None
    assert links == [(GUILD, GAMES, card_message_id)]
    assert f"channels/{GUILD}/{SUB}" not in json.dumps(board_edits[-1].payload)


async def test_a_copy_refused_again_after_a_resume_alerts_the_admins_once_more(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha")
    await subscribe(db, card_id, SUB)
    world.fluxer.overrides[f"POST {SUB_MESSAGES}"] = Override(403, b'{"code": "X", "message": "no"}')

    await poller.tick()
    await poller.tick()     # the copy is paused, so this tick neither posts it nor alerts again
    assert len(copy_posts(world.fluxer, SUB)) == 1
    assert len(admin_dms(world.fluxer)) == 2     # one per admin, from the first pause only

    await db.run(lambda c: subscriptions.resume(c, card_id, SUB))
    await poller.tick()     # refused again: that pause alerts each admin once more
    assert len(copy_posts(world.fluxer, SUB)) == 2
    assert len(admin_dms(world.fluxer)) == 4


async def test_a_copy_whose_request_drops_is_logged_and_does_not_stop_the_other_copies(
        world: World, poller: Poller, db: Database, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poller")
    card_id = await seed(db, "alpha")
    await subscribe(db, card_id, SUB)
    await subscribe(db, card_id, SUB2)
    world.fluxer.drops.add(f"POST {SUB_MESSAGES}")     # the connection closes before any response

    await poller.tick()

    assert len(posts(world.fluxer)) == 1
    assert (await subscription(db, card_id, SUB)).message_id is None
    assert (await subscription(db, card_id, SUB2)).message_id is not None
    [failed] = records(caplog, "subscription_failed")
    assert failed.panel_fields["card_id"] == card_id and failed.panel_fields["channel_id"] == SUB


async def test_copies_hold_the_cards_semaphore_slot(world: World, db: Database):
    """With one poll slot, the first card's copy holds it, so the second card waits for the copy to finish."""
    alpha = await seed(db, "alpha", title="Alpha")
    beta = await seed(db, "beta", title="Beta")
    await subscribe(db, alpha, SUB)
    await subscribe(db, beta, SUB2)
    release = asyncio.Event()
    for channel in (SUB, SUB2):
        world.fluxer.gates[f"POST {channel_messages(channel)}"] = release
    poller = world.make_poller(config=config_for(POLL_CONCURRENCY="1"))
    tick = asyncio.create_task(poller.tick())
    try:
        await wait_until(lambda: len(world.fluxer.gated) == 1)
        await settle()
        assert len(world.fluxer.gated) == 1
        assert len(posts(world.fluxer)) == 1      # the second card's own post waits for the first card's copy
    finally:
        release.set()
        await tick
    assert len(copy_posts(world.fluxer, SUB)) == 1 and len(copy_posts(world.fluxer, SUB2)) == 1


# Open wake requests on the card, its copy and the board (AC2.2, AC2.6) -----------------------------

async def open_requests(db: Database, card_id: int, *requesters: str) -> None:
    def tx(conn: Any) -> None:
        for user in requesters:
            wake_requests.add_open(conn, card_id, user, 0)

    await db.run(tx)


async def test_open_requests_show_on_the_card_its_copy_and_the_board(world: World, poller: Poller, db: Database):
    world.panel.servers[SERVER].current_state = "offline"
    card_id = await seed(db, "alpha", title="Alpha", manager=cards.ManagerRef("pterodactyl", SERVER))
    await subscribe(db, card_id, SUB)
    await open_requests(db, card_id, "2101", "2102")

    await poller.tick()

    [card_post] = posts(world.fluxer)
    assert fields_of(card_post.payload["embeds"][0])[text.FIELD_STATUS] == "Asleep · Requested · 2"
    [copy_post] = requests_for(world.fluxer, "POST", SUB_MESSAGES)
    assert fields_of(copy_post.payload["embeds"][0])[text.FIELD_STATUS] == "Asleep · Requested · 2"
    board_edits = requests_for(world.fluxer, "PATCH", f"{GAMES_MESSAGES}/{BOARD_MESSAGE}")
    assert "Asleep · Requested · 2" in json.dumps(board_edits[-1].payload, ensure_ascii=False)


# Shared status in copies of every mode (AC4.3) -----------------------------------------------------

REDIRECT_COPY = "3303"          # a redirect copy in the far community
LOCAL_COPY = "3304"             # a local copy in the same far community


def set_local(conn: Any, card_id: int, channel: str) -> None:
    sub = subscriptions.get(conn, card_id, channel)
    assert sub is not None and subscriptions.set_mode_if(conn, sub.id, "redirect", "local")


async def test_a_status_change_gives_a_local_copy_the_same_status_text_as_a_redirect_copy(
        world: World, poller: Poller, db: Database):
    world.panel.servers[SERVER].current_state = "offline"
    card_id = await seed(db, "alpha", title="Alpha", manager=cards.ManagerRef("pterodactyl", SERVER))
    await subscribe_in(db, card_id, REDIRECT_COPY, FAR_GUILD)
    await subscribe_in(db, card_id, LOCAL_COPY, FAR_GUILD)
    await db.run(lambda c: set_local(c, card_id, LOCAL_COPY))
    await poller.tick()
    asleep = fields_of(copy_posts(world.fluxer, LOCAL_COPY)[0].payload["embeds"][0])[text.FIELD_STATUS]
    assert asleep == text.STATUS_ASLEEP

    world.panel.servers[SERVER].current_state = "running"
    await poller.tick()

    [redirect_edit] = copy_patches(world.fluxer, REDIRECT_COPY)
    [local_edit] = copy_patches(world.fluxer, LOCAL_COPY)
    redirect_status = fields_of(redirect_edit.payload["embeds"][0])[text.FIELD_STATUS]
    local_status = fields_of(local_edit.payload["embeds"][0])[text.FIELD_STATUS]
    assert redirect_status != asleep
    assert local_status == redirect_status
    assert_card_writes_safe(world.fluxer)


async def test_a_suspended_server_shows_no_requested_suffix(world: World, poller: Poller, db: Database):
    world.panel.servers[SERVER].is_suspended = True
    card_id = await seed(db, "alpha", title="Alpha", manager=cards.ManagerRef("pterodactyl", SERVER))
    await open_requests(db, card_id, "2101", "2102")

    await poller.tick()

    [card_post] = posts(world.fluxer)
    assert fields_of(card_post.payload["embeds"][0])[text.FIELD_STATUS] == text.STATUS_UNAVAILABLE


async def test_status_of_returns_the_last_evaluated_status_and_none_once_the_card_is_not_live(
        world: World, poller: Poller, db: Database):
    world.panel.servers[SERVER].current_state = "offline"
    card_id = await seed(db, "alpha", manager=cards.ManagerRef("pterodactyl", SERVER))
    assert poller.status_of(card_id) is None

    await poller.tick()
    status = poller.status_of(card_id)
    assert status is not None and status.kind is StatusKind.ASLEEP

    await db.run(lambda c: cards.set_state(c, card_id, "archived", 0))
    await poller.tick()
    assert poller.status_of(card_id) is None


# Wake requests in the poller's ticks (AC2.1, AC2.8) -----------------------------------------------

WAKE_BOT = "5000"          # the bot's own user id in these tests
WAKER = "2101"             # a member who presses the wake emoji
CARD_MESSAGE = "5001"      # the first card message the fake posts in the games channel
CARD_TITLE = "Alpha"
WAKE_EXPIRY = 7200         # WAKE_REQUEST_EXPIRY_SECONDS is not set in these tests, so the default applies


class RecordingWake:
    """Stands in for WakeRequests where only the tick's close pass matters."""

    def __init__(self) -> None:
        self.closes = 0

    async def close_due(self) -> int:
        self.closes += 1
        return 0


def no_games_config() -> Config:
    return replace(config_for(), games_channel_id=None)


def wire_wake(world: World, poller: Poller) -> WakeRequests:
    """The wake requests the way `__main__` builds them, handed to the poller."""
    wake = WakeRequests(db=world.db, rest=world.rest, config=world.config, sources=world.sources, locks=world.locks,
                        status_of=poller.status_of, clock=world.clock, kick=poller.kick)
    poller.set_wake(wake)
    return wake


def press_bot(world: World, poller: Poller, wake: WakeRequests) -> Bot:
    """A bot whose wake presses reach `wake`, through the same presses and interactions as the running process."""
    identity = BotIdentity(user_id=WAKE_BOT)
    pacer = ChannelPacer(max_calls=1000)
    games = GamesGuild(world.rest, world.config)
    interactions = Interactions(db=world.db, rest=world.rest, config=world.config, locks=world.locks, games_guild=games,
                                seeder=Seeder(rest=world.rest, pacer=pacer, db=world.db), pacer=pacer,
                                identity=identity, roles=RoleCache(world.rest), kick=poller.kick, wake=wake)
    presses = Presses(db=world.db, rest=world.rest, config=world.config, handler=interactions, games_guild=games,
                      pacer=pacer, identity=identity, clock=world.clock)
    return Bot(rest=world.rest, config=world.config, roles=RoleCache(world.rest), presses=presses, identity=identity)


async def press_wake(bot: Bot, message: str) -> None:
    await bot.on_dispatch("MESSAGE_REACTION_ADD", {"user_id": WAKER, "channel_id": GAMES, "message_id": message,
                                                   "guild_id": GUILD, "emoji": {"name": WAKE_EMOJI}})


async def open_counts_of(db: Database) -> dict[int, int]:
    return await db.run(lambda c: wake_requests.open_counts(c))


def dm_text(state: FakeFluxerState) -> str:
    return json.dumps([r.payload for r in dm_messages(state)], ensure_ascii=False)


async def test_a_wake_press_shows_requested_on_the_next_tick_and_closes_when_the_server_runs(
        world: World, poller: Poller, db: Database):
    world.panel.servers[SERVER].current_state = "offline"
    card_id = await seed(db, "alpha", title=CARD_TITLE, manager=cards.ManagerRef("pterodactyl", SERVER))
    wake = wire_wake(world, poller)
    bot = press_bot(world, poller, wake)
    await poller.tick()
    [posted] = posts(world.fluxer)
    assert fields_of(posted.payload["embeds"][0])[text.FIELD_STATUS] == "Asleep"

    await press_wake(bot, CARD_MESSAGE)
    assert await open_counts_of(db) == {card_id: 1}
    assert text.WAKE_SENT.format(card=CARD_TITLE) in dm_text(world.fluxer)

    await poller.tick()
    [suffixed] = requests_for(world.fluxer, "PATCH", POSTED)
    assert fields_of(suffixed.payload["embeds"][0])[text.FIELD_STATUS] == "Asleep · Requested · 1"

    world.panel.servers[SERVER].current_state = "running"
    await poller.tick()                 # the server runs: this tick's close pass closes the request
    assert await open_counts_of(db) == {}
    assert text.WAKE_READY.format(card=CARD_TITLE) in dm_text(world.fluxer)

    await poller.tick()                 # the tick after the close renders the card without the suffix
    last = requests_for(world.fluxer, "PATCH", POSTED)[-1]
    assert "Requested" not in fields_of(last.payload["embeds"][0])[text.FIELD_STATUS]


async def test_without_a_games_channel_an_expired_request_closes_and_the_copy_drops_the_suffix(
        world: World, db: Database):
    world.panel.servers[SERVER].current_state = "offline"
    card_id = await seed(db, "alpha", title=CARD_TITLE, manager=cards.ManagerRef("pterodactyl", SERVER))
    await subscribe(db, card_id, SUB)
    await open_requests(db, card_id, WAKER)           # opened at clock 0
    poller = world.make_poller(config=no_games_config())
    wire_wake(world, poller)

    await poller.tick()
    [copy_post] = copy_posts(world.fluxer, SUB)
    assert fields_of(copy_post.payload["embeds"][0])[text.FIELD_STATUS] == "Asleep · Requested · 1"

    world.clock.now = WAKE_EXPIRY + 1.0
    await poller.tick()                 # the close pass runs on the copies-only path too
    assert await open_counts_of(db) == {}
    assert text.WAKE_GAVE_UP.format(card=CARD_TITLE) in dm_text(world.fluxer)

    await poller.tick()
    assert "Requested" not in fields_of(copy_patches(world.fluxer, SUB)[-1].payload["embeds"][0])[text.FIELD_STATUS]


async def test_each_tick_runs_the_close_pass_with_a_games_channel(world: World, poller: Poller):
    wake = RecordingWake()
    poller.set_wake(wake)
    await poller.tick()
    assert wake.closes == 1


async def test_each_tick_runs_the_close_pass_without_a_games_channel(world: World):
    poller = world.make_poller(config=no_games_config())
    wake = RecordingWake()
    poller.set_wake(wake)
    await poller.tick()
    assert wake.closes == 1


# Play-now expiry on the poller's tick ----------------------------------------------------------------

PLAY_MESSAGE = "5201"      # a play-now message in the games channel


def wire_interactions(world: World, poller: Poller) -> Interactions:
    """The interactions the way `__main__` builds them, handed to the poller. They share the test clock."""
    pacer = ChannelPacer(max_calls=1000)
    identity = BotIdentity(user_id=WAKE_BOT)
    games = GamesGuild(world.rest, world.config)
    interactions = Interactions(db=world.db, rest=world.rest, config=world.config, locks=world.locks, games_guild=games,
                                seeder=Seeder(rest=world.rest, pacer=pacer, db=world.db), pacer=pacer,
                                identity=identity, roles=RoleCache(world.rest), clock=world.clock)
    poller.set_interactions(interactions)
    return interactions


async def test_a_due_play_now_message_ends_on_a_tick_even_without_a_games_channel(world: World, db: Database):
    card_id = await seed(db, "alpha", title=CARD_TITLE)
    await db.run(lambda c: play_now.add(c, card_id=card_id, guild_id=GUILD, channel_id=GAMES,
                                        message_id=PLAY_MESSAGE, presser_id=WAKER, offers_wake=False,
                                        now=0, expires_at=100))
    poller = world.make_poller(config=no_games_config())
    wire_interactions(world, poller)
    world.clock.now = 99
    await poller.tick()
    assert [r for r in world.fluxer.requests if r.method == "PATCH" and r.path.endswith(PLAY_MESSAGE)] == []

    world.clock.now = 100
    await poller.tick()
    [ended] = [r for r in world.fluxer.requests if r.method == "PATCH" and r.path == f"{GAMES_MESSAGES}/{PLAY_MESSAGE}"]
    assert ended.payload["content"] == text.PLAY_NOW_ENDED_CONTENT.format(card=render.escape(CARD_TITLE))
    assert [r for r in world.fluxer.requests
            if r.method == "DELETE" and r.path == f"{GAMES_MESSAGES}/{PLAY_MESSAGE}/reactions"]


# The timed session jobs in the poller: each tick runs them, and their next due time ends the wait (AC5, poller wake-ups).

SESSION_CHANNEL = "3301"   # a session channel that is not the games channel


def wired_session_jobs(world: World, poller: Poller) -> SessionJobs:
    """The session jobs as `__main__` builds them, handed to the poller: a real flow and post on the test database."""
    pacer = ChannelPacer(max_calls=1000)
    post = SessionPost(db=world.db, rest=world.rest, config=world.config, pacer=pacer)
    flow = SessionFlow(db=world.db, rest=world.rest, config=world.config, locks=world.locks, session_post=post,
                       games_guild=GamesGuild(world.rest, world.config), pacer=pacer,
                       identity=BotIdentity(user_id=WAKE_BOT),
                       backoff=DmBackoff(world.config.dm_backoff_seconds, clock=world.clock),
                       limiter=NonAdminLimiter(world.config, clock=world.clock), clock=world.clock)
    jobs = SessionJobs(db=world.db, rest=world.rest, config=world.config, locks=world.locks, session_post=post,
                       session_flow=flow, status_of=poller.status_of, clock=world.clock)
    poller.set_session_jobs(jobs)
    return jobs


async def add_upcoming_session(db: Database, card_id: int, *, starts_at: int, ends_at: int) -> int:
    def tx(conn: Any) -> int:
        return sessions.add(conn, card_id=card_id, guild_id=GUILD, channel_id=SESSION_CHANNEL, message_id="5501",
                            starts_at=starts_at, ends_at=ends_at, proposer_id=AUTHOR, now=0)

    return await db.run(tx)


async def test_the_wait_ends_when_the_next_session_job_is_due(world: World, db: Database) -> None:
    poller = world.make_poller()
    jobs = wired_session_jobs(world, poller)
    card_id = await seed(db, "alpha", title=CARD_TITLE)
    await add_upcoming_session(db, card_id, starts_at=900, ends_at=1000)

    world.clock.now = 800
    await jobs.run()
    assert poller._wait_timeout() == 200           # the end at 1000, not the poll interval
    world.clock.now = 960
    assert poller._wait_timeout() == 40


async def test_the_wait_is_the_poll_interval_when_no_session_job_is_pending(world: World) -> None:
    poller = world.make_poller()
    jobs = wired_session_jobs(world, poller)
    world.clock.now = 800
    await jobs.run()
    assert jobs.due is None
    assert poller._wait_timeout() == world.config.poll_interval_seconds


async def test_a_tick_runs_the_session_jobs(world: World, db: Database) -> None:
    poller = world.make_poller()
    wired_session_jobs(world, poller)
    card_id = await seed(db, "alpha", title=CARD_TITLE)
    session_id = await add_upcoming_session(db, card_id, starts_at=900, ends_at=1000)

    world.clock.now = 1000
    await poller.tick()
    ended = await db.run(lambda c: sessions.get(c, session_id))
    assert ended is not None and ended.state == "ended"


class StubJobs:
    """Stands in for SessionJobs where only the poller's use of it matters. Each run is counted, and `headsups` records
    the `headsup` flag each run was given. A run leaves `refreshed_due` in `due`, as a real run leaves the soonest time
    still to come, and leaves `due` alone when it is None. `run_error` makes a run raise."""

    def __init__(self, due: float | None, *, refreshed_due: float | None = None,
                 run_error: BaseException | None = None) -> None:
        self.due = due
        self.refreshed_due = refreshed_due
        self.run_error = run_error
        self.runs = 0
        self.headsups: list[bool] = []

    async def run(self, *, headsup: bool = True) -> None:
        self.runs += 1
        self.headsups.append(headsup)
        if self.run_error is not None:
            raise self.run_error
        if self.refreshed_due is not None:
            self.due = self.refreshed_due


class FailingWake:
    """A wake pass that fails on every tick."""

    async def close_due(self) -> int:
        raise RuntimeError("wake pass failed")


def failing_open_counts(conn: Any) -> dict[int, int]:
    raise RuntimeError("database unavailable")


async def test_a_failed_tick_leaves_a_stale_session_due_out_of_the_wait(
        world: World, poller: Poller, monkeypatch: pytest.MonkeyPatch) -> None:
    # The due time is left over from an earlier tick and is now in the past. This tick fails before any step that a
    # run would refresh, so the wait must not drop to zero because of it.
    poller.set_session_jobs(StubJobs(due=1000.0))
    monkeypatch.setattr(wake_requests, "open_counts", failing_open_counts)
    world.clock.now = 2000.0
    with pytest.raises(RuntimeError):
        await poller.tick()
    assert poller._wait_timeout() == world.config.poll_interval_seconds


async def test_a_failed_tick_still_runs_the_session_jobs_and_refreshes_their_due(
        world: World, poller: Poller, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = StubJobs(due=1000.0, refreshed_due=3000.0)
    poller.set_session_jobs(jobs)
    monkeypatch.setattr(wake_requests, "open_counts", failing_open_counts)
    world.clock.now = 2000.0
    with pytest.raises(RuntimeError, match="database unavailable"):
        await poller.tick()
    assert jobs.runs == 1
    assert jobs.headsups == [False]                  # the card pass did not finish, so the cached statuses are not read
    assert jobs.due == 3000.0
    assert poller._wait_timeout() == 1000.0          # the refreshed due, 1000 seconds away, is under the poll interval


async def test_a_failed_after_tick_step_still_runs_the_session_jobs_and_refreshes_their_due(
        world: World, poller: Poller) -> None:
    jobs = StubJobs(due=1000.0, refreshed_due=3000.0)
    poller.set_session_jobs(jobs)
    poller.set_wake(FailingWake())
    world.clock.now = 2000.0
    with pytest.raises(RuntimeError, match="wake pass failed"):
        await poller.tick()
    assert jobs.runs == 1
    assert jobs.headsups == [True]                   # the card pass finished, so the statuses are this tick's
    assert jobs.due == 3000.0
    assert poller._wait_timeout() == 1000.0


async def test_a_failing_session_job_run_does_not_replace_the_tick_error(
        world: World, poller: Poller, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger="fluxer_panel.poller")
    poller.set_session_jobs(StubJobs(due=None, run_error=ValueError("jobs failed")))
    poller.set_wake(FailingWake())
    with pytest.raises(RuntimeError, match="wake pass failed"):
        await poller.tick()
    assert records(caplog, "session_job_failed")


class SlowWake:
    """A wake pass that takes `seconds` on the fake clock, as its DMs would, and fails when `fail` is set."""

    def __init__(self, world: World, seconds: float, *, fail: bool = False) -> None:
        self.world, self.seconds, self.fail = world, seconds, fail

    async def close_due(self) -> int:
        self.world.clock.now += self.seconds
        if self.fail:
            raise RuntimeError("wake pass failed")
        return 0


class SlowJobs(StubJobs):
    """A StubJobs whose run takes `seconds` on the fake clock and leaves `due` where it is: the worst case for the wait."""

    def __init__(self, world: World, due: float, seconds: float) -> None:
        super().__init__(due)
        self.world, self.seconds = world, seconds

    async def run(self, *, headsup: bool = True) -> None:
        await super().run(headsup=headsup)
        self.world.clock.now += self.seconds


async def test_a_repost_that_falls_due_during_the_tick_gives_a_zero_wait(world: World, poller: Poller) -> None:
    # The board check reads the clock before the re-post is due, and the wake pass after it takes the clock past it.
    poller._board_armed = False
    poller._repost_due = 2003.0
    world.clock.now = 2000.0
    poller.set_wake(SlowWake(world, 5.0))
    await poller.tick()
    assert poller._repost_due == 2003.0 and world.clock.now == 2005.0
    assert poller._wait_timeout() == 0.0


async def test_a_session_job_due_that_falls_due_during_the_tick_gives_a_zero_wait(world: World, poller: Poller) -> None:
    poller.set_session_jobs(SlowJobs(world, due=2003.0, seconds=5.0))
    world.clock.now = 2000.0
    await poller.tick()
    assert world.clock.now == 2005.0
    assert poller._wait_timeout() == 0.0


async def test_a_stale_repost_due_does_not_make_the_wait_zero(
        world: World, poller: Poller, monkeypatch: pytest.MonkeyPatch) -> None:
    # A re-post due time from before a tick that failed early: that tick had its chance at it, so it is stale.
    poller._repost_due = 1000.0
    monkeypatch.setattr(wake_requests, "open_counts", failing_open_counts)
    world.clock.now = 2000.0
    with pytest.raises(RuntimeError, match="database unavailable"):
        await poller.tick()
    assert poller._wait_timeout() == world.config.poll_interval_seconds
    poller._repost_due = 2500.0                      # set after that tick started: still ahead of it
    assert poller._wait_timeout() == 500.0


class _StopLoop(BaseException):
    """Ends the run loop from the fake wait below, so the test never sleeps."""


@pytest.mark.parametrize("mode", ["session_due", "repost_due"])
async def test_a_tick_that_keeps_failing_with_a_mid_tick_due_time_retries_once_then_waits(
        world: World, poller: Poller, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    # The run loop with its wait replaced by one that only moves the fake clock. Each tick fails after the due time has
    # passed during it. The first wait is zero. The next tick starts after the due time, so the time is stale there
    # and the loop waits the poll interval from then on.
    waits: list[float] = []

    def fake_wait_for(awaitable: Any, timeout: float) -> None:
        awaitable.close()
        waits.append(timeout)
        world.clock.now += timeout
        if len(waits) == 3:
            raise _StopLoop()
        raise TimeoutError()

    class FakeAsyncio:
        wait_for = staticmethod(fake_wait_for)

        def __getattr__(self, name: str) -> Any:
            return getattr(asyncio, name)

    monkeypatch.setattr(poller_module, "asyncio", FakeAsyncio())
    world.clock.now = 2000.0
    poller._board_armed = False
    if mode == "session_due":
        poller.set_session_jobs(SlowJobs(world, due=2003.0, seconds=5.0))
        poller.set_wake(FailingWake())
    else:
        poller._repost_due = 2003.0
        poller.set_wake(SlowWake(world, 5.0, fail=True))
    with pytest.raises(_StopLoop):
        await poller.run()
    interval = float(world.config.poll_interval_seconds)
    assert waits == [0.0, interval, interval]
    assert waits.count(0.0) == 1


# The asleep heads-up after a failed tick (AC5.6): the cached status is not read by a tick whose card pass did not finish.

HEADSUP_START = 200_000   # a session's start on the fake clock; its heads-up window opens 30 minutes before


def headsup_dms(world: World, content: str) -> list[Recorded]:
    return [r for r in dm_messages(world.fluxer) if r.payload.get("content") == content]


async def test_a_failed_tick_before_the_card_pass_sends_no_heads_up_from_a_cached_status(
        world: World, poller: Poller, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    world.panel.servers[SERVER].current_state = "offline"
    card_id = await seed(db, "alpha", title=CARD_TITLE, manager=cards.ManagerRef("pterodactyl", SERVER))
    wired_session_jobs(world, poller)
    await add_upcoming_session(db, card_id, starts_at=HEADSUP_START, ends_at=HEADSUP_START + 3600)
    expected = text.SESSION_HEADSUP.format(card=render.escape(CARD_TITLE), ts=HEADSUP_START, n=1)

    world.clock.now = HEADSUP_START - 2 * 3600       # outside the window: the good tick caches ASLEEP, sends nothing
    await poller.tick()
    status = poller.status_of(card_id)
    assert status is not None and status.kind is StatusKind.ASLEEP
    assert headsup_dms(world, expected) == []

    world.clock.now = HEADSUP_START - 29 * 60        # inside the window, with the cached ASLEEP status still held
    original_open_counts = wake_requests.open_counts
    monkeypatch.setattr(wake_requests, "open_counts", failing_open_counts)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await poller.tick()
    assert headsup_dms(world, expected) == []

    monkeypatch.setattr(wake_requests, "open_counts", original_open_counts)
    await poller.tick()
    assert len(headsup_dms(world, expected)) == 2    # one DM to each of the two admins
    await poller.tick()
    assert len(headsup_dms(world, expected)) == 2    # sent once, not again on the next tick


# The next session on a card and its copies (AC3.5-AC3.7). The games channel is in GUILD, so a copy in GUILD is a home
# copy, and a copy in another community is a far copy that gets the line without its jump link.

SESSION_CHANNEL_NEXT = "3301"   # the session's channel, not the games channel
SESSION_MESSAGE = "5501"        # the session's own message
FAR_GUILD = "9001"              # a community other than the games channel's
FAR_COPY = "3302"               # a copy's channel in that community
NEXT_LINK = f"[Open](https://fluxer.invalid/channels/{GUILD}/{SESSION_CHANNEL_NEXT}/{SESSION_MESSAGE})"


async def subscribe_in(db: Database, card_id: int, channel: str, guild: str) -> None:
    await db.run(lambda c: subscriptions.add(c, card_id, guild, channel))


async def add_session_with_next_line(db: Database, card_id: int, *, going_extra: list[str] = ()) -> int:
    def tx(conn: Any) -> int:
        session_id = sessions.add(conn, card_id=card_id, guild_id=GUILD, channel_id=SESSION_CHANNEL_NEXT,
                                  message_id=SESSION_MESSAGE, starts_at=5000, ends_at=8000, proposer_id=AUTHOR, now=0)
        session = sessions.get(conn, session_id)
        assert session is not None
        for user_id in going_extra:
            sessions.add_presence(conn, session, user_id, "going", 1)
        sessions.recompute(conn, session, None, 1)
        return session_id

    return await db.run(tx)


def next_session_value(embed: dict[str, Any]) -> str | None:
    return {field["name"]: field["value"] for field in embed["fields"]}.get(text.FIELD_NEXT_SESSION)


async def test_the_card_and_a_home_copy_link_the_next_session_and_a_far_copy_does_not(
        world: World, poller: Poller, db: Database):
    world.fluxer.channels[GAMES] = {"id": GAMES, "guild_id": GUILD, "type": 0}
    world.clock.now = 1000
    card_id = await seed(db, "alpha", title="Alpha")
    await add_session_with_next_line(db, card_id)
    await subscribe(db, card_id, SUB)                       # a copy in the games community: a home copy
    await subscribe_in(db, card_id, FAR_COPY, FAR_GUILD)    # a copy in another community: a far copy

    await poller.tick()

    [main] = posts(world.fluxer)
    home_value = next_session_value(main.payload["embeds"][0])
    assert home_value is not None and home_value.endswith(NEXT_LINK)
    [home] = copy_posts(world.fluxer, SUB)
    assert next_session_value(home.payload["embeds"][0]) == home_value
    [far] = copy_posts(world.fluxer, FAR_COPY)
    far_value = next_session_value(far.payload["embeds"][0])
    assert far_value is not None and far_value == home_value.replace(f" · {NEXT_LINK}", "")
    assert "](" not in far_value
    for write in (main, home, far):
        assert write.payload["embeds"][0]["footer"]["text"] == text.CARD_DM_HINT
    assert_card_writes_safe(world.fluxer)


async def test_an_rsvp_that_changes_the_count_shows_on_the_next_tick_and_a_cancel_removes_the_line(
        world: World, poller: Poller, db: Database):
    world.clock.now = 1000
    card_id = await seed(db, "alpha", title="Alpha")
    session_id = await add_session_with_next_line(db, card_id)
    await poller.tick()
    assert next_session_value(posts(world.fluxer)[0].payload["embeds"][0]).startswith("<t:5000:F> (<t:5000:R>) · 1 going")

    def going(conn: Any) -> None:
        session = sessions.get(conn, session_id)
        assert session is not None
        sessions.add_presence(conn, session, "6601", "going", 2)
        sessions.recompute(conn, session, None, 2)

    await db.run(going)
    await poller.tick()
    card_patches = [r for r in world.fluxer.requests if r.method == "PATCH" and r.path == POSTED]
    assert len(card_patches) == 1
    assert next_session_value(card_patches[-1].payload["embeds"][0]) == f"<t:5000:F> (<t:5000:R>) · 2 going · {NEXT_LINK}"

    await db.run(lambda c: sessions.cancel_if(c, session_id, 3))
    await poller.tick()
    card_patches = [r for r in world.fluxer.requests if r.method == "PATCH" and r.path == POSTED]
    assert next_session_value(card_patches[-1].payload["embeds"][0]) is None
    assert_card_writes_safe(world.fluxer)


# The timed poll jobs on the tick (AC6.1, "within one tick"): the wait ends at a poll job's due time, a stale one is
# ignored, and the jobs run after a failed card pass as the session jobs do.

POLL_CHANNEL = "3301"          # a poll's channel, which is not the games channel
POLL_MESSAGE = "5601"


def wired_poll_jobs(world: World, poller: Poller) -> PollJobs:
    """The poll jobs as `__main__` builds them, handed to the poller: a real close path on the test database."""
    backoff = DmBackoff(world.config.dm_backoff_seconds, clock=world.clock)
    post = PollPost(db=world.db, rest=world.rest, config=world.config, pacer=ChannelPacer(max_calls=1000),
                    identity=BotIdentity(user_id=WAKE_BOT), backoff=backoff, edit_delay=0, sleep=no_sleep)
    closer = PollCloser(db=world.db, locks=world.locks, poll_post=post, clock=world.clock)
    jobs = PollJobs(db=world.db, rest=world.rest, config=world.config, closer=closer, backoff=backoff,
                    clock=world.clock)
    poller.set_poll_jobs(jobs)
    return jobs


async def store_poll(db: Database, card_id: int, *, closes_at: int) -> int:
    def tx(conn: Any) -> int:
        poll_id = polls.create(conn, card_id=card_id, guild_id=GUILD, channel_id=POLL_CHANNEL, message_id=POLL_MESSAGE,
                               starter_id=AUTHOR, kind="approval", parent_poll_id=None, now=0, closes_at=closes_at,
                               ballot_size=6, options=[(closes_at + 7200, "offered")], recipients=[])
        assert poll_id is not None
        return poll_id

    return await db.run(tx)


async def test_the_wait_ends_when_the_next_poll_job_is_due(world: World, poller: Poller) -> None:
    poller.set_poll_jobs(StubJobs(due=1000.0))
    world.clock.now = 800.0
    assert poller._wait_timeout() == 200.0
    world.clock.now = 960.0
    assert poller._wait_timeout() == 40.0


async def test_a_poll_job_due_that_falls_due_during_the_tick_gives_a_zero_wait(world: World, poller: Poller) -> None:
    poller.set_poll_jobs(SlowJobs(world, due=2003.0, seconds=5.0))
    world.clock.now = 2000.0
    await poller.tick()
    assert world.clock.now == 2005.0
    assert poller._wait_timeout() == 0.0


async def test_a_stale_poll_job_due_does_not_make_the_wait_zero(
        world: World, poller: Poller, monkeypatch: pytest.MonkeyPatch) -> None:
    # The due time is from before a tick that failed early: that tick had its chance at it, so it is stale.
    poller.set_poll_jobs(StubJobs(due=1000.0))
    monkeypatch.setattr(wake_requests, "open_counts", failing_open_counts)
    world.clock.now = 2000.0
    with pytest.raises(RuntimeError, match="database unavailable"):
        await poller.tick()
    assert poller._wait_timeout() == world.config.poll_interval_seconds


async def test_a_tick_runs_the_poll_jobs(world: World, poller: Poller) -> None:
    jobs = StubJobs(due=None)
    poller.set_poll_jobs(jobs)
    await poller.tick()
    assert jobs.runs == 1


async def test_a_tick_whose_card_pass_raises_still_runs_the_poll_jobs_and_refreshes_their_due(
        world: World, poller: Poller, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = StubJobs(due=1000.0, refreshed_due=3000.0)
    poller.set_poll_jobs(jobs)
    monkeypatch.setattr(wake_requests, "open_counts", failing_open_counts)
    world.clock.now = 2000.0
    with pytest.raises(RuntimeError, match="database unavailable"):
        await poller.tick()
    assert jobs.runs == 1
    assert jobs.due == 3000.0
    assert poller._wait_timeout() == 1000.0


async def test_a_failing_poll_job_run_does_not_replace_the_tick_error(
        world: World, poller: Poller, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger="fluxer_panel.poller")
    poller.set_poll_jobs(StubJobs(due=None, run_error=ValueError("jobs failed")))
    poller.set_wake(FailingWake())
    with pytest.raises(RuntimeError, match="wake pass failed"):
        await poller.tick()
    [failed] = records(caplog, "poll_job_failed")
    assert getattr(failed, FIELDS_ATTR) == {"kind": "run", "reason": "ValueError"}


async def test_a_poll_due_at_T_is_closed_by_the_first_tick_at_or_after_T(
        world: World, poller: Poller, db: Database) -> None:
    T = 3000
    wired_poll_jobs(world, poller)
    card_id = await seed(db, "alpha", title=CARD_TITLE)
    world.fluxer.messages[(POLL_CHANNEL, POLL_MESSAGE)] = {"embeds": []}
    poll_id = await store_poll(db, card_id, closes_at=T)

    world.clock.now = T - 1
    await poller.tick()
    assert (await db.run(lambda c: polls.get(c, poll_id))).state == "open"

    world.clock.now = T
    await poller.tick()
    assert (await db.run(lambda c: polls.get(c, poll_id))).state == "closed"


async def test_a_poll_job_due_at_the_start_of_the_tick_is_stale_and_gives_the_poll_interval(
        world: World, poller: Poller) -> None:
    # The tick starts at 2000 and the due time is 2000: that tick had its chance at the time, so it does not shorten the wait.
    poller.set_poll_jobs(StubJobs(due=2000.0))
    world.clock.now = 2000.0
    await poller.tick()
    assert poller._wait_timeout() == world.config.poll_interval_seconds


# Each scope's next session on its own copy (AC4.1). A local copy in FAR_GUILD shows only its own sessions, with a jump
# link into FAR_GUILD. A redirect copy and the home card keep showing the home session.

LOCAL_SESSION_CHANNEL = "3305"          # a session channel in the local copy's community
LOCAL_SESSION_MESSAGE = "5601"          # that session's own message
LOCAL_LINK = f"[Open](https://fluxer.invalid/channels/{FAR_GUILD}/{LOCAL_SESSION_CHANNEL}/{LOCAL_SESSION_MESSAGE})"


async def add_local_session(db: Database, card_id: int, sub_id: int, *, starts_at: int = 6000) -> int:
    def tx(conn: Any) -> int:
        return sessions.add(conn, card_id=card_id, guild_id=FAR_GUILD, channel_id=LOCAL_SESSION_CHANNEL,
                            message_id=LOCAL_SESSION_MESSAGE, starts_at=starts_at, ends_at=starts_at + 3000,
                            proposer_id=AUTHOR, now=0, scope_sub_id=sub_id)

    return await db.run(tx)


async def local_copy_setup(world: World, db: Database) -> tuple[int, int]:
    """A card with a home session, a local copy and a redirect copy in FAR_GUILD. Returns (card id, local sub id)."""
    world.clock.now = 1000
    card_id = await seed(db, "alpha", title="Alpha")
    await add_session_with_next_line(db, card_id)
    await subscribe_in(db, card_id, LOCAL_COPY, FAR_GUILD)
    await subscribe_in(db, card_id, REDIRECT_COPY, FAR_GUILD)
    await db.run(lambda c: set_local(c, card_id, LOCAL_COPY))
    sub = await subscription(db, card_id, LOCAL_COPY)
    return card_id, sub.id


async def test_a_local_copy_with_no_session_of_its_own_shows_no_next_session(
        world: World, poller: Poller, db: Database):
    await local_copy_setup(world, db)

    await poller.tick()

    [local] = copy_posts(world.fluxer, LOCAL_COPY)
    assert next_session_value(local.payload["embeds"][0]) is None
    [redirect] = copy_posts(world.fluxer, REDIRECT_COPY)
    assert next_session_value(redirect.payload["embeds"][0]) is not None
    assert_card_writes_safe(world.fluxer)


async def test_a_local_session_shows_on_its_copy_with_a_jump_into_its_community_and_the_home_card_keeps_its_own(
        world: World, poller: Poller, db: Database):
    card_id, sub_id = await local_copy_setup(world, db)
    await poller.tick()
    home_value = next_session_value(posts(world.fluxer)[0].payload["embeds"][0])
    assert home_value is not None and home_value.endswith(NEXT_LINK)

    await add_local_session(db, card_id, sub_id)
    await poller.tick()

    [local_edit] = copy_patches(world.fluxer, LOCAL_COPY)
    local_value = next_session_value(local_edit.payload["embeds"][0])
    assert local_value is not None and local_value.endswith(LOCAL_LINK)
    assert "5601" in local_value and NEXT_LINK not in local_value
    assert copy_patches(world.fluxer, REDIRECT_COPY) == []
    home_patches = [r for r in world.fluxer.requests if r.method == "PATCH" and r.path == POSTED]
    assert home_patches == []
    assert_card_writes_safe(world.fluxer)


async def test_a_local_session_sooner_than_the_home_session_never_shows_on_the_home_card(
        world: World, poller: Poller, db: Database):
    card_id, sub_id = await local_copy_setup(world, db)
    await add_local_session(db, card_id, sub_id, starts_at=4000)     # sooner than the home session at 5000

    await poller.tick()

    [main] = posts(world.fluxer)
    home_value = next_session_value(main.payload["embeds"][0])
    assert home_value is not None and home_value.endswith(NEXT_LINK)
    [local] = copy_posts(world.fluxer, LOCAL_COPY)
    assert next_session_value(local.payload["embeds"][0]).endswith(LOCAL_LINK)
    [redirect] = copy_posts(world.fluxer, REDIRECT_COPY)
    assert next_session_value(redirect.payload["embeds"][0]) == home_value.replace(f" · {NEXT_LINK}", "")


async def test_an_unchanged_local_copy_makes_no_patch_on_the_next_tick(world: World, poller: Poller, db: Database):
    card_id, sub_id = await local_copy_setup(world, db)
    await add_local_session(db, card_id, sub_id)
    await poller.tick()
    await poller.tick()

    assert copy_patches(world.fluxer, LOCAL_COPY) == []


async def test_a_copy_switched_to_local_after_evaluation_gets_the_shared_payload_then_its_own(
        world: World, poller: Poller, db: Database):
    world.clock.now = 1000
    card_id = await seed(db, "alpha", title="Alpha")
    await add_session_with_next_line(db, card_id)
    await subscribe_in(db, card_id, LOCAL_COPY, FAR_GUILD)
    card = await load(db, card_id)
    result = await poller._evaluate(card)
    assert result.scope_embeds == {}                # the copy was still a redirect copy at evaluation

    await db.run(lambda c: set_local(c, card_id, LOCAL_COPY))
    await poller._publish(card_id, result.embed, result.far_embed, result.scope_embeds)

    [shared] = copy_posts(world.fluxer, LOCAL_COPY)
    assert shared.payload == render.card_message(result.far_embed)
    await poller.tick()
    [own] = copy_patches(world.fluxer, LOCAL_COPY)
    assert next_session_value(own.payload["embeds"][0]) is None
    assert_card_writes_safe(world.fluxer)


# Both copies (AC4.2): the next-session line is the sooner of the copy's own session and the home session. The home
# session shows with the link to its mirror in this copy, or with no link when there is none, and never with the home
# link. The poller never back-fills a missing mirror.

BOTH_COPY = "3306"                       # a both copy in the far community
BOTH_MIRROR_MESSAGE = "5602"             # the mirror of the home session in that copy
BOTH_MIRROR_LINK = f"[Open](https://fluxer.invalid/channels/{FAR_GUILD}/{BOTH_COPY}/{BOTH_MIRROR_MESSAGE})"


async def both_copy_setup(world: World, db: Database, *, mirror: bool) -> tuple[int, int, int]:
    """A card with a home session at 5000 and a both copy in FAR_GUILD. Returns (card id, both sub id, home session id).
    With `mirror`, the home session already has its mirror in the copy."""
    world.clock.now = 1000
    card_id = await seed(db, "alpha", title="Alpha")
    home_id = await add_session_with_next_line(db, card_id)
    await subscribe_in(db, card_id, BOTH_COPY, FAR_GUILD)
    await db.run(lambda c: set_mode(c, card_id, BOTH_COPY, "both"))
    sub = await subscription(db, card_id, BOTH_COPY)
    if mirror:
        await db.run(lambda c: mirrors.add(c, sub_id=sub.id, channel_id=BOTH_COPY, message_id=BOTH_MIRROR_MESSAGE,
                                           kind="session", object_id=home_id, content_hash=None))
    return card_id, sub.id, home_id


def set_mode(conn: Any, card_id: int, channel: str, mode: str) -> None:
    sub = subscriptions.get(conn, card_id, channel)
    assert sub is not None and subscriptions.set_mode_if(conn, sub.id, "redirect", mode)


async def test_a_both_copy_shows_its_own_session_when_it_is_sooner_than_the_home_session(
        world: World, poller: Poller, db: Database):
    card_id, sub_id, _ = await both_copy_setup(world, db, mirror=True)
    await add_local_session(db, card_id, sub_id, starts_at=4000)     # sooner than the home session at 5000

    await poller.tick()

    [copy] = copy_posts(world.fluxer, BOTH_COPY)
    value = next_session_value(copy.payload["embeds"][0])
    assert value is not None and value.endswith(LOCAL_LINK)
    assert_card_writes_safe(world.fluxer)


async def test_a_both_copy_shows_the_home_session_linked_to_its_mirror_when_its_own_is_later(
        world: World, poller: Poller, db: Database):
    card_id, sub_id, _ = await both_copy_setup(world, db, mirror=True)
    await add_local_session(db, card_id, sub_id, starts_at=6000)     # later than the home session at 5000

    await poller.tick()

    [copy] = copy_posts(world.fluxer, BOTH_COPY)
    value = next_session_value(copy.payload["embeds"][0])
    assert value is not None and value.endswith(BOTH_MIRROR_LINK)
    assert SESSION_MESSAGE not in json.dumps(copy.payload)
    assert_card_writes_safe(world.fluxer)


async def test_a_both_copy_shows_the_home_session_with_no_link_when_it_has_no_mirror(
        world: World, poller: Poller, db: Database):
    await both_copy_setup(world, db, mirror=False)

    await poller.tick()

    [copy] = copy_posts(world.fluxer, BOTH_COPY)
    value = next_session_value(copy.payload["embeds"][0])
    assert value is not None and "fluxer.invalid" not in value
    assert SESSION_MESSAGE not in json.dumps(copy.payload)


async def test_a_local_copy_keeps_showing_only_its_own_session_when_the_home_session_is_sooner(
        world: World, poller: Poller, db: Database):
    card_id, sub_id = await local_copy_setup(world, db)
    await add_local_session(db, card_id, sub_id, starts_at=6000)

    await poller.tick()

    [local] = copy_posts(world.fluxer, LOCAL_COPY)
    assert next_session_value(local.payload["embeds"][0]).endswith(LOCAL_LINK)
    assert SESSION_MESSAGE not in json.dumps(local.payload)


async def test_the_poller_never_back_fills_a_missing_mirror_of_a_both_copy(world: World, poller: Poller, db: Database):
    _, sub_id, _ = await both_copy_setup(world, db, mirror=False)

    await poller.tick()
    await poller.tick()

    assert await db.run(lambda c: mirrors.for_sub(c, sub_id)) == []
