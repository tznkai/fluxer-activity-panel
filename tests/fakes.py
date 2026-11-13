# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fake Fluxer servers built on aiohttp.web, driven through pytest-aiohttp's aiohttp_server.

`FakeFluxerState` is the single place tests configure and inspect the fake. Routes are
added to `make_rest_app(state)`; the gateway fake will extend this module with its own
app factory that shares the same state object.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from aiohttp import web

TIMEOUT_DEFAULT = 2.0
RATE_LIMIT_BODY = {"code": "RATE_LIMITED", "message": "slow", "global": False, "retry_after": 0.25}


@dataclass
class Override:
    """A canned raw response for `"METHOD /path"`, used to exercise error shapes."""

    status: int
    body: bytes
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Recorded:
    method: str
    path: str
    authorization: str | None
    content_type: str
    status: int
    payload: Any = None
    files: list[tuple[str, bytes]] = field(default_factory=list)  # (filename, bytes)
    raw_path: str = ""  # percent-encoded path as sent, so encoding is visible
    headers: Mapping[str, str] = field(default_factory=dict)  # case-insensitive copy of request headers


@dataclass
class FakeFluxerState:
    gateway_url: str = "ws://fake-gateway.test/gateway"
    roles: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    members: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    # GET /channels/{id} and GET /guilds/{id} answer from these (404 when unknown).
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    guilds: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The next N message POSTs answer 429 (RATE_LIMIT_BODY).
    rate_limit_next: int = 0
    # When True, multipart message POSTs (attachments) get 403, as without Attach Files.
    forbid_attachments: bool = False
    # Keyed by "METHOD /path"; checked before any default behaviour.
    overrides: dict[str, Override] = field(default_factory=dict)
    # Keyed by "METHOD /path" (decoded path): how many more 429s the route returns before succeeding.
    rate_limit: dict[str, int] = field(default_factory=dict)
    # recipient user id -> DM channel id, filled by POST /users/@me/channels.
    dm_channels: dict[str, str] = field(default_factory=dict)
    # Snowflake-shaped ids for created messages (the card store CHECKs ids as digits).
    next_message_id: int = 5001
    # (channel_id, message_id) -> last payload, for created messages.
    messages: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    # (channel_id, message_id) of messages the fake has deleted.
    deleted: set[tuple[str, str]] = field(default_factory=set)
    # The id the fake treats as @me (the bot's own user).
    bot_user_id: str = "1900"
    # (channel_id, message_id) -> emoji name -> user ids, in add order.
    reactions: dict[tuple[str, str], dict[str, list[str]]] = field(default_factory=dict)
    # Id given to the next role created by POST /guilds/{gid}/roles.
    next_role_id: int = 7001
    # "METHOD /path" -> seconds to sleep before the handler runs (for timeout tests).
    delays: dict[str, float] = field(default_factory=dict)
    # "METHOD /path" -> an Event the request waits on before the handler runs, so a test can hold requests in flight.
    # `gated` records each request that reached its gate, in arrival order.
    gates: dict[str, asyncio.Event] = field(default_factory=dict)
    gated: list[str] = field(default_factory=list)
    # "METHOD /path" -> how many of the next requests on that route pass their gate without waiting, so a test can let
    # the first request through (a board post, say) and hold the requests after it.
    gate_passes: dict[str, int] = field(default_factory=dict)
    # "METHOD /path" -> the connection is closed before any response, so the client raises an aiohttp
    # ClientError. A dropped request is recorded with status 0 and changes no state (no handler runs).
    drops: set[str] = field(default_factory=set)
    requests: list[Recorded] = field(default_factory=list)
    # Gateway fake: when False the fake never ACKs heartbeats (op 1 gets no op 11).
    ack: bool = True
    # One entry per websocket connection: the client frames it received, in order.
    connections: list[list[dict[str, Any]]] = field(default_factory=list)
    close_codes: list[int | None] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    queries: list[dict[str, str]] = field(default_factory=list)
    # repr() of any exception a scenario script raised (tests fail fast on these).
    errors: list[str] = field(default_factory=list)

    def requests_to(self, path: str) -> list[Recorded]:
        return [r for r in self.requests if r.path == path]


def _send(
    state: FakeFluxerState,
    request: web.Request,
    status: int,
    body: Any,
    *,
    payload: Any = None,
    files: list[tuple[str, bytes]] | None = None,
    commit: Callable[[], None] | None = None,
) -> web.Response:
    """Answer one request. `commit` applies the request's state change and runs only when the request
    succeeds: an override or a rate_limit count answers instead, and the stored state stays as it was."""
    key = f"{request.method} {request.path}"
    override = state.overrides.get(key)
    if override is not None:
        state.requests.append(_record(request, override.status, payload, files))
        return web.Response(
            status=override.status,
            body=override.body,
            content_type=override.content_type,
            headers=override.headers,
        )
    if state.rate_limit.get(key, 0) > 0:
        state.rate_limit[key] -= 1
        state.requests.append(_record(request, 429, payload, files))
        return web.json_response(RATE_LIMIT_BODY, status=429)
    if commit is not None:
        commit()
    state.requests.append(_record(request, status, payload, files))
    if status == 204:
        return web.Response(status=204)
    return web.json_response(body, status=status)


def _record(request: web.Request, status: int, payload: Any, files: list[tuple[str, bytes]] | None) -> Recorded:
    return Recorded(
        method=request.method,
        path=request.path,
        authorization=request.headers.get("Authorization"),
        content_type=request.content_type,
        status=status,
        payload=payload,
        files=files or [],
        raw_path=request.raw_path,
        headers=request.headers.copy(),
    )


def make_rest_app(state: FakeFluxerState) -> web.Application:
    async def well_known(request: web.Request) -> web.Response:
        base = str(request.url.origin())
        body = {
            "api_code_version": 1,
            "endpoints": {"api_public": f"{base}/api", "gateway": "ws://unused"},
        }
        return _send(state, request, 200, body)

    async def gateway_bot(request: web.Request) -> web.Response:
        return _send(state, request, 200, {"url": state.gateway_url, "shards": 1})

    async def create_message(request: web.Request) -> web.Response:
        if request.content_type == "multipart/form-data":
            form = await request.post()
            payload = json.loads(form["payload_json"])
            files = [
                (value.filename, value.file.read())
                for name, value in form.items()
                if name.startswith("files[") and hasattr(value, "filename")
            ]
        else:
            payload = await request.json()
            files = []
        if files and state.forbid_attachments:
            return _send(state, request, 403, {"code": "MISSING_PERMISSIONS", "message": "Missing Permissions"},
                         payload=payload, files=files)
        if state.rate_limit_next > 0:
            state.rate_limit_next -= 1
            return _send(state, request, 429, RATE_LIMIT_BODY, payload=payload, files=files)
        cid = request.match_info["cid"]
        mid = str(state.next_message_id)

        def commit() -> None:
            state.next_message_id += 1
            state.messages[(cid, mid)] = payload

        return _send(state, request, 200, {"id": mid, "channel_id": cid}, payload=payload, files=files,
                     commit=commit)

    async def channel(request: web.Request) -> web.Response:
        cid = request.match_info["cid"]
        if cid not in state.channels:
            return _send(state, request, 404, {"code": "UNKNOWN_CHANNEL", "message": "Unknown channel"})
        return _send(state, request, 200, state.channels[cid])

    async def guild(request: web.Request) -> web.Response:
        gid = request.match_info["gid"]
        if gid not in state.guilds:
            return _send(state, request, 404, {"code": "UNKNOWN_GUILD", "message": "Unknown guild"})
        return _send(state, request, 200, state.guilds[gid])

    async def roles(request: web.Request) -> web.Response:
        guild = request.match_info["gid"]
        if guild not in state.roles:
            return _send(state, request, 404, {"code": "UNKNOWN_GUILD", "message": "Unknown guild"})
        return _send(state, request, 200, state.roles[guild])

    async def member(request: web.Request) -> web.Response:
        key = (request.match_info["gid"], request.match_info["uid"])
        if key not in state.members:
            return _send(state, request, 404, {"code": "UNKNOWN_MEMBER", "message": "Unknown member"})
        return _send(state, request, 200, state.members[key])

    async def members_list(request: web.Request) -> web.Response:
        gid = request.match_info["gid"]
        limit = int(request.query.get("limit", "1"))
        after = request.query.get("after")
        rows = sorted(((uid, m) for (g, uid), m in state.members.items() if g == gid), key=lambda r: int(r[0]))
        if after is not None:
            rows = [r for r in rows if int(r[0]) > int(after)]
        body = [{**m, "user": {**m.get("user", {}), "id": uid}} for uid, m in rows[:limit]]
        return _send(state, request, 200, body)

    def _key(request: web.Request) -> tuple[str, str]:
        return request.match_info["cid"], request.match_info["mid"]

    def _reactors(key: tuple[str, str], emoji: str) -> list[str]:
        return list(state.reactions.get(key, {}).get(emoji, []))

    def _set_reactors(key: tuple[str, str], emoji: str, users: list[str]) -> None:
        message = state.reactions.setdefault(key, {})
        if users:
            message[emoji] = users
        else:
            message.pop(emoji, None)
        if not message:
            state.reactions.pop(key, None)

    async def reaction(request: web.Request) -> web.Response:
        # PUT .../@me adds our reaction; DELETE .../{user} removes a reaction (204 either way)
        key = _key(request)
        emoji = request.match_info["emoji"]
        user = request.match_info["user"]
        uid = state.bot_user_id if user == "@me" else user

        def commit() -> None:
            users = _reactors(key, emoji)
            if request.method == "PUT":
                if uid not in users:
                    users.append(uid)
            else:
                users = [u for u in users if u != uid]
            _set_reactors(key, emoji, users)

        return _send(state, request, 204, None, commit=commit)

    async def reaction_users(request: web.Request) -> web.Response:
        key = _key(request)
        emoji = request.match_info["emoji"]
        limit = int(request.query.get("limit", "25"))
        after = request.query.get("after")
        users = sorted(_reactors(key, emoji), key=int)
        if after is not None:
            users = [u for u in users if int(u) > int(after)]
        page = users[:limit]
        has_more = len(users) > limit
        # The paged shape the live run confirmed for .../reactions/{emoji}/users.
        body = {
            "items": [{"id": u, "bot": u == state.bot_user_id} for u in page],
            "has_more": has_more,
            "next_after": page[-1] if has_more and page else None,
        }
        return _send(state, request, 200, body)

    async def remove_emoji_reactions(request: web.Request) -> web.Response:
        key = _key(request)
        emoji = request.match_info["emoji"]
        return _send(state, request, 204, None, commit=lambda: _set_reactors(key, emoji, []))

    async def remove_all_reactions(request: web.Request) -> web.Response:
        key = _key(request)
        return _send(state, request, 204, None, commit=lambda: state.reactions.pop(key, None))

    async def create_role(request: web.Request) -> web.Response:
        gid = request.match_info["gid"]
        body = await request.json()
        # Mirrors Fluxer: create ignores mentionable and hoist and always lands at position 1.
        role = {
            "id": str(state.next_role_id),
            "name": body["name"],
            "permissions": body.get("permissions", "0"),
            "position": 1,
            "color": body.get("color", 0),
            "hoist": False,
            "mentionable": False,
        }

        def commit() -> None:
            state.roles.setdefault(gid, []).append(role)
            state.next_role_id += 1

        return _send(state, request, 200, role, payload=body, commit=commit)

    async def patch_role(request: web.Request) -> web.Response:
        gid = request.match_info["gid"]
        rid = request.match_info["rid"]
        body = await request.json()
        role = next((r for r in state.roles.get(gid, []) if r["id"] == rid), None)
        if role is None:
            return _send(state, request, 404, {"code": "UNKNOWN_ROLE", "message": "Unknown role"}, payload=body)

        def commit() -> None:
            role["mentionable"] = bool(body["mentionable"])

        return _send(state, request, 200, role, payload=body, commit=commit)

    async def channel_messages(request: web.Request) -> web.Response:
        cid = request.match_info["cid"]
        limit = int(request.query.get("limit", "50"))
        keys = [key for key in state.messages if key[0] == cid and key not in state.deleted]
        keys.sort(key=lambda key: int(key[1]), reverse=True)
        return _send(state, request, 200, [_message_view(key) for key in keys[:limit]])

    async def edit_message(request: web.Request) -> web.Response:
        payload = await request.json()
        key = _key(request)
        mid = key[1]
        if key in state.deleted:
            return _send(state, request, 404, {"code": "UNKNOWN_MESSAGE", "message": "Unknown Message"},
                         payload=payload)

        def commit() -> None:
            # Only messages the fake created are updated; an unknown id echoes without being stored.
            if key in state.messages:
                state.messages[key] = {**state.messages[key], **payload}

        return _send(state, request, 200, {"id": mid, **payload}, payload=payload, commit=commit)

    def _message_view(key: tuple[str, str]) -> dict[str, Any]:
        cid, mid = key
        view: dict[str, Any] = {"id": mid, "channel_id": cid, **state.messages[key]}
        reactions = state.reactions.get(key)
        if reactions:
            view["reactions"] = [
                {"emoji": {"name": name, "id": None}, "count": len(users), "me": state.bot_user_id in users}
                for name, users in reactions.items()
            ]
        return view

    async def get_message(request: web.Request) -> web.Response:
        key = _key(request)
        if key not in state.messages or key in state.deleted:
            return _send(state, request, 404, {"code": "UNKNOWN_MESSAGE", "message": "Unknown Message"})
        return _send(state, request, 200, _message_view(key))

    async def delete_message(request: web.Request) -> web.Response:
        key = _key(request)
        if key not in state.messages or key in state.deleted:
            return _send(state, request, 404, {"code": "UNKNOWN_MESSAGE", "message": "Unknown Message"})
        return _send(state, request, 204, None, commit=lambda: state.deleted.add(key))

    async def role(request: web.Request) -> web.Response:
        return _send(state, request, 204, None)

    async def open_dm(request: web.Request) -> web.Response:
        payload = await request.json()
        recipient = str(payload.get("recipient_id"))
        channel = state.dm_channels.setdefault(recipient, str(9000 + len(state.dm_channels)))
        return _send(state, request, 200, {"id": channel, "type": 1}, payload=payload)

    @web.middleware
    async def _delay_or_drop(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        key = f"{request.method} {request.path}"
        if key in state.drops:
            state.requests.append(_record(request, 0, None, None))
            request.transport.close()
            raise web.HTTPOk()  # never written: the client sees the connection closed before a response
        delay = state.delays.get(key, 0.0)
        if delay:
            await asyncio.sleep(delay)
        gate = state.gates.get(key)
        if gate is not None:
            if state.gate_passes.get(key, 0) > 0:
                state.gate_passes[key] -= 1
            else:
                state.gated.append(key)
                await gate.wait()
        return await handler(request)

    app = web.Application(middlewares=[_delay_or_drop])
    app.router.add_get("/.well-known/fluxer", well_known)
    app.router.add_get("/api/v1/gateway/bot", gateway_bot)
    app.router.add_post("/api/v1/channels/{cid}/messages", create_message)
    app.router.add_get("/api/v1/channels/{cid}", channel)
    app.router.add_get("/api/v1/guilds/{gid}", guild)
    app.router.add_get("/api/v1/guilds/{gid}/roles", roles)
    app.router.add_post("/api/v1/guilds/{gid}/roles", create_role)
    app.router.add_patch("/api/v1/guilds/{gid}/roles/{rid}", patch_role)
    app.router.add_get("/api/v1/guilds/{gid}/members/{uid}", member)
    app.router.add_get("/api/v1/guilds/{gid}/members", members_list)
    app.router.add_get("/api/v1/channels/{cid}/messages", channel_messages)
    reactions_path = "/api/v1/channels/{cid}/messages/{mid}/reactions"
    app.router.add_get(f"{reactions_path}/{{emoji}}/users", reaction_users)
    app.router.add_delete(f"{reactions_path}/{{emoji}}", remove_emoji_reactions)
    app.router.add_delete(reactions_path, remove_all_reactions)
    reaction_path = f"{reactions_path}/{{emoji}}/{{user}}"
    app.router.add_put(reaction_path, reaction)
    app.router.add_delete(reaction_path, reaction)
    message_path = "/api/v1/channels/{cid}/messages/{mid}"
    app.router.add_get(message_path, get_message)
    app.router.add_delete(message_path, delete_message)
    app.router.add_patch(message_path, edit_message)
    role_path = "/api/v1/guilds/{gid}/members/{uid}/roles/{rid}"
    app.router.add_put(role_path, role)
    app.router.add_delete(role_path, role)
    app.router.add_post("/api/v1/users/@me/channels", open_dm)

    async def unmatched(request: web.Request) -> web.Response:
        # recorded like any other call, so a request to a path nobody expects still shows up
        return _send(state, request, 404, {"code": "NOT_FOUND", "message": "no such route"})

    app.router.add_route("*", "/{tail:.*}", unmatched)
    return app


# ---------------------------------------------------------------------------
# Gateway fake: a websocket route on "/" and "/gateway". Each connection runs
# `script(ws, conn_index, state)`; the helpers below read/write frames on `ws`.
# ---------------------------------------------------------------------------

GatewayScript = Callable[[web.WebSocketResponse, int, FakeFluxerState], Awaitable[None]]
GATEWAY_STATE: web.AppKey[FakeFluxerState] = web.AppKey("gateway_state", FakeFluxerState)


async def send(ws: web.WebSocketResponse, op: int, d: Any = None, t: str | None = None, s: int | None = None) -> None:
    payload: dict[str, Any] = {"op": op, "d": d}
    if t is not None:
        payload["t"] = t
    if s is not None:
        payload["s"] = s
    await ws.send_json(payload)


def message_delete(message_id: str, channel_id: str, guild_id: str | None = None) -> dict:
    """MESSAGE_DELETE payload for gateway scripts: `send(ws, 0, message_delete(...), t="MESSAGE_DELETE", s=n)`."""
    d = {"id": message_id, "channel_id": channel_id}
    if guild_id is not None:
        d["guild_id"] = guild_id
    return d


async def _receive(ws: web.WebSocketResponse, timeout: float) -> dict[str, Any] | None:
    """Next client frame, recorded on its connection and ACKed if it is a heartbeat.

    Returns None once the client has closed the socket.
    """
    msg = await asyncio.wait_for(ws.receive(), timeout)
    if msg.type is not aiohttp.WSMsgType.TEXT:
        return None
    frame: dict[str, Any] = json.loads(msg.data)
    ws.fake_frames.append(frame)  # type: ignore[attr-defined]
    if frame.get("op") == 1 and ws.fake_state.ack:  # type: ignore[attr-defined]
        await send(ws, 11)
    return frame


async def expect(ws: web.WebSocketResponse, op: int, timeout: float = TIMEOUT_DEFAULT) -> dict[str, Any]:
    """Wait for the next client frame with `op` (overall deadline, heartbeats ACKed meanwhile)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        frame = await _receive(ws, deadline - loop.time())
        if frame is None:
            raise AssertionError(f"client closed while waiting for op {op}")
        if frame.get("op") == op:
            return frame


async def hold(ws: web.WebSocketResponse, timeout: float = 5.0) -> None:
    """Keep reading and ACKing heartbeats until the client closes the socket."""
    while await _receive(ws, timeout) is not None:
        pass


def make_gateway_app(script: GatewayScript) -> web.Application:
    """Fake gateway app. The shared state is `app[GATEWAY_STATE]`."""
    state = FakeFluxerState()

    async def gateway_ws(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        index = len(state.connections)
        frames: list[dict[str, Any]] = []
        state.connections.append(frames)
        state.close_codes.append(None)
        state.paths.append(request.path)
        state.queries.append(dict(request.query))
        ws.fake_frames = frames  # type: ignore[attr-defined]
        ws.fake_state = state  # type: ignore[attr-defined]
        try:
            await script(ws, index, state)
        except Exception as exc:  # noqa: BLE001 - surfaced by tests as a fast failure
            state.errors.append(repr(exc))
        finally:
            await ws.close()
            state.close_codes[index] = ws.close_code
        return ws

    app = web.Application()
    app[GATEWAY_STATE] = state
    app.router.add_get("/", gateway_ws)
    app.router.add_get("/gateway", gateway_ws)
    return app


def instance_url_of_length(length: int) -> str:
    """An https URL of exactly `length` characters (17 or more), built from .invalid parts. Every label is between
    1 and 63 characters, so the host is a valid DNS name at every such length."""
    body = length - len("https://") - len(".invalid")
    labels = -(-(body + 1) // 63)  # enough labels that none exceeds 63 characters once the dots are placed
    letters = body - (labels - 1)  # the host's characters other than the dots between labels
    sizes = [letters // labels + (1 if i < letters % labels else 0) for i in range(labels)]
    return "https://" + ".".join("a" * size for size in sizes) + ".invalid"


def instance_url_of_units(units: int) -> str:
    """An https URL of exactly `units` UTF-16 code units, for the cap that counts units. After an ASCII prefix it is
    filled with astral characters, two units each, and an odd count ends in one ASCII character. So it has fewer
    characters than units."""
    prefix = "https://fluxer.invalid/"
    room = units - len(prefix)
    if room < 0:
        raise ValueError("units must leave room for the prefix")
    return prefix + "\U0001F600" * (room // 2) + "a" * (room % 2)


# Test-side waits, shared by every suite that drives the fake servers from a running task.

async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, *,
                     waiting_for: str = "the fake servers") -> None:
    """Polls `predicate` every 10 ms until it holds, and fails the test once `timeout` seconds have passed."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {waiting_for}")
        await asyncio.sleep(0.01)


async def settle() -> None:
    """Gives the running tasks time to reach their next wait, so a count taken after it does not change."""
    for _ in range(20):
        await asyncio.sleep(0.01)
