# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Process-level behaviour of `python -m fluxer_panel` against in-test fakes.

The child env is built from scratch and never inherits os.environ, so no real token can leak in.
The child runs with cwd=tmp_path. Every wait is bounded, and a child still alive at the end is killed.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

import fluxer_panel.__main__ as panel_main
from fluxer_client.gateway import FatalGatewayError
from fluxer_panel import cards
from fluxer_panel import text as panel_text
from fluxer_panel.__main__ import IDENTIFY_PROPERTIES, purge_once, run
from fluxer_panel.config import Config
from fluxer_panel.db import LATEST_VERSION, Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.session_post import SessionPost
from fluxer_panel.wake import WakeRequests
from tests.fakes import (
    GATEWAY_STATE,
    FakeFluxerState,
    expect,
    hold,
    make_gateway_app,
    make_rest_app,
    message_delete,
    send,
)

TOKEN = "SENTINEL-TOKEN-7f3a9c"
BOUND = 30  # seconds: generous for a cold interpreter, still a hard stop
ADMIN_DEFAULT = "1001"
GAMES_CHANNEL = "3001"
GAMES_POSTS = f"/api/v1/channels/{GAMES_CHANNEL}/messages"


def child_env(tmp_path: Path, **overrides: str | None) -> dict[str, str]:
    """A minimal env built from scratch. A value of None removes the key."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC",
        "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
        "ADMIN_USER_IDS": ADMIN_DEFAULT,
        "DATA_DIR": str(tmp_path / "data"),
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def json_lines(text: str) -> list[dict[str, Any]]:
    """Every line must be a JSON object: AC3.4."""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


async def wait_for_ok_status(path: Path) -> None:
    """Poll until the purge status file records a success (the startup purge)."""
    while True:
        with contextlib.suppress(OSError, ValueError):
            if json.loads(path.read_bytes()).get("ok") is True:
                return
        await asyncio.sleep(0.05)


async def run_panel(env: dict[str, str], tmp_path: Path, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "fluxer_panel", *args, env=env, cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), BOUND)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert proc.returncode is not None
    return proc.returncode, out.decode(), err.decode()


async def test_sigterm_after_ready_exits_zero(aiohttp_server, tmp_path):
    async def gateway_script(ws, index, state):
        await send(ws, 10, {"heartbeat_interval": 30000})
        await expect(ws, 2)
        await send(ws, 0, {"session_id": "S1", "user": {"id": "5000"}}, t="READY", s=1)
        await hold(ws)

    gateway_app = make_gateway_app(gateway_script)
    gateway_server = await aiohttp_server(gateway_app)
    gateway_url = str(gateway_server.make_url("/gateway").with_scheme("ws"))
    rest = await aiohttp_server(make_rest_app(FakeFluxerState(gateway_url=gateway_url)))
    env = child_env(tmp_path, FLUXER_INSTANCE_URL=None,
                    FLUXER_API_URL=str(rest.make_url("/api")).rstrip("/"),
                    FLUXER_GATEWAY_URL=gateway_url, GAMES_CHANNEL_ID=GAMES_CHANNEL)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "fluxer_panel", env=env, cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    seen: list[str] = []
    started: set[str] = set()
    try:
        assert proc.stdout is not None
        # ready and poller_started arrive in either order; both must be out before stopping
        while not {"ready", "poller_started"} <= started:
            line = await asyncio.wait_for(proc.stdout.readline(), BOUND)
            if not line:
                raise AssertionError(f"exited before ready: {seen}")
            seen.append(line.decode())
            started.add(json.loads(seen[-1]).get("event"))
        # the startup purge runs right after the gateway task starts: wait for its status before stopping
        await asyncio.wait_for(wait_for_ok_status(tmp_path / "data" / "purge-status.json"), BOUND)
        proc.send_signal(signal.SIGTERM)
        rest_out, err = await asyncio.wait_for(proc.communicate(), BOUND)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    out = "".join(seen) + rest_out.decode()
    ready = [e for e in json_lines(out) if e["event"] == "ready"]
    assert ready == [{"ts": ready[0]["ts"], "level": "INFO", "logger": "fluxer_panel.bot",
                      "event": "ready", "user_id": "5000"}]
    assert proc.returncode == 0
    events = [e["event"] for e in json_lines(out)]
    assert "shutting_down" in events
    assert "purge_done" in events
    assert "poller_started" in events
    assert "poller_tick_failed" not in events
    assert TOKEN not in out
    assert TOKEN not in err.decode()
    assert "Traceback" not in err.decode()
    with contextlib.closing(sqlite3.connect(tmp_path / "data" / "panel.db")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
    gateway_state = gateway_app[GATEWAY_STATE]
    assert gateway_state.errors == []
    [identify] = [f for f in gateway_state.connections[0] if f.get("op") == 2]
    assert identify["d"]["properties"] == IDENTIFY_PROPERTIES


def _seed_live_card(conn: sqlite3.Connection) -> None:
    card_id = cards.create_card(conn, slug="alpha", title="Alpha", author_id=ADMIN_DEFAULT, now=0)
    cards.set_state(conn, card_id, "live", 0)


def _stored_message_id(path: Path) -> str | None:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        row = conn.execute("SELECT message_id FROM cards WHERE slug = 'alpha'").fetchone()
    return None if row is None else row[0]


async def test_a_live_card_is_posted_by_the_running_process_and_sigterm_exits_zero(aiohttp_server, tmp_path):
    db_path = tmp_path / "data" / "panel.db"
    db = Database.open(db_path)
    try:
        await db.run(_seed_live_card)
    finally:
        db.close()

    async def gateway_script(ws, index, state):
        await send(ws, 10, {"heartbeat_interval": 30000})
        await expect(ws, 2)
        await send(ws, 0, {"session_id": "S1", "user": {"id": "5000"}}, t="READY", s=1)
        await hold(ws)

    gateway_server = await aiohttp_server(make_gateway_app(gateway_script))
    gateway_url = str(gateway_server.make_url("/gateway").with_scheme("ws"))
    rest_state = FakeFluxerState(gateway_url=gateway_url)
    rest = await aiohttp_server(make_rest_app(rest_state))
    env = child_env(tmp_path, FLUXER_INSTANCE_URL=None,
                    FLUXER_API_URL=str(rest.make_url("/api")).rstrip("/"),
                    FLUXER_GATEWAY_URL=gateway_url, GAMES_CHANNEL_ID=GAMES_CHANNEL)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "fluxer_panel", env=env, cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        async def posted_and_stored() -> dict[str, Any]:
            while not _games_posts(rest_state):
                await asyncio.sleep(0.05)
            # The id is stored after the POST returns, so wait for the store before stopping the process.
            while _stored_message_id(db_path) is None:
                await asyncio.sleep(0.05)
            return _games_posts(rest_state)[0].payload

        payload = await asyncio.wait_for(posted_and_stored(), BOUND)
        proc.send_signal(signal.SIGTERM)
        out, err = await asyncio.wait_for(proc.communicate(), BOUND)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["embeds"][0]["title"] == "Alpha"
    assert "content" not in payload
    assert proc.returncode == 0
    events = [e["event"] for e in json_lines(out.decode())]
    assert "poller_started" in events
    assert "shutting_down" in events
    assert "poller_tick_failed" not in events
    assert "Traceback" not in err.decode()
    # The card posts first and takes 5001. The board takes 5002, and no pin request is sent.
    assert _stored_message_id(db_path) == "5001"


def _seed_draft_card(conn: sqlite3.Connection) -> None:
    cards.create_card(conn, slug="alpha", title="Alpha", author_id=ADMIN_DEFAULT, now=0)


async def test_a_publish_by_direct_message_kicks_the_poller_before_the_poll_interval(aiohttp_server, tmp_path):
    db_path = tmp_path / "data" / "panel.db"
    db = Database.open(db_path)
    try:
        await db.run(_seed_draft_card)
    finally:
        db.close()

    async def gateway_script(ws, index, state):
        await send(ws, 10, {"heartbeat_interval": 30000})
        await expect(ws, 2)
        await send(ws, 0, {"session_id": "S1", "user": {"id": "5000"}}, t="READY", s=1)
        await send(ws, 0, {"id": "8001", "channel_id": "9100", "author": {"id": ADMIN_DEFAULT, "bot": False},
                           "content": "!card publish alpha"}, t="MESSAGE_CREATE", s=2)
        await hold(ws)

    gateway_server = await aiohttp_server(make_gateway_app(gateway_script))
    gateway_url = str(gateway_server.make_url("/gateway").with_scheme("ws"))
    rest_state = FakeFluxerState(gateway_url=gateway_url)
    rest = await aiohttp_server(make_rest_app(rest_state))
    # The poll interval is an hour: a post inside BOUND can only come from the kick that !card publish sends.
    env = child_env(tmp_path, FLUXER_INSTANCE_URL=None,
                    FLUXER_API_URL=str(rest.make_url("/api")).rstrip("/"),
                    FLUXER_GATEWAY_URL=gateway_url, GAMES_CHANNEL_ID=GAMES_CHANNEL, POLL_INTERVAL_SECONDS="3600")

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "fluxer_panel", env=env, cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        async def posted_and_stored() -> None:
            while not _games_posts(rest_state):
                await asyncio.sleep(0.05)
            while _stored_message_id(db_path) is None:
                await asyncio.sleep(0.05)

        await asyncio.wait_for(posted_and_stored(), BOUND)
        proc.send_signal(signal.SIGTERM)
        out, err = await asyncio.wait_for(proc.communicate(), BOUND)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    assert _games_posts(rest_state)[0].payload["embeds"][0]["title"] == "Alpha"
    assert proc.returncode == 0
    assert "poller_tick_failed" not in [e["event"] for e in json_lines(out.decode())]
    assert "Traceback" not in err.decode()


async def test_a_message_delete_in_the_running_process_reposts_the_card(aiohttp_server, tmp_path):
    db_path = tmp_path / "data" / "panel.db"
    db = Database.open(db_path)
    try:
        await db.run(_seed_live_card)
    finally:
        db.close()
    rest_state = FakeFluxerState()

    async def gateway_script(ws, index, state):
        await send(ws, 10, {"heartbeat_interval": 30000})
        await expect(ws, 2)
        await send(ws, 0, {"session_id": "S1", "user": {"id": "5000"}}, t="READY", s=1)
        # Delete only once the id is stored, so the compare-and-clear sees the message the poller posted.
        while not _games_posts(rest_state) or _stored_message_id(db_path) is None:
            await asyncio.sleep(0.05)
        await send(ws, 0, message_delete("5001", GAMES_CHANNEL), t="MESSAGE_DELETE", s=2)   # the card
        await hold(ws)

    gateway_server = await aiohttp_server(make_gateway_app(gateway_script))
    gateway_url = str(gateway_server.make_url("/gateway").with_scheme("ws"))
    rest_state.gateway_url = gateway_url
    rest = await aiohttp_server(make_rest_app(rest_state))
    # The poll interval is an hour: the second post can only come from the kick that MESSAGE_DELETE sends.
    env = child_env(tmp_path, FLUXER_INSTANCE_URL=None,
                    FLUXER_API_URL=str(rest.make_url("/api")).rstrip("/"),
                    FLUXER_GATEWAY_URL=gateway_url, GAMES_CHANNEL_ID=GAMES_CHANNEL, POLL_INTERVAL_SECONDS="3600")

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "fluxer_panel", env=env, cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        async def reposted() -> None:
            while len(_games_posts(rest_state)) < 2:
                await asyncio.sleep(0.05)
            while _stored_message_id(db_path) != "5003":
                await asyncio.sleep(0.05)

        await asyncio.wait_for(reposted(), BOUND)
        proc.send_signal(signal.SIGTERM)
        out, err = await asyncio.wait_for(proc.communicate(), BOUND)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    assert len(_games_posts(rest_state)) == 2
    assert proc.returncode == 0
    assert "poller_tick_failed" not in [e["event"] for e in json_lines(out.decode())]
    assert "Traceback" not in err.decode()


def _games_posts(state: FakeFluxerState) -> list[Any]:
    """The card posts in the games channel. The summary board is posted there too, after the cards, and is not a card."""
    return [r for r in state.requests if r.method == "POST" and r.path == GAMES_POSTS
            and r.payload["embeds"][0].get("title") != panel_text.BOARD_TITLE]


@pytest.mark.parametrize(("name", "overrides"), [
    ("FLUXER_TOKEN", {"FLUXER_TOKEN": None}),
    ("FLUXER_TOKEN", {"FLUXER_TOKEN": "SENTINEL TOKEN"}),
    ("FLUXER_INSTANCE_URL", {"FLUXER_INSTANCE_URL": None}),
    ("ADMIN_USER_IDS", {"ADMIN_USER_IDS": "01001"}),
    ("LOG_LEVEL", {"LOG_LEVEL": "LOUD"}),
    ("PURGE_INTERVAL_SECONDS", {"PURGE_INTERVAL_SECONDS": "0"}),
])
def test_config_errors_exit_2_and_name_the_variable(name, overrides, tmp_path):
    env = child_env(tmp_path, **overrides)
    proc = subprocess.run([sys.executable, "-m", "fluxer_panel"], env=env, cwd=str(tmp_path),
                          capture_output=True, text=True, timeout=BOUND)

    assert proc.returncode == 2
    assert "Configuration error:" in proc.stderr
    assert name in proc.stderr
    assert "SENTINEL" not in proc.stderr + proc.stdout
    assert "Traceback" not in proc.stderr


@pytest.mark.parametrize(("name", "overrides"), [
    ("SESSION_TIME_ZONE", {"SESSION_TIME_ZONE": None}),
    ("SESSION_SLOTS_WEEKEND", {"SESSION_SLOTS_WEEKEND": "25:00"}),
])
async def test_bad_session_settings_exit_2_and_name_the_variable(name, overrides, tmp_path):
    # `purge` makes no Fluxer connection, so a config that wrongly passes cannot hang the test
    code, out, err = await run_panel(child_env(tmp_path, **overrides), tmp_path, "purge")

    assert code == 2
    assert "Configuration error:" in err
    assert name in err
    assert "25:00" not in err + out
    assert "Traceback" not in err


async def test_unknown_subcommand_exits_2_with_usage(tmp_path):
    code, out, err = await run_panel(child_env(tmp_path), tmp_path, "frobnicate")

    assert code == 2
    assert "usage: python -m fluxer_panel [run|purge]" in err
    assert TOKEN not in out + err


async def test_fake_gateway_fatal_close_exits_1_with_gateway_fatal(aiohttp_server, tmp_path):
    async def gateway_script(ws, index, state):
        await send(ws, 10, {"heartbeat_interval": 30000})
        await expect(ws, 2)
        await ws.close(code=4004)

    gateway_server = await aiohttp_server(make_gateway_app(gateway_script))
    gateway_url = str(gateway_server.make_url("/gateway").with_scheme("ws"))
    rest = await aiohttp_server(make_rest_app(FakeFluxerState(gateway_url=gateway_url)))
    env = child_env(tmp_path, FLUXER_INSTANCE_URL=None,
                    FLUXER_API_URL=str(rest.make_url("/api")).rstrip("/"),
                    FLUXER_GATEWAY_URL=gateway_url)
    code, out, err = await run_panel(env, tmp_path)

    assert code == 1
    assert "gateway_fatal" in [e["event"] for e in json_lines(out)]
    assert TOKEN not in out + err
    assert "Traceback" not in err


async def test_discovery_500_exits_1_with_fatal_error_and_no_token(aiohttp_server, tmp_path):
    async def well_known(request: web.Request) -> web.Response:
        return web.Response(status=500, text="boom")

    app = web.Application()
    app.router.add_get("/.well-known/fluxer", well_known)
    server = await aiohttp_server(app)
    env = child_env(tmp_path, FLUXER_INSTANCE_URL=str(server.make_url("/")))
    code, out, err = await run_panel(env, tmp_path)

    assert code == 1
    [fatal] = [e for e in json_lines(out) if e["event"] == "fatal_error"]
    assert "exc" in fatal
    assert TOKEN not in out + err
    assert "Traceback" not in err


SHUTDOWN_BOUND = 5  # seconds: a stop during discovery must not wait for the discovery request (30 s timeout)


async def test_sigterm_during_discovery_exits_zero_promptly(aiohttp_server, tmp_path):
    discovery_seen = asyncio.Event()
    release = asyncio.Event()

    async def hanging_well_known(request: web.Request) -> web.Response:
        discovery_seen.set()
        await asyncio.wait_for(release.wait(), BOUND)   # hangs until the test lets go
        return web.Response(status=500, text="released")

    app = web.Application()
    app.router.add_get("/.well-known/fluxer", hanging_well_known)
    server = await aiohttp_server(app)
    env = child_env(tmp_path, FLUXER_INSTANCE_URL=str(server.make_url("/")))

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "fluxer_panel", env=env, cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(discovery_seen.wait(), BOUND)   # ready: discovery is in flight
        proc.send_signal(signal.SIGTERM)
        out, err = await asyncio.wait_for(proc.communicate(), SHUTDOWN_BOUND)
    finally:
        release.set()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    assert proc.returncode == 0
    events = [e["event"] for e in json_lines(out.decode())]
    assert "shutting_down" in events
    assert "endpoints_resolved" not in events
    assert not (tmp_path / "data" / "purge-status.json").exists()   # stop won the race: no purge ever started
    assert TOKEN not in out.decode() + err.decode()
    assert "Traceback" not in err.decode()


DAY = 86400


def _seed_purge_rows(conn: sqlite3.Connection, now: int) -> None:
    """One expired session with an RSVP and an expired request, plus a kept session, RSVP and request."""
    card = conn.execute("INSERT INTO cards (title, created_by, created_at, updated_at) "
                        "VALUES ('Game', '1001', ?, ?)", (now, now)).lastrowid
    expired = conn.execute(
        "INSERT INTO sessions (card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, created_at, "
        "state, closed_at) VALUES (?, '3001', '3002', '3003', ?, ?, '1001', ?, 'ended', ?)",
        (card, now - 9 * DAY, now - 8 * DAY, now - 9 * DAY, now - 8 * DAY)).lastrowid
    kept = conn.execute(
        "INSERT INTO sessions (card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, created_at) "
        "VALUES (?, '3001', '3002', '3004', ?, ?, '1001', ?)", (card, now, now + DAY, now)).lastrowid
    for session_id in (expired, kept):
        conn.execute("INSERT INTO rsvps (session_id, user_id, choice, responded_at) VALUES (?, '5000', 'going', ?)",
                     (session_id, now))
    conn.execute("INSERT INTO requests (kind, card_id, requester_id, status, created_at, closed_at) "
                 "VALUES ('wake', ?, '5000', 'cleared', ?, ?)", (card, now - 31 * DAY, now - 31 * DAY))
    conn.execute("INSERT INTO play_now (card_id, guild_id, channel_id, message_id, presser_id, offers_wake, state, "
                 "created_at, expires_at) VALUES (?, '1003', '1004', '6000', '5000', 0, 'expired', ?, ?)",
                 (card, now - 31 * DAY, now - 31 * DAY + 3600))
    conn.execute("INSERT INTO requests (kind, card_id, requester_id, status, created_at) "
                 "VALUES ('wake', ?, '5000', 'open', ?)", (card, now))


async def test_purge_subcommand_defers_due_sessions_and_exits_zero(tmp_path):
    now = int(time.time())
    db = Database.open(tmp_path / "panel.db")
    try:
        await db.run(lambda conn: _seed_purge_rows(conn, now))
    finally:
        db.close()

    code, out, err = await run_panel(child_env(tmp_path, DATA_DIR=str(tmp_path)), tmp_path, "purge")

    assert code == 0
    status = json.loads((tmp_path / "purge-status.json").read_text())
    # ok is True with no network step: the subcommand has no REST, so the due session is deferred, not scrubbed
    assert status["ok"] is True
    assert status["schema_version"] == 2
    assert status["deleted"] == {"sessions": 0, "rsvps": 0, "requests": 1, "play_now": 1, "polls": 0}
    assert status["sessions_deferred"] == 1
    assert "purge_done" in [e["event"] for e in json_lines(out)]
    assert TOKEN not in out + err
    with contextlib.closing(sqlite3.connect(tmp_path / "panel.db")) as conn:
        counts = [conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("sessions", "rsvps", "requests", "play_now")]
    # the due session and its RSVP stay; the old request and the old play-now post go, the open request stays
    assert counts == [2, 2, 1, 0]


async def test_purge_subcommand_uses_the_configured_session_retention(tmp_path):
    now = int(time.time())
    db = Database.open(tmp_path / "panel.db")
    try:
        # closed two days ago: due under a one-day retention, kept under the seven-day default
        def seed(conn: sqlite3.Connection) -> None:
            card = conn.execute("INSERT INTO cards (title, created_by, created_at, updated_at) "
                                "VALUES ('Game', '1001', ?, ?)", (now, now)).lastrowid
            conn.execute(
                "INSERT INTO sessions (card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, "
                "created_at, state, closed_at) VALUES (?, '3001', '3002', '3003', ?, ?, '1001', ?, 'ended', ?)",
                (card, now - 3 * DAY, now - 2 * DAY - 3600, now - 3 * DAY, now - 2 * DAY))

        await db.run(seed)
    finally:
        db.close()

    env = child_env(tmp_path, DATA_DIR=str(tmp_path), SESSION_RETENTION_SECONDS=str(DAY))
    code, out, err = await run_panel(env, tmp_path, "purge")

    assert code == 0
    status = json.loads((tmp_path / "purge-status.json").read_text())
    assert status["sessions_deferred"] == 1
    assert status["deleted"]["sessions"] == 0
    with contextlib.closing(sqlite3.connect(tmp_path / "panel.db")) as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    assert TOKEN not in out + err


def test_purge_once_exits_1_when_the_purge_fails(tmp_path):
    db = Database.open(tmp_path / "panel.db")
    # break a purge query without touching the panel's own connection. The play-now delete runs on every purge, even
    # with no due session (the subcommand defers sessions, so the RSVP query would not run).
    with contextlib.closing(sqlite3.connect(tmp_path / "panel.db")) as conn:
        conn.execute("DROP TABLE play_now")
        conn.commit()
    config = Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
                              "ADMIN_USER_IDS": ADMIN_DEFAULT, "DATA_DIR": str(tmp_path)})
    try:
        assert purge_once(config, db) == 1
    finally:
        db.close()
    assert json.loads((tmp_path / "purge-status.json").read_text())["ok"] is False


class _PurgeProbe:
    """Stands in for purge_loop: records that it started, whether it was cancelled, and the keywords it was given."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.kwargs: dict[str, Any] = {}


def _probe_purge_loop(monkeypatch: pytest.MonkeyPatch) -> _PurgeProbe:
    probe = _PurgeProbe()

    async def fake_purge_loop(db, status_path, interval, clock=None, **kwargs):
        probe.kwargs = kwargs
        probe.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            probe.cancelled = True
            raise

    monkeypatch.setattr(panel_main, "purge_loop", fake_purge_loop)
    return probe


async def _in_process_panel(aiohttp_server, tmp_path: Path, gateway_script) -> tuple[Config, Database]:
    gateway_server = await aiohttp_server(make_gateway_app(gateway_script))
    gateway_url = str(gateway_server.make_url("/gateway").with_scheme("ws"))
    rest = await aiohttp_server(make_rest_app(FakeFluxerState(gateway_url=gateway_url)))
    config = Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_API_URL": str(rest.make_url("/api")).rstrip("/"),
                              "FLUXER_GATEWAY_URL": gateway_url, "ADMIN_USER_IDS": ADMIN_DEFAULT,
                              "DATA_DIR": str(tmp_path)})
    return config, Database.open(tmp_path / "panel.db")


async def test_purge_task_is_cancelled_when_the_gateway_fails_fatally(aiohttp_server, tmp_path, monkeypatch):
    probe = _probe_purge_loop(monkeypatch)

    async def gateway_script(ws, index, state):
        await send(ws, 10, {"heartbeat_interval": 30000})
        await expect(ws, 2)
        await ws.close(code=4004)

    config, db = await _in_process_panel(aiohttp_server, tmp_path, gateway_script)
    try:
        with pytest.raises(FatalGatewayError):
            await run(config, db)
    finally:
        db.close()
    assert probe.started.is_set()
    assert probe.cancelled


async def test_run_hands_the_session_scrubber_and_the_retention_to_the_purge_loop(aiohttp_server, tmp_path,
                                                                                  monkeypatch):
    probe = _probe_purge_loop(monkeypatch)

    async def gateway_script(ws, index, state):
        await send(ws, 10, {"heartbeat_interval": 30000})
        await expect(ws, 2)
        await ws.close(code=4004)

    config, db = await _in_process_panel(aiohttp_server, tmp_path, gateway_script)
    config = dataclasses.replace(config, session_retention_seconds=120)   # distinct from the 7-day default
    try:
        with pytest.raises(FatalGatewayError):
            await run(config, db)
    finally:
        db.close()
    scrub = probe.kwargs["scrub"]
    assert isinstance(scrub.__self__, SessionPost) and scrub.__name__ == "scrub"
    assert probe.kwargs["retention"] == 120


class _PollerProbe:
    """Stands in for Poller: records that run started, whether it was cancelled, and whether the REST client still
    answered from inside the cancelled run. It answers only while the HTTP session is open."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False
        self.rest_answered: bool | None = None
        self.wake: Any = None           # what `set_wake` was given, before the loop started
        self.interactions: Any = None   # what `set_interactions` was given, before the loop started
        self.poller: Any = None         # the ProbePoller instance the composition root built
        self.poller_kwargs: dict[str, Any] = {}   # the keyword arguments the composition root gave the poller


def _probe_poller(monkeypatch: pytest.MonkeyPatch) -> _PollerProbe:
    probe = _PollerProbe()

    class ProbePoller:
        def __init__(self, **kwargs: Any) -> None:
            self._rest = kwargs["rest"]
            probe.poller = self
            probe.poller_kwargs = kwargs

        def kick(self) -> None:
            pass

        def set_wake(self, wake: Any) -> None:
            probe.wake = wake

        def set_interactions(self, interactions: Any) -> None:
            probe.interactions = interactions

        def set_session_jobs(self, jobs: Any) -> None:
            pass

        def set_poll_jobs(self, jobs: Any) -> None:
            pass

        def on_games_message(self, channel_id: str) -> None:
            pass

        def status_of(self, card_id: int) -> None:
            return None

        async def on_message_delete(self, event: Any) -> None:
            pass

        async def run(self) -> None:
            probe.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                probe.cancelled = True
                probe.rest_answered = await _rest_answers(self._rest)
                raise

    monkeypatch.setattr(panel_main, "Poller", ProbePoller)
    return probe


async def _rest_answers(rest: Any) -> bool:
    """One cheap REST call against the fake. False when the session is already closed."""
    try:
        await rest.get_gateway_url()
    except Exception:  # noqa: BLE001 - a closed session is the answer this probe records
        return False
    return True


async def test_poller_task_is_cancelled_when_the_gateway_fails_fatally(aiohttp_server, tmp_path, monkeypatch):
    probe = _probe_poller(monkeypatch)

    async def gateway_script(ws, index, state):
        await send(ws, 10, {"heartbeat_interval": 30000})
        await expect(ws, 2)
        await ws.close(code=4004)

    config, db = await _in_process_panel(aiohttp_server, tmp_path, gateway_script)
    try:
        with pytest.raises(FatalGatewayError):
            await run(config, db)
    finally:
        db.close()
    assert probe.started.is_set()
    assert probe.cancelled
    assert probe.rest_answered is True      # the poller was cancelled while the session was still open


async def test_poller_task_is_cancelled_when_run_is_cancelled_from_outside(
        aiohttp_server, tmp_path, monkeypatch):
    probe = _probe_poller(monkeypatch)
    config, db = await _in_process_panel(aiohttp_server, tmp_path, _ready_gateway_script)
    try:
        task = asyncio.create_task(run(config, db))
        await asyncio.wait_for(probe.started.wait(), BOUND)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, BOUND)
    finally:
        db.close()
    assert probe.cancelled
    assert probe.rest_answered is True      # the poller was cancelled while the session was still open
    assert _pending_gateway_tasks() == []


def _pending_gateway_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if getattr(t.get_coro(), "__qualname__", "") == "Gateway.run"]


async def _ready_gateway_script(ws, index, state):
    await send(ws, 10, {"heartbeat_interval": 30000})
    await expect(ws, 2)
    await send(ws, 0, {"session_id": "S1", "user": {"id": "5000"}}, t="READY", s=1)
    await hold(ws)


async def test_the_poller_is_given_the_wake_requests_before_it_starts(aiohttp_server, tmp_path, monkeypatch):
    probe = _probe_poller(monkeypatch)
    config, db = await _in_process_panel(aiohttp_server, tmp_path, _ready_gateway_script)
    try:
        task = asyncio.create_task(run(config, db))
        await asyncio.wait_for(probe.started.wait(), BOUND)
        assert isinstance(probe.wake, WakeRequests)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, BOUND)
    finally:
        db.close()


async def test_the_poller_is_given_the_interactions_before_it_starts(aiohttp_server, tmp_path, monkeypatch):
    """The play-now expiry runs on the poller's tick, so the poller must hold the interactions before its loop starts."""
    probe = _probe_poller(monkeypatch)
    config, db = await _in_process_panel(aiohttp_server, tmp_path, _ready_gateway_script)
    try:
        task = asyncio.create_task(run(config, db))
        await asyncio.wait_for(probe.started.wait(), BOUND)
        assert probe.interactions is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, BOUND)
    finally:
        db.close()


async def test_interactions_and_the_bot_get_the_same_wake_requests_the_poller_has(aiohttp_server, tmp_path, monkeypatch):
    probe = _probe_poller(monkeypatch)
    given: dict[str, Any] = {}
    real_interactions = panel_main.Interactions
    real_bot = panel_main.Bot

    def recording_interactions(**kwargs: Any) -> Any:
        given["interactions"] = kwargs["wake"]
        return real_interactions(**kwargs)

    def recording_bot(**kwargs: Any) -> Any:
        given["bot"] = kwargs["card_ctx"].wake
        return real_bot(**kwargs)

    monkeypatch.setattr(panel_main, "Interactions", recording_interactions)
    monkeypatch.setattr(panel_main, "Bot", recording_bot)
    config, db = await _in_process_panel(aiohttp_server, tmp_path, _ready_gateway_script)
    try:
        task = asyncio.create_task(run(config, db))
        await asyncio.wait_for(probe.started.wait(), BOUND)
        assert isinstance(probe.wake, WakeRequests)
        assert given["interactions"] is probe.wake
        assert given["bot"] is probe.wake
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, BOUND)
    finally:
        db.close()


def _recording(real: Any, log: list[tuple[dict[str, Any], Any]]) -> Any:
    def build(**kwargs: Any) -> Any:
        made = real(**kwargs)
        log.append((kwargs, made))
        return made
    return build


def _record_constructions(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[tuple[dict[str, Any], Any]]]:
    """Wraps the reaction components' constructors in the composition root. Each construction is recorded with the keyword
    arguments it was given and the instance the real class made, so a test can see what each component was handed."""
    made: dict[str, list[tuple[dict[str, Any], Any]]] = {}
    for name in ("CardContext", "Seeder", "Presses", "Interactions", "Bot"):
        made[name] = []
        monkeypatch.setattr(panel_main, name, _recording(getattr(panel_main, name), made[name]))
    return made


async def test_the_bot_gets_the_presses_and_the_pollers_games_message_hook(aiohttp_server, tmp_path, monkeypatch):
    probe = _probe_poller(monkeypatch)
    made = _record_constructions(monkeypatch)
    config, db = await _in_process_panel(aiohttp_server, tmp_path, _ready_gateway_script)
    task = asyncio.create_task(run(config, db))
    try:
        await asyncio.wait_for(probe.started.wait(), BOUND)
        [(bot_kwargs, _bot)] = made["Bot"]
        [(_presses_kwargs, presses)] = made["Presses"]
        assert bot_kwargs.get("presses") is presses
        assert bot_kwargs.get("on_games_message") == probe.poller.on_games_message
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, BOUND)
        db.close()


async def test_one_channel_pacer_reaches_the_seeder_the_presses_and_the_interactions(
        aiohttp_server, tmp_path, monkeypatch):
    probe = _probe_poller(monkeypatch)
    made = _record_constructions(monkeypatch)
    config, db = await _in_process_panel(aiohttp_server, tmp_path, _ready_gateway_script)
    task = asyncio.create_task(run(config, db))
    try:
        await asyncio.wait_for(probe.started.wait(), BOUND)
        [(seeder_kwargs, _seeder)] = made["Seeder"]
        [(presses_kwargs, _presses)] = made["Presses"]
        [(interactions_kwargs, _interactions)] = made["Interactions"]
        pacer = seeder_kwargs["pacer"]
        assert isinstance(pacer, ChannelPacer)
        assert presses_kwargs["pacer"] is pacer
        assert interactions_kwargs["pacer"] is pacer
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, BOUND)
        db.close()


async def test_one_identity_and_one_games_guild_are_shared_by_every_component_that_takes_them(
        aiohttp_server, tmp_path, monkeypatch):
    probe = _probe_poller(monkeypatch)
    made = _record_constructions(monkeypatch)
    config, db = await _in_process_panel(aiohttp_server, tmp_path, _ready_gateway_script)
    task = asyncio.create_task(run(config, db))
    try:
        await asyncio.wait_for(probe.started.wait(), BOUND)
        [(ctx_kwargs, _)] = made["CardContext"]
        [(interactions_kwargs, _)] = made["Interactions"]
        [(presses_kwargs, _)] = made["Presses"]
        [(bot_kwargs, _)] = made["Bot"]
        identities = [ctx_kwargs["identity"], interactions_kwargs["identity"], presses_kwargs["identity"],
                      bot_kwargs["identity"]]
        guilds = [ctx_kwargs["games_guild"], probe.poller_kwargs["games_guild"],
                  interactions_kwargs["games_guild"], presses_kwargs["games_guild"]]
        assert isinstance(identities[0], BotIdentity)
        assert all(identity is identities[0] for identity in identities)
        assert isinstance(guilds[0], GamesGuild)
        assert all(guild is guilds[0] for guild in guilds)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, BOUND)
        db.close()


async def test_purge_task_is_cancelled_when_run_is_cancelled_from_outside(
        aiohttp_server, tmp_path, monkeypatch, caplog):
    probe = _probe_purge_loop(monkeypatch)
    caplog.set_level(logging.WARNING, logger="fluxer_client.gateway")
    config, db = await _in_process_panel(aiohttp_server, tmp_path, _ready_gateway_script)
    try:
        task = asyncio.create_task(run(config, db))
        await asyncio.wait_for(probe.started.wait(), BOUND)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, BOUND)
    finally:
        db.close()
    assert probe.cancelled
    # the gateway task is finished before run returns, so the session never closes under a live reconnect
    assert _pending_gateway_tasks() == []
    assert not any("Connector is closed" in r.getMessage() for r in caplog.records)


async def test_gateway_task_is_cancelled_when_stop_fails_during_shutdown(
        aiohttp_server, tmp_path, monkeypatch):
    probe = _probe_purge_loop(monkeypatch)

    async def failing_stop(self) -> None:
        raise RuntimeError("stop failed")

    monkeypatch.setattr(panel_main.Gateway, "stop", failing_stop)
    config, db = await _in_process_panel(aiohttp_server, tmp_path, _ready_gateway_script)
    loop = asyncio.get_running_loop()
    try:
        task = asyncio.create_task(run(config, db))
        await asyncio.wait_for(probe.started.wait(), BOUND)
        os.kill(os.getpid(), signal.SIGTERM)      # run() installed the handler: this is a stop request
        with pytest.raises(RuntimeError, match="stop failed"):
            await asyncio.wait_for(task, BOUND)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        loop.remove_signal_handler(signal.SIGINT)
        db.close()
    assert probe.cancelled
    assert _pending_gateway_tasks() == []


async def test_garbage_db_exits_3_and_is_quarantined(tmp_path):
    db = tmp_path / "panel.db"
    db.write_bytes(b"not a database" * 100)
    code, out, err = await run_panel(child_env(tmp_path, DB_PATH=str(db)), tmp_path)

    assert code == 3
    assert not db.exists()
    assert (tmp_path / "panel.db.corrupt").exists()
    assert "db_quarantined" in [e["event"] for e in json_lines(out)]
    assert TOKEN not in out + err
    assert "Traceback" not in err


async def test_newer_schema_exits_3_and_leaves_file_unchanged(tmp_path):
    db = tmp_path / "panel.db"
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE future(x)")
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
    before = db.read_bytes()
    mtime = db.stat().st_mtime_ns
    code, out, err = await run_panel(child_env(tmp_path, DB_PATH=str(db)), tmp_path)

    assert code == 3
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == mtime
    assert sorted(p.name for p in tmp_path.glob("panel.db*")) == ["panel.db"]
    assert "db_schema_newer" in [e["event"] for e in json_lines(out)]
    assert TOKEN not in out + err
    assert "Traceback" not in err


async def test_db_open_failure_exits_1_with_fatal_error_not_refusal(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    code, out, err = await run_panel(child_env(tmp_path, DATA_DIR=str(blocker / "data")), tmp_path)

    assert code == 1
    assert "fatal_error" in [e["event"] for e in json_lines(out)]
    assert TOKEN not in out + err
    assert "Traceback" not in err


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions")
async def test_read_only_db_exits_3_as_unavailable_and_is_untouched(tmp_path):
    db = tmp_path / "panel.db"
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE kept(x)")
        conn.commit()
    before = db.read_bytes()
    db.chmod(0o444)
    try:
        code, out, err = await run_panel(child_env(tmp_path, DB_PATH=str(db)), tmp_path)
    finally:
        db.chmod(0o600)

    assert code == 3
    assert db.read_bytes() == before
    assert not list(tmp_path.glob("panel.db.corrupt*"))
    assert "db_unavailable" in [e["event"] for e in json_lines(out)]
    assert TOKEN not in out + err
    assert "Traceback" not in err
