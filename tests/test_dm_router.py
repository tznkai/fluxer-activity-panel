# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The DM router is the security boundary in front of `admin.handle`. Non-admin `!session` commands and draft replies
take the rate-limited path. Admin heads from anyone reach `admin.handle`, which refuses non-admins and logs it. A bot
author, a non-snowflake author and a guild message never reach a session command or a draft. Clocks are stubs."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import text
from fluxer_panel.bot import Bot
from fluxer_panel.card_commands import CardContext
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_router import NON_ADMIN_LIMIT, NON_ADMIN_WINDOW, DmRouter, NonAdminLimiter
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.sources.registry import build_sources
from tests.fakes import FakeFluxerState, make_rest_app

TOKEN = "SENTINEL-TOKEN-dm-router"
ADMIN = "1001"
NON_ADMIN = "6001"
OTHER_NON_ADMIN = "6002"
DM = "9100"
GUILD_CHANNEL = "3001"
GUILD = "2001"
DM_MESSAGES = f"/api/v1/channels/{DM}/messages"
READY = {"session_id": "s1", "user": {"id": "5000"}}


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Recorder:
    """Stands in for the session command handler: records (args, author) and does nothing else."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, args: str, event: events.MessageCreate) -> None:
        self.calls.append((args, event.author_id))


class StubFlow:
    """Stands in for the session flow: a set of users with an open draft, and the replies it was given."""

    def __init__(self, drafts: tuple[str, ...] = ()) -> None:
        self.drafts = set(drafts)
        self.replies: list[str] = []

    def has_draft(self, user_id: str) -> bool:
        return user_id in self.drafts

    async def on_reply(self, event: events.MessageCreate) -> None:
        self.replies.append(event.author_id)


def make_config() -> Config:
    return Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC",
                            "FLUXER_INSTANCE_URL": "https://fluxer.invalid", "ADMIN_USER_IDS": ADMIN})


def message(content: str, author: str = NON_ADMIN, *, bot: bool = False, guild: bool = False) -> events.MessageCreate:
    data: dict[str, Any] = {"id": "8001", "channel_id": GUILD_CHANNEL if guild else DM, "content": content,
                            "author": {"id": author, "bot": bot}}
    if guild:
        data["guild_id"] = GUILD
    parsed = events.parse("MESSAGE_CREATE", data)
    assert isinstance(parsed, events.MessageCreate)
    return parsed


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState()
    server = await aiohttp_server(make_rest_app(state))
    return state, str(server.make_url("/")).rstrip("/")


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def card_db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "router.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def captured(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


def panel_records(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("fluxer_panel") and r.getMessage() == event]


def make_router(rest: FluxerREST, card_ctx: CardContext | None, limiter: NonAdminLimiter, *,
                recorder: Recorder | None = None, flow: StubFlow | None = None) -> DmRouter:
    return DmRouter(config=make_config(), rest=rest, card_ctx=card_ctx, limiter=limiter, session_flow=flow,
                    session_commands=recorder)


@pytest.fixture
def harness(fake, session, card_db):
    state, base = fake
    rest = FluxerREST(session, f"{base}/api", TOKEN)
    config = make_config()
    card_ctx = CardContext(db=card_db, rest=rest, config=config, sources=build_sources(session, config))
    return state, rest, card_ctx


# Session commands and the non-admin rate limit -----------------------------------------------------------

async def test_a_bare_session_from_a_non_admin_reaches_the_session_commands_with_no_args(harness) -> None:
    _, rest, card_ctx = harness
    recorder = Recorder()
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=recorder)
    await router.route(message("!session"))
    assert recorder.calls == [("", NON_ADMIN)]


async def test_a_session_command_passes_its_argument_string(harness) -> None:
    _, rest, card_ctx = harness
    recorder = Recorder()
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=recorder)
    await router.route(message("  !session   list  "))
    assert recorder.calls == [("list", NON_ADMIN)]


async def test_a_word_that_only_starts_with_session_is_not_a_session_command(harness) -> None:
    _, rest, card_ctx = harness
    recorder = Recorder()
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=recorder)
    await router.route(message("!sessions list"))
    assert recorder.calls == []


async def test_six_session_commands_in_a_minute_give_five_calls_and_a_silent_drop(harness) -> None:
    state, rest, card_ctx = harness
    recorder = Recorder()
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=recorder)
    for _ in range(NON_ADMIN_LIMIT + 1):
        await router.route(message("!session list"))
    assert len(recorder.calls) == NON_ADMIN_LIMIT
    assert state.requests == []


async def test_the_limit_lifts_once_the_window_has_passed(harness) -> None:
    _, rest, card_ctx = harness
    clock = Clock()
    recorder = Recorder()
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=clock), recorder=recorder)
    for _ in range(NON_ADMIN_LIMIT):
        await router.route(message("!session list"))
    clock.now = NON_ADMIN_WINDOW - 0.5
    await router.route(message("!session list"))
    assert len(recorder.calls) == NON_ADMIN_LIMIT

    clock.now = NON_ADMIN_WINDOW
    await router.route(message("!session list"))
    assert len(recorder.calls) == NON_ADMIN_LIMIT + 1


# The limiter's memory: an idle user's entry is swept, and a busy user keeps its count ---------------------

def test_an_idle_users_entry_is_swept_once_the_window_has_passed_and_the_user_is_still_admitted() -> None:
    clock = Clock()
    limiter = NonAdminLimiter(make_config(), clock=clock)
    assert limiter.admit(NON_ADMIN) is True
    assert NON_ADMIN in limiter._hits

    clock.now = NON_ADMIN_WINDOW
    assert limiter.admit(OTHER_NON_ADMIN) is True   # the first admit after the window runs the sweep
    assert NON_ADMIN not in limiter._hits

    assert limiter.admit(NON_ADMIN) is True
    assert NON_ADMIN in limiter._hits


def test_a_sweep_keeps_a_user_whose_hits_are_still_in_the_window() -> None:
    clock = Clock()
    limiter = NonAdminLimiter(make_config(), clock=clock)
    assert limiter.admit(NON_ADMIN) is True    # the first sweep runs here, at t=0
    clock.now = NON_ADMIN_WINDOW / 2
    for _ in range(NON_ADMIN_LIMIT - 1):
        assert limiter.admit(NON_ADMIN) is True

    clock.now = NON_ADMIN_WINDOW
    assert limiter.admit(OTHER_NON_ADMIN) is True   # sweep: NON_ADMIN's newest hit is at 30 s, so it stays
    assert NON_ADMIN in limiter._hits
    assert limiter.admit(NON_ADMIN) is True         # the t=0 hit has aged out; the four hits at 30 s remain
    assert limiter.admit(NON_ADMIN) is False        # five in the window again: the sweep kept the live hits


async def test_the_limit_is_per_user(harness) -> None:
    _, rest, card_ctx = harness
    recorder = Recorder()
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=recorder)
    for _ in range(NON_ADMIN_LIMIT):
        await router.route(message("!session list"))
    await router.route(message("!session list", author=OTHER_NON_ADMIN))
    assert recorder.calls[-1] == ("list", OTHER_NON_ADMIN)


async def test_an_admin_is_exempt_from_the_limit(harness) -> None:
    _, rest, card_ctx = harness
    recorder = Recorder()
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=recorder)
    for _ in range(NON_ADMIN_LIMIT + 1):
        await router.route(message("!session list", author=ADMIN))
    assert len(recorder.calls) == NON_ADMIN_LIMIT + 1


async def test_a_rate_limited_drop_is_logged_with_no_user_id(harness, captured) -> None:
    _, rest, card_ctx = harness
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=Recorder())
    for _ in range(NON_ADMIN_LIMIT + 1):
        await router.route(message("!session list"))
    [record] = panel_records(captured, "dm_rate_limited")
    assert NON_ADMIN not in repr(getattr(record, FIELDS_ATTR, {}))


# Draft replies share the limit ---------------------------------------------------------------------------

async def test_a_draft_reply_reaches_the_flow(harness) -> None:
    _, rest, card_ctx = harness
    flow = StubFlow(drafts=(NON_ADMIN,))
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), flow=flow)
    await router.route(message("tomorrow 19:30"))
    assert flow.replies == [NON_ADMIN]


async def test_a_non_command_dm_with_no_draft_does_nothing(harness) -> None:
    state, rest, card_ctx = harness
    flow = StubFlow()
    recorder = Recorder()
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=recorder, flow=flow)
    await router.route(message("hello there"))
    assert flow.replies == []
    assert recorder.calls == []
    assert state.requests == []


async def test_six_draft_replies_in_a_minute_give_five_and_a_silent_drop(harness) -> None:
    state, rest, card_ctx = harness
    flow = StubFlow(drafts=(NON_ADMIN,))
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), flow=flow)
    for _ in range(NON_ADMIN_LIMIT + 1):
        await router.route(message("sat 14:00"))
    assert len(flow.replies) == NON_ADMIN_LIMIT
    assert state.requests == []


async def test_session_commands_and_draft_replies_share_one_budget(harness) -> None:
    _, rest, card_ctx = harness
    flow = StubFlow(drafts=(NON_ADMIN,))
    recorder = Recorder()
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=recorder, flow=flow)
    for _ in range(3):
        await router.route(message("!session list"))
    for _ in range(3):
        await router.route(message("sat 14:00"))
    assert len(recorder.calls) + len(flow.replies) == NON_ADMIN_LIMIT


# Bot authors, non-snowflake authors and guild messages never reach the session paths ---------------------

async def test_a_bot_author_never_reaches_a_session_command_or_the_flow_and_gets_no_reply(harness) -> None:
    state, rest, card_ctx = harness
    flow = StubFlow(drafts=(NON_ADMIN,))
    recorder = Recorder()
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=recorder, flow=flow)
    await router.route(message("!session list", author=NON_ADMIN, bot=True))
    await router.route(message("sat 14:00", author=NON_ADMIN, bot=True))
    assert recorder.calls == []
    assert flow.replies == []
    assert state.requests == []


async def test_a_non_snowflake_author_never_reaches_a_session_command_or_the_flow(harness) -> None:
    state, rest, card_ctx = harness
    flow = StubFlow(drafts=("not-a-snowflake",))
    recorder = Recorder()
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=recorder, flow=flow)
    # The parser refuses a malformed author id, so this author is built directly to reach the router's own check.
    for content in ("!session list", "sat 14:00"):
        await router.route(events.MessageCreate(message_id="8001", channel_id=DM, guild_id=None,
                                                author_id="not-a-snowflake", author_is_bot=False,
                                                member_role_ids=None, content=content))
    assert recorder.calls == []
    assert flow.replies == []
    assert state.requests == []


async def test_a_card_command_from_a_bot_author_is_refused_and_logged_through_the_router(harness, captured) -> None:
    state, rest, card_ctx = harness
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=Recorder())
    await router.route(message("!card list", author=ADMIN, bot=True))
    assert state.requests == []
    [record] = panel_records(captured, "admin_denied")
    assert getattr(record, FIELDS_ATTR) == {"user_id": ADMIN, "channel_id": DM}


async def test_a_card_command_from_a_non_admin_reaches_admin_handle_and_is_refused_silently(harness, captured) -> None:
    state, rest, card_ctx = harness
    recorder = Recorder()
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=recorder)
    await router.route(message("!card list"))
    assert state.requests == []
    assert len(panel_records(captured, "admin_denied")) == 1
    assert recorder.calls == []


async def test_admin_heads_from_a_non_admin_are_not_rate_limited_and_stay_refused(harness, captured) -> None:
    state, rest, card_ctx = harness
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=Recorder())
    for _ in range(NON_ADMIN_LIMIT + 2):
        await router.route(message("!card list"))
    assert state.requests == []
    assert len(panel_records(captured, "admin_denied")) == NON_ADMIN_LIMIT + 2
    assert panel_records(captured, "dm_rate_limited") == []


async def test_a_card_command_from_an_admin_is_answered_as_before(harness) -> None:
    state, rest, card_ctx = harness
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=Recorder())
    await router.route(message("!card list", author=ADMIN))
    [reply] = state.requests_to(DM_MESSAGES)
    assert reply.payload["content"] == text.NO_CARDS


async def test_a_guild_session_command_is_never_answered_and_never_reaches_the_session_commands(harness) -> None:
    state, rest, card_ctx = harness
    recorder = Recorder()
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=recorder)
    await router.route(message("!session list", author=ADMIN, guild=True))
    await router.route(message("!session list", guild=True))
    assert recorder.calls == []
    assert state.requests == []


async def test_a_guild_draft_reply_never_reaches_the_flow(harness) -> None:
    state, rest, card_ctx = harness
    flow = StubFlow(drafts=(NON_ADMIN,))
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), flow=flow)
    await router.route(message("sat 14:00", guild=True))
    assert flow.replies == []
    assert state.requests == []


# Wiring through Bot ------------------------------------------------------------------------------------

async def test_bot_sends_a_dm_through_the_router(harness) -> None:
    state, rest, card_ctx = harness
    recorder = Recorder()
    router = make_router(rest, card_ctx, NonAdminLimiter(make_config(), clock=Clock()), recorder=recorder)
    bot = Bot(rest=rest, config=make_config(), roles=RoleCache(rest), card_ctx=card_ctx, dm_router=router)
    await bot.on_dispatch("READY", READY)
    await bot.on_dispatch("MESSAGE_CREATE", {"id": "8001", "channel_id": DM, "content": "!session list",
                                             "author": {"id": NON_ADMIN, "bot": False}})
    assert recorder.calls == [("list", NON_ADMIN)]
    assert state.requests == []
