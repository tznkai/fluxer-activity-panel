# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The panel database: one serialised SQLite connection, safe startup (quarantine, refusal, backup) and migrations."""

from __future__ import annotations

import asyncio
import contextlib
import glob
import importlib.resources
import logging
import os
import re
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fluxer_panel.fsutil import fsync_dir, next_free_path, quarantine_target
from fluxer_panel.logs import log_event

log = logging.getLogger(__name__)
_MIGRATION = re.compile(r"(\d{4})_[a-z0-9_]+\.sql")
SIDE_FILES = ("-wal", "-shm", "-journal")
# How long a connection waits for another connection's write lock before the panel refuses (db_unavailable).
BUSY_TIMEOUT_SECONDS = 5.0


class DatabaseRefused(Exception):
    """Startup must stop with exit 3. `reason` is 'quarantined', 'schema_newer' or 'unavailable'; no path in the text."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"database refused: {reason}")
        self.reason = reason


def load_migrations() -> list[tuple[int, str]]:
    """[(version, sql)] sorted; versions must be 1..N with no gaps (raises RuntimeError otherwise)."""
    folder = importlib.resources.files("fluxer_panel") / "migrations"
    found = []
    for entry in folder.iterdir():
        m = _MIGRATION.fullmatch(entry.name)
        if m:
            found.append((int(m.group(1)), entry.read_text("utf-8")))
    found.sort()
    if [v for v, _ in found] != list(range(1, len(found) + 1)):
        raise RuntimeError("migration versions must be contiguous from 1")
    return found


LATEST_VERSION = len(load_migrations())


def _integrity_ok(conn: sqlite3.Connection) -> bool:
    return conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]


_CORRUPT_CODES = frozenset({sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CORRUPT})
# Primary codes that mean "this process cannot use the file right now", not "the file is damaged".
_UNAVAILABLE_CODES = frozenset({sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CANTOPEN,
                                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_PERM})


def _primary_code(exc: sqlite3.Error) -> int:
    # compare the primary code: extended codes such as SQLITE_CORRUPT_INDEX (779) & 0xFF == SQLITE_CORRUPT
    return getattr(exc, "sqlite_errorcode", 0) & 0xFF


def _unavailable(exc: sqlite3.Error) -> DatabaseRefused:
    """Log db_unavailable with the SQLite error name (no path, no message) and build the refusal."""
    log_event(log, "db_unavailable", level=logging.ERROR, code=getattr(exc, "sqlite_errorname", "unknown"))
    return DatabaseRefused("unavailable")


def _inspect(path: Path) -> tuple[bool, int]:
    """(healthy, user_version) from a read-only connection; never modifies the file.

    Only real corruption (integrity not ok, SQLITE_NOTADB, SQLITE_CORRUPT) returns healthy=False and leads to
    quarantine. Any other SQLite error (permissions, locked, a hot rollback journal that needs a write to roll
    back) raises DatabaseRefused("unavailable") and leaves the file where it is.
    """
    try:
        with contextlib.closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            if not _integrity_ok(conn):
                return False, 0
            return True, conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as exc:
        if _primary_code(exc) in _CORRUPT_CODES:
            return False, 0
        raise _unavailable(exc) from None


def _write_probe(conn: sqlite3.Connection) -> None:
    """Prove the file can be written and its write lock taken, without changing anything.

    A file that is not writable opens silently read-only, and BEGIN IMMEDIATE alone can still succeed on it,
    so the probe attempts a write (the same user_version it read) and always rolls back. Nothing is committed,
    so neither the DB file nor the WAL changes.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.execute(f"PRAGMA user_version = {int(version)}")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")


def _quarantine(path: Path) -> None:
    target = quarantine_target(path)
    os.replace(path, target)
    for side in SIDE_FILES:
        extra = path.with_name(path.name + side)
        if extra.exists():
            os.replace(extra, target.with_name(target.name + side))
    fsync_dir(path.parent)


class Database:
    def __init__(self, conn: sqlite3.Connection, executor: ThreadPoolExecutor) -> None:
        self._conn = conn
        self._executor = executor

    @classmethod
    def open(cls, path: Path) -> Database:
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        version = 0
        if not existed and any(path.parent.glob(glob.escape(path.name) + ".corrupt*")):
            # a previous start quarantined the DB: never silently replace it with an empty one
            log_event(log, "db_quarantine_pending", level=logging.ERROR)
            raise DatabaseRefused("quarantined")
        if existed:
            healthy, version = _inspect(path)
            if not healthy:
                _quarantine(path)
                log_event(log, "db_quarantined", level=logging.ERROR)
                raise DatabaseRefused("quarantined")
            if version > LATEST_VERSION:
                log_event(log, "db_schema_newer", level=logging.ERROR, count=version)
                raise DatabaseRefused("schema_newer")
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="panel-db")
        try:
            conn = executor.submit(cls._connect, path).result()
        except BaseException as exc:
            executor.shutdown(wait=True)
            if isinstance(exc, sqlite3.Error) and _primary_code(exc) in _UNAVAILABLE_CODES:
                raise _unavailable(exc) from None
            raise
        db = cls(conn, executor)
        try:
            executor.submit(db._migrate, path, version, existed).result()
        except BaseException as exc:
            db.close()
            if isinstance(exc, sqlite3.Error) and _primary_code(exc) in _UNAVAILABLE_CODES:
                raise _unavailable(exc) from None
            raise
        return db

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=BUSY_TIMEOUT_SECONDS)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA secure_delete = ON")
            _write_probe(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    def _migrate(self, path: Path, current: int, existed: bool) -> None:
        pending = [(v, sql) for v, sql in load_migrations() if v > current]
        if not pending:
            return
        if existed:
            backup_path = next_free_path(path, f".pre-v{pending[0][0]}")
            dest = sqlite3.connect(backup_path)
            try:
                self._conn.backup(dest)
            finally:
                dest.close()
            fsync_dir(path.parent)      # the backup's directory entry must survive a crash before the migration commits
            log_event(log, "db_backup_written", count=pending[0][0])
        for version, sql in pending:
            try:
                self._conn.executescript(f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise
            log_event(log, "db_migrated", count=version)

    async def run[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run fn(conn) in one transaction on the DB thread; commit on return, roll back on any exception."""
        return await asyncio.get_running_loop().run_in_executor(self._executor, self._transaction, fn)

    def _transaction[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(self._conn)
            self._conn.execute("COMMIT")
        except BaseException:
            # SQLite may already have rolled back; an unconditional ROLLBACK would mask the real error,
            # and a failed COMMIT must not leave the connection stuck inside a transaction
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise
        return result

    async def checkpoint(self) -> None:
        """Flush and truncate the WAL (outside any transaction) so deleted rows don't linger in it."""
        await asyncio.get_running_loop().run_in_executor(
            self._executor, lambda: self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall())

    def close(self) -> None:
        """Waits for the running job (a transaction finishes or rolls back), then closes the connection."""
        self._executor.submit(self._conn.close).result()
        self._executor.shutdown(wait=True)
