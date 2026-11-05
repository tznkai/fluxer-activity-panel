# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retention purge: delete expired sessions, RSVPs, requests and play-now posts; log each attempt in a status file."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from fluxer_panel.db import Database
from fluxer_panel.fsutil import atomic_write
from fluxer_panel.logs import log_event

log = logging.getLogger(__name__)
DAY = 86400
SESSION_RETENTION = 7 * DAY
REQUEST_RETENTION = 30 * DAY
STATUS_SCHEMA_VERSION = 1

_EXPIRED_SESSIONS = """(
    (recurrence IS NULL AND ends_at < :cutoff)
    OR (recurrence IS NOT NULL AND recurrence_until IS NOT NULL AND recurrence_until < :cutoff)
)"""


@dataclass(frozen=True)
class PurgeResult:
    sessions: int
    rsvps: int
    requests: int
    play_now: int


def _purge_tx(conn: sqlite3.Connection, now: int) -> PurgeResult:
    cutoff = {"cutoff": now - SESSION_RETENTION}
    rsvps = conn.execute(
        f"SELECT count(*) FROM rsvps WHERE session_id IN (SELECT id FROM sessions WHERE {_EXPIRED_SESSIONS})",
        cutoff).fetchone()[0]
    sessions = conn.execute(f"DELETE FROM sessions WHERE {_EXPIRED_SESSIONS}", cutoff).rowcount
    requests = conn.execute("DELETE FROM requests WHERE created_at < :cutoff",
                            {"cutoff": now - REQUEST_RETENTION}).rowcount
    play_now = conn.execute("DELETE FROM play_now WHERE created_at < :cutoff",
                            {"cutoff": now - REQUEST_RETENTION}).rowcount
    return PurgeResult(sessions=sessions, rsvps=rsvps, requests=requests, play_now=play_now)


async def purge(db: Database, now: int) -> PurgeResult:
    """One transaction; strict comparisons keep a row exactly at the cutoff. RSVPs go by ON DELETE CASCADE."""
    return await db.run(lambda conn: _purge_tx(conn, now))


def _previous_success(path: Path) -> int | None:
    """The previous status file is advisory: any way it can fail to parse (even a RecursionError or MemoryError) means None."""
    try:
        value = json.loads(path.read_bytes()).get("last_success_at")
    except Exception:  # noqa: BLE001 - never let a bad status file stop the purge from being recorded
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def write_status(path: Path, *, now: int, result: PurgeResult | None) -> None:
    data = {
        "last_success_at": now if result is not None else _previous_success(path),
        "last_attempt_at": now,
        "ok": result is not None,
        "deleted": asdict(result) if result is not None else {"sessions": 0, "rsvps": 0, "requests": 0, "play_now": 0},
        "schema_version": STATUS_SCHEMA_VERSION,
    }
    atomic_write(path, (json.dumps(data, indent=1) + "\n").encode("utf-8"))


async def run_purge(db: Database, status_path: Path, now: int) -> PurgeResult | None:
    """Purge, checkpoint, record status. Never raises (except CancelledError): the bot keeps running."""
    started = time.monotonic()
    try:
        result = await purge(db, now)
        await db.checkpoint()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - any purge failure is recorded, logged and survived
        log.exception("purge_failed")
        result = None
    else:
        log_event(log, "purge_done", count=result.sessions + result.rsvps + result.requests + result.play_now,
                  duration_ms=round((time.monotonic() - started) * 1000))
    try:
        write_status(status_path, now=now, result=result)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a failed status write is logged; the purge result still stands
        log.exception("purge_status_write_failed")
    return result


async def purge_loop(db: Database, status_path: Path, interval: float,
                     clock: Callable[[], float] = time.time) -> None:
    """Purge now, then every `interval` seconds, until cancelled. An iteration that fails is logged and the loop goes on."""
    while True:
        try:
            await run_purge(db, status_path, int(clock()))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - nothing watches this task: it must never end on its own
            log.exception("purge_loop_error")
        await asyncio.sleep(interval)
