# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0008 against real SQLite files: a v7 database becomes v8, the v8 CHECKs hold, and a v7 build refuses v8."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import fluxer_panel.db as dbmod
from fluxer_panel.db import Database, DatabaseRefused

SESSION_COLUMNS = {"id", "card_id", "guild_id", "channel_id", "message_id", "starts_at", "ends_at", "proposer_id",
                   "location_text", "seats", "recurrence", "series_id", "next_posted", "state", "closed_at",
                   "moved", "headsup_sent", "created_at", "scope_sub_id"}
REACTION_COLUMNS = {"session_id", "user_id", "choice", "added_at", "seq", "implicit", "via_sub_id", "via_message_id"}
RSVP_COLUMNS = {"session_id", "user_id", "choice", "responded_at", "waitlist_pos"}

SESSION_DEFAULTS: dict[str, Any] = {"card_id": 1, "guild_id": "3001", "channel_id": "3002", "message_id": "3004",
                                    "starts_at": 10, "ends_at": 20, "proposer_id": "1001", "created_at": 1}


def _session_insert(**overrides: Any) -> tuple[str, tuple[Any, ...]]:
    """An INSERT into sessions with the valid defaults, changed by the overrides."""
    row = {**SESSION_DEFAULTS, **overrides}
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    return f"INSERT INTO sessions ({columns}) VALUES ({marks})", tuple(row.values())


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def seeded(db: Database) -> Database:
    """A v8 database with one card and one upcoming session (id 1, message 3003)."""
    await db.run(lambda c: c.execute("INSERT INTO cards (title, created_by, created_at, updated_at) "
                                     "VALUES ('t', '1001', 1, 1)"))
    await db.run(lambda c: c.execute("INSERT INTO sessions (card_id, guild_id, channel_id, message_id, starts_at, "
                                     "ends_at, proposer_id, created_at) VALUES (1, '3001', '3002', '3003', 10, 20, "
                                     "'1001', 1)"))
    return db


# Migration 0008 from a v7 database -------------------------------------------------------------------

async def test_v7_database_migrates_to_v8_and_keeps_the_family_tables(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "panel.db"
    real_migrations = dbmod.load_migrations()
    assert [version for version, _ in real_migrations] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

    monkeypatch.setattr(dbmod, "load_migrations", lambda: real_migrations[:7])
    monkeypatch.setattr(dbmod, "LATEST_VERSION", 7)
    Database.open(path).close()
    with contextlib.closing(sqlite3.connect(path)) as c:
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("INSERT INTO families (id, created_at) VALUES (1, 1)")
        c.execute("INSERT INTO family_members (family_id, user_id, relation) VALUES (1, '1001', 'parent')")
        c.execute("INSERT INTO cards (title, created_by, created_at, updated_at) VALUES ('Game', '1001', 1, 1)")
        c.execute("INSERT INTO sessions (card_id, group_role_id, starts_at, ends_at, proposer_id, created_at) "
                  "VALUES (1, '2001', 10, 20, '1001', 1)")
        c.execute("INSERT INTO rsvps (session_id, user_id, status, responded_at) VALUES (1, '1002', 'yes', 1)")
        c.commit()
        family_sql_before = c.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IN ('families', 'family_members') ORDER BY name").fetchall()

    monkeypatch.undo()
    database = Database.open(path)
    try:
        assert path.with_name(path.name + ".pre-v8").exists()

        def inspect(c: sqlite3.Connection) -> dict[str, Any]:
            return {
                "version": c.execute("PRAGMA user_version").fetchone()[0],
                "sessions": {row[1] for row in c.execute("PRAGMA table_info(sessions)")},
                "reaction": {row[1] for row in c.execute("PRAGMA table_info(rsvp_reaction)")},
                "rsvps": {row[1] for row in c.execute("PRAGMA table_info(rsvps)")},
                "session_rows": c.execute("SELECT id FROM sessions").fetchall(),
                "rsvp_rows": c.execute("SELECT session_id, user_id FROM rsvps").fetchall(),
                "family_sql": c.execute(
                    "SELECT name, sql FROM sqlite_master WHERE name IN ('families', 'family_members') "
                    "ORDER BY name").fetchall(),
                "family_rows": c.execute("SELECT family_id, user_id, relation FROM family_members").fetchall(),
            }

        state = await database.run(inspect)
    finally:
        database.close()

    assert state["version"] == dbmod.LATEST_VERSION == 10
    assert state["sessions"] == SESSION_COLUMNS
    assert state["reaction"] == REACTION_COLUMNS
    assert state["rsvps"] == RSVP_COLUMNS
    assert state["session_rows"] == []
    assert state["rsvp_rows"] == []
    assert state["family_sql"] == family_sql_before
    assert state["family_rows"] == [(1, "1001", "parent")]


# The v8 CHECKs ----------------------------------------------------------------------------------------

SESSION_REJECTS: list[tuple[str, dict[str, Any]]] = [
    ("ends equal starts", {"ends_at": 10}),
    ("ends before starts", {"ends_at": 5}),
    ("state ended without a close time", {"state": "ended"}),
    ("state upcoming with a close time", {"closed_at": 5}),
    ("recurrence is daily", {"recurrence": "daily"}),
    ("seats is zero", {"seats": 0}),
    ("seats is 1000", {"seats": 1000}),
    ("location text is empty", {"location_text": ""}),
    ("location text is 201 characters", {"location_text": "x" * 201}),
    ("guild id has a letter", {"guild_id": "30x1"}),
    ("channel id has a letter", {"channel_id": "30x2"}),
    ("message id has a letter", {"message_id": "30x4"}),
    ("proposer id is empty", {"proposer_id": ""}),
    ("message id is a duplicate", {"message_id": "3003"}),
]


@pytest.mark.parametrize(("label", "overrides"), SESSION_REJECTS, ids=[case[0] for case in SESSION_REJECTS])
async def test_v8_sessions_reject_invalid_rows(seeded: Database, label: str, overrides: dict[str, Any]) -> None:
    sql, params = _session_insert(**overrides)
    with pytest.raises(sqlite3.IntegrityError):
        await seeded.run(lambda c: c.execute(sql, params))


REACTION_AND_RSVP_REJECTS: list[tuple[str, str]] = [
    ("reaction choice is yes",
     "INSERT INTO rsvp_reaction (session_id,user_id,choice,added_at,seq) VALUES (1,'1001','yes',1,1)"),
    ("reaction seq is zero",
     "INSERT INTO rsvp_reaction (session_id,user_id,choice,added_at,seq) VALUES (1,'1001','going',1,0)"),
    ("reaction user id has a letter",
     "INSERT INTO rsvp_reaction (session_id,user_id,choice,added_at,seq) VALUES (1,'x','going',1,1)"),
    ("rsvp choice is yes",
     "INSERT INTO rsvps (session_id,user_id,choice,responded_at) VALUES (1,'1001','yes',1)"),
    ("waiting without a waitlist position",
     "INSERT INTO rsvps (session_id,user_id,choice,responded_at) VALUES (1,'1001','waiting',1)"),
    ("going with a waitlist position",
     "INSERT INTO rsvps (session_id,user_id,choice,responded_at,waitlist_pos) VALUES (1,'1001','going',1,1)"),
    ("waitlist position is zero",
     "INSERT INTO rsvps (session_id,user_id,choice,responded_at,waitlist_pos) VALUES (1,'1001','waiting',1,0)"),
]


@pytest.mark.parametrize(("label", "sql"), REACTION_AND_RSVP_REJECTS, ids=[case[0] for case in REACTION_AND_RSVP_REJECTS])
async def test_v8_reactions_and_rsvps_reject_invalid_rows(seeded: Database, label: str, sql: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await seeded.run(lambda c: c.execute(sql))


async def test_a_reaction_sequence_number_is_unique_per_session(seeded: Database) -> None:
    await seeded.run(lambda c: c.execute(
        "INSERT INTO rsvp_reaction (session_id,user_id,choice,added_at,seq) VALUES (1,'1001','going',1,1)"))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        await seeded.run(lambda c: c.execute(
            "INSERT INTO rsvp_reaction (session_id,user_id,choice,added_at,seq) VALUES (1,'1002','maybe',1,1)"))


async def test_deleting_a_session_cascades_its_reactions_and_rsvps(seeded: Database) -> None:
    await seeded.run(lambda c: c.execute(
        "INSERT INTO rsvp_reaction (session_id,user_id,choice,added_at,seq) VALUES (1,'1001','going',1,1)"))
    await seeded.run(lambda c: c.execute(
        "INSERT INTO rsvps (session_id,user_id,choice,responded_at) VALUES (1,'1001','going',1)"))

    await seeded.run(lambda c: c.execute("DELETE FROM sessions WHERE id = 1"))

    counts = await seeded.run(lambda c: [c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                                         for t in ("rsvp_reaction", "rsvps")])
    assert counts == [0, 0]


async def test_deleting_a_card_with_a_session_is_refused(seeded: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await seeded.run(lambda c: c.execute("DELETE FROM cards WHERE id = 1"))


# AC9.2 ------------------------------------------------------------------------------------------------

async def test_a_v8_file_is_refused_by_a_v7_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "panel.db"
    Database.open(path).close()
    real_migrations = dbmod.load_migrations()
    monkeypatch.setattr(dbmod, "load_migrations", lambda: real_migrations[:7])
    monkeypatch.setattr(dbmod, "LATEST_VERSION", 7)

    with pytest.raises(DatabaseRefused) as info:
        Database.open(path)

    assert info.value.reason == "schema_newer"
    assert str(info.value) == "database refused: schema_newer"
