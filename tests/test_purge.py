# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for fluxer_panel.purge against a real Database under tmp_path."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from pathlib import Path

import pytest

from fluxer_panel.db import Database
from fluxer_panel.purge import PurgeResult, purge, purge_loop, run_purge

NOW = 2_000_000_000
DAY = 86400
CUT = NOW - 7 * DAY          # session cutoff
REQ_CUT = NOW - 30 * DAY     # request cutoff
ANCIENT = NOW - 400 * DAY
FAIL_TRIGGER = "CREATE TRIGGER fail_requests BEFORE DELETE ON requests BEGIN SELECT RAISE(ABORT, 'boom'); END"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def status(tmp_path: Path) -> Path:
    return tmp_path / "purge-status.json"


# Seeding helpers --------------------------------------------------------------------------------------

def _card(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO cards (kind, title, created_by, created_at, updated_at) VALUES ('online', 'Game', '1001', ?, ?)",
        (NOW, NOW))
    return cur.lastrowid


def _session(conn: sqlite3.Connection, card_id: int, *, ends_at: int,
             recurrence: str | None = None, recurrence_until: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO sessions (card_id, group_role_id, starts_at, ends_at, recurrence, recurrence_until, "
        "proposer_id, created_at) VALUES (?, '2001', ?, ?, ?, ?, '1001', ?)",
        (card_id, ends_at - 3600, ends_at, recurrence, recurrence_until, NOW))
    return cur.lastrowid


def _rsvps(conn: sqlite3.Connection, session_id: int, *user_ids: str) -> None:
    for user_id in user_ids:
        conn.execute("INSERT INTO rsvps (session_id, user_id, status, responded_at) VALUES (?, ?, 'yes', ?)",
                     (session_id, user_id, NOW))


def _request(conn: sqlite3.Connection, card_id: int, *, status: str, created_at: int) -> None:
    conn.execute("INSERT INTO requests (kind, card_id, requester_id, status, created_at) "
                 "VALUES ('wake', ?, '1001', ?, ?)", (card_id, status, created_at))


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _session_ids(conn: sqlite3.Connection) -> set[int]:
    return {row[0] for row in conn.execute("SELECT id FROM sessions")}


def _read_status(path: Path) -> dict | None:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


async def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the purge status file")
        await asyncio.sleep(0.01)


# AC6.1 ------------------------------------------------------------------------------------------------

async def test_expired_session_and_its_rsvps_are_deleted(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        _rsvps(conn, _session(conn, _card(conn), ends_at=CUT - 1), "1001", "1002")

    await db.run(seed)

    result = await purge(db, NOW)

    assert result == PurgeResult(sessions=1, rsvps=2, requests=0)
    assert await db.run(lambda c: _count(c, "sessions")) == 0
    assert await db.run(lambda c: _count(c, "rsvps")) == 0


async def test_session_at_or_after_the_cutoff_is_kept(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        card = _card(conn)
        _rsvps(conn, _session(conn, card, ends_at=CUT), "1001", "1002")
        _rsvps(conn, _session(conn, card, ends_at=CUT + 1), "1003")

    await db.run(seed)

    result = await purge(db, NOW)

    assert result == PurgeResult(sessions=0, rsvps=0, requests=0)
    assert await db.run(lambda c: _count(c, "sessions")) == 2
    assert await db.run(lambda c: _count(c, "rsvps")) == 3


# AC6.2 ------------------------------------------------------------------------------------------------

async def test_recurring_session_is_deleted_only_once_its_series_ended_before_the_cutoff(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> dict[str, int]:
        card = _card(conn)
        return {
            "ended_long_ago_but_series_continues": _session(
                conn, card, ends_at=ANCIENT, recurrence="weekly", recurrence_until=CUT + 10),
            "series_ended_before_cutoff": _session(
                conn, card, ends_at=ANCIENT, recurrence="weekly", recurrence_until=CUT - 1),
            "series_ends_exactly_at_cutoff": _session(
                conn, card, ends_at=ANCIENT, recurrence="weekly", recurrence_until=CUT),
            "open_ended_series": _session(conn, card, ends_at=ANCIENT, recurrence="weekly", recurrence_until=None),
        }

    ids = await db.run(seed)

    result = await purge(db, NOW)

    assert result == PurgeResult(sessions=1, rsvps=0, requests=0)
    remaining = await db.run(_session_ids)
    assert remaining == {ids["ended_long_ago_but_series_continues"], ids["series_ends_exactly_at_cutoff"],
                         ids["open_ended_series"]}


# AC6.3 ------------------------------------------------------------------------------------------------

async def test_old_requests_are_deleted_whatever_their_status(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        card = _card(conn)
        for status in ("open", "done", "declined"):
            _request(conn, card, status=status, created_at=REQ_CUT - 1)
        _request(conn, card, status="open", created_at=REQ_CUT)
        _request(conn, card, status="open", created_at=NOW)

    await db.run(seed)

    result = await purge(db, NOW)

    assert result == PurgeResult(sessions=0, rsvps=0, requests=3)
    assert await db.run(lambda c: _count(c, "requests")) == 2


# AC6.4 ------------------------------------------------------------------------------------------------

async def test_run_purge_writes_a_valid_status_file(db: Database, status: Path) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        _rsvps(conn, _session(conn, _card(conn), ends_at=CUT - 1), "1001")
        _request(conn, _card(conn), status="done", created_at=REQ_CUT - 1)

    await db.run(seed)

    result = await run_purge(db, status, NOW)

    assert result == PurgeResult(sessions=1, rsvps=1, requests=1)
    data = json.loads(status.read_text("utf-8"))
    assert data == {
        "last_success_at": NOW,
        "last_attempt_at": NOW,
        "ok": True,
        "deleted": asdict(result),
        "schema_version": 1,
    }


async def test_status_file_mode_is_world_readable(db: Database, status: Path) -> None:
    await run_purge(db, status, NOW)

    assert status.stat().st_mode & 0o777 == 0o644


async def test_successful_run_logs_purge_done_with_count_and_duration(
        db: Database, status: Path, caplog: pytest.LogCaptureFixture) -> None:
    await db.run(lambda conn: _rsvps(conn, _session(conn, _card(conn), ends_at=CUT - 1), "1001", "1002"))
    caplog.set_level(logging.INFO, logger="fluxer_panel.purge")

    await run_purge(db, status, NOW)

    done = [r for r in caplog.records if r.getMessage() == "purge_done"]
    assert len(done) == 1
    fields = done[0].panel_fields  # type: ignore[attr-defined]
    assert fields["count"] == 3          # 1 session + 2 rsvps + 0 requests
    assert isinstance(fields["duration_ms"], int)


async def test_failed_status_write_keeps_the_previous_file_and_never_raises(
        db: Database, status: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    await run_purge(db, status, NOW)
    before = status.read_bytes()

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr("fluxer_panel.fsutil.os.replace", failing_replace)
    caplog.set_level(logging.ERROR, logger="fluxer_panel.purge")

    result = await run_purge(db, status, NOW + 60)

    assert result == PurgeResult(sessions=0, rsvps=0, requests=0)
    assert status.read_bytes() == before
    assert json.loads(before)["last_attempt_at"] == NOW
    assert list(tmp_path.glob(".purge-status.json.*.tmp")) == []
    assert any(r.getMessage() == "purge_status_write_failed" for r in caplog.records)


# AC6.5 ------------------------------------------------------------------------------------------------

async def test_failing_purge_rolls_back_and_keeps_the_previous_success(
        db: Database, status: Path, caplog: pytest.LogCaptureFixture) -> None:
    await run_purge(db, status, NOW - 100)
    await db.run(lambda conn: _rsvps(conn, _session(conn, _card(conn), ends_at=CUT - 1), "1001", "1002"))
    await db.run(lambda conn: _request(conn, _card(conn), status="open", created_at=REQ_CUT - 1))
    await db.run(lambda conn: conn.execute(FAIL_TRIGGER))
    caplog.set_level(logging.ERROR, logger="fluxer_panel.purge")

    result = await run_purge(db, status, NOW)

    assert result is None
    # the session was deleted before the failing request delete: the rollback must undo it
    assert await db.run(lambda c: _count(c, "sessions")) == 1
    assert await db.run(lambda c: _count(c, "rsvps")) == 2
    assert await db.run(lambda c: _count(c, "requests")) == 1
    data = json.loads(status.read_text("utf-8"))
    assert data["ok"] is False
    assert data["last_attempt_at"] == NOW
    assert data["last_success_at"] == NOW - 100
    assert data["deleted"] == {"sessions": 0, "rsvps": 0, "requests": 0}
    assert any(r.getMessage() == "purge_failed" for r in caplog.records)


async def test_first_ever_failure_records_null_last_success(db: Database, status: Path) -> None:
    await db.run(lambda conn: _request(conn, _card(conn), status="open", created_at=REQ_CUT - 1))
    await db.run(lambda conn: conn.execute(FAIL_TRIGGER))

    assert await run_purge(db, status, NOW) is None

    data = json.loads(status.read_text("utf-8"))
    assert data["ok"] is False
    assert data["last_success_at"] is None
    assert data["last_attempt_at"] == NOW


async def test_purge_loop_keeps_running_through_failures_and_recovers(db: Database, status: Path) -> None:
    await db.run(lambda conn: _request(conn, _card(conn), status="open", created_at=REQ_CUT - 1))
    await db.run(lambda conn: conn.execute(FAIL_TRIGGER))
    clock = itertools.count(NOW)
    task = asyncio.create_task(purge_loop(db, status, interval=0.05, clock=lambda: next(clock)))
    try:
        # the loop runs on its own: two failed attempts at NOW and NOW + 1, and the task is still alive
        await _wait_until(lambda: (_read_status(status) or {}).get("last_attempt_at", 0) >= NOW + 1)
        assert not task.done()
        assert _read_status(status)["ok"] is False

        await db.run(lambda conn: conn.execute("DROP TRIGGER fail_requests"))

        await _wait_until(lambda: (_read_status(status) or {}).get("ok") is True)
        assert _read_status(status)["deleted"]["requests"] == 1
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
