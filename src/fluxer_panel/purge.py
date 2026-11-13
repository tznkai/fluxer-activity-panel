# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retention purge: scrub and delete expired sessions, delete old requests, play-now posts and closed polls; log each
attempt in a status file."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from fluxer_panel import sessions
from fluxer_panel.db import Database
from fluxer_panel.fsutil import atomic_write
from fluxer_panel.logs import log_event
from fluxer_panel.sessions import Session

log = logging.getLogger(__name__)
DAY = 86400
SESSION_RETENTION = 7 * DAY
REQUEST_RETENTION = 30 * DAY
STATUS_SCHEMA_VERSION = 2
Scrub = Callable[[Session], Awaitable[None]]   # raises on failure; the caller logs and continues


@dataclass(frozen=True)
class PurgeResult:
    sessions: int
    rsvps: int
    requests: int
    play_now: int
    sessions_deferred: int = 0
    polls: int = 0


_NOTHING = PurgeResult(sessions=0, rsvps=0, requests=0, play_now=0)


def _purge_tx(conn: sqlite3.Connection, now: int, *, session_ids: Sequence[int], cutoff: int) -> PurgeResult:
    """One transaction. The session delete re-checks each row against the due rule, so a row that changed since it
    was read is kept, and its RSVPs with it. Closed polls past retention are deleted in the same transaction; their
    options, ballots and votes go by cascade, and no REST call is made. The ids are bound as parameters; nothing is
    formatted into the SQL."""
    sessions_deleted = rsvps = 0
    if session_ids:
        marks = ", ".join(["?"] * len(session_ids))
        due = f"id IN ({marks}) AND {sessions.PURGE_DUE_SQL}"
        params = (*session_ids, cutoff)
        # counted before the delete, so the count is of the RSVPs whose session the delete removes
        rsvps = conn.execute(f"SELECT count(*) FROM rsvps WHERE session_id IN (SELECT id FROM sessions WHERE {due})",
                             params).fetchone()[0]
        sessions_deleted = conn.execute(f"DELETE FROM sessions WHERE {due}", params).rowcount
    requests = conn.execute("DELETE FROM requests WHERE created_at < :cutoff",
                            {"cutoff": now - REQUEST_RETENTION}).rowcount
    play_now = conn.execute("DELETE FROM play_now WHERE created_at < :cutoff",
                            {"cutoff": now - REQUEST_RETENTION}).rowcount
    polls_deleted = conn.execute("DELETE FROM polls WHERE state = 'closed' AND closed_at < :cutoff",
                                 {"cutoff": cutoff}).rowcount
    return PurgeResult(sessions=sessions_deleted, rsvps=rsvps, requests=requests, play_now=play_now,
                       polls=polls_deleted)


async def purge(db: Database, now: int, *, scrub: Scrub | None, retention: int) -> PurgeResult:
    """Scrub each due session, then delete the due rows. The scrub runs before the delete, so each edit is sent while
    its row exists. A failed scrub is logged and the row is still deleted. With no scrubber (the offline `purge`
    subcommand has no REST), due sessions stay and are counted as deferred. Strict comparisons keep a row exactly at
    the cutoff. RSVPs go by ON DELETE CASCADE. Closed polls past retention are deleted in the same transaction as the
    sessions, with their options, ballots and votes by cascade and no REST call, so they never reach the scrub."""
    cutoff = now - retention
    due = await db.run(lambda conn: sessions.closed_before(conn, cutoff))
    if scrub is None:
        deferred, session_ids = len(due), []
    else:
        for session in due:
            try:
                await scrub(session)
            except Exception as exc:  # noqa: BLE001 - a failed scrub is logged; the delete still runs
                log_event(log, "session_scrub_failed", level=logging.WARNING, session_id=session.id,
                          status=getattr(exc, "status", None), reason=type(exc).__name__)
        deferred, session_ids = 0, [session.id for session in due]
    result = await db.run(lambda conn: _purge_tx(conn, now, session_ids=session_ids, cutoff=cutoff))
    return replace(result, sessions_deferred=deferred)


def _previous_success(path: Path) -> int | None:
    """The previous status file is advisory: any way it can fail to parse (even a RecursionError or MemoryError) means None."""
    try:
        value = json.loads(path.read_bytes()).get("last_success_at")
    except Exception:  # noqa: BLE001 - never let a bad status file stop the purge from being recorded
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def write_status(path: Path, *, now: int, result: PurgeResult | None) -> None:
    shown = result if result is not None else _NOTHING
    data = {
        "last_success_at": now if result is not None else _previous_success(path),
        "last_attempt_at": now,
        "ok": result is not None,
        # listed key by key, so the deferred count is not reported as a deletion
        "deleted": {"sessions": shown.sessions, "rsvps": shown.rsvps, "requests": shown.requests,
                    "play_now": shown.play_now, "polls": shown.polls},
        "sessions_deferred": shown.sessions_deferred,
        "schema_version": STATUS_SCHEMA_VERSION,
    }
    atomic_write(path, (json.dumps(data, indent=1) + "\n").encode("utf-8"))


async def run_purge(db: Database, status_path: Path, now: int, *, scrub: Scrub | None = None,
                    retention: int = SESSION_RETENTION) -> PurgeResult | None:
    """Purge, checkpoint, record status. Never raises (except CancelledError): the bot keeps running."""
    started = time.monotonic()
    try:
        result = await purge(db, now, scrub=scrub, retention=retention)
        await db.checkpoint()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - any purge failure is recorded, logged and survived
        log.exception("purge_failed")
        result = None
    else:
        log_event(log, "purge_done",
                  count=result.sessions + result.rsvps + result.requests + result.play_now + result.polls,
                  duration_ms=round((time.monotonic() - started) * 1000))
    try:
        write_status(status_path, now=now, result=result)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a failed status write is logged; the purge result still stands
        log.exception("purge_status_write_failed")
    return result


async def purge_loop(db: Database, status_path: Path, interval: float, *, scrub: Scrub | None = None,
                     retention: int = SESSION_RETENTION, clock: Callable[[], float] = time.time) -> None:
    """Purge now, then every `interval` seconds, until cancelled. An iteration that fails is logged and the loop goes on."""
    while True:
        try:
            await run_purge(db, status_path, int(clock()), scrub=scrub, retention=retention)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - nothing watches this task: it must never end on its own
            log.exception("purge_loop_error")
        await asyncio.sleep(interval)
