# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The DM helpers: a user DM returns its ids, fails without raising, and never logs the recipient's id or the content."""

from __future__ import annotations

import logging

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_panel import notify
from fluxer_panel.config import Config
from fluxer_panel.logs import FIELDS_ATTR
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "SENTINEL-TOKEN-notify"
USER = "1500"
CONTENT = "SECRET-CONTENT-notify"
OPEN_DM = "/api/v1/users/@me/channels"


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
def captured(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


def make_config(admins: str) -> Config:
    env = {"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": "https://fluxer.invalid", "ADMIN_USER_IDS": admins}
    return Config.from_env(env)


def panel_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The records the panel wrote; aiohttp's access log is left out."""
    return [record for record in caplog.records if record.name.startswith("fluxer_panel")]


def rendered(caplog: pytest.LogCaptureFixture) -> str:
    """Everything a panel log record carries: the message, its fields and the traceback text."""
    parts: list[str] = []
    for record in panel_records(caplog):
        parts.append(record.getMessage())
        parts.append(repr(getattr(record, FIELDS_ATTR, {})))
        if record.exc_text:
            parts.append(record.exc_text)
    return "\n".join(parts)


async def test_dm_user_sends_with_no_mentions_and_returns_the_ids(fake, session) -> None:
    state, base = fake
    rest = FluxerREST(session, f"{base}/api", TOKEN)

    result = await notify.dm_user(rest, USER, CONTENT)

    dm_channel = state.dm_channels[USER]
    assert result == (dm_channel, result[1])
    [sent] = state.requests_to(f"/api/v1/channels/{dm_channel}/messages")
    assert sent.payload["allowed_mentions"] == {"parse": []}
    assert sent.payload["content"] == CONTENT
    assert result[1] == str(state.next_message_id - 1)


async def test_dm_user_failure_gives_none_and_logs_the_type_only(fake, session, captured) -> None:
    state, base = fake
    state.overrides[f"POST {OPEN_DM}"] = Override(403, b'{"code":"MISSING_PERMISSIONS","message":"no"}')
    rest = FluxerREST(session, f"{base}/api", TOKEN)

    assert await notify.dm_user(rest, USER, CONTENT) is None

    [record] = [r for r in panel_records(captured) if r.getMessage() == "user_dm_failed"]
    assert getattr(record, FIELDS_ATTR) == {"reason": "FluxerHTTPError"}
    text = rendered(captured)
    assert USER not in text
    assert CONTENT not in text


async def test_dm_user_failure_on_send_gives_none(fake, session, captured) -> None:
    state, base = fake
    state.dm_channels[USER] = "9100"
    state.overrides["POST /api/v1/channels/9100/messages"] = Override(
        403, b'{"code":"MISSING_PERMISSIONS","message":"no"}')
    rest = FluxerREST(session, f"{base}/api", TOKEN)

    assert await notify.dm_user(rest, USER, CONTENT) is None
    assert USER not in rendered(captured)
    assert CONTENT not in rendered(captured)


async def test_dm_admins_sends_each_admin_one_dm_in_id_order(fake, session) -> None:
    state, base = fake
    rest = FluxerREST(session, f"{base}/api", TOKEN)
    config = make_config("1002,1001")

    await notify.dm_admins(rest, config, CONTENT, failed_event="admin_alert_failed")

    first, second = state.dm_channels["1001"], state.dm_channels["1002"]
    [to_first] = state.requests_to(f"/api/v1/channels/{first}/messages")
    [to_second] = state.requests_to(f"/api/v1/channels/{second}/messages")
    assert to_first.payload == {"content": CONTENT, "allowed_mentions": {"parse": []}}
    assert to_second.payload == {"content": CONTENT, "allowed_mentions": {"parse": []}}


async def test_dm_admins_failure_is_logged_under_the_given_event_and_the_rest_still_send(
        fake, session, captured) -> None:
    state, base = fake
    state.dm_channels["1001"] = "9101"
    state.overrides["POST /api/v1/channels/9101/messages"] = Override(
        403, b'{"code":"MISSING_PERMISSIONS","message":"no"}')
    rest = FluxerREST(session, f"{base}/api", TOKEN)

    await notify.dm_admins(rest, make_config("1001,1002"), CONTENT, failed_event="admin_alert_failed")

    assert [r.getMessage() for r in panel_records(captured)] == ["admin_alert_failed"]
    assert "1002" in state.dm_channels
