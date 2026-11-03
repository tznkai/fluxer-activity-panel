# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""REST extensions: edit_message, reactions, member roles and open_dm against the fake server."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import aiohttp
import pytest

from fluxer_client.rest import FluxerHTTPError, FluxerREST
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "test.token"
BAD_IDS = ["1\n", " 1", "1 ", "١", "", "abc"]  # trailing newline, spaces, non-ASCII digit, empty, letters
THUMB = "%F0%9F%91%8D"


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


def make_rest(session, base, *, sleeps: list[float] | None = None) -> FluxerREST:
    async def record_sleep(delay: float) -> None:
        if sleeps is not None:
            sleeps.append(delay)

    return FluxerREST(session, f"{base}/api", TOKEN, sleep=record_sleep)


# AC1.2: edit_message


async def test_edit_message_content_only_sends_only_content(fake, session):
    state, base = fake
    await make_rest(session, base).edit_message("1", "2", content="x")
    (rec,) = state.requests
    assert rec.method == "PATCH"
    assert rec.payload == {"content": "x"}


async def test_edit_message_embeds_only_sends_only_embeds(fake, session):
    state, base = fake
    embeds = [{"description": "d"}]
    await make_rest(session, base).edit_message("1", "2", embeds=embeds)
    assert state.requests[0].payload == {"embeds": embeds}


async def test_edit_message_both_fields_send_both(fake, session):
    state, base = fake
    await make_rest(session, base).edit_message("1", "2", content="x", embeds=[])
    assert state.requests[0].payload == {"content": "x", "embeds": []}


async def test_edit_message_content_none_is_sent_as_null(fake, session):
    state, base = fake
    await make_rest(session, base).edit_message("1", "2", content=None)
    assert state.requests[0].payload == {"content": None}


async def test_edit_message_with_neither_argument_raises_without_request(fake, session):
    state, base = fake
    with pytest.raises(ValueError):
        await make_rest(session, base).edit_message("1", "2")
    assert state.requests == []


async def test_edit_message_returns_the_response_body(fake, session):
    _, base = fake
    result = await make_rest(session, base).edit_message("1", "2", content="x")
    assert result == {"id": "2", "content": "x"}


# AC1.3: reactions


async def test_add_reaction_puts_percent_encoded_unicode_to_me(fake, session):
    state, base = fake
    result = await make_rest(session, base).add_reaction("1", "2", "👍")
    (rec,) = state.requests
    assert rec.method == "PUT"
    assert rec.raw_path.endswith(f"/reactions/{THUMB}/@me")
    assert result is None


async def test_remove_reaction_deletes_custom_emoji_for_user(fake, session):
    state, base = fake
    result = await make_rest(session, base).remove_reaction("1", "2", "name:42", "3001")
    (rec,) = state.requests
    assert rec.method == "DELETE"
    assert rec.path.endswith("/reactions/name:42/3001")
    assert result is None


async def test_add_custom_emoji_reaction_passes_through(fake, session):
    state, base = fake
    await make_rest(session, base).add_reaction("1", "2", "party_parrot:123456")
    assert state.requests[0].path.endswith("/reactions/party_parrot:123456/@me")


# AC1.4: bad emoji never reaches the network


@pytest.mark.parametrize("emoji", ["", "a/b", "a?b", "bad:"])
async def test_bad_emoji_raises_before_any_request(fake, session, emoji):
    state, base = fake
    rest = make_rest(session, base)
    with pytest.raises(ValueError):
        await rest.add_reaction("1", "2", emoji)
    with pytest.raises(ValueError):
        await rest.remove_reaction("1", "2", emoji, "3")
    assert state.requests == []


# AC1.5: member roles and audit reasons


async def test_add_member_role_sends_audit_header(fake, session):
    state, base = fake
    await make_rest(session, base).add_member_role("1", "2", "3", reason="panel follow")
    (rec,) = state.requests
    assert rec.method == "PUT"
    assert rec.path == "/api/v1/guilds/1/members/2/roles/3"
    assert rec.headers["x-audit-log-reason"] == "panel follow"
    assert rec.authorization == f"Bot {TOKEN}"


async def test_add_member_role_without_reason_has_no_audit_header(fake, session):
    state, base = fake
    await make_rest(session, base).add_member_role("1", "2", "3")
    assert "X-Audit-Log-Reason" not in state.requests[0].headers


async def test_remove_member_role_sends_delete_with_reason(fake, session):
    state, base = fake
    await make_rest(session, base).remove_member_role("1", "2", "3", reason="panel unfollow")
    (rec,) = state.requests
    assert rec.method == "DELETE"
    assert rec.path == "/api/v1/guilds/1/members/2/roles/3"
    assert rec.headers["x-audit-log-reason"] == "panel unfollow"


@pytest.mark.parametrize("reason", ["é", "x" * 129])
async def test_bad_reason_raises_before_any_request(fake, session, reason):
    state, base = fake
    rest = make_rest(session, base)
    with pytest.raises(ValueError):
        await rest.add_member_role("1", "2", "3", reason=reason)
    with pytest.raises(ValueError):
        await rest.remove_member_role("1", "2", "3", reason=reason)
    assert state.requests == []


# AC1.6: open_dm


async def test_open_dm_posts_recipient_and_returns_channel_id(fake, session):
    state, base = fake
    channel = await make_rest(session, base).open_dm("1001")
    (rec,) = state.requests
    assert rec.method == "POST"
    assert rec.path == "/api/v1/users/@me/channels"
    assert rec.payload == {"recipient_id": "1001"}
    assert channel == "9000"


async def test_open_dm_is_stable_for_the_same_recipient(fake, session):
    _, base = fake
    rest = make_rest(session, base)
    assert await rest.open_dm("1001") == await rest.open_dm("1001")


async def test_open_dm_403_surfaces_status_and_code(fake, session):
    state, base = fake
    state.overrides["POST /api/v1/users/@me/channels"] = Override(
        403, b'{"code":"MISSING_PERMISSIONS","message":"x"}'
    )
    with pytest.raises(FluxerHTTPError) as info:
        await make_rest(session, base).open_dm("1001")
    assert info.value.status == 403
    assert info.value.code == "MISSING_PERMISSIONS"


async def test_open_dm_404_surfaces_status_and_code(fake, session):
    state, base = fake
    state.overrides["POST /api/v1/users/@me/channels"] = Override(
        404, b'{"code":"UNKNOWN_USER","message":"x"}'
    )
    with pytest.raises(FluxerHTTPError) as info:
        await make_rest(session, base).open_dm("1001")
    assert info.value.status == 404
    assert info.value.code == "UNKNOWN_USER"


# A 200 whose body has no usable channel id must raise, not return a bad id.
# The two messages differ: "no string id" is open_dm's own guard, "not a snowflake id"
# is require_snowflake's. Matching both means deleting either guard changes the outcome.
@pytest.mark.parametrize(
    "body, message",
    [
        (b"{}", "no string id"),
        (b'{"id": 7}', "no string id"),
        (b"[]", "no string id"),
        (b'{"id": "abc"}', "not a snowflake id"),
    ],
    ids=["missing-id", "non-string-id", "non-dict-body", "non-snowflake-id"],
)
async def test_open_dm_malformed_response_raises(fake, session, body, message):
    state, base = fake
    state.overrides["POST /api/v1/users/@me/channels"] = Override(200, body)
    with pytest.raises(ValueError, match=message):
        await make_rest(session, base).open_dm("1001")


# AC1.7: every id argument of every new method is validated before any request

CALLS: list[tuple[str, Callable[[FluxerREST, str], Awaitable[object]]]] = [
    ("edit_message channel", lambda r, v: r.edit_message(v, "2", content="x")),
    ("edit_message message", lambda r, v: r.edit_message("1", v, content="x")),
    ("add_reaction channel", lambda r, v: r.add_reaction(v, "2", "👍")),
    ("add_reaction message", lambda r, v: r.add_reaction("1", v, "👍")),
    ("remove_reaction channel", lambda r, v: r.remove_reaction(v, "2", "👍", "3")),
    ("remove_reaction message", lambda r, v: r.remove_reaction("1", v, "👍", "3")),
    ("remove_reaction user", lambda r, v: r.remove_reaction("1", "2", "👍", v)),
    ("add_member_role guild", lambda r, v: r.add_member_role(v, "2", "3")),
    ("add_member_role user", lambda r, v: r.add_member_role("1", v, "3")),
    ("add_member_role role", lambda r, v: r.add_member_role("1", "2", v)),
    ("remove_member_role guild", lambda r, v: r.remove_member_role(v, "2", "3")),
    ("remove_member_role user", lambda r, v: r.remove_member_role("1", v, "3")),
    ("remove_member_role role", lambda r, v: r.remove_member_role("1", "2", v)),
    ("open_dm user", lambda r, v: r.open_dm(v)),
    ("get_channel channel", lambda r, v: r.get_channel(v)),
    ("get_guild guild", lambda r, v: r.get_guild(v)),
    ("get_message channel", lambda r, v: r.get_message(v, "2")),
    ("get_message message", lambda r, v: r.get_message("1", v)),
    ("delete_message channel", lambda r, v: r.delete_message(v, "2")),
    ("delete_message message", lambda r, v: r.delete_message("1", v)),
]


@pytest.mark.parametrize("bad", BAD_IDS, ids=repr)
@pytest.mark.parametrize("label, call", CALLS, ids=[label for label, _ in CALLS])
async def test_bad_snowflake_raises_before_any_request(fake, session, label, call, bad):
    state, base = fake
    with pytest.raises(ValueError):
        await call(make_rest(session, base), bad)
    assert state.requests == []


# Phase 2 (AC5.1): allowed_mentions on send and edit. Every card send and edit carries an
# empty parse list, so Phase 6 can rely on it. It is never counted as content.

ALLOWED_NONE = {"parse": []}


async def test_send_message_embeds_with_allowed_mentions_sends_both_and_no_content(fake, session):
    state, base = fake
    embeds = [{"description": "d"}]
    await make_rest(session, base).send_message("1", embeds=embeds, allowed_mentions=ALLOWED_NONE)
    assert state.requests[0].payload == {"embeds": embeds, "allowed_mentions": {"parse": []}}


async def test_send_message_without_allowed_mentions_has_no_such_key(fake, session):
    state, base = fake
    await make_rest(session, base).send_message("1", content="x")
    assert state.requests[0].payload == {"content": "x"}


async def test_send_message_allowed_mentions_alone_raises_without_request(fake, session):
    state, base = fake
    with pytest.raises(ValueError):
        await make_rest(session, base).send_message("1", allowed_mentions=ALLOWED_NONE)
    assert state.requests == []


async def test_send_message_multipart_carries_allowed_mentions_in_payload_json(fake, session):
    state, base = fake
    await make_rest(session, base).send_message(
        "1", content="x", allowed_mentions=ALLOWED_NONE, file=("card.png", b"\x89PNG", "image/png"),
    )
    assert state.requests[0].payload == {"content": "x", "allowed_mentions": {"parse": []}}


async def test_edit_message_embeds_with_allowed_mentions_has_no_content_key(fake, session):
    state, base = fake
    embeds = [{"description": "d"}]
    await make_rest(session, base).edit_message("1", "2", embeds=embeds, allowed_mentions=ALLOWED_NONE)
    payload = state.requests[0].payload
    assert payload == {"embeds": embeds, "allowed_mentions": {"parse": []}}
    assert "content" not in payload


async def test_edit_message_allowed_mentions_alone_raises_without_request(fake, session):
    state, base = fake
    with pytest.raises(ValueError):
        await make_rest(session, base).edit_message("1", "2", allowed_mentions=ALLOWED_NONE)
    assert state.requests == []


# Phase 2: channel, guild and message reads and message delete


async def test_get_channel_and_get_guild_return_seeded_objects(fake, session):
    state, base = fake
    state.channels["3001"] = {"id": "3001", "name": "games"}
    state.guilds["2001"] = {"id": "2001", "name": "server"}
    rest = make_rest(session, base)
    assert await rest.get_channel("3001") == {"id": "3001", "name": "games"}
    assert await rest.get_guild("2001") == {"id": "2001", "name": "server"}


async def test_get_channel_and_get_guild_unknown_ids_raise_404(fake, session):
    _, base = fake
    rest = make_rest(session, base)
    with pytest.raises(FluxerHTTPError) as channel:
        await rest.get_channel("3001")
    with pytest.raises(FluxerHTTPError) as guild:
        await rest.get_guild("2001")
    assert channel.value.status == 404
    assert guild.value.status == 404


async def test_get_guild_403_and_500_surface_their_status(fake, session):
    state, base = fake
    state.guilds["2001"] = {"id": "2001"}
    rest = make_rest(session, base)
    state.overrides["GET /api/v1/guilds/2001"] = Override(403, b'{"code":"MISSING_ACCESS","message":"x"}')
    with pytest.raises(FluxerHTTPError) as forbidden:
        await rest.get_guild("2001")
    assert forbidden.value.status == 403
    state.overrides["GET /api/v1/guilds/2001"] = Override(500, b'{"code":"INTERNAL","message":"x"}')
    with pytest.raises(FluxerHTTPError) as broken:
        await rest.get_guild("2001")
    assert broken.value.status == 500


@pytest.mark.parametrize("path, call", [
    ("GET /api/v1/channels/3001", lambda r: r.get_channel("3001")),
    ("GET /api/v1/guilds/2001", lambda r: r.get_guild("2001")),
])
async def test_get_channel_and_guild_non_object_body_raises_shape_error(fake, session, path, call):
    state, base = fake
    state.overrides[path] = Override(200, b"[]")
    with pytest.raises(FluxerHTTPError, match="unexpected response shape") as info:
        await call(make_rest(session, base))
    assert info.value.status == 200


async def test_get_message_returns_a_sent_message(fake, session):
    state, base = fake
    rest = make_rest(session, base)
    sent = await rest.send_message("1", content="x")
    assert sent["id"].isdigit()
    got = await rest.get_message("1", sent["id"])
    assert got == {"id": sent["id"], "channel_id": "1", "content": "x"}


async def test_get_message_after_delete_is_404(fake, session):
    _, base = fake
    rest = make_rest(session, base)
    mid = (await rest.send_message("1", content="x"))["id"]
    await rest.delete_message("1", mid)
    with pytest.raises(FluxerHTTPError) as info:
        await rest.get_message("1", mid)
    assert info.value.status == 404


async def test_delete_message_twice_second_is_404(fake, session):
    state, base = fake
    rest = make_rest(session, base)
    mid = (await rest.send_message("1", content="x"))["id"]
    assert await rest.delete_message("1", mid) is None
    with pytest.raises(FluxerHTTPError) as info:
        await rest.delete_message("1", mid)
    assert info.value.status == 404
    assert [r.status for r in state.requests if r.method == "DELETE"] == [204, 404]


async def test_delete_message_never_sent_is_404(fake, session):
    _, base = fake
    with pytest.raises(FluxerHTTPError) as info:
        await make_rest(session, base).delete_message("1", "9999")
    assert info.value.status == 404


# Retry: 429 on an encoded reaction path is retried and the encoding survives the retry


async def test_add_reaction_retries_429_with_same_encoding(fake, session):
    state, base = fake
    state.rate_limit["PUT /api/v1/channels/1/messages/2/reactions/👍/@me"] = 1
    sleeps: list[float] = []
    await make_rest(session, base, sleeps=sleeps).add_reaction("1", "2", "👍")
    assert len(sleeps) == 1
    assert len(state.requests) == 2
    assert [rec.status for rec in state.requests] == [429, 204]
    assert all(THUMB in rec.raw_path for rec in state.requests)
