# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for fluxer_panel.db against real SQLite files under tmp_path."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.resources
import logging
import os
import re
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import fluxer_panel.db as dbmod
from fluxer_panel.db import Database, DatabaseRefused

EXPECTED_TABLES = {
    "cards", "card_server", "card_location", "card_seats", "families",
    "family_members", "sessions", "rsvps", "requests",
}


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "d" / "panel.db")
    try:
        yield database
    finally:
        database.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


# Packaging ------------------------------------------------------------------------------------------

def test_migration_is_a_package_resource() -> None:
    folder = importlib.resources.files("fluxer_panel") / "migrations"
    assert "0001_init.sql" in {entry.name for entry in folder.iterdir()}
    assert "CREATE TABLE cards" in (folder / "0001_init.sql").read_text("utf-8")
    assert [version for version, _ in dbmod.load_migrations()] == [1]


def test_migrations_leave_transactions_and_version_to_the_runner() -> None:
    for version, sql in dbmod.load_migrations():
        assert not re.search(r"\b(BEGIN|COMMIT|ROLLBACK)\b|user_version", sql, re.IGNORECASE), version


# AC4.1 ----------------------------------------------------------------------------------------------

async def test_missing_db_is_created_at_latest_version_with_all_tables(db: Database, tmp_path: Path) -> None:
    assert (tmp_path / "d" / "panel.db").exists()
    version = await db.run(lambda c: c.execute("PRAGMA user_version").fetchone()[0])
    assert version == dbmod.LATEST_VERSION
    names = await db.run(lambda c: _tables(c))
    assert EXPECTED_TABLES <= names


async def test_new_db_has_pragmas_on(db: Database) -> None:
    assert await db.run(lambda c: c.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    assert await db.run(lambda c: c.execute("PRAGMA secure_delete").fetchone()[0]) == 1
    assert await db.run(lambda c: c.execute("PRAGMA journal_mode").fetchone()[0]) == "wal"


async def test_new_db_writes_no_backup(db: Database, tmp_path: Path) -> None:
    assert list((tmp_path / "d").glob("*.pre-v*")) == []


# AC4.2 ----------------------------------------------------------------------------------------------

def _make_legacy_v0(path: Path) -> None:
    with contextlib.closing(sqlite3.connect(path)) as c:
        c.execute("CREATE TABLE legacy(x)")
        c.execute("INSERT INTO legacy(x) VALUES ('kept-row')")
        c.commit()


async def test_older_db_is_backed_up_and_migrated_with_data_kept(tmp_path: Path) -> None:
    path = tmp_path / "panel.db"
    _make_legacy_v0(path)

    database = Database.open(path)
    try:
        backup = path.with_name(path.name + ".pre-v1")
        assert backup.exists()
        with contextlib.closing(sqlite3.connect(backup)) as b:
            assert b.execute("SELECT x FROM legacy").fetchall() == [("kept-row",)]
        assert await database.run(lambda c: c.execute("SELECT x FROM legacy").fetchall()) == [("kept-row",)]
        version = await database.run(lambda c: c.execute("PRAGMA user_version").fetchone()[0])
        assert version == dbmod.LATEST_VERSION
    finally:
        database.close()


async def test_second_migration_backs_up_to_pre_v2_and_keeps_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "panel.db"
    first = Database.open(path)
    try:
        await first.run(lambda c: c.execute("INSERT INTO families (id, created_at) VALUES (7, 1)"))
    finally:
        first.close()

    real_v1 = dbmod.load_migrations()[0][1]
    # STRICT needs a datatype on every column, so the plan's literal "extra(x)" would fail before the backup check
    monkeypatch.setattr(dbmod, "load_migrations", lambda: [(1, real_v1), (2, "CREATE TABLE extra(x TEXT) STRICT;")])
    monkeypatch.setattr(dbmod, "LATEST_VERSION", 2)

    database = Database.open(path)
    try:
        assert path.with_name(path.name + ".pre-v2").exists()
        assert await database.run(lambda c: c.execute("SELECT id FROM families").fetchall()) == [(7,)]
        assert await database.run(lambda c: c.execute("PRAGMA user_version").fetchone()[0]) == 2
        assert await database.run(lambda c: "extra" in _tables(c))
    finally:
        database.close()


async def test_failing_migration_rolls_back_and_keeps_v1_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "panel.db"
    first = Database.open(path)
    try:
        await first.run(lambda c: c.execute("INSERT INTO families (id, created_at) VALUES (7, 1)"))
    finally:
        first.close()

    real_v1 = dbmod.load_migrations()[0][1]
    monkeypatch.setattr(dbmod, "load_migrations", lambda: [(1, real_v1), (2, "CREATE TABLE bad(;")])
    monkeypatch.setattr(dbmod, "LATEST_VERSION", 2)

    with pytest.raises(sqlite3.Error):
        Database.open(path)

    with contextlib.closing(sqlite3.connect(path)) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 1
        assert c.execute("SELECT id FROM families").fetchall() == [(7,)]
        assert "bad" not in _tables(c)


# AC4.3 ----------------------------------------------------------------------------------------------

async def test_garbage_db_is_quarantined_with_side_files(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "panel.db"
    path.write_bytes(b"not a database" * 100)
    path.with_name(path.name + "-wal").write_bytes(b"wal junk")
    path.with_name(path.name + "-shm").write_bytes(b"shm junk")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(DatabaseRefused) as info:
            Database.open(path)

    assert info.value.reason == "quarantined"
    assert not path.exists()
    assert path.with_name("panel.db.corrupt").exists()
    assert path.with_name("panel.db.corrupt-wal").exists()
    assert path.with_name("panel.db.corrupt-shm").exists()
    assert not path.with_name("panel.db-wal").exists()
    records = [r for r in caplog.records if r.getMessage() == "db_quarantined"]
    assert records, "expected a db_quarantined record"
    assert all(str(tmp_path) not in r.getMessage() for r in caplog.records)


async def test_second_quarantine_uses_dot_corrupt_1_and_creates_no_db(tmp_path: Path) -> None:
    path = tmp_path / "panel.db"
    path.write_bytes(b"first garbage" * 50)
    with pytest.raises(DatabaseRefused):
        Database.open(path)

    path.write_bytes(b"second garbage" * 50)
    with pytest.raises(DatabaseRefused) as info:
        Database.open(path)

    assert info.value.reason == "quarantined"
    assert path.with_name("panel.db.corrupt.1").exists()
    assert not path.exists()


async def test_integrity_failure_is_quarantined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "panel.db"
    Database.open(path).close()
    monkeypatch.setattr(dbmod, "_integrity_ok", lambda conn: False)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(DatabaseRefused) as info:
            Database.open(path)

    assert info.value.reason == "quarantined"
    assert not path.exists()
    assert path.with_name("panel.db.corrupt").exists()
    assert any(r.getMessage() == "db_quarantined" for r in caplog.records)
    assert all(str(tmp_path) not in r.getMessage() for r in caplog.records)


async def test_no_reset_after_quarantine_while_file_is_missing(tmp_path: Path,
                                                               caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "panel.db"
    path.write_bytes(b"garbage" * 100)
    with pytest.raises(DatabaseRefused):
        Database.open(path)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(DatabaseRefused) as info:
            Database.open(path)

    assert info.value.reason == "quarantined"
    assert not path.exists()
    assert any(r.getMessage() == "db_quarantine_pending" for r in caplog.records)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions")
async def test_unreadable_db_is_unavailable_not_quarantined(tmp_path: Path,
                                                            caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "panel.db"
    Database.open(path).close()
    path.chmod(0o000)
    try:
        with caplog.at_level(logging.ERROR):
            with pytest.raises(DatabaseRefused) as info:
                Database.open(path)
        assert info.value.reason == "unavailable"
        assert path.exists()
        assert list(tmp_path.glob("panel.db.corrupt*")) == []
        assert any(r.getMessage() == "db_unavailable" for r in caplog.records)
    finally:
        path.chmod(0o600)


# AC4.4 ----------------------------------------------------------------------------------------------

async def test_newer_schema_is_refused_and_untouched(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "panel.db"
    with contextlib.closing(sqlite3.connect(path)) as c:
        c.execute("CREATE TABLE future(x)")
        c.execute("PRAGMA user_version = 99")
        c.commit()
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    with caplog.at_level(logging.ERROR):
        with pytest.raises(DatabaseRefused) as info:
            Database.open(path)

    assert info.value.reason == "schema_newer"
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime
    assert list(tmp_path.glob("panel.db.corrupt*")) == []
    assert list(tmp_path.glob("panel.db.pre-v*")) == []
    assert any(r.getMessage() == "db_schema_newer" for r in caplog.records)


# run() transaction semantics -----------------------------------------------------------------------

async def test_run_rolls_back_when_fn_raises(db: Database) -> None:
    def insert_then_fail(c: sqlite3.Connection) -> None:
        c.execute("INSERT INTO families (id, created_at) VALUES (1, 1)")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await db.run(insert_then_fail)

    assert await db.run(lambda c: c.execute("SELECT COUNT(*) FROM families").fetchone()[0]) == 0


async def test_concurrent_runs_both_complete_serialised(db: Database) -> None:
    def insert(family_id: int):
        def fn(c: sqlite3.Connection) -> int:
            c.execute("INSERT INTO families (id, created_at) VALUES (?, 1)", (family_id,))
            return family_id
        return fn

    results = await asyncio.wait_for(asyncio.gather(db.run(insert(1)), db.run(insert(2))), timeout=10)

    assert sorted(results) == [1, 2]
    assert await db.run(lambda c: c.execute("SELECT COUNT(*) FROM families").fetchone()[0]) == 2
