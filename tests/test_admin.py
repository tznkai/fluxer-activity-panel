# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin authorization and the ping command: pure checks, plus handle() against the fake REST server."""

from __future__ import annotations

import logging

import aiohttp
import pytest

from fluxer_client.events import MessageCreate
from fluxer_client.rest import FluxerREST
from fluxer_panel.admin import authorize, handle, parse_command, parse_head
from fluxer_panel.config import Config
from tests.fakes import FakeFluxerState, make_rest_app

TOKEN = "SENTINEL-TOKEN-7f3a9c"
CONTENT = "SENTINEL-CONTENT-b41e"
DM = "9100"
GUILD_CHANNEL = "3001"
DM_MESSAGES = f"/api/v1/channels/{DM}/messages"


def make_config(**extra: str) -> Config:
    env = {"FLUXER_TOKEN": TOKEN, "FLUXER_INSTANCE_URL": "https://fluxer.invalid", "ADMIN_USER_IDS": "1001", **extra}
    return Config.from_env(env)


def make_message(content: str, *, author: str = "1001", guild: str | None = None,
                 bot: bool = False, channel: str = DM) -> MessageCreate:
    return MessageCreate(message_id="8001", channel_id=channel, guild_id=guild, author_id=author,
                         author_is_bot=bot, member_role_ids=None, content=content)


def make_rest(session: aiohttp.ClientSession, base: str) -> FluxerREST:
    return FluxerREST(session, f"{base}/api", TOKEN)


def admin_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "fluxer_panel.admin"]


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState()
    server = await aiohttp_server(make_rest_app(state))
    return state, str(server.make_url("/")).rstrip("/")


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


# --- parse_command (pure) ------------------------------------------------------------

@pytest.mark.parametrize(("content", "prefix", "expected"), [
    ("!admin ping", "!", "ping"),
    ("!admin", "!", ""),
    ("  !admin   PING  ", "!", "ping"),
    ("!admin dance now", "!", "dance"),
    ("!administrator", "!", None),
    ("hello", "!", None),
    ("!admin ping", "?", None),
    ("?admin ping", "?", "ping"),
])
def test_parse_command(content: str, prefix: str, expected: str | None) -> None:
    assert parse_command(content, prefix) == expected


# --- authorize (pure) ----------------------------------------------------------------

def test_admin_dm_is_authorized() -> None:
    assert authorize(make_message("!admin ping"), make_config()) is True


@pytest.mark.parametrize(("author", "bot"), [
    ("1001", True),          # a bot whose id matches an admin
    ("01001", False),        # leading zero
    (" 1001", False),        # whitespace
    ("1001 ", False),
    ("1001\n", False),
    ("١٠٠١", False),   # Arabic-Indic digits spelling 1001
    ("abc", False),          # malformed
])
async def test_forged_or_malformed_author_is_not_authorized(fake, session, author: str, bot: bool) -> None:
    state, base = fake
    config = make_config()
    message = make_message("!admin ping", author=author, bot=bot)
    assert authorize(message, config) is False
    await handle(message, config, make_rest(session, base))
    assert state.requests == []


# --- handle() against the fake REST server -------------------------------------------

async def test_admin_dm_ping_gets_pong_in_that_dm(fake, session) -> None:
    state, base = fake
    await handle(make_message("!admin ping"), make_config(), make_rest(session, base))
    [request] = state.requests_to(DM_MESSAGES)
    assert request.payload == {"content": "pong"}
    assert request.authorization == f"Bot {TOKEN}"


async def test_non_admin_dm_gets_no_reply_and_logs_ids_only(fake, session, caplog) -> None:
    state, base = fake
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.admin")
    await handle(make_message(f"!admin ping {CONTENT}", author="2002"), make_config(), make_rest(session, base))
    assert state.requests == []
    [record] = admin_records(caplog)
    assert record.getMessage() == "admin_denied"
    assert record.panel_fields == {"user_id": "2002", "channel_id": DM}
    assert CONTENT not in caplog.text


async def test_admin_in_guild_channel_gets_no_reply(fake, session) -> None:
    state, base = fake
    message = make_message("!admin ping", guild="2001", channel=GUILD_CHANNEL)
    await handle(message, make_config(), make_rest(session, base))
    assert state.requests == []


@pytest.mark.parametrize("content", ["!admin dance", "!admin"])
async def test_unknown_subcommand_gets_one_line_list(fake, session, content: str) -> None:
    state, base = fake
    await handle(make_message(content), make_config(), make_rest(session, base))
    [request] = state.requests_to(DM_MESSAGES)
    reply = request.payload["content"]
    assert "\n" not in reply
    assert "!admin ping" in reply


async def test_custom_prefix_is_honoured(fake, session) -> None:
    state, base = fake
    config = make_config(COMMAND_PREFIX="?")
    rest = make_rest(session, base)
    await handle(make_message("!admin ping"), config, rest)
    assert state.requests == []
    await handle(make_message("?admin ping"), config, rest)
    [request] = state.requests_to(DM_MESSAGES)
    assert request.payload == {"content": "pong"}


async def test_non_command_dm_is_silent(fake, session, caplog) -> None:
    state, base = fake
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.admin")
    await handle(make_message("hello"), make_config(), make_rest(session, base))
    assert state.requests == []
    assert admin_records(caplog) == []


# --- parse_head and the !card head -----------------------------------------------------

@pytest.mark.parametrize(("content", "expected"), [
    ("!card list", ("card", "list")),
    ("  !card   set mc title  Hi ", ("card", "set mc title  Hi")),
    ("!card", ("card", "")),
    ("!admin ping", ("admin", "ping")),
    ("!request list", ("request", "list")),
    ("!request   CLEAR mc ", ("request", "CLEAR mc")),
    ("!requests list", None),
    ("!cards list", None),
    ("!card\nlist", None),
    ("hello", None),
])
def test_parse_head(content: str, expected: tuple[str, str] | None) -> None:
    assert parse_head(content, "!") == expected


async def test_card_head_without_a_card_context_is_ignored(fake, session) -> None:
    state, base = fake
    await handle(make_message("!card list"), make_config(), make_rest(session, base))
    assert state.requests == []
