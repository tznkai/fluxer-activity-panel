# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Schema v1 constraints against a real SQLite file: id shapes, enums, ranges, cascades and column allowlist."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fluxer_panel.db import Database

# Copied from src/fluxer_panel/migrations/0001_init.sql. Adding a column means updating this on purpose.
ALLOWED_COLUMNS: dict[str, set[str]] = {
    "cards": {"id", "kind", "title", "description", "channel_id", "message_id", "created_by",
              "approved_by", "archived", "created_at", "updated_at"},
    "card_server": {"card_id", "ptero_server_id", "join_address", "query_kind", "query_port"},
    "card_location": {"card_id", "location_text", "visibility"},
    "card_seats": {"card_id", "seats"},
    "families": {"id", "created_at"},
    "family_members": {"family_id", "user_id", "relation"},
    "sessions": {"id", "card_id", "group_role_id", "starts_at", "ends_at", "location_text", "seats",
                 "recurrence", "recurrence_until", "proposer_id", "created_at"},
    "rsvps": {"session_id", "user_id", "status", "responded_at"},
    "requests": {"id", "kind", "card_id", "requester_id", "status", "created_at"},
}

FORBIDDEN_TOKENS = {"name", "nick", "nickname", "age", "birth", "birthday", "dob", "gender",
                    "member", "membership", "group"}
ALLOWED_GROUP_COLUMNS = {"group_role_id"}   # the Fluxer role that gates a session; an id, not membership


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


async def _exec(db: Database, sql: str, params: tuple[object, ...] = ()) -> None:
    await db.run(lambda c: c.execute(sql, params))


async def _seed(db: Database) -> None:
    """Valid base rows: one family, one card (id 1) and one session (id 1) on that card."""
    await _exec(db, "INSERT INTO families (id, created_at) VALUES (1, 1)")
    await _exec(db, "INSERT INTO cards (kind, title, created_by, created_at, updated_at) "
                    "VALUES ('server', 't', '1001', 1, 1)")
    await _exec(db, "INSERT INTO sessions (card_id, group_role_id, starts_at, ends_at, proposer_id, created_at) "
                    "VALUES (1, '2001', 10, 20, '1001', 1)")


@pytest.fixture
async def seeded(db: Database) -> Database:
    await _seed(db)
    return db


# AC4.5 ----------------------------------------------------------------------------------------------

REJECTED: list[tuple[str, str, tuple[object, ...]]] = [
    ("created_by is letters", "INSERT INTO cards (kind,title,created_by,created_at,updated_at) "
                              "VALUES ('server','t',?,1,1)", ("abc",)),
    ("created_by is empty", "INSERT INTO cards (kind,title,created_by,created_at,updated_at) "
                            "VALUES ('server','t',?,1,1)", ("",)),
    ("created_by is 26 digits", "INSERT INTO cards (kind,title,created_by,created_at,updated_at) "
                                "VALUES ('server','t',?,1,1)", ("9" * 26,)),
    ("created_by has a space", "INSERT INTO cards (kind,title,created_by,created_at,updated_at) "
                               "VALUES ('server','t',?,1,1)", ("12 3",)),
    ("card kind is unknown", "INSERT INTO cards (kind,title,created_by,created_at,updated_at) "
                             "VALUES ('quest','t','1001',1,1)", ()),
    ("rsvp status is unknown", "INSERT INTO rsvps (session_id,user_id,status,responded_at) "
                               "VALUES (1,'1001','perhaps',1)", ()),
    ("family relation is unknown", "INSERT INTO family_members (family_id,user_id,relation) "
                                   "VALUES (1,'1001','cousin')", ()),
    ("location visibility is public", "INSERT INTO card_location (card_id,location_text,visibility) "
                                      "VALUES (1,'x','public')", ()),
    ("session ends equal starts", "INSERT INTO sessions (card_id,group_role_id,starts_at,ends_at,proposer_id,created_at) "
                                  "VALUES (1,'2001',10,10,'1001',1)", ()),
    ("session ends before starts", "INSERT INTO sessions (card_id,group_role_id,starts_at,ends_at,proposer_id,created_at) "
                                   "VALUES (1,'2001',10,5,'1001',1)", ()),
    ("card seats is zero", "INSERT INTO card_seats (card_id,seats) VALUES (1,0)", ()),
    ("session seats is negative", "INSERT INTO sessions (card_id,group_role_id,starts_at,ends_at,seats,proposer_id,created_at) "
                                  "VALUES (1,'2001',10,20,-1,'1001',1)", ()),
    ("request kind is ping", "INSERT INTO requests (kind,card_id,requester_id,status,created_at) "
                             "VALUES ('ping',1,'1001','open',1)", ()),
    ("request status is new", "INSERT INTO requests (kind,card_id,requester_id,status,created_at) "
                              "VALUES ('wake',1,'1001','new',1)", ()),
    ("rsvp user id is a letter", "INSERT INTO rsvps (session_id,user_id,status,responded_at) "
                                 "VALUES (1,'x','yes',1)", ()),
    ("group role id has a dot", "INSERT INTO sessions (card_id,group_role_id,starts_at,ends_at,proposer_id,created_at) "
                                "VALUES (1,'1.5',10,20,'1001',1)", ()),
    ("archived is two", "INSERT INTO cards (kind,title,created_by,created_at,updated_at,archived) "
                        "VALUES ('server','t','1001',1,1,2)", ()),
    ("query port is zero", "INSERT INTO card_server (card_id,query_port) VALUES (1,0)", ()),
]


@pytest.mark.parametrize(("label", "sql", "params"), REJECTED, ids=[case[0] for case in REJECTED])
async def test_schema_rejects_invalid_row(seeded: Database, label: str, sql: str, params: tuple[object, ...]) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        await _exec(seeded, sql, params)


async def test_strict_table_rejects_text_for_integer_column(seeded: Database) -> None:
    with pytest.raises(sqlite3.Error):
        await _exec(seeded, "INSERT INTO sessions (card_id,group_role_id,starts_at,ends_at,proposer_id,created_at) "
                            "VALUES (1,'2001','soon',20,'1001',1)")


async def test_valid_base_rows_insert_and_null_approved_by_is_accepted(seeded: Database) -> None:
    count = await seeded.run(lambda c: c.execute("SELECT COUNT(*) FROM cards WHERE approved_by IS NULL").fetchone()[0])
    assert count == 1
    assert await seeded.run(lambda c: c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]) == 1


# AC4.6 ----------------------------------------------------------------------------------------------

async def test_columns_match_allowlist_exactly(seeded: Database) -> None:
    def introspect(c: sqlite3.Connection) -> tuple[set[str], set[tuple[str, str]]]:
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
        pairs = {(t, row[1]) for t in tables for row in c.execute(f"PRAGMA table_info({t})")}
        return tables, pairs

    tables, pairs = await seeded.run(introspect)

    assert tables == set(ALLOWED_COLUMNS)
    expected = {(t, col) for t, cols in ALLOWED_COLUMNS.items() for col in cols}
    assert pairs == expected


async def test_no_column_has_a_personal_data_token(seeded: Database) -> None:
    def columns(c: sqlite3.Connection) -> list[tuple[str, str]]:
        names = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return [(t, row[1]) for t in names for row in c.execute(f"PRAGMA table_info({t})")]

    offending = []
    for table, column in await seeded.run(columns):
        tokens = set(column.split("_"))
        if column in ALLOWED_GROUP_COLUMNS:
            continue
        if tokens & FORBIDDEN_TOKENS:
            offending.append(f"{table}.{column}")
    assert offending == []


# AC4.7 ----------------------------------------------------------------------------------------------

async def test_deleting_session_cascades_its_rsvps(seeded: Database) -> None:
    await _exec(seeded, "INSERT INTO rsvps (session_id,user_id,status,responded_at) VALUES (1,'1001','yes',1)")
    await _exec(seeded, "INSERT INTO rsvps (session_id,user_id,status,responded_at) VALUES (1,'1002','maybe',1)")

    await _exec(seeded, "DELETE FROM sessions WHERE id = 1")

    assert await seeded.run(lambda c: c.execute("SELECT COUNT(*) FROM rsvps").fetchone()[0]) == 0


async def test_deleting_card_with_sessions_is_refused(seeded: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await _exec(seeded, "DELETE FROM cards WHERE id = 1")

    assert await seeded.run(lambda c: c.execute("SELECT COUNT(*) FROM cards WHERE id = 1").fetchone()[0]) == 1


async def test_deleting_family_removes_its_members(seeded: Database) -> None:
    await _exec(seeded, "INSERT INTO family_members (family_id,user_id,relation) VALUES (1,'1001','parent')")
    await _exec(seeded, "INSERT INTO family_members (family_id,user_id,relation) VALUES (1,'1002','child')")

    await _exec(seeded, "DELETE FROM families WHERE id = 1")

    assert await seeded.run(lambda c: c.execute("SELECT COUNT(*) FROM family_members").fetchone()[0]) == 0
