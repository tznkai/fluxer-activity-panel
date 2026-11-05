# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""REST extensions: edit_message, reactions, member roles and open_dm against the fake server."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable

import aiohttp
import pytest

from fluxer_client.rest import FluxerHTTPError, FluxerREST, ReactionUsersPage
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
    ("create_role guild", lambda r, v: r.create_role(v, name="x", mentionable=False)),
    ("set_role_mentionable guild", lambda r, v: r.set_role_mentionable(v, "2", mentionable=True)),
    ("set_role_mentionable role", lambda r, v: r.set_role_mentionable("1", v, mentionable=True)),
    ("get_messages channel", lambda r, v: r.get_messages(v, limit=1)),
    ("get_reaction_users channel", lambda r, v: r.get_reaction_users(v, "2", "\U0001F64B")),
    ("get_reaction_users message", lambda r, v: r.get_reaction_users("1", v, "\U0001F64B")),
    ("get_reaction_users_page channel", lambda r, v: r.get_reaction_users_page(v, "2", "\U0001F64B")),
    ("get_reaction_users_page message", lambda r, v: r.get_reaction_users_page("1", v, "\U0001F64B")),
    ("remove_emoji_reactions channel", lambda r, v: r.remove_emoji_reactions(v, "2", "\U0001F64B")),
    ("remove_emoji_reactions message", lambda r, v: r.remove_emoji_reactions("1", v, "\U0001F64B")),
    ("remove_all_reactions channel", lambda r, v: r.remove_all_reactions(v, "2")),
    ("remove_all_reactions message", lambda r, v: r.remove_all_reactions("1", v)),
]


@pytest.mark.parametrize("bad", BAD_IDS, ids=repr)
@pytest.mark.parametrize("label, call", CALLS, ids=[label for label, _ in CALLS])
async def test_bad_snowflake_raises_before_any_request(fake, session, label, call, bad):
    state, base = fake
    with pytest.raises(ValueError):
        await call(make_rest(session, base), bad)
    assert state.requests == []


# AC5.1: allowed_mentions on send and edit. Every card send and edit carries an empty parse list.
# It is never counted as content.

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


# Channel, guild and message reads and message delete


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
    ("GET /api/v1/channels/3001/messages/5001", lambda r: r.get_message("3001", "5001")),
])
async def test_get_reads_non_object_body_raises_shape_error(fake, session, path, call):
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


# Injected failures leave stored state unchanged: the fake changes stored messages only when the
# request succeeds, so an override or a rate_limit count never alters what a later read returns.


async def test_delete_with_500_override_keeps_message_readable(fake, session):
    state, base = fake
    rest = make_rest(session, base)
    mid = (await rest.send_message("1", content="x"))["id"]
    state.overrides[f"DELETE /api/v1/channels/1/messages/{mid}"] = Override(
        500, b'{"code":"INTERNAL","message":"x"}'
    )
    with pytest.raises(FluxerHTTPError) as info:
        await rest.delete_message("1", mid)
    assert info.value.status == 500
    assert await rest.get_message("1", mid) == {"id": mid, "channel_id": "1", "content": "x"}


async def test_patch_with_500_override_leaves_stored_message_unchanged(fake, session):
    state, base = fake
    rest = make_rest(session, base)
    mid = (await rest.send_message("1", content="original"))["id"]
    state.overrides[f"PATCH /api/v1/channels/1/messages/{mid}"] = Override(
        500, b'{"code":"INTERNAL","message":"x"}'
    )
    with pytest.raises(FluxerHTTPError) as info:
        await rest.edit_message("1", mid, content="changed")
    assert info.value.status == 500
    assert await rest.get_message("1", mid) == {"id": mid, "channel_id": "1", "content": "original"}


async def test_post_with_500_override_stores_nothing_and_keeps_the_id(fake, session):
    state, base = fake
    rest = make_rest(session, base)
    state.overrides["POST /api/v1/channels/1/messages"] = Override(500, b'{"code":"INTERNAL","message":"x"}')
    with pytest.raises(FluxerHTTPError) as info:
        await rest.send_message("1", content="x")
    assert info.value.status == 500
    assert state.messages == {}
    del state.overrides["POST /api/v1/channels/1/messages"]
    sent = await rest.send_message("1", content="y")
    assert sent["id"] == "5001"
    assert state.messages == {("1", "5001"): {"content": "y"}}


async def test_rate_limited_post_then_retry_stores_exactly_one_message(fake, session):
    state, base = fake
    state.rate_limit["POST /api/v1/channels/1/messages"] = 1
    sleeps: list[float] = []
    sent = await make_rest(session, base, sleeps=sleeps).send_message("1", content="x")
    assert [rec.status for rec in state.requests] == [429, 200]
    assert len(sleeps) == 1
    assert state.messages == {("1", sent["id"]): {"content": "x"}}
    assert state.next_message_id == 5002


# PATCH to an unknown id echoes without creating it.


async def test_patch_to_never_created_message_echoes_without_creating_it(fake, session):
    state, base = fake
    rest = make_rest(session, base)
    assert await rest.edit_message("1", "9999", content="x") == {"id": "9999", "content": "x"}
    assert state.messages == {}
    with pytest.raises(FluxerHTTPError) as got:
        await rest.get_message("1", "9999")
    assert got.value.status == 404
    with pytest.raises(FluxerHTTPError) as gone:
        await rest.delete_message("1", "9999")
    assert gone.value.status == 404


# PATCH after DELETE and slow responses


async def test_edit_after_delete_raises_404_unknown_message(fake, session):
    _, base = fake
    rest = make_rest(session, base)
    mid = (await rest.send_message("1", content="x"))["id"]
    await rest.delete_message("1", mid)
    with pytest.raises(FluxerHTTPError) as info:
        await rest.edit_message("1", mid, content="y")
    assert info.value.status == 404
    assert info.value.code == "UNKNOWN_MESSAGE"


async def test_delay_entry_makes_call_time_out(fake, session):
    state, base = fake
    state.delays["PATCH /api/v1/channels/1/messages/2"] = 0.5
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.02):
            await make_rest(session, base).edit_message("1", "2", content="x")


# Dropped connections: a real client error from the transport, with no state change on the server

async def test_dropped_connections_raise_client_errors_and_change_no_state(fake, session):
    state, base = fake
    state.guilds["1001"] = {"id": "1001"}
    state.drops.update({"GET /api/v1/guilds/1001", "POST /api/v1/channels/1/messages"})
    rest = make_rest(session, base)

    with pytest.raises(aiohttp.ClientError):
        await rest.get_guild("1001")
    with pytest.raises(aiohttp.ClientError):
        await rest.send_message("1", content="x")

    assert state.messages == {}
    assert state.next_message_id == 5001
    # Every attempt got no response. aiohttp resends the idempotent GET once on a dropped connection, while the
    # POST is sent once: the non-idempotent request is never repeated by the client.
    attempts = [(r.method, r.path) for r in state.requests]
    assert {r.status for r in state.requests} == {0}
    assert set(attempts) == {("GET", "/api/v1/guilds/1001"), ("POST", "/api/v1/channels/1/messages")}
    assert attempts.count(("POST", "/api/v1/channels/1/messages")) == 1


# Live-capability groundwork: role creation, channel message lists, reactor lists and bulk reaction removal

PLAY_EMOJI = "\u25b6\ufe0f"
RAISED_HAND = "\U0001F64B"


async def test_create_role_sends_fixed_permissions_and_returns_the_role(fake, session):
    state, base = fake
    role = await make_rest(session, base).create_role("1", name="live_ping_test", mentionable=True)
    (rec,) = state.requests
    assert rec.method == "POST"
    assert rec.path == "/api/v1/guilds/1/roles"
    assert rec.payload == {
        "name": "live_ping_test", "permissions": "0", "color": 0, "hoist": False, "mentionable": True,
    }
    assert role["id"] == "7001"
    assert role["name"] == "live_ping_test"
    assert role["permissions"] == "0"
    # Fluxer ignores mentionable on create (the live run confirmed it); only set_role_mentionable sets it.
    assert role["mentionable"] is False


async def test_create_role_has_no_permissions_parameter():
    assert "permissions" not in inspect.signature(FluxerREST.create_role).parameters


async def test_create_role_mirrors_fluxer_and_ignores_mentionable_and_hoist(fake, session):
    state, base = fake
    role = await make_rest(session, base).create_role("1", name="x", mentionable=True)
    assert role["mentionable"] is False
    assert role["hoist"] is False
    assert role["position"] == 1
    assert state.roles["1"] == [role]


async def test_set_role_mentionable_patches_only_the_flag(fake, session):
    state, base = fake
    rest = make_rest(session, base)
    role = await rest.create_role("1", name="live_ping_test", mentionable=False)
    updated = await rest.set_role_mentionable("1", role["id"], mentionable=True)
    rec = state.requests[-1]
    assert rec.method == "PATCH"
    assert rec.path == f"/api/v1/guilds/1/roles/{role['id']}"
    assert rec.payload == {"mentionable": True}
    assert updated["mentionable"] is True
    assert state.roles["1"][0]["mentionable"] is True


async def test_set_role_mentionable_false_sends_false(fake, session):
    state, base = fake
    rest = make_rest(session, base)
    role = await rest.create_role("1", name="x", mentionable=False)
    await rest.set_role_mentionable("1", role["id"], mentionable=True)
    await rest.set_role_mentionable("1", role["id"], mentionable=False)
    assert state.requests[-1].payload == {"mentionable": False}
    assert state.roles["1"][0]["mentionable"] is False


async def test_set_role_mentionable_sends_audit_reason_header(fake, session):
    state, base = fake
    rest = make_rest(session, base)
    role = await rest.create_role("1", name="x", mentionable=False)
    await rest.set_role_mentionable("1", role["id"], mentionable=True, reason="fluxer panel live test")
    assert state.requests[-1].headers["x-audit-log-reason"] == "fluxer panel live test"


async def test_set_role_mentionable_unknown_role_is_404(fake, session):
    _, base = fake
    with pytest.raises(FluxerHTTPError) as info:
        await make_rest(session, base).set_role_mentionable("1", "9999", mentionable=True)
    assert info.value.status == 404


async def test_set_role_mentionable_has_only_the_flag_and_audit_parameters():
    params = list(inspect.signature(FluxerREST.set_role_mentionable).parameters)
    assert params == ["self", "guild_id", "role_id", "mentionable", "reason"]


async def test_create_role_sends_audit_reason_header(fake, session):
    state, base = fake
    await make_rest(session, base).create_role("1", name="x", mentionable=False, reason="fluxer panel live test")
    assert state.requests[0].headers["x-audit-log-reason"] == "fluxer panel live test"


@pytest.mark.parametrize("name", ["", "x" * 101, "bad\tname"], ids=["blank", "too-long", "non-printable"])
async def test_create_role_bad_name_raises_before_any_request(fake, session, name):
    state, base = fake
    with pytest.raises(ValueError):
        await make_rest(session, base).create_role("1", name=name, mentionable=False)
    assert state.requests == []


async def test_get_messages_limit_one_returns_only_the_newest_surviving_message(fake, session):
    state, base = fake
    rest = make_rest(session, base)
    older = (await rest.send_message("1", content="older"))["id"]
    newer = (await rest.send_message("1", content="newer"))["id"]
    await rest.delete_message("1", newer)
    newest = await rest.get_messages("1", limit=1)
    assert [m["id"] for m in newest] == [older]
    assert newest[0]["content"] == "older"
    assert state.requests[-1].method == "GET"


async def test_get_messages_is_newest_first_across_the_limit(fake, session):
    _, base = fake
    rest = make_rest(session, base)
    ids = [(await rest.send_message("1", content=str(n)))["id"] for n in range(3)]
    assert [m["id"] for m in await rest.get_messages("1", limit=2)] == [ids[2], ids[1]]


@pytest.mark.parametrize("limit", [0, 101])
async def test_get_messages_limit_out_of_range_raises_without_request(fake, session, limit):
    state, base = fake
    with pytest.raises(ValueError):
        await make_rest(session, base).get_messages("1", limit=limit)
    assert state.requests == []


async def test_get_reaction_users_hits_users_path_and_returns_id_order(fake, session):
    state, base = fake
    state.reactions[("1", "2")] = {RAISED_HAND: ["1003", "1001", "1002"]}
    users = await make_rest(session, base).get_reaction_users("1", "2", RAISED_HAND)
    (rec,) = state.requests
    assert rec.path == "/api/v1/channels/1/messages/2/reactions/" + RAISED_HAND + "/users"
    assert [u["id"] for u in users] == ["1001", "1002", "1003"]


async def test_get_reaction_users_flags_the_bot(fake, session):
    state, base = fake
    await make_rest(session, base).add_reaction("1", "2", RAISED_HAND)
    state.reactions[("1", "2")][RAISED_HAND].insert(0, "1001")
    users = await make_rest(session, base).get_reaction_users("1", "2", RAISED_HAND)
    assert users == [{"id": "1001", "bot": False}, {"id": state.bot_user_id, "bot": True}]


async def test_get_reaction_users_respects_limit_and_after(fake, session):
    state, base = fake
    state.reactions[("1", "2")] = {RAISED_HAND: ["1001", "1002", "1003", "1004"]}
    rest = make_rest(session, base)
    assert [u["id"] for u in await rest.get_reaction_users("1", "2", RAISED_HAND, limit=2)] == ["1001", "1002"]
    assert [u["id"] for u in await rest.get_reaction_users("1", "2", RAISED_HAND, after="1002")] == ["1003", "1004"]
    page = await rest.get_reaction_users("1", "2", RAISED_HAND, limit=1, after="1001")
    assert [u["id"] for u in page] == ["1002"]


@pytest.mark.parametrize("limit", [0, 101])
async def test_get_reaction_users_limit_out_of_range_raises_without_request(fake, session, limit):
    state, base = fake
    with pytest.raises(ValueError):
        await make_rest(session, base).get_reaction_users("1", "2", RAISED_HAND, limit=limit)
    assert state.requests == []


async def test_get_reaction_users_unwraps_a_paged_object(fake, session):
    state, base = fake
    state.overrides[f"GET /api/v1/channels/1/messages/2/reactions/{RAISED_HAND}/users"] = Override(
        200, b'{"items": [{"id": "1001", "bot": false}], "has_more": false, "next_after": null}'
    )
    users = await make_rest(session, base).get_reaction_users("1", "2", RAISED_HAND)
    assert users == [{"id": "1001", "bot": False}]


async def test_get_reaction_users_keeps_the_plain_list_case(fake, session):
    state, base = fake
    state.overrides[f"GET /api/v1/channels/1/messages/2/reactions/{RAISED_HAND}/users"] = Override(
        200, b'[{"id": "1001", "bot": false}]'
    )
    users = await make_rest(session, base).get_reaction_users("1", "2", RAISED_HAND)
    assert users == [{"id": "1001", "bot": False}]


async def test_get_reaction_users_has_more_and_next_after_page_through_the_list(fake, session):
    state, base = fake
    state.reactions[("1", "2")] = {RAISED_HAND: ["1001", "1002", "1003"]}
    rest = make_rest(session, base)
    first = await rest.get_reaction_users("1", "2", RAISED_HAND, limit=2)
    assert [u["id"] for u in first] == ["1001", "1002"]
    raw = await rest.request("GET", f"/channels/1/messages/2/reactions/{RAISED_HAND}/users?limit=2")
    assert raw["has_more"] is True
    assert raw["next_after"] == "1002"
    second = await rest.get_reaction_users("1", "2", RAISED_HAND, limit=2, after=raw["next_after"])
    assert [u["id"] for u in second] == ["1003"]
    last = await rest.request("GET", f"/channels/1/messages/2/reactions/{RAISED_HAND}/users?limit=2&after=1003")
    assert last == {"items": [], "has_more": False, "next_after": None}


async def test_get_reaction_users_page_returns_items_has_more_and_next_after(fake, session):
    state, base = fake
    state.overrides[f"GET /api/v1/channels/1/messages/2/reactions/{RAISED_HAND}/users"] = Override(
        200, b'{"items": [{"id": "1001", "bot": false}, {"id": "1002", "bot": true}],'
             b' "has_more": true, "next_after": "1002"}'
    )
    page = await make_rest(session, base).get_reaction_users_page("1", "2", RAISED_HAND)
    assert page == ReactionUsersPage(
        users=[{"id": "1001", "bot": False}, {"id": "1002", "bot": True}], has_more=True, next_after="1002")


async def test_get_reaction_users_page_treats_a_plain_list_as_the_last_page(fake, session):
    state, base = fake
    state.overrides[f"GET /api/v1/channels/1/messages/2/reactions/{RAISED_HAND}/users"] = Override(
        200, b'[{"id": "1001", "bot": false}]'
    )
    page = await make_rest(session, base).get_reaction_users_page("1", "2", RAISED_HAND)
    assert page == ReactionUsersPage(users=[{"id": "1001", "bot": False}], has_more=False, next_after=None)


async def test_get_reaction_users_page_walks_the_pages_with_next_after(fake, session):
    state, base = fake
    state.reactions[("1", "2")] = {RAISED_HAND: ["1001", "1002", "1003"]}
    rest = make_rest(session, base)
    first = await rest.get_reaction_users_page("1", "2", RAISED_HAND, limit=2)
    assert [u["id"] for u in first.users] == ["1001", "1002"]
    assert first.has_more is True
    second = await rest.get_reaction_users_page("1", "2", RAISED_HAND, limit=2, after=first.next_after)
    assert [u["id"] for u in second.users] == ["1003"]
    assert second.has_more is False
    assert second.next_after is None


async def test_get_reaction_users_page_ignores_a_non_string_next_after(fake, session):
    state, base = fake
    state.overrides[f"GET /api/v1/channels/1/messages/2/reactions/{RAISED_HAND}/users"] = Override(
        200, b'{"items": [], "has_more": true, "next_after": 1002}'
    )
    page = await make_rest(session, base).get_reaction_users_page("1", "2", RAISED_HAND)
    assert page == ReactionUsersPage(users=[], has_more=True, next_after=None)


async def test_get_reaction_users_encodes_play_emoji_with_variation_selector(fake, session):
    state, base = fake
    await make_rest(session, base).get_reaction_users("1", "2", PLAY_EMOJI)
    assert "/reactions/%E2%96%B6%EF%B8%8F/users" in state.requests[0].raw_path


async def test_remove_emoji_reactions_clears_only_that_emoji(fake, session):
    _, base = fake
    rest = make_rest(session, base)
    mid = (await rest.send_message("1", content="x"))["id"]
    await rest.add_reaction("1", mid, PLAY_EMOJI)
    await rest.add_reaction("1", mid, RAISED_HAND)
    assert await rest.remove_emoji_reactions("1", mid, PLAY_EMOJI) is None
    left = (await rest.get_message("1", mid))["reactions"]
    assert [r["emoji"]["name"] for r in left] == [RAISED_HAND]


async def test_remove_all_reactions_clears_every_emoji(fake, session):
    _, base = fake
    rest = make_rest(session, base)
    mid = (await rest.send_message("1", content="x"))["id"]
    await rest.add_reaction("1", mid, PLAY_EMOJI)
    await rest.add_reaction("1", mid, RAISED_HAND)
    assert await rest.remove_all_reactions("1", mid) is None
    assert "reactions" not in await rest.get_message("1", mid)


async def test_bulk_removal_leaves_other_messages_alone(fake, session):
    _, base = fake
    rest = make_rest(session, base)
    first = (await rest.send_message("1", content="a"))["id"]
    second = (await rest.send_message("1", content="b"))["id"]
    await rest.add_reaction("1", first, RAISED_HAND)
    await rest.add_reaction("1", second, RAISED_HAND)
    await rest.remove_all_reactions("1", first)
    assert "reactions" not in await rest.get_message("1", first)
    assert [r["emoji"]["name"] for r in (await rest.get_message("1", second))["reactions"]] == [RAISED_HAND]


@pytest.mark.parametrize("method, path, call", [
    ("POST", "/api/v1/guilds/1/roles", lambda r: r.create_role("1", name="x", mentionable=False)),
    ("PATCH", "/api/v1/guilds/1/roles/2", lambda r: r.set_role_mentionable("1", "2", mentionable=True)),
    ("GET", "/api/v1/channels/1/messages", lambda r: r.get_messages("1", limit=1)),
    ("GET", f"/api/v1/channels/1/messages/2/reactions/{RAISED_HAND}/users",
     lambda r: r.get_reaction_users("1", "2", RAISED_HAND)),
    ("DELETE", f"/api/v1/channels/1/messages/2/reactions/{RAISED_HAND}",
     lambda r: r.remove_emoji_reactions("1", "2", RAISED_HAND)),
    ("DELETE", "/api/v1/channels/1/messages/2/reactions", lambda r: r.remove_all_reactions("1", "2")),
], ids=["create_role", "set_role_mentionable", "get_messages", "get_reaction_users",
        "remove_emoji_reactions", "remove_all_reactions"])
async def test_new_calls_surface_a_403(fake, session, method, path, call):
    state, base = fake
    state.overrides[f"{method} {path}"] = Override(403, b'{"code":"MISSING_PERMISSIONS","message":"x"}')
    with pytest.raises(FluxerHTTPError) as info:
        await call(make_rest(session, base))
    assert info.value.status == 403
    assert info.value.code == "MISSING_PERMISSIONS"
