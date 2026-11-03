# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fake Pterodactyl client API, shaped after Panel v1.15.2 for the read-only endpoints.

Every request is recorded first, then a scripted reply is used if one is queued for
`"METHOD /path"`, then the built-in behaviour answers. Error bodies always contain
`SENTINEL-PANEL-BODY` so tests can check that bodies never reach logs or results.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from aiohttp import web

PANEL_BODY_SENTINEL = "SENTINEL-PANEL-BODY"
_PATH = re.compile(r"/api/client/servers/([^/]+)(/resources|/schedules)?")


@dataclass
class FakeServer:
    current_state: str = "offline"
    is_suspended: bool = False
    schedules: list[dict] = field(default_factory=list)  # raw "attributes" dicts, tasks under relationships


@dataclass
class Scripted:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b"{}"
    delay: float = 0.0  # sleep before answering (timeout tests)


@dataclass
class PanelRecorded:
    method: str
    path: str
    authorization: str | None
    accept: str | None


@dataclass
class FakePanelState:
    servers: dict[str, FakeServer] = field(default_factory=dict)
    scripted: dict[str, list[Scripted]] = field(default_factory=dict)  # "GET /path" -> queue
    requests: list[PanelRecorded] = field(default_factory=list)


def schedule(
    name: str,
    *,
    active: bool = True,
    next_run_at: str | None = "2026-11-03T06:00:00+01:00",
    tasks: tuple[dict, ...] = (),
) -> dict:
    """A schedule's attribute dict in the Panel's shape, with its tasks under relationships."""
    return {
        "name": name,
        "is_active": active,
        "next_run_at": next_run_at,
        "relationships": {
            "tasks": {"object": "list", "data": [{"object": "schedule_task", "attributes": t} for t in tasks]},
        },
    }


def task(seq: int, action: str, payload: str) -> dict:
    """A task's attribute dict in the Panel's shape."""
    return {"sequence_id": seq, "action": action, "payload": payload, "time_offset": 0}


def _error(status: int, code: str) -> web.Response:
    body = {"errors": [{"code": code, "status": str(status), "detail": PANEL_BODY_SENTINEL}]}
    return web.json_response(body, status=status)


def _details(sid: str, server: FakeServer) -> web.Response:
    attributes = {
        "identifier": sid,
        "is_suspended": server.is_suspended,
        "status": "suspended" if server.is_suspended else None,
        "name": "SENTINEL-SERVER-NAME",
    }
    return web.json_response({"object": "server", "attributes": attributes})


def _resources(server: FakeServer) -> web.Response:
    if server.is_suspended:
        return _error(409, "ServerStateConflictException")
    attributes = {"current_state": server.current_state, "is_suspended": False, "resources": {}}
    return web.json_response({"object": "stats", "attributes": attributes})


def _schedules(server: FakeServer) -> web.Response:
    if server.is_suspended:
        return _error(409, "ServerStateConflictException")
    data = [{"object": "server_schedule", "attributes": sched} for sched in server.schedules]
    return web.json_response({"object": "list", "data": data})


async def _answer(state: FakePanelState, request: web.Request) -> web.StreamResponse:
    state.requests.append(
        PanelRecorded(
            method=request.method,
            path=request.path,
            authorization=request.headers.get("Authorization"),
            accept=request.headers.get("Accept"),
        )
    )
    queue = state.scripted.get(f"{request.method} {request.path}")
    if queue:
        entry = queue.pop(0)
        if entry.delay:
            await asyncio.sleep(entry.delay)
        return web.Response(status=entry.status, body=entry.body, headers=entry.headers)

    match = _PATH.fullmatch(request.path)
    if match is None:
        return _error(404, "NotFoundHttpException")
    if request.method != "GET":
        return _error(405, "MethodNotAllowedHttpException")
    sid, endpoint = match.group(1), match.group(2)
    server = state.servers.get(sid)
    if server is None:
        return _error(404, "NotFoundHttpException")
    if endpoint == "/resources":
        return _resources(server)
    if endpoint == "/schedules":
        return _schedules(server)
    return _details(sid, server)


def make_panel_app(state: FakePanelState) -> web.Application:
    async def dispatch(request: web.Request) -> web.StreamResponse:
        return await _answer(state, request)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", dispatch)
    return app

