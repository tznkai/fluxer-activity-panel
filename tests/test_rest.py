# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fluxer REST client tests against the fake server (no aiohttp internals mocked)."""

from __future__ import annotations

import aiohttp
import pytest

from fluxer_client.rest import (
    FluxerHTTPError,
    FluxerREST,
    discover_api,
    resolve_endpoints,
)
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "test.token"
PNG = b"\x89PNG\r\n\x1a\n-card-bytes"
MESSAGES = "/api/v1/channels/3001/messages"


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState()
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    return state, base


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


def make_rest(session, base, *, sleeps=None, **kwargs) -> FluxerREST:
    async def record_sleep(delay: float) -> None:
        if sleeps is not None:
            sleeps.append(delay)

    return FluxerREST(session, f"{base}/api", TOKEN, sleep=record_sleep, **kwargs)


# Endpoint discovery and overrides


async def test_discover_api_returns_api_public_from_well_known(fake, session):
    _, base = fake
    assert await discover_api(session, base) == f"{base}/api"


async def test_discover_api_tolerates_trailing_slash(fake, session):
    _, base = fake
    assert await discover_api(session, base + "/") == f"{base}/api"


async def test_resolve_endpoints_discovers_both_from_instance_url(fake, session):
    state, base = fake
    api, gateway = await resolve_endpoints(
        session, token=TOKEN, instance_url=base, api_url=None, gateway_url=None
    )
    assert (api, gateway) == (f"{base}/api", state.gateway_url)
    paths = [r.path for r in state.requests]
    assert "/.well-known/fluxer" in paths
    assert "/api/v1/gateway/bot" in paths


async def test_explicit_api_and_gateway_overrides_skip_all_discovery(fake, session):
    state, base = fake
    result = await resolve_endpoints(
        session,
        token=TOKEN,
        instance_url=None,
        api_url=f"{base}/api",
        gateway_url="ws://override/gw",
    )
    assert result == (f"{base}/api", "ws://override/gw")
    assert state.requests == []


async def test_explicit_api_override_wins_over_instance_discovery(fake, session):
    state, base = fake
    result = await resolve_endpoints(
        session,
        token=TOKEN,
        instance_url=base,
        api_url=f"{base}/api",
        gateway_url="ws://override/gw",
    )
    assert result == (f"{base}/api", "ws://override/gw")
    assert state.requests == []


async def test_api_url_only_gets_gateway_from_gateway_bot(fake, session):
    state, base = fake
    result = await resolve_endpoints(
        session, token=TOKEN, instance_url=None, api_url=f"{base}/api", gateway_url=None
    )
    assert result == (f"{base}/api", state.gateway_url)
    paths = [r.path for r in state.requests]
    assert paths == ["/api/v1/gateway/bot"]


async def test_resolve_endpoints_without_instance_or_api_url_raises_and_requests_nothing(fake, session):
    state, _ = fake
    with pytest.raises(ValueError):
        await resolve_endpoints(session, token=TOKEN, instance_url=None, api_url=None, gateway_url=None)
    assert state.requests == []


async def test_trailing_slash_on_api_url_is_tolerated(fake, session):
    state, base = fake
    rest = FluxerREST(session, f"{base}/api/", TOKEN)
    assert await rest.get_gateway_url() == state.gateway_url
    assert state.requests_to("/api/v1/gateway/bot")[0].status == 200


async def test_every_rest_call_carries_bot_authorization(fake, session):
    state, base = fake
    state.roles["1001"] = [{"id": "r1", "name": "GM"}]
    state.members[("1001", "2001")] = {"roles": ["r1"], "nick": None}
    rest = make_rest(session, base)
    await rest.get_gateway_url()
    await rest.get_roles("1001")
    await rest.get_member("1001", "2001")
    await rest.send_message("3001", embeds=[{"description": "hi"}])
    assert len(state.requests) == 4
    assert {r.authorization for r in state.requests} == {f"Bot {TOKEN}"}


# Messages: JSON and multipart


async def test_send_message_with_image_is_multipart_with_attachment_reference(fake, session):
    state, base = fake
    embeds = [{"description": "hi", "image": {"url": "attachment://card.png"}}]
    await make_rest(session, base).send_message(
        "3001", embeds=embeds, file=("card.png", PNG, "image/png"), reply_to="40000"
    )
    [sent] = state.requests_to(MESSAGES)
    assert sent.content_type == "multipart/form-data"
    assert sent.payload["embeds"][0]["image"]["url"] == "attachment://card.png"
    assert sent.payload["message_reference"] == {"message_id": "40000"}
    assert sent.files == [("card.png", PNG)]


async def test_send_message_without_image_is_json_with_no_image_key(fake, session):
    state, base = fake
    await make_rest(session, base).send_message("3001", embeds=[{"description": "hi"}])
    [sent] = state.requests_to(MESSAGES)
    assert sent.content_type == "application/json"
    assert "image" not in sent.payload["embeds"][0]
    assert "message_reference" not in sent.payload
    assert sent.files == []


async def test_send_message_content_only_posts_json_content(fake, session):
    state, base = fake
    await make_rest(session, base).send_message("3001", content="hi")
    [sent] = state.requests_to(MESSAGES)
    assert sent.content_type == "application/json"
    assert sent.payload == {"content": "hi"}


async def test_send_message_without_content_embeds_or_file_raises_and_requests_nothing(fake, session):
    state, base = fake
    rest = make_rest(session, base)
    with pytest.raises(ValueError):
        await rest.send_message("3001")
    with pytest.raises(ValueError):
        await rest.send_message("3001", reply_to="40000")
    assert state.requests == []


async def test_attachment_filename_outside_allowed_set_raises_and_requests_nothing(fake, session):
    state, base = fake
    with pytest.raises(ValueError):
        await make_rest(session, base).send_message("3001", file=("../x.png", PNG, "image/png"))
    assert state.requests == []


# 429 retries


async def test_429_is_retried_after_retry_after_with_intact_multipart(fake, session):
    state, base = fake
    state.rate_limit_next = 2
    sleeps: list[float] = []
    embeds = [{"description": "hi", "image": {"url": "attachment://card.png"}}]
    await make_rest(session, base, sleeps=sleeps).send_message(
        "3001", embeds=embeds, file=("card.png", PNG, "image/png")
    )
    assert sleeps == [0.25, 0.25]
    attempts = state.requests_to(MESSAGES)
    assert [r.status for r in attempts] == [429, 429, 200]
    assert attempts[-1].files == [("card.png", PNG)]
    assert attempts[-1].payload["embeds"][0]["image"]["url"] == "attachment://card.png"


async def test_form_factory_is_called_once_per_attempt(fake, session):
    state, base = fake
    state.rate_limit_next = 2
    built: list[int] = []

    def form() -> aiohttp.FormData:
        built.append(len(built))
        fd = aiohttp.FormData()
        fd.add_field("payload_json", "{}", content_type="application/json")
        fd.add_field("files[0]", PNG, filename="card.png", content_type="image/png")
        return fd

    rest = make_rest(session, base)
    await rest.request("POST", "/channels/3001/messages", form=form)
    assert built == [0, 1, 2]
    assert [r.status for r in state.requests_to(MESSAGES)] == [429, 429, 200]
    assert state.requests_to(MESSAGES)[-1].files == [("card.png", PNG)]


async def test_429_after_max_retries_raises_fluxer_http_error(fake, session):
    state, base = fake
    state.rate_limit_next = 5
    sleeps: list[float] = []
    with pytest.raises(FluxerHTTPError) as caught:
        await make_rest(session, base, sleeps=sleeps, max_retries=1).send_message(
            "3001", embeds=[{"description": "hi"}]
        )
    assert caught.value.status == 429
    assert caught.value.code == "RATE_LIMITED"
    assert sleeps == [0.25]
    assert len(state.requests_to(MESSAGES)) == 2


async def test_429_without_json_body_falls_back_to_retry_after_header(fake, session):
    state, base = fake
    state.overrides[f"POST {MESSAGES}"] = Override(
        429, b"", content_type="text/plain", headers={"Retry-After": "2"}
    )
    sleeps: list[float] = []
    with pytest.raises(FluxerHTTPError) as caught:
        await make_rest(session, base, sleeps=sleeps, max_retries=1).send_message(
            "3001", embeds=[{"description": "hi"}]
        )
    assert caught.value.status == 429
    assert sleeps == [2.0]


@pytest.mark.parametrize(
    ("body", "content_type", "headers", "expected"),
    [
        (b'{"code":"RATE_LIMITED","retry_after":NaN}', "application/json", {}, 1.0),
        (b'{"code":"RATE_LIMITED","retry_after":Infinity}', "application/json", {}, 1.0),
        (b'{"code":"RATE_LIMITED","retry_after":1e9}', "application/json", {}, 60.0),
        (b'{"code":"RATE_LIMITED","retry_after":-3}', "application/json", {}, 0.001),
        (b"", "text/plain", {"Retry-After": "-5"}, 0.001),
        (b"", "text/plain", {"Retry-After": "nan"}, 1.0),
        (b"", "text/plain", {"Retry-After": "inf"}, 1.0),
        (b"", "text/plain", {"Retry-After": "1e9"}, 60.0),
        (b"", "text/plain", {}, 1.0),
    ],
)
async def test_429_retry_delay_is_finite_and_clamped(fake, session, body, content_type, headers, expected):
    state, base = fake
    state.overrides[f"POST {MESSAGES}"] = Override(429, body, content_type=content_type, headers=headers)
    sleeps: list[float] = []
    with pytest.raises(FluxerHTTPError):
        await make_rest(session, base, sleeps=sleeps, max_retries=1).send_message(
            "3001", embeds=[{"description": "hi"}]
        )
    assert sleeps == [expected]


# Other errors


async def test_403_raises_with_status_and_code(fake, session):
    state, base = fake
    state.overrides["GET /api/v1/guilds/1001/roles"] = Override(
        403, b'{"code":"MISSING_PERMISSIONS","message":"Missing permissions"}'
    )
    with pytest.raises(FluxerHTTPError) as caught:
        await make_rest(session, base).get_roles("1001")
    assert caught.value.status == 403
    assert caught.value.code == "MISSING_PERMISSIONS"
    assert caught.value.message == "Missing permissions"


async def test_500_with_empty_body_raises_with_no_code(fake, session):
    state, base = fake
    state.overrides["GET /api/v1/guilds/1001/roles"] = Override(500, b"", content_type="text/plain")
    with pytest.raises(FluxerHTTPError) as caught:
        await make_rest(session, base).get_roles("1001")
    assert caught.value.status == 500
    assert caught.value.code is None


async def test_500_with_json_list_body_raises_with_no_code(fake, session):
    state, base = fake
    state.overrides["GET /api/v1/guilds/1001/roles"] = Override(500, b"[1, 2]")
    with pytest.raises(FluxerHTTPError) as caught:
        await make_rest(session, base).get_roles("1001")
    assert caught.value.status == 500
    assert caught.value.code is None


# Roles and members


async def test_get_roles_and_get_member_return_parsed_json(fake, session):
    state, base = fake
    roles = [{"id": "r1", "name": "GM"}, {"id": "r2", "name": "Player"}]
    member = {"roles": ["r1"], "nick": "Gemma", "user": {"id": "2001"}}
    state.roles["1001"] = roles
    state.members[("1001", "2001")] = member
    rest = make_rest(session, base)
    assert await rest.get_roles("1001") == roles
    assert await rest.get_member("1001", "2001") == member
    assert [r.path for r in state.requests] == [
        "/api/v1/guilds/1001/roles",
        "/api/v1/guilds/1001/members/2001",
    ]


async def test_rest_rejects_malformed_ids_without_making_a_request(fake, session):
    state, base = fake
    rest = make_rest(session, base)

    with pytest.raises(ValueError):
        await rest.send_message("../x", embeds=[{"description": "hi"}])
    with pytest.raises(ValueError):
        await rest.send_message("3001", embeds=[{"description": "hi"}], reply_to="1/../2")
    with pytest.raises(ValueError):
        await rest.get_roles("1001?x")
    with pytest.raises(ValueError):
        await rest.get_member("1001", "#2001")

    assert state.requests == []
