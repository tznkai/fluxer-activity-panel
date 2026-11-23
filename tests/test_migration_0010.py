# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0010 against real SQLite files: a v9 database becomes v10 with redirect subscriptions and home-scoped rows."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

import fluxer_panel.db as dbmod
from fluxer_panel.db import Database, DatabaseRefused

NEW_TABLES = ("subscription_follow_role", "mirror_message")
HASH = "a" * 64


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


def _seed_v9(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Create a v9 file with two cards, subscriptions, a session, a poll, a ballot, an RSVP and a play-now post."""
    real_migrations = dbmod.load_migrations()
    monkeypatch.setattr(dbmod, "load_migrations", lambda: real_migrations[:9])
    monkeypatch.setattr(dbmod, "LATEST_VERSION", 9)
    Database.open(path).close()
    monkeypatch.undo()
    with contextlib.closing(sqlite3.connect(path)) as c:
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("INSERT INTO cards (title, created_by, created_at, updated_at) VALUES ('One', '1001', 1, 1)")
        c.execute("INSERT INTO cards (title, created_by, created_at, updated_at) VALUES ('Two', '1002', 1, 1)")
        # Three subscriptions on card 1 in insertion order; the rowid order is 3001, 3002, 3003.
        c.execute("INSERT INTO card_subscription (card_id, guild_id, channel_id, paused) VALUES (1, '4001', '3001', 0)")
        c.execute("INSERT INTO card_subscription (card_id, guild_id, channel_id, paused) VALUES (1, '4002', '3002', 1)")
        c.execute("INSERT INTO card_subscription (card_id, guild_id, channel_id, message_id, content_hash, paused) "
                  "VALUES (1, '4003', '3003', '5003', ?, 0)", (HASH,))
        c.execute("INSERT INTO card_subscription (card_id, guild_id, channel_id, paused) VALUES (2, '4004', '3004', 0)")
        c.execute("INSERT INTO sessions (card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, "
                  "created_at) VALUES (1, '3001', '3002', '5010', 100, 200, '1001', 1)")
        c.execute("INSERT INTO rsvp_reaction (session_id, user_id, choice, added_at, seq) "
                  "VALUES (1, '1002', 'going', 1, 1)")
        c.execute("INSERT INTO polls (id, card_id, guild_id, channel_id, message_id, starter_id, kind, state, "
                  "created_at, closes_at, ballot_size) "
                  "VALUES (1, 1, '3001', '3002', '5020', '1001', 'approval', 'open', 1, 100, 6)")
        c.execute("INSERT INTO poll_options (id, poll_id, position, starts_at, origin) VALUES (1, 1, 0, 20, 'offered')")
        c.execute("INSERT INTO poll_ballots (poll_id, user_id) VALUES (1, '1002')")
        c.execute("INSERT INTO play_now (card_id, guild_id, channel_id, message_id, presser_id, offers_wake, state, "
                  "created_at, expires_at) VALUES (1, '3001', '3002', '5030', '1001', 0, 'live', 1, 100)")
        c.commit()


def _migrate(path: Path, monkeypatch: pytest.MonkeyPatch) -> Database:
    _seed_v9(path, monkeypatch)
    monkeypatch.undo()
    return Database.open(path)


# Migration 0010 from a v9 database -------------------------------------------------------------------

async def test_v9_database_becomes_v10_with_redirect_subscriptions_and_home_scope(tmp_path: Path,
                                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "panel.db"
    real_migrations = dbmod.load_migrations()
    assert [version for version, _ in real_migrations] == list(range(1, 11))

    database = _migrate(path, monkeypatch)
    try:
        backup = path.with_name(path.name + ".pre-v10")
        assert backup.exists()
        with contextlib.closing(sqlite3.connect(backup)) as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 9

        def inspect(c: sqlite3.Connection) -> dict[str, Any]:
            return {
                "version": c.execute("PRAGMA user_version").fetchone()[0],
                "subs": c.execute("SELECT id, card_id, guild_id, channel_id, message_id, content_hash, paused, mode, "
                                  "local_channel_id FROM card_subscription ORDER BY id").fetchall(),
                "sessions_scope": c.execute("SELECT scope_sub_id FROM sessions").fetchall(),
                "polls_scope": c.execute("SELECT scope_sub_id FROM polls").fetchall(),
                "play_now_scope": c.execute("SELECT scope_sub_id FROM play_now").fetchall(),
                "ballot_via": c.execute("SELECT via_sub_id FROM poll_ballots").fetchall(),
                "rsvp_via": c.execute("SELECT via_sub_id FROM rsvp_reaction").fetchall(),
                "fk_problems": c.execute("PRAGMA foreign_key_check").fetchall(),
                # PRAGMA table_list columns: schema, name, type, ncol, wr, strict
                "strict": {row[1]: bool(row[5]) for row in c.execute("PRAGMA table_list")
                           if row[1] in NEW_TABLES},
            }

        state = await database.run(inspect)
    finally:
        database.close()

    assert state["version"] == dbmod.LATEST_VERSION == 10
    assert state["subs"] == [
        (1, 1, "4001", "3001", None, None, 0, "redirect", None),
        (2, 1, "4002", "3002", None, None, 1, "redirect", None),
        (3, 1, "4003", "3003", "5003", HASH, 0, "redirect", None),
        (4, 2, "4004", "3004", None, None, 0, "redirect", None),
    ]
    assert state["sessions_scope"] == [(None,)]
    assert state["polls_scope"] == [(None,)]
    assert state["play_now_scope"] == [(None,)]
    assert state["ballot_via"] == [(None,)]
    assert state["rsvp_via"] == [(None,)]
    assert state["fk_problems"] == []
    assert state["strict"] == {name: True for name in NEW_TABLES}


async def test_v10_subscription_table_keeps_its_snowflake_checks(db: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        await db.run(lambda c: c.execute("INSERT INTO card_subscription (card_id, guild_id, channel_id, paused) "
                                         "VALUES (1, '12a', '2001', 0)"))


# Scoped open polls -----------------------------------------------------------------------------------

async def test_open_once_index_is_per_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _migrate(tmp_path / "panel.db", monkeypatch)
    try:
        def poll(c: sqlite3.Connection, message_id: str, scope: int | None) -> None:
            c.execute("INSERT INTO polls (card_id, guild_id, channel_id, message_id, starter_id, kind, state, "
                      "created_at, closes_at, ballot_size, scope_sub_id) "
                      "VALUES (1, '3001', '3002', ?, '1001', 'approval', 'open', 1, 100, 6, ?)", (message_id, scope))

        def refused(c: sqlite3.Connection, message_id: str, scope: int | None) -> bool:
            try:
                poll(c, message_id, scope)
            except sqlite3.IntegrityError:
                return True
            return False

        # The poll from the seed is home-scoped and open, so a second home poll is refused.
        assert await database.run(lambda c: refused(c, "5101", None)) is True
        # A subscription scope has its own open slot, and a second one in that scope is refused.
        await database.run(lambda c: poll(c, "5102", 1))
        assert await database.run(lambda c: refused(c, "5103", 1)) is True
    finally:
        database.close()


# Mirror and subscription-owned rows ------------------------------------------------------------------

async def test_mirror_message_needs_exactly_one_object(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _migrate(tmp_path / "panel.db", monkeypatch)
    try:
        def insert(c: sqlite3.Connection, session_id: int | None, poll_id: int | None) -> None:
            c.execute("INSERT INTO mirror_message (sub_id, channel_id, message_id, session_id, poll_id) "
                      "VALUES (1, '3005', '6001', ?, ?)", (session_id, poll_id))

        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            await database.run(lambda c: insert(c, None, None))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            await database.run(lambda c: insert(c, 1, 1))
        await database.run(lambda c: insert(c, 1, None))
    finally:
        database.close()


async def test_a_subscription_with_a_session_cannot_be_deleted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _migrate(tmp_path / "panel.db", monkeypatch)
    try:
        await database.run(lambda c: c.execute("UPDATE sessions SET scope_sub_id = 1 WHERE id = 1"))
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            await database.run(lambda c: c.execute("DELETE FROM card_subscription WHERE id = 1"))
    finally:
        database.close()


async def test_deleting_a_subscription_cascades_its_role_and_mirrors(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    database = _migrate(tmp_path / "panel.db", monkeypatch)
    try:
        await database.run(lambda c: c.execute("INSERT INTO subscription_follow_role (sub_id, role_id) "
                                               "VALUES (2, '7001')"))
        await database.run(lambda c: c.execute("INSERT INTO mirror_message (sub_id, channel_id, message_id, session_id) "
                                               "VALUES (2, '3005', '6001', 1)"))
        await database.run(lambda c: c.execute("DELETE FROM card_subscription WHERE id = 2"))

        def counts(c: sqlite3.Connection) -> tuple[int, int]:
            return (c.execute("SELECT COUNT(*) FROM subscription_follow_role").fetchone()[0],
                    c.execute("SELECT COUNT(*) FROM mirror_message").fetchone()[0])

        assert await database.run(counts) == (0, 0)
    finally:
        database.close()


async def test_subscription_ids_are_never_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _migrate(tmp_path / "panel.db", monkeypatch)
    try:
        await database.run(lambda c: c.execute("DELETE FROM card_subscription WHERE id = 4"))
        await database.run(lambda c: c.execute("INSERT INTO card_subscription (card_id, guild_id, channel_id, paused) "
                                               "VALUES (2, '4005', '3005', 0)"))
        new_id = await database.run(lambda c: c.execute("SELECT MAX(id) FROM card_subscription").fetchone()[0])
        assert new_id > 4
    finally:
        database.close()


# Refusal ---------------------------------------------------------------------------------------------

async def test_a_v10_file_is_refused_by_a_v9_build_and_left_unchanged(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "panel.db"
    Database.open(path).close()
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    real_migrations = dbmod.load_migrations()
    monkeypatch.setattr(dbmod, "load_migrations", lambda: real_migrations[:9])
    monkeypatch.setattr(dbmod, "LATEST_VERSION", 9)

    with pytest.raises(DatabaseRefused) as info:
        Database.open(path)

    assert info.value.reason == "schema_newer"
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime
    assert not list(tmp_path.glob("panel.db.pre-v*"))
