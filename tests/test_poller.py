# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The card poller against a real SQLite file, the fake Fluxer REST and gateway, the fake Panel and stub probes.

Each test name states the behaviour it checks. The `AC` comments name the acceptance criteria for the card poller.
"""

from __future__ import annotations

import asyncio
import io
import logging
import random
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.events import MessageCreate, MessageDelete
from fluxer_client.gateway import Gateway
from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import admin, cards, render, text
from fluxer_panel.bot import Bot
from fluxer_panel.card_commands import CardContext, CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.logs import setup_logging
from fluxer_panel.poller import _DELETED_CAP, Poller
from fluxer_panel.sources.base import PlayerCounts, ProbeDetails, ProbeFailed, ProbeResult
from fluxer_panel.sources.pterodactyl import PterodactylManager
from fluxer_panel.sources.registry import Sources
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
POSTED = f"{GAMES_MESSAGES}/5001"
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
        "FLUXER_TOKEN": TOKEN,
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
    "voice": cards.set_voice,
    "text_channel": lambda c, card_id, value: cards.set_text_channel(c, card_id, value[0], value[1]),
    "address": cards.set_address,
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


async def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the fake servers")
        await asyncio.sleep(0.01)


def posts(state: FakeFluxerState) -> list[Recorded]:
    return [r for r in state.requests if r.method == "POST" and r.path == GAMES_MESSAGES]


def patches(state: FakeFluxerState) -> list[Recorded]:
    return [r for r in state.requests if r.method == "PATCH"]


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
        await _wait_until(lambda: len(posts(world.fluxer)) == 1)
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

    assert [(r.method, r.status) for r in world.fluxer.requests] == [("GET", 200)]


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
    gets = [(r.path, r.status) for r in world.fluxer.requests if r.method == "GET"]
    assert gets[-1] == (f"{GAMES_MESSAGES}/5102", 404)     # the later card's lookup ran after the failed one


# AC6.6: MESSAGE_DELETE recreates the card without the poll --------------------------------------------

async def test_a_message_delete_recreates_the_card_without_waiting_for_the_poll(
        world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha")
    run_task = asyncio.create_task(poller.run())
    try:
        await _wait_until(lambda: len(posts(world.fluxer)) == 1)
        await poller.on_message_delete(MessageDelete("5001", GAMES, None))
        await _wait_until(lambda: len(posts(world.fluxer)) == 2, timeout=2.0)
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
        await _wait_until(lambda: len(posts(world.fluxer)) == 1)
        world.fluxer.deleted.add((GAMES, "5001"))
        deleted.set()
        await _wait_until(lambda: len(posts(world.fluxer)) == 2, timeout=5.0)
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
        await _wait_until(lambda: len(posts(world.fluxer)) == 1)
        await wait_for_card(db, card_id, lambda c: c.message_id == "5001")
        await set_title(db, card_id, "Beta")
        world.fluxer.delays[f"PATCH {POSTED}"] = 0.3
        poller.kick()
        await asyncio.sleep(0.1)     # the edit is delayed 0.3 s, so it is still in flight here
        await poller.on_message_delete(MessageDelete("5001", GAMES, None))
        await _wait_until(lambda: len(posts(world.fluxer)) == 2, timeout=2.0)
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
        await _wait_until(lambda: len(posts(world.fluxer)) == 2, timeout=2.0)
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
        await _wait_until(lambda: len(posts(world.fluxer)) == 1)
        await wait_for_card(db, card_id, lambda c: c.message_id == "5001")
        await set_title(db, card_id, "Beta")
        poller.kick()
        await _wait_until(lambda: len(patches(world.fluxer)) == 1, timeout=2.0)
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
        await _wait_until(lambda: len(posts(world.fluxer)) == 1)
        await wait_for_card(db, card_id, lambda c: c.message_id == "5001")
        world.fluxer.deleted.add((GAMES, "5001"))
        await set_title(db, card_id, "Beta")
        poller.kick()
        await _wait_until(lambda: len(posts(world.fluxer)) == 2, timeout=2.0)
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
        await _wait_until(lambda: bool(records(caplog, "poller_tick_failed")))
        await db.run(lambda c: c.execute("ALTER TABLE card_runtime_away RENAME TO card_runtime"))
        poller.kick()
        await _wait_until(lambda: len(posts(world.fluxer)) == 1)
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
    await _wait_until(lambda: len(world.probe.calls) == 1)

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


async def test_a_voice_cleared_during_a_name_refresh_stays_cleared(world: World, poller: Poller, db: Database):
    card_id = await seed(db, "alpha", voice=cards.ChannelLink(GUILD, VOICE, "Old", True))
    world.fluxer.channels[VOICE] = voice_channel(name="New")
    world.fluxer.delays[f"GET /api/v1/channels/{VOICE}"] = 0.3
    tick = asyncio.create_task(poller.tick())
    await asyncio.sleep(0.1)     # the channel lookup is delayed 0.3 s, so the admin's clear lands during it
    await db.run(lambda c: cards.clear_slot(c, card_id, "voice"))
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
    await db.run(lambda c: cards.set_voice(c, card_id, admin_choice))
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
    assert "#New" in fields_of(sent.payload["embeds"][0])[text.FIELD_CHANNELS]


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
    assert text.FIELD_CHANNELS not in fields_of(sent.payload["embeds"][0])
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
    assert "#Admin" in fields_of(sent.payload["embeds"][0])[text.FIELD_CHANNELS]
    assert_card_writes_safe(world.fluxer)
