# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for fluxer_panel.purge against a real Database under tmp_path."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_panel import cards, sessions
from fluxer_panel import purge as purge_module
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.purge import PurgeResult, purge, purge_loop, run_purge, write_status
from fluxer_panel.session_post import SessionPost
from fluxer_panel.sessions import Session
from tests.fakes import FakeFluxerState, make_rest_app, wait_until

NOW = 2_000_000_000
DAY = 86400
CUT = NOW - 7 * DAY          # session cutoff
REQ_CUT = NOW - 30 * DAY     # request cutoff
ANCIENT = NOW - 400 * DAY
FAIL_TRIGGER = "CREATE TRIGGER fail_requests BEFORE DELETE ON requests BEGIN SELECT RAISE(ABORT, 'boom'); END"
DEEP_NESTED = b"[" * 100_000  # json.loads raises RecursionError on this
REST_TOKEN = "purge-test-token"
REST_URL = "https://fluxer.invalid"
ADMIN = "1001"
PROPOSER = "1002"
GUILD = "2001"
CHANNEL = "2101"
MSG = "5101"
GAMES = "3100"
STARTS = 1_800_000_000
LOCATION = "PURGE-SCRUB-LOCATION-SENTINEL"
PATCH = f"/api/v1/channels/{CHANNEL}/messages/{MSG}"


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


@pytest.fixture
async def rest_world(db: Database, aiohttp_server: Any) -> AsyncIterator[SimpleNamespace]:
    """A real SessionPost against the fake REST server, for the scrub's wire behaviour."""
    state = FakeFluxerState(bot_user_id="1900")
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    config = Config.from_env({"FLUXER_TOKEN": REST_TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": REST_URL,
                              "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES, "POLL_INTERVAL_SECONDS": "3600"})
    async with aiohttp.ClientSession() as client:
        rest = FluxerREST(client, f"{base}/api", REST_TOKEN)
        post = SessionPost(db=db, rest=rest, config=config, pacer=ChannelPacer(), backoff=DmBackoff(3600))
        yield SimpleNamespace(state=state, rest=rest, post=post)


# Seeding helpers --------------------------------------------------------------------------------------

def _card(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO cards (title, created_by, created_at, updated_at) VALUES ('Game', '1001', ?, ?)",
        (NOW, NOW))
    return cur.lastrowid


_MESSAGE_IDS = itertools.count(4001)


def _session(conn: sqlite3.Connection, card_id: int, *, closed_at: int, state: str = "ended") -> int:
    """A closed (ended or cancelled) session. It ran for an hour that ended one second before it closed."""
    cur = conn.execute(
        "INSERT INTO sessions (card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, "
        "created_at, state, closed_at) VALUES (?, '1001', '1002', ?, ?, ?, '1001', ?, ?, ?)",
        (card_id, str(next(_MESSAGE_IDS)), closed_at - 100, closed_at - 1, NOW, state, closed_at))
    return cur.lastrowid


def _weekly(conn: sqlite3.Connection, card_id: int, *, closed_at: int, next_posted: int) -> int:
    """A closed occurrence of a weekly series. Its own id is its series id, as `sessions.add` gives it."""
    cur = conn.execute(
        "INSERT INTO sessions (card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, "
        "created_at, state, closed_at, recurrence, next_posted) "
        "VALUES (?, '1001', '1002', ?, ?, ?, '1001', ?, 'ended', ?, 'weekly', ?)",
        (card_id, str(next(_MESSAGE_IDS)), closed_at - 100, closed_at - 1, NOW, closed_at, next_posted))
    session_id = cur.lastrowid
    conn.execute("UPDATE sessions SET series_id = id WHERE id = ?", (session_id,))
    return session_id


def _upcoming(conn: sqlite3.Connection, card_id: int, *, ends_at: int) -> int:
    cur = conn.execute(
        "INSERT INTO sessions (card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, "
        "created_at) VALUES (?, '1001', '1002', ?, ?, ?, '1001', ?)",
        (card_id, str(next(_MESSAGE_IDS)), ends_at - 3600, ends_at, NOW))
    return cur.lastrowid


def _rsvps(conn: sqlite3.Connection, session_id: int, *user_ids: str) -> None:
    for user_id in user_ids:
        conn.execute("INSERT INTO rsvps (session_id, user_id, choice, responded_at) VALUES (?, ?, 'going', ?)",
                     (session_id, user_id, NOW))


def _request(conn: sqlite3.Connection, card_id: int, *, status: str, created_at: int,
             requester_id: str = "1001") -> None:
    closed_at = None if status == "open" else created_at
    conn.execute("INSERT INTO requests (kind, card_id, requester_id, status, created_at, closed_at) "
                 "VALUES ('wake', ?, ?, ?, ?, ?)", (card_id, requester_id, status, created_at, closed_at))


def _play_now(conn: sqlite3.Connection, card_id: int, *, state: str, created_at: int, message_id: str) -> None:
    conn.execute("INSERT INTO play_now (card_id, guild_id, channel_id, message_id, presser_id, offers_wake, state, "
                 "created_at, expires_at) VALUES (?, '1001', '1002', ?, '1001', 0, ?, ?, ?)",
                 (card_id, message_id, state, created_at, created_at + 3600))


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _session_ids(conn: sqlite3.Connection) -> set[int]:
    return {row[0] for row in conn.execute("SELECT id FROM sessions")}


def _read_status(path: Path) -> dict | None:
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


# Test doubles and call helpers ------------------------------------------------------------------------

async def _ok(session: Session) -> None:
    """A scrub that succeeds and changes nothing."""


class _Scrubber:
    """Stands in for SessionPost.scrub. Records each session it is asked to scrub, and whether that session's row
    still existed when the call ran. With `fail`, every call raises that exception."""

    def __init__(self, db: Database, *, fail: BaseException | None = None) -> None:
        self._db = db
        self._fail = fail
        self.calls: list[int] = []
        self.present: list[bool] = []

    async def __call__(self, session: Session) -> None:
        self.calls.append(session.id)
        self.present.append(await self._db.run(lambda conn: sessions.get(conn, session.id)) is not None)
        if self._fail is not None:
            raise self._fail


async def _purge(db: Database, now: int = NOW, *, scrub: Any = _ok, retention: int = 7 * DAY) -> PurgeResult:
    return await purge(db, now, scrub=scrub, retention=retention)


async def _run(db: Database, status: Path, now: int = NOW, *, scrub: Any = _ok) -> PurgeResult | None:
    return await run_purge(db, status, now, scrub=scrub)


# AC6.1 ------------------------------------------------------------------------------------------------

async def test_expired_session_and_its_rsvps_are_deleted(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        _rsvps(conn, _session(conn, _card(conn), closed_at=CUT - 1), "1001", "1002")

    await db.run(seed)

    result = await _purge(db, NOW)

    assert result == PurgeResult(sessions=1, rsvps=2, requests=0, play_now=0)
    assert await db.run(lambda c: _count(c, "sessions")) == 0
    assert await db.run(lambda c: _count(c, "rsvps")) == 0


async def test_session_at_or_after_the_cutoff_is_kept(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        card = _card(conn)
        _rsvps(conn, _session(conn, card, closed_at=CUT), "1001", "1002")
        _rsvps(conn, _session(conn, card, closed_at=CUT + 1), "1003")

    await db.run(seed)

    result = await _purge(db, NOW)

    assert result == PurgeResult(sessions=0, rsvps=0, requests=0, play_now=0)
    assert await db.run(lambda c: _count(c, "sessions")) == 2
    assert await db.run(lambda c: _count(c, "rsvps")) == 3


# AC6.2 ------------------------------------------------------------------------------------------------

async def test_upcoming_session_is_never_deleted_however_old_its_end(db: Database) -> None:
    session_id = await db.run(lambda conn: _upcoming(conn, _card(conn), ends_at=ANCIENT))

    result = await _purge(db, NOW)

    assert result == PurgeResult(sessions=0, rsvps=0, requests=0, play_now=0)
    assert await db.run(_session_ids) == {session_id}


async def test_cancelled_session_expires_by_its_close_time_like_an_ended_one(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> dict[str, int]:
        card = _card(conn)
        return {
            "old": _session(conn, card, closed_at=CUT - 1, state="cancelled"),
            "young": _session(conn, card, closed_at=CUT + 1, state="cancelled"),
        }

    ids = await db.run(seed)

    result = await _purge(db, NOW)

    assert result == PurgeResult(sessions=1, rsvps=0, requests=0, play_now=0)
    assert await db.run(_session_ids) == {ids["young"]}


# AC6.3 ------------------------------------------------------------------------------------------------

async def test_old_requests_are_deleted_whatever_their_status(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        card = _card(conn)
        for index, status in enumerate(("open", "ready", "expired", "cleared")):
            _request(conn, card, status=status, created_at=REQ_CUT - 1, requester_id=f"100{index}")
        _request(conn, card, status="open", created_at=REQ_CUT, requester_id="1005")
        _request(conn, card, status="open", created_at=NOW, requester_id="1006")

    await db.run(seed)

    result = await _purge(db, NOW)

    assert result == PurgeResult(sessions=0, rsvps=0, requests=4, play_now=0)
    assert await db.run(lambda c: _count(c, "requests")) == 2


async def test_old_play_now_posts_are_deleted_in_either_state_and_young_ones_kept(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        card = _card(conn)
        _play_now(conn, card, state="live", created_at=REQ_CUT - 1, message_id="5001")
        _play_now(conn, card, state="expired", created_at=REQ_CUT - 1, message_id="5002")
        _play_now(conn, card, state="live", created_at=REQ_CUT, message_id="5003")
        _play_now(conn, card, state="expired", created_at=NOW, message_id="5004")

    await db.run(seed)

    result = await _purge(db, NOW)

    assert result == PurgeResult(sessions=0, rsvps=0, requests=0, play_now=2)
    assert await db.run(lambda c: _count(c, "play_now")) == 2


# AC6.4 ------------------------------------------------------------------------------------------------

async def test_run_purge_writes_a_valid_status_file(db: Database, status: Path) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        _rsvps(conn, _session(conn, _card(conn), closed_at=CUT - 1), "1001")
        _request(conn, _card(conn), status="cleared", created_at=REQ_CUT - 1)

    await db.run(seed)

    result = await _run(db, status, NOW)

    assert result == PurgeResult(sessions=1, rsvps=1, requests=1, play_now=0)
    data = json.loads(status.read_text("utf-8"))
    assert data == {
        "last_success_at": NOW,
        "last_attempt_at": NOW,
        "ok": True,
        "deleted": {"sessions": 1, "rsvps": 1, "requests": 1, "play_now": 0},
        "sessions_deferred": 0,
        "schema_version": 2,
    }


async def test_status_file_mode_is_world_readable(db: Database, status: Path) -> None:
    await _run(db, status, NOW)

    assert status.stat().st_mode & 0o777 == 0o644


async def test_successful_run_logs_purge_done_with_count_and_duration(
        db: Database, status: Path, caplog: pytest.LogCaptureFixture) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        card = _card(conn)
        _rsvps(conn, _session(conn, card, closed_at=CUT - 1), "1001", "1002")
        _request(conn, card, status="cleared", created_at=REQ_CUT - 1)
        _play_now(conn, card, state="expired", created_at=REQ_CUT - 1, message_id="5001")

    await db.run(seed)
    caplog.set_level(logging.INFO, logger="fluxer_panel.purge")

    await _run(db, status, NOW)

    done = [r for r in caplog.records if r.getMessage() == "purge_done"]
    assert len(done) == 1
    fields = done[0].panel_fields  # type: ignore[attr-defined]
    assert fields["count"] == 5          # 1 session + 2 rsvps + 1 request + 1 play_now
    assert isinstance(fields["duration_ms"], int)


async def test_failed_status_write_keeps_the_previous_file_and_never_raises(
        db: Database, status: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    await _run(db, status, NOW)
    before = status.read_bytes()

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr("fluxer_panel.fsutil.os.replace", failing_replace)
    caplog.set_level(logging.ERROR, logger="fluxer_panel.purge")

    result = await _run(db, status, NOW + 60)

    assert result == PurgeResult(sessions=0, rsvps=0, requests=0, play_now=0)
    assert status.read_bytes() == before
    assert json.loads(before)["last_attempt_at"] == NOW
    assert list(tmp_path.glob(".purge-status.json.*.tmp")) == []
    assert any(r.getMessage() == "purge_status_write_failed" for r in caplog.records)


# AC6.5 ------------------------------------------------------------------------------------------------

async def test_failing_purge_rolls_back_and_keeps_the_previous_success(
        db: Database, status: Path, caplog: pytest.LogCaptureFixture) -> None:
    await _run(db, status, NOW - 100)
    await db.run(lambda conn: _rsvps(conn, _session(conn, _card(conn), closed_at=CUT - 1), "1001", "1002"))
    await db.run(lambda conn: _request(conn, _card(conn), status="open", created_at=REQ_CUT - 1))
    await db.run(lambda conn: conn.execute(FAIL_TRIGGER))
    caplog.set_level(logging.ERROR, logger="fluxer_panel.purge")

    result = await _run(db, status, NOW)

    assert result is None
    # the session was deleted before the failing request delete: the rollback must undo it
    assert await db.run(lambda c: _count(c, "sessions")) == 1
    assert await db.run(lambda c: _count(c, "rsvps")) == 2
    assert await db.run(lambda c: _count(c, "requests")) == 1
    data = json.loads(status.read_text("utf-8"))
    assert data["ok"] is False
    assert data["last_attempt_at"] == NOW
    assert data["last_success_at"] == NOW - 100
    assert data["deleted"] == {"sessions": 0, "rsvps": 0, "requests": 0, "play_now": 0}
    assert any(r.getMessage() == "purge_failed" for r in caplog.records)


async def test_first_ever_failure_records_null_last_success(db: Database, status: Path) -> None:
    await db.run(lambda conn: _request(conn, _card(conn), status="open", created_at=REQ_CUT - 1))
    await db.run(lambda conn: conn.execute(FAIL_TRIGGER))

    assert await _run(db, status, NOW) is None

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
        await wait_until(lambda: (_read_status(status) or {}).get("last_attempt_at", 0) >= NOW + 1,
                         waiting_for="the purge status file")
        assert not task.done()
        assert _read_status(status)["ok"] is False

        await db.run(lambda conn: conn.execute("DROP TRIGGER fail_requests"))

        await wait_until(lambda: (_read_status(status) or {}).get("ok") is True, waiting_for="the purge status file")
        assert _read_status(status)["deleted"]["requests"] == 1
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_purge_loop_survives_an_iteration_that_raises(db: Database, status: Path,
                                                            monkeypatch: pytest.MonkeyPatch,
                                                            caplog: pytest.LogCaptureFixture) -> None:
    original = purge_module.run_purge
    calls = 0

    async def first_call_raises(database: Database, status_path: Path, now: int, **kwargs: Any) -> PurgeResult | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unexpected failure outside the purge")
        return await original(database, status_path, now, **kwargs)

    monkeypatch.setattr(purge_module, "run_purge", first_call_raises)
    caplog.set_level(logging.ERROR, logger="fluxer_panel.purge")
    clock = itertools.count(NOW)
    task = asyncio.create_task(purge_loop(db, status, interval=0.05, clock=lambda: next(clock)))
    try:
        await wait_until(lambda: task.done() or (_read_status(status) or {}).get("ok") is True,
                         waiting_for="the purge status file")
        assert not task.done()
        assert calls >= 2
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(r.getMessage() == "purge_loop_error" for r in caplog.records)


# AC9.4: each due session is scrubbed while its row exists, then the row is deleted. ---------------------

async def test_each_due_session_is_scrubbed_while_its_row_exists_and_then_deleted(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> tuple[int, int]:
        card = _card(conn)
        first = _session(conn, card, closed_at=CUT - 1)
        second = _session(conn, card, closed_at=CUT - 5)
        _rsvps(conn, first, "1001", "1002")
        return first, second

    first, second = await db.run(seed)
    scrub = _Scrubber(db)

    result = await _purge(db, NOW, scrub=scrub)

    assert scrub.calls == [first, second]           # once per due session, in id order
    assert scrub.present == [True, True]           # the row still existed for every edit
    assert result == PurgeResult(sessions=2, rsvps=2, requests=0, play_now=0)
    assert await db.run(_session_ids) == set()


async def test_the_scrub_edit_reaches_rest_while_the_row_exists_and_carries_no_location(
        db: Database, rest_world: SimpleNamespace, status: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def seed(conn: sqlite3.Connection) -> int:
        card = cards.create_card(conn, slug="mc", title="Lobby", author_id=ADMIN, now=1)
        cards.set_state(conn, card, "live", 1)
        session_id = sessions.add(conn, card_id=card, guild_id=GUILD, channel_id=CHANNEL, message_id=MSG,
                                  starts_at=STARTS, ends_at=STARTS + 3600, proposer_id=PROPOSER, now=1,
                                  location_text=LOCATION)
        assert sessions.end_if(conn, session_id, CUT - 1)
        return session_id

    session_id = await db.run(seed)
    row_present_at_edit: list[bool] = []
    original_edit = rest_world.rest.edit_message

    async def recording_edit(channel_id: str, message_id: str, **kwargs: Any) -> Any:
        row_present_at_edit.append(await db.run(lambda conn: sessions.get(conn, session_id)) is not None)
        return await original_edit(channel_id, message_id, **kwargs)

    monkeypatch.setattr(rest_world.rest, "edit_message", recording_edit)

    await _run(db, status, NOW, scrub=rest_world.post.scrub)

    assert row_present_at_edit == [True]
    [patch] = [r for r in rest_world.state.requests if r.method == "PATCH"]
    assert patch.path == PATCH
    fields = patch.payload["embeds"][0]["fields"]
    assert "Location" not in [f["name"] for f in fields]
    assert LOCATION not in json.dumps(patch.payload)
    assert ("Status", "Ended") in [(f["name"], f["value"]) for f in fields]
    assert await db.run(lambda conn: sessions.get(conn, session_id)) is None


# AC9.5: a failed scrub is logged with the session id, and the rows are deleted anyway. ----------------

@pytest.mark.parametrize(("failure", "expected_status"), [
    (FluxerHTTPError(404, "UNKNOWN_MESSAGE", ""), 404),
    (FluxerHTTPError(403, None, ""), 403),
    (RuntimeError(), None),
], ids=["http-404", "http-403", "other-error"])
async def test_a_failed_scrub_is_logged_and_the_rows_are_deleted_anyway(
        db: Database, failure: BaseException, expected_status: int | None,
        caplog: pytest.LogCaptureFixture) -> None:
    def seed(conn: sqlite3.Connection) -> int:
        session_id = _session(conn, _card(conn), closed_at=CUT - 1)
        _rsvps(conn, session_id, "1001")
        return session_id

    session_id = await db.run(seed)
    caplog.set_level(logging.WARNING, logger="fluxer_panel.purge")

    result = await _purge(db, NOW, scrub=_Scrubber(db, fail=failure))

    assert result == PurgeResult(sessions=1, rsvps=1, requests=0, play_now=0)
    assert await db.run(_session_ids) == set()
    [record] = [r for r in caplog.records if r.getMessage() == "session_scrub_failed"]
    fields = getattr(record, FIELDS_ATTR)
    assert fields["session_id"] == session_id
    assert fields["reason"] == type(failure).__name__
    assert fields.get("status") == expected_status


async def test_a_session_that_changes_before_its_delete_runs_is_kept(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> tuple[int, int]:
        card = _card(conn)
        reopened = _session(conn, card, closed_at=CUT - 1)
        deleted = _session(conn, card, closed_at=CUT - 2)
        _rsvps(conn, reopened, "1001")
        return reopened, deleted

    reopened, deleted = await db.run(seed)

    async def move_close_time_past_the_cutoff(session: Session) -> None:
        if session.id == reopened:
            await db.run(lambda conn: conn.execute("UPDATE sessions SET closed_at = ? WHERE id = ?", (NOW, reopened)))

    result = await _purge(db, NOW, scrub=move_close_time_past_the_cutoff)

    # the delete re-checks each row, so the row that is no longer due survives with its RSVP
    assert result == PurgeResult(sessions=1, rsvps=0, requests=0, play_now=0)
    assert await db.run(_session_ids) == {reopened}
    assert await db.run(lambda conn: _count(conn, "rsvps")) == 1


# Shutdown: a cancel during a scrub stops the purge. It is not a failed scrub, so nothing is deleted. ----

async def test_a_cancel_raised_by_a_scrub_propagates_and_the_next_session_is_not_scrubbed(
        db: Database, caplog: pytest.LogCaptureFixture) -> None:
    def seed(conn: sqlite3.Connection) -> tuple[int, int]:
        card = _card(conn)
        return _session(conn, card, closed_at=CUT - 1), _session(conn, card, closed_at=CUT - 2)

    first, second = await db.run(seed)
    scrub = _Scrubber(db, fail=asyncio.CancelledError())
    caplog.set_level(logging.WARNING, logger="fluxer_panel.purge")

    with pytest.raises(asyncio.CancelledError):
        await _purge(db, NOW, scrub=scrub)

    assert scrub.calls == [first]                  # the cancel stops the loop before the second session
    assert await db.run(_session_ids) == {first, second}
    assert not [r for r in caplog.records if r.getMessage() == "session_scrub_failed"]


async def test_a_task_cancelled_during_a_scrub_ends_run_purge_without_deleting_or_recording(
        db: Database, status: Path, caplog: pytest.LogCaptureFixture) -> None:
    session_id = await db.run(lambda conn: _session(conn, _card(conn), closed_at=CUT - 1))
    in_scrub = asyncio.Event()
    never_released = asyncio.Event()
    scrub_calls: list[int] = []

    async def blocking_scrub(session: Session) -> None:
        scrub_calls.append(session.id)
        in_scrub.set()
        await never_released.wait()                # blocks until the task is cancelled

    caplog.set_level(logging.WARNING, logger="fluxer_panel.purge")
    task = asyncio.create_task(run_purge(db, status, NOW, scrub=blocking_scrub))
    await asyncio.wait_for(in_scrub.wait(), 5)     # the scrub is running; the cancel lands inside it
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert scrub_calls == [session_id]             # no scrub after the cancelled one
    assert await db.run(_session_ids) == {session_id}
    assert not status.exists()                     # the run ended before its status write
    assert not [r for r in caplog.records if r.getMessage() == "session_scrub_failed"]


# AC9.6 (purge subcommand): with no scrubber, due sessions stay and are reported as deferred. ----------

async def test_without_a_scrubber_due_sessions_stay_and_are_counted_as_deferred(db: Database, status: Path) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        card = _card(conn)
        _rsvps(conn, _session(conn, card, closed_at=CUT - 1), "1001")
        _session(conn, card, closed_at=CUT - 5)
        _request(conn, card, status="cleared", created_at=REQ_CUT - 1)
        _play_now(conn, card, state="expired", created_at=REQ_CUT - 1, message_id="5001")

    await db.run(seed)

    result = await run_purge(db, status, NOW)

    assert result == PurgeResult(sessions=0, rsvps=0, requests=1, play_now=1, sessions_deferred=2)
    assert await db.run(lambda c: _count(c, "sessions")) == 2
    assert await db.run(lambda c: _count(c, "rsvps")) == 1
    assert _read_status(status) == {
        "last_success_at": NOW,
        "last_attempt_at": NOW,
        "ok": True,
        "deleted": {"sessions": 0, "rsvps": 0, "requests": 1, "play_now": 1},
        "sessions_deferred": 2,
        "schema_version": 2,
    }


async def test_purge_loop_passes_the_scrubber_and_the_retention_to_each_run(db: Database, status: Path) -> None:
    def seed(conn: sqlite3.Connection) -> tuple[int, int]:
        card = _card(conn)
        return _session(conn, card, closed_at=NOW - 61), _session(conn, card, closed_at=NOW - 60)

    due, at_cutoff = await db.run(seed)
    scrub = _Scrubber(db)
    task = asyncio.create_task(purge_loop(db, status, interval=60, scrub=scrub, retention=60, clock=lambda: NOW))
    try:
        await wait_until(lambda: (_read_status(status) or {}).get("ok") is True, waiting_for="the purge status file")
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert scrub.calls == [due]                    # 61 s closed is due under a 60 s retention; 60 s is not
    assert await db.run(_session_ids) == {at_cutoff}


# AC9.7: the cutoff is strict, the retention comes from the argument, and an upcoming session is never scrubbed. --

async def test_a_session_closed_exactly_at_the_cutoff_is_kept_and_not_scrubbed(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> tuple[int, int]:
        card = _card(conn)
        at_cutoff = _session(conn, card, closed_at=CUT)
        before_cutoff = _session(conn, card, closed_at=CUT - 1)
        return at_cutoff, before_cutoff

    at_cutoff, before_cutoff = await db.run(seed)
    scrub = _Scrubber(db)

    result = await _purge(db, NOW, scrub=scrub)

    assert scrub.calls == [before_cutoff]
    assert result.sessions == 1
    assert await db.run(_session_ids) == {at_cutoff}


async def test_an_upcoming_session_is_never_scrubbed(db: Database) -> None:
    session_id = await db.run(lambda conn: _upcoming(conn, _card(conn), ends_at=ANCIENT))
    scrub = _Scrubber(db)

    await _purge(db, NOW, scrub=scrub)

    assert scrub.calls == []
    assert await db.run(_session_ids) == {session_id}


async def test_the_retention_comes_from_the_argument(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> tuple[int, int]:
        card = _card(conn)
        due = conn.execute("INSERT INTO sessions (card_id, guild_id, channel_id, message_id, starts_at, ends_at, "
                           "proposer_id, created_at, state, closed_at) "
                           "VALUES (?, '1001', '1002', '6101', ?, ?, '1001', ?, 'ended', ?)",
                           (card, NOW - 200, NOW - 100, NOW, NOW - 61)).lastrowid
        at_cutoff = conn.execute("INSERT INTO sessions (card_id, guild_id, channel_id, message_id, starts_at, ends_at, "
                                 "proposer_id, created_at, state, closed_at) "
                                 "VALUES (?, '1001', '1002', '6102', ?, ?, '1001', ?, 'ended', ?)",
                                 (card, NOW - 200, NOW - 100, NOW, NOW - 60)).lastrowid
        return due, at_cutoff

    due, at_cutoff = await db.run(seed)

    result = await _purge(db, NOW, retention=60)

    assert result.sessions == 1
    assert await db.run(_session_ids) == {at_cutoff}
    assert due not in await db.run(_session_ids)


# AC9.4 for the weekly series: an occurrence whose successor is not posted yet is kept until it is. -------

async def test_a_closed_weekly_occurrence_waits_for_its_unposted_successor(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> int:
        return _weekly(conn, _card(conn), closed_at=CUT - 1, next_posted=0)

    occurrence = await db.run(seed)
    scrub = _Scrubber(db)

    assert await _purge(db, NOW, scrub=scrub) == PurgeResult(sessions=0, rsvps=0, requests=0, play_now=0)
    assert scrub.calls == []
    assert await db.run(_session_ids) == {occurrence}

    assert await db.run(lambda conn: sessions.mark_next_posted_if(conn, occurrence)) is True

    assert await _purge(db, NOW, scrub=scrub) == PurgeResult(sessions=1, rsvps=0, requests=0, play_now=0)
    assert scrub.calls == [occurrence]
    assert await db.run(_session_ids) == set()


async def test_a_closed_occurrence_of_a_stopped_series_is_purged_normally(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> int:
        occurrence = _weekly(conn, _card(conn), closed_at=CUT - 1, next_posted=0)
        conn.execute("UPDATE sessions SET recurrence = NULL WHERE id = ?", (occurrence,))
        return occurrence

    occurrence = await db.run(seed)

    result = await _purge(db, NOW)

    assert result.sessions == 1
    assert await db.run(_session_ids) == set()
    assert occurrence not in await db.run(_session_ids)


# AC9.8: request and play-now retention are unchanged by the session scrub. ----------------------------

async def test_old_requests_and_play_now_are_purged_without_a_session_due(db: Database) -> None:
    def seed(conn: sqlite3.Connection) -> None:
        card = _card(conn)
        _request(conn, card, status="cleared", created_at=REQ_CUT - 1)
        _play_now(conn, card, state="live", created_at=REQ_CUT - 1, message_id="5001")

    await db.run(seed)

    assert await _purge(db, NOW) == PurgeResult(sessions=0, rsvps=0, requests=1, play_now=1)


# Status file edge cases -------------------------------------------------------------------------------

@pytest.mark.parametrize("previous", [
    b"not json at all",
    b"\x80\x81\x82",                      # not UTF-8
    b"[1, 2]",                            # a list has no .get
    b'"a string"',
    b"null",
    b"42",
    b'{"last_success_at": true}',         # a bool is not a timestamp
    b'{"last_success_at": 1.5}',          # a float is not a timestamp
    b'{"last_success_at": "1767225600"}',
    b'{"last_success_at": null}',
    DEEP_NESTED,                          # RecursionError while decoding
], ids=["non-json", "non-utf8", "list", "string", "null", "number", "bool", "float", "text-timestamp",
        "explicit-null", "deeply-nested"])
def test_write_status_treats_a_malformed_previous_file_as_no_success(tmp_path: Path, previous: bytes) -> None:
    path = tmp_path / "purge-status.json"
    path.write_bytes(previous)

    write_status(path, now=NOW, result=None)

    data = json.loads(path.read_text("utf-8"))
    assert data["last_success_at"] is None
    assert data["ok"] is False
    assert data["last_attempt_at"] == NOW
    assert data["deleted"] == {"sessions": 0, "rsvps": 0, "requests": 0, "play_now": 0}


async def test_deeply_nested_previous_status_does_not_stop_a_failed_run(db: Database, status: Path) -> None:
    status.write_bytes(DEEP_NESTED)
    await db.run(lambda conn: _request(conn, _card(conn), status="open", created_at=REQ_CUT - 1))
    await db.run(lambda conn: conn.execute(FAIL_TRIGGER))

    assert await _run(db, status, NOW) is None

    data = json.loads(status.read_text("utf-8"))
    assert data["ok"] is False
    assert data["last_success_at"] is None


async def test_any_status_write_failure_is_logged_and_survived(db: Database, status: Path,
                                                               monkeypatch: pytest.MonkeyPatch,
                                                               caplog: pytest.LogCaptureFixture) -> None:
    def broken_write(path: Path, *, now: int, result: PurgeResult | None) -> None:
        raise RuntimeError("not an OSError")

    monkeypatch.setattr(purge_module, "write_status", broken_write)
    caplog.set_level(logging.ERROR, logger="fluxer_panel.purge")

    assert await _run(db, status, NOW) == PurgeResult(sessions=0, rsvps=0, requests=0, play_now=0)
    assert any(r.getMessage() == "purge_status_write_failed" for r in caplog.records)


async def test_checkpoint_failure_after_commit_reports_not_ok_and_keeps_the_rows_deleted(
        db: Database, status: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await _run(db, status, NOW - 100)
    await db.run(lambda conn: _rsvps(conn, _session(conn, _card(conn), closed_at=CUT - 1), "1001", "1002"))

    async def broken_checkpoint() -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "checkpoint", broken_checkpoint)

    assert await _run(db, status, NOW) is None

    assert json.loads(status.read_text("utf-8")) == {
        "last_success_at": NOW - 100,
        "last_attempt_at": NOW,
        "ok": False,
        "deleted": {"sessions": 0, "rsvps": 0, "requests": 0, "play_now": 0},
        "sessions_deferred": 0,
        "schema_version": 2,
    }
    # the commit happened before the checkpoint failed: the expired rows are gone
    assert await db.run(lambda c: _count(c, "sessions")) == 0
    assert await db.run(lambda c: _count(c, "rsvps")) == 0
