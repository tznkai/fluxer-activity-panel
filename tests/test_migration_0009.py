# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0009 against real SQLite files: a v8 database becomes v9, the poll CHECKs hold, and a v8 build refuses v9."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import fluxer_panel.db as dbmod
from fluxer_panel.db import Database, DatabaseRefused

POLL_TABLES = ("polls", "poll_options", "poll_ballots", "poll_votes")


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def seeded(db: Database) -> Database:
    """A v9 database: card 1; poll 1 closed with one option; poll 2 open with one option and one ballot (user 1002)."""

    def seed(c: sqlite3.Connection) -> None:
        c.execute("INSERT INTO cards (title, created_by, created_at, updated_at) VALUES ('t', '1001', 1, 1)")
        c.execute("INSERT INTO polls (id, card_id, guild_id, channel_id, message_id, starter_id, kind, state, "
                  "created_at, closes_at, closed_at, ballot_size, outcome) "
                  "VALUES (1, 1, '3001', '3002', '5001', '1001', 'approval', 'closed', 1, 3, 5, 6, 'none')")
        c.execute("INSERT INTO polls (id, card_id, guild_id, channel_id, message_id, starter_id, kind, state, "
                  "created_at, closes_at, ballot_size) "
                  "VALUES (2, 1, '3001', '3002', '5002', '1001', 'approval', 'open', 1, 100, 6)")
        c.execute("INSERT INTO poll_options (id, poll_id, position, starts_at, origin) VALUES (1, 1, 0, 10, 'offered')")
        c.execute("INSERT INTO poll_options (id, poll_id, position, starts_at, origin) VALUES (2, 2, 0, 20, 'offered')")
        c.execute("INSERT INTO poll_ballots (poll_id, user_id) VALUES (2, '1002')")

    await db.run(seed)
    return db


# Migration 0009 from a v8 database -------------------------------------------------------------------

async def test_v8_database_migrates_to_v9_and_keeps_its_cards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "panel.db"
    real_migrations = dbmod.load_migrations()
    assert [version for version, _ in real_migrations] == list(range(1, 11))

    monkeypatch.setattr(dbmod, "load_migrations", lambda: real_migrations[:8])
    monkeypatch.setattr(dbmod, "LATEST_VERSION", 8)
    Database.open(path).close()
    with contextlib.closing(sqlite3.connect(path)) as c:
        c.execute("INSERT INTO cards (title, created_by, created_at, updated_at) VALUES ('Game', '1001', 1, 1)")
        c.commit()

    monkeypatch.undo()
    database = Database.open(path)
    try:
        backup = path.with_name(path.name + ".pre-v9")
        assert backup.exists()
        with contextlib.closing(sqlite3.connect(backup)) as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 8

        def inspect(c: sqlite3.Connection) -> dict[str, Any]:
            return {
                "version": c.execute("PRAGMA user_version").fetchone()[0],
                "cards": c.execute("SELECT id, title FROM cards").fetchall(),
                # PRAGMA table_list columns: schema, name, type, ncol, wr, strict
                "strict": {row[1]: bool(row[5]) for row in c.execute("PRAGMA table_list")
                           if row[1] in POLL_TABLES},
            }

        state = await database.run(inspect)
    finally:
        database.close()

    assert state["version"] == dbmod.LATEST_VERSION == 10
    assert state["cards"] == [(1, "Game")]
    assert state["strict"] == {name: True for name in POLL_TABLES}


# The poll CHECKs and the composite foreign key ---------------------------------------------------------

POLL_REJECTS: list[tuple[str, str]] = [
    ("two options with the same starts_at in one poll",
     "INSERT INTO poll_options (poll_id, position, starts_at, origin) VALUES (2, 1, 20, 'offered')"),
    ("two options with the same position in one poll",
     "INSERT INTO poll_options (poll_id, position, starts_at, origin) VALUES (2, 0, 30, 'offered')"),
    ("a vote for an option of another poll",
     "INSERT INTO poll_votes (poll_id, user_id, option_id) VALUES (2, '1002', 1)"),
    ("a closed poll without a close time", "UPDATE polls SET closed_at = NULL WHERE id = 1"),
    ("a winner outcome without a winner option", "UPDATE polls SET outcome = 'winner' WHERE id = 1"),
    ("a ballot size of zero", "UPDATE polls SET ballot_size = 0 WHERE id = 2"),
    ("a ballot size of 21", "UPDATE polls SET ballot_size = 21 WHERE id = 2"),
]


@pytest.mark.parametrize(("label", "sql"), POLL_REJECTS, ids=[case[0] for case in POLL_REJECTS])
async def test_v9_polls_reject_invalid_rows(seeded: Database, label: str, sql: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await seeded.run(lambda c: c.execute(sql))


async def test_v9_indexes_ballots_by_user(seeded: Database) -> None:
    """The DM router looks up a user's open ballots on every non-admin DM, so poll_ballots is indexed by user id."""
    columns = await seeded.run(lambda c: [row[2] for row in c.execute("PRAGMA index_info('poll_ballots_user')")])
    assert columns == ["user_id"]


# AC1.7 ------------------------------------------------------------------------------------------------
# The exit code 3 for a schema_newer refusal is pinned by tests/test_main.py:952-968.

async def test_a_v9_file_is_refused_by_a_v8_build_and_left_unchanged(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "panel.db"
    Database.open(path).close()
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    real_migrations = dbmod.load_migrations()
    monkeypatch.setattr(dbmod, "load_migrations", lambda: real_migrations[:8])
    monkeypatch.setattr(dbmod, "LATEST_VERSION", 8)

    with pytest.raises(DatabaseRefused) as info:
        Database.open(path)

    assert info.value.reason == "schema_newer"
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime
    assert not list(tmp_path.glob("panel.db.pre-v*"))
