# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0002 against real SQLite files: a fresh database and a v1 database that holds rows."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest

import fluxer_panel.db as dbmod
from fluxer_panel.db import Database


async def test_fresh_database_reaches_the_latest_version(tmp_path: Path) -> None:
    database = Database.open(tmp_path / "panel.db")
    try:
        version = await database.run(lambda c: c.execute("PRAGMA user_version").fetchone()[0])
    finally:
        database.close()
    assert version == dbmod.LATEST_VERSION


async def test_v1_database_with_rows_migrates_and_keeps_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "panel.db"
    real_migrations = dbmod.load_migrations()
    assert [version for version, _ in real_migrations] == [1, 2, 3, 4, 5, 6]

    # Build a genuine v1 database: only the first migration exists at this point.
    monkeypatch.setattr(dbmod, "load_migrations", lambda: real_migrations[:1])
    monkeypatch.setattr(dbmod, "LATEST_VERSION", 1)
    Database.open(path).close()
    with contextlib.closing(sqlite3.connect(path)) as c:
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("INSERT INTO families (id, created_at) VALUES (1, 1)")
        c.execute("INSERT INTO cards (kind, title, created_by, created_at, updated_at) "
                  "VALUES ('server', 'Old card', '1001', 1, 1)")
        c.execute("INSERT INTO sessions (card_id, group_role_id, starts_at, ends_at, proposer_id, created_at) "
                  "VALUES (1, '2001', 10, 20, '1001', 1)")
        c.execute("INSERT INTO rsvps (session_id, user_id, status, responded_at) VALUES (1, '1002', 'yes', 1)")
        c.execute("INSERT INTO requests (kind, card_id, requester_id, status, created_at) "
                  "VALUES ('wake', 1, '1002', 'open', 1)")
        c.commit()

    # Undo the v1 patch and reopen: the real migrations run.
    monkeypatch.undo()
    database = Database.open(path)
    try:
        assert path.with_name(path.name + ".pre-v2").exists()
        version = await database.run(lambda c: c.execute("PRAGMA user_version").fetchone()[0])
        session = await database.run(lambda c: c.execute(
            "SELECT id, card_id, group_role_id, starts_at, ends_at, channel_id, message_id FROM sessions").fetchall())
        rsvps = await database.run(lambda c: c.execute("SELECT session_id, user_id, status FROM rsvps").fetchall())
        requests = await database.run(lambda c: c.execute(
            "SELECT id, kind, card_id, requester_id, status FROM requests").fetchall())
        cards = await database.run(lambda c: c.execute("SELECT id, title, created_by FROM cards").fetchall())
    finally:
        database.close()

    assert version == dbmod.LATEST_VERSION
    assert session == [(1, 1, "2001", 10, 20, None, None)]
    assert rsvps == [(1, "1002", "yes")]
    assert requests == [(1, "wake", 1, "1002", "open")]
    assert cards == [(1, "Old card", "1001")]


async def test_v5_requests_are_rebuilt_as_closable_wake_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "panel.db"
    real_migrations = dbmod.load_migrations()

    # Build a genuine v5 database with requests in every shape the rebuild must handle.
    monkeypatch.setattr(dbmod, "load_migrations", lambda: real_migrations[:5])
    monkeypatch.setattr(dbmod, "LATEST_VERSION", 5)
    Database.open(path).close()
    with contextlib.closing(sqlite3.connect(path)) as c:
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("INSERT INTO cards (title, created_by, created_at, updated_at) VALUES ('Card', '1001', 1, 1)")
        c.executemany("INSERT INTO requests (id, kind, card_id, requester_id, status, created_at) "
                      "VALUES (?, 'wake', ?, ?, ?, ?)", [
                          (1, 1, "1001", "open", 10),       # kept: the first open row for this requester
                          (2, 1, "1002", "done", 20),       # becomes cleared, closed at its created_at
                          (3, 1, "1003", "declined", 30),   # becomes cleared, closed at its created_at
                          (4, None, "1004", "open", 40),    # dropped: no card
                          (5, 1, "1001", "open", 50),       # dropped: a second open row for the same requester
                      ])
        c.commit()

    # Undo the v5 patch and reopen: migration 0006 runs against the real files.
    monkeypatch.undo()
    database = Database.open(path)
    try:
        assert path.with_name(path.name + ".pre-v6").exists()
        version = await database.run(lambda c: c.execute("PRAGMA user_version").fetchone()[0])
        requests = await database.run(lambda c: c.execute(
            "SELECT id, card_id, requester_id, status, created_at, closed_at FROM requests ORDER BY id").fetchall())
    finally:
        database.close()

    assert version == dbmod.LATEST_VERSION
    assert requests == [
        (1, 1, "1001", "open", 10, None),
        (2, 1, "1002", "cleared", 20, 20),
        (3, 1, "1003", "cleared", 30, 30),
    ]
