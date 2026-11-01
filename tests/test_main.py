# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Process-level behaviour of `python -m fluxer_panel` against in-test fakes.

The child env is built from scratch and never inherits os.environ, so no real token can leak in.
The child runs with cwd=tmp_path. Every wait is bounded, and a child still alive at the end is killed.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from fluxer_panel.__main__ import IDENTIFY_PROPERTIES, purge_once, run
from fluxer_panel.config import Config
from fluxer_panel.db import LATEST_VERSION, Database
from tests.fakes import (
    GATEWAY_STATE,
    FakeFluxerState,
    expect,
    hold,
    make_gateway_app,
    make_rest_app,
    send,
)

TOKEN = "SENTINEL-TOKEN-7f3a9c"
BOUND = 30  # seconds: generous for a cold interpreter, still a hard stop
ADMIN_DEFAULT = "1001"


def child_env(tmp_path: Path, **overrides: str | None) -> dict[str, str]:
    """A minimal env built from scratch. A value of None removes the key."""
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "FLUXER_TOKEN": TOKEN,
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
                    FLUXER_GATEWAY_URL=gateway_url)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "fluxer_panel", env=env, cwd=str(tmp_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    seen: list[str] = []
    try:
        assert proc.stdout is not None
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), BOUND)
            if not line:
                raise AssertionError(f"exited before ready: {seen}")
            seen.append(line.decode())
            if json.loads(seen[-1]).get("event") == "ready":
                break
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
    assert "shutting_down" in [e["event"] for e in json_lines(out)]
    assert "purge_done" in [e["event"] for e in json_lines(out)]
    assert TOKEN not in out
    assert TOKEN not in err.decode()
    assert "Traceback" not in err.decode()
    with contextlib.closing(sqlite3.connect(tmp_path / "data" / "panel.db")) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
    gateway_state = gateway_app[GATEWAY_STATE]
    assert gateway_state.errors == []
    [identify] = [f for f in gateway_state.connections[0] if f.get("op") == 2]
    assert identify["d"]["properties"] == IDENTIFY_PROPERTIES


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
    card = conn.execute("INSERT INTO cards (kind, title, created_by, created_at, updated_at) "
                        "VALUES ('online', 'Game', '1001', ?, ?)", (now, now)).lastrowid
    expired = conn.execute(
        "INSERT INTO sessions (card_id, group_role_id, starts_at, ends_at, proposer_id, created_at) "
        "VALUES (?, '2001', ?, ?, '1001', ?)", (card, now - 9 * DAY, now - 8 * DAY, now - 9 * DAY)).lastrowid
    kept = conn.execute(
        "INSERT INTO sessions (card_id, group_role_id, starts_at, ends_at, proposer_id, created_at) "
        "VALUES (?, '2001', ?, ?, '1001', ?)", (card, now, now + DAY, now)).lastrowid
    for session_id in (expired, kept):
        conn.execute("INSERT INTO rsvps (session_id, user_id, status, responded_at) VALUES (?, '5000', 'yes', ?)",
                     (session_id, now))
    conn.execute("INSERT INTO requests (kind, card_id, requester_id, status, created_at) "
                 "VALUES ('wake', ?, '5000', 'done', ?)", (card, now - 31 * DAY))
    conn.execute("INSERT INTO requests (kind, card_id, requester_id, status, created_at) "
                 "VALUES ('wake', ?, '5000', 'open', ?)", (card, now))


async def test_purge_subcommand_runs_once_and_exits_zero(tmp_path):
    now = int(time.time())
    db = Database.open(tmp_path / "panel.db")
    try:
        await db.run(lambda conn: _seed_purge_rows(conn, now))
    finally:
        db.close()

    code, out, err = await run_panel(child_env(tmp_path, DATA_DIR=str(tmp_path)), tmp_path, "purge")

    assert code == 0
    status = json.loads((tmp_path / "purge-status.json").read_text())
    assert status["ok"] is True
    assert status["deleted"] == {"sessions": 1, "rsvps": 1, "requests": 1}
    assert "purge_done" in [e["event"] for e in json_lines(out)]
    assert TOKEN not in out + err
    with contextlib.closing(sqlite3.connect(tmp_path / "panel.db")) as conn:
        counts = [conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("sessions", "rsvps", "requests")]
    assert counts == [1, 1, 1]      # only the expired rows went; the kept session, RSVP and request remain


def test_purge_once_exits_1_when_the_purge_fails(tmp_path):
    db = Database.open(tmp_path / "panel.db")
    # break the purge's query without touching the panel's own connection
    with contextlib.closing(sqlite3.connect(tmp_path / "panel.db")) as conn:
        conn.execute("DROP TABLE rsvps")
        conn.commit()
    config = Config.from_env({"FLUXER_TOKEN": TOKEN, "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
                              "ADMIN_USER_IDS": ADMIN_DEFAULT, "DATA_DIR": str(tmp_path)})
    try:
        assert purge_once(config, db) == 1
    finally:
        db.close()
    assert json.loads((tmp_path / "purge-status.json").read_text())["ok"] is False


class _PurgeProbe:
    """Stands in for purge_loop: records that it started and whether it was cancelled."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False


def _probe_purge_loop(monkeypatch: pytest.MonkeyPatch) -> _PurgeProbe:
    probe = _PurgeProbe()

    async def fake_purge_loop(db, status_path, interval, clock=None):
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
    config = Config.from_env({"FLUXER_TOKEN": TOKEN, "FLUXER_API_URL": str(rest.make_url("/api")).rstrip("/"),
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


def _pending_gateway_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if getattr(t.get_coro(), "__qualname__", "") == "Gateway.run"]


async def _ready_gateway_script(ws, index, state):
    await send(ws, 10, {"heartbeat_interval": 30000})
    await expect(ws, 2)
    await send(ws, 0, {"session_id": "S1", "user": {"id": "5000"}}, t="READY", s=1)
    await hold(ws)


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
