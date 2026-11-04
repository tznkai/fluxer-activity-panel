# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bot routing: typed dispatches, self and bot filtering, reactions, log safety and gateway reconnects.

Every wait is bounded: `until` has a deadline and fails fast when the fake gateway script raised.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import pytest

from fluxer_client.events import MessageDelete
from fluxer_client.gateway import Gateway
from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import cards, text
from fluxer_panel.bot import Bot
from fluxer_panel.card_commands import CardContext
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.logs import setup_logging
from fluxer_panel.sources.registry import build_sources
from tests.fakes import (
    GATEWAY_STATE,
    FakeFluxerState,
    Override,
    expect,
    hold,
    make_gateway_app,
    make_rest_app,
    message_delete,
    send,
)

TOKEN = "SENTINEL-TOKEN-7f3a9c"
CONTENT = "SENTINEL-CONTENT-b41e"
TIMEOUT = 5
DM = "9100"
DM_MESSAGES = f"/api/v1/channels/{DM}/messages"
HELLO = {"heartbeat_interval": 30000}
READY = {"session_id": "s1", "user": {"id": "5000"}}
FAST: dict[str, Any] = {
    "backoff_base": 0.01,
    "backoff_max": 0.05,
    "invalid_session_delay": (0.0, 0.0),
    "reconnect_delay": (0.0, 0.0),
}


def make_config(admins: str = "1001") -> Config:
    return Config.from_env({"FLUXER_TOKEN": TOKEN, "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
                            "ADMIN_USER_IDS": admins})


def make_rest(session: aiohttp.ClientSession, base: str) -> FluxerREST:
    return FluxerREST(session, f"{base}/api", TOKEN)


def make_bot(rest: FluxerREST, admins: str = "1001", on_message_delete=None) -> Bot:
    return Bot(rest=rest, config=make_config(admins), roles=RoleCache(rest), on_message_delete=on_message_delete)


def dm_message(content: str, author: str = "1001", *, bot: bool = False) -> dict[str, Any]:
    return {"id": "8001", "channel_id": DM, "author": {"id": author, "bot": bot}, "content": content}


def guild_message(content: str) -> dict[str, Any]:
    return {"id": "8001", "channel_id": "3001", "guild_id": "2001", "author": {"id": "1001"}, "content": content}


async def until(cond: Callable[[], bool], state: FakeFluxerState, timeout: float = TIMEOUT) -> None:
    """Poll until `cond()`; fail fast if the fake recorded a script error."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if state.errors:
            raise AssertionError(f"fake gateway script failed: {state.errors}")
        if loop.time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.005)


def ws_url(server, path: str = "/") -> str:
    return str(server.make_url(path).with_scheme("ws"))


def make_gateway(session: aiohttp.ClientSession, server, on_dispatch) -> Gateway:
    return Gateway(session, ws_url(server), TOKEN, on_dispatch, rng=random.Random(0), **FAST)


@contextlib.asynccontextmanager
async def running(gateway: Gateway):
    task = asyncio.create_task(gateway.run())
    try:
        yield task
    finally:
        await gateway.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, TIMEOUT)


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


@pytest.fixture
def captured(root_logging) -> io.StringIO:
    stream = io.StringIO()
    setup_logging("DEBUG", [TOKEN], stream)
    return stream


def assert_clean(text: str) -> None:
    assert TOKEN not in text
    assert CONTENT not in text
    lines = text.splitlines()
    assert lines
    for line in lines:
        json.loads(line)   # every line is one JSON object


# --- routing -------------------------------------------------------------------------

async def test_ready_records_bot_user_id(fake, session, caplog) -> None:
    _, base = fake
    caplog.set_level(logging.INFO, logger="fluxer_panel.bot")
    bot = make_bot(make_rest(session, base))
    await bot.on_dispatch("READY", READY)
    assert bot.user_id == "5000"
    ready = [r for r in caplog.records if r.getMessage() == "ready"]
    assert [r.panel_fields for r in ready] == [{"user_id": "5000"}]


async def test_own_and_bot_messages_and_guild_messages_get_no_reply(fake, session) -> None:
    state, base = fake
    # the bot's own id (5000) is listed as an admin, so only the self-message filter can silence it
    bot = make_bot(make_rest(session, base), admins="1001,5000")
    await bot.on_dispatch("READY", READY)
    await bot.on_dispatch("MESSAGE_CREATE", dm_message("!admin ping", author="5000"))
    await bot.on_dispatch("MESSAGE_CREATE", dm_message("!admin ping", author="1001", bot=True))
    await bot.on_dispatch("MESSAGE_CREATE", guild_message("!admin ping"))
    assert state.requests == []
    # control: the same admin command in a DM from a human admin is answered
    await bot.on_dispatch("MESSAGE_CREATE", dm_message("!admin ping"))
    assert len(state.requests_to(DM_MESSAGES)) == 1


async def test_reactions_are_logged_with_ids_only(fake, session, caplog) -> None:
    _, base = fake
    caplog.set_level(logging.INFO, logger="fluxer_panel.bot")
    bot = make_bot(make_rest(session, base))
    await bot.on_dispatch("MESSAGE_REACTION_ADD", {
        "user_id": "1001", "channel_id": "3001", "message_id": "8001", "guild_id": "2001",
        "emoji": {"name": "x"},
    })
    await bot.on_dispatch("MESSAGE_REACTION_REMOVE", {
        "user_id": "1001", "channel_id": DM, "message_id": "8001", "emoji": {"name": "x"},
    })
    reactions = [r for r in caplog.records if r.getMessage() == "reaction"]
    assert len(reactions) == 2
    allowed = {"event_type", "user_id", "channel_id", "message_id", "guild_id"}
    for record in reactions:
        assert set(record.panel_fields) <= allowed
    assert reactions[0].panel_fields["guild_id"] == "2001"
    assert "guild_id" not in reactions[1].panel_fields   # a DM reaction carries no guild


async def test_guild_role_change_invalidates_role_cache(fake, session) -> None:
    state, base = fake
    state.roles["2001"] = [{"id": "4001", "name": "GM"}]
    bot = make_bot(make_rest(session, base))
    assert await bot.roles.role_ids("2001", "gm") == frozenset({"4001"})
    await bot.on_dispatch("GUILD_ROLE_UPDATE", {"guild_id": "2001"})
    assert await bot.roles.role_ids("2001", "gm") == frozenset({"4001"})
    assert len(state.requests_to("/api/v1/guilds/2001/roles")) == 2


async def test_unknown_dispatch_is_ignored(fake, session) -> None:
    state, base = fake
    bot = make_bot(make_rest(session, base))
    await bot.on_dispatch("TYPING_START", {"channel_id": DM})
    assert state.requests == []


# --- AC3.5: nothing sensitive reaches the logs ---------------------------------------

async def test_handled_admin_dm_logs_no_token_or_content(captured, fake, session) -> None:
    state, base = fake
    bot = make_bot(make_rest(session, base))
    await bot.on_dispatch("READY", READY)
    await bot.on_dispatch("MESSAGE_CREATE", dm_message(f"!admin ping {CONTENT}"))
    assert len(state.requests_to(DM_MESSAGES)) == 1
    assert "admin_command" in captured.getvalue()
    assert_clean(captured.getvalue())


async def test_denied_admin_dm_logs_no_token_or_content(captured, fake, session) -> None:
    state, base = fake
    bot = make_bot(make_rest(session, base))
    await bot.on_dispatch("MESSAGE_CREATE", dm_message(f"!admin ping {CONTENT}", author="2002"))
    assert state.requests == []
    assert "admin_denied" in captured.getvalue()
    assert_clean(captured.getvalue())


async def test_reaction_event_logs_no_token_or_content(captured, fake, session) -> None:
    _, base = fake
    bot = make_bot(make_rest(session, base))
    await bot.on_dispatch("MESSAGE_REACTION_ADD", {
        "user_id": "1001", "channel_id": DM, "message_id": "8001", "emoji": {"name": CONTENT},
    })
    assert "reaction" in captured.getvalue()
    assert_clean(captured.getvalue())


async def test_rest_error_in_gateway_dispatch_logs_redacted_traceback(captured, aiohttp_server, session) -> None:
    rest_state = FakeFluxerState()
    rest_state.overrides[f"POST {DM_MESSAGES}"] = Override(403, b'{"code":"MISSING_PERMISSIONS","message":"x"}')
    rest_server = await aiohttp_server(make_rest_app(rest_state))
    rest_base = str(rest_server.make_url("/")).rstrip("/")

    async def script(ws, index, state):
        await send(ws, 10, HELLO)
        await expect(ws, 2)
        await send(ws, 0, READY, t="READY", s=1)
        await send(ws, 0, dm_message(f"!admin ping {CONTENT}"), t="MESSAGE_CREATE", s=2)
        await hold(ws)

    gw_app = make_gateway_app(script)
    gw_state = gw_app[GATEWAY_STATE]
    gw_server = await aiohttp_server(gw_app)
    bot = make_bot(make_rest(session, rest_base))
    gateway = make_gateway(session, gw_server, bot.on_dispatch)
    async with running(gateway):
        await until(lambda: "dispatch handler failed" in captured.getvalue(), gw_state)
    text = captured.getvalue()
    assert_clean(text)
    [failed] = [json.loads(line) for line in text.splitlines() if "dispatch handler failed" in line]
    assert "FluxerHTTPError" in failed["exc"]


# --- AC6.6 (parsing and routing): MESSAGE_DELETE reaches the hook, and no hook is harmless ---

async def test_message_delete_from_gateway_reaches_hook_once(aiohttp_server, fake, session) -> None:
    rest_state, base = fake
    seen: list[MessageDelete] = []

    async def hook(deleted: MessageDelete) -> None:
        seen.append(deleted)

    async def script(ws, index, state):
        await send(ws, 10, HELLO)
        await expect(ws, 2)
        await send(ws, 0, READY, t="READY", s=1)
        await send(ws, 0, message_delete("8001", "3001", "2001"), t="MESSAGE_DELETE", s=2)
        await hold(ws)

    gw_app = make_gateway_app(script)
    gw_state = gw_app[GATEWAY_STATE]
    gw_server = await aiohttp_server(gw_app)
    bot = make_bot(make_rest(session, base), on_message_delete=hook)
    gateway = make_gateway(session, gw_server, bot.on_dispatch)
    async with running(gateway):
        await until(lambda: len(seen) == 1, gw_state)
    assert seen == [MessageDelete("8001", "3001", "2001")]
    assert rest_state.requests == []


async def test_message_delete_without_hook_is_ignored(fake, session) -> None:
    state, base = fake
    bot = make_bot(make_rest(session, base))
    await bot.on_dispatch("MESSAGE_DELETE", {"id": "8001", "channel_id": "3001", "guild_id": "2001"})
    assert state.requests == []


# --- AC3.3 at bot level: the gateway resumes or re-identifies, the process keeps running ---

def reconnect_script(second: Callable[[Any, FakeFluxerState], Awaitable[None]]):
    """Connection 0: READY then a 4000 close. Connection 1 is handled by `second`."""
    async def script(ws, index, state):
        await send(ws, 10, HELLO)
        if index == 0:
            await expect(ws, 2)
            await send(ws, 0, READY, t="READY", s=1)
            await ws.close(code=4000)
            return
        await second(ws, state)
    return script


async def test_gateway_drop_resumes_then_answers_admin_dm(aiohttp_server, fake, session) -> None:
    rest_state, base = fake

    async def resume(ws, state):
        await expect(ws, 6)
        await send(ws, 0, {}, t="RESUMED", s=2)
        await send(ws, 0, dm_message("!admin ping"), t="MESSAGE_CREATE", s=3)
        await hold(ws)

    gw_app = make_gateway_app(reconnect_script(resume))
    gw_state = gw_app[GATEWAY_STATE]
    gw_server = await aiohttp_server(gw_app)
    bot = make_bot(make_rest(session, base))
    gateway = make_gateway(session, gw_server, bot.on_dispatch)
    async with running(gateway):
        await until(lambda: len(rest_state.requests_to(DM_MESSAGES)) == 1, gw_state)
    assert len(gw_state.connections) == 2
    assert gw_state.connections[1][0]["op"] == 6
    assert gw_state.connections[1][0]["d"]["session_id"] == "s1"
    assert gw_state.errors == []
    assert rest_state.requests_to(DM_MESSAGES)[0].payload == {"content": "pong"}


async def test_invalid_session_reidentifies_then_answers_admin_dm(aiohttp_server, fake, session) -> None:
    rest_state, base = fake

    async def reidentify(ws, state):
        await expect(ws, 6)
        await send(ws, 9, False)                      # invalid session, not resumable
        await expect(ws, 2)                           # identify again on the same socket
        await send(ws, 0, {"session_id": "s2", "user": {"id": "5000"}}, t="READY", s=3)
        await send(ws, 0, dm_message("!admin ping"), t="MESSAGE_CREATE", s=4)
        await hold(ws)

    gw_app = make_gateway_app(reconnect_script(reidentify))
    gw_state = gw_app[GATEWAY_STATE]
    gw_server = await aiohttp_server(gw_app)
    bot = make_bot(make_rest(session, base))
    gateway = make_gateway(session, gw_server, bot.on_dispatch)
    async with running(gateway):
        await until(lambda: len(rest_state.requests_to(DM_MESSAGES)) == 1, gw_state)
    assert len(gw_state.connections) == 2
    assert [f["op"] for f in gw_state.connections[1][:2]] == [6, 2]
    assert gw_state.errors == []
    assert rest_state.requests_to(DM_MESSAGES)[0].payload == {"content": "pong"}


@pytest.fixture
async def card_db(tmp_path):
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


async def test_dm_card_list_is_answered_through_the_card_context(fake, session, card_db) -> None:
    state, base = fake
    rest = make_rest(session, base)
    config = make_config("1001")
    card_ctx = CardContext(db=card_db, rest=rest, config=config, sources=build_sources(session, config))
    bot = Bot(rest=rest, config=config, roles=RoleCache(rest), card_ctx=card_ctx)
    await bot.on_dispatch("READY", READY)
    await bot.on_dispatch("MESSAGE_CREATE", dm_message("!card list"))

    [message] = state.requests_to(DM_MESSAGES)
    assert message.payload["content"] == text.NO_CARDS


async def test_bot_without_a_card_context_sends_nothing_for_card_commands(fake, session) -> None:
    state, base = fake
    rest = make_rest(session, base)
    bot = Bot(rest=rest, config=make_config("1001"), roles=RoleCache(rest))
    await bot.on_dispatch("READY", READY)
    await bot.on_dispatch("MESSAGE_CREATE", dm_message("!card list"))

    assert state.requests == []


# --- AC7.2 through the gateway: a refused command is logged and silent; other guild messages are not logged ---

NON_LOG_FIELDS = {"ts", "level", "logger", "event"}


def log_events(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def logged_fields(stream: io.StringIO, event: str) -> list[dict[str, Any]]:
    """The fields of each logged `event`, without the envelope keys every line carries."""
    return [{k: v for k, v in line.items() if k not in NON_LOG_FIELDS}
            for line in log_events(stream) if line["event"] == event]


def card_bot(session: aiohttp.ClientSession, base: str, db: Database) -> Bot:
    rest = make_rest(session, base)
    config = make_config("1001")
    card_ctx = CardContext(db=db, rest=rest, config=config, sources=build_sources(session, config))
    return Bot(rest=rest, config=config, roles=RoleCache(rest), card_ctx=card_ctx)


async def test_guild_card_command_from_an_admin_is_refused_and_logged(captured, fake, session, card_db) -> None:
    state, base = fake
    bot = card_bot(session, base, card_db)
    await bot.on_dispatch("READY", READY)
    await bot.on_dispatch("MESSAGE_CREATE", guild_message(f"!card new x X {CONTENT}"))

    assert state.requests == []
    assert await card_db.run(lambda c: cards.list_cards(c)) == []
    assert logged_fields(captured, "admin_denied") == [{"user_id": "1001", "channel_id": "3001"}]
    assert_clean(captured.getvalue())


async def test_dm_card_command_from_a_bot_author_is_refused_and_logged(captured, fake, session, card_db) -> None:
    state, base = fake
    bot = card_bot(session, base, card_db)
    await bot.on_dispatch("READY", READY)
    await bot.on_dispatch("MESSAGE_CREATE", dm_message(f"!card new x X {CONTENT}", bot=True))

    assert state.requests == []
    assert await card_db.run(lambda c: cards.list_cards(c)) == []
    assert logged_fields(captured, "admin_denied") == [{"user_id": "1001", "channel_id": DM}]
    assert_clean(captured.getvalue())


async def test_guild_message_that_is_not_a_command_is_ignored_without_a_log(captured, fake, session, card_db) -> None:
    state, base = fake
    bot = card_bot(session, base, card_db)
    await bot.on_dispatch("READY", READY)
    logged_before = len(log_events(captured))
    await bot.on_dispatch("MESSAGE_CREATE", guild_message(f"hello {CONTENT}"))

    assert state.requests == []
    assert await card_db.run(lambda c: cards.list_cards(c)) == []
    assert log_events(captured)[logged_before:] == []


async def test_own_card_command_is_skipped_without_a_denial_log(captured, fake, session, card_db) -> None:
    state, base = fake
    bot = card_bot(session, base, card_db)
    await bot.on_dispatch("READY", READY)
    logged_before = len(log_events(captured))
    await bot.on_dispatch("MESSAGE_CREATE", dm_message("!card new x X", author="5000", bot=True))

    assert state.requests == []
    assert log_events(captured)[logged_before:] == []
