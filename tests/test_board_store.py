# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The summary board's single-row store, against a real SQLite file: the seeded row, the CHECKs and the
compare-and-set writes that a MESSAGE_DELETE can race."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fluxer_panel import board
from fluxer_panel.db import Database

CHANNEL = "3001"
OTHER_CHANNEL = "3002"
MESSAGE = "5001"
LATER_MESSAGE = "5002"
HASH = "a" * 64
LATER_HASH = "b" * 64


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


async def _exec(db: Database, sql: str, params: tuple[object, ...] = ()) -> None:
    await db.run(lambda c: c.execute(sql, params))


async def test_a_fresh_database_has_one_empty_board_row(db: Database) -> None:
    rows = await db.run(lambda c: c.execute("SELECT COUNT(*) FROM board").fetchone()[0])

    assert rows == 1
    assert await db.run(board.get) == board.BoardState(None, None, None)


async def test_set_message_stores_the_channel_the_message_and_the_hash(db: Database) -> None:
    await db.run(lambda c: board.set_message(c, CHANNEL, MESSAGE, HASH))

    assert await db.run(board.get) == board.BoardState(CHANNEL, MESSAGE, HASH)


async def test_set_message_replaces_a_previous_message_and_its_channel(db: Database) -> None:
    await db.run(lambda c: board.set_message(c, CHANNEL, MESSAGE, HASH))

    await db.run(lambda c: board.set_message(c, OTHER_CHANNEL, LATER_MESSAGE, LATER_HASH))

    assert await db.run(board.get) == board.BoardState(OTHER_CHANNEL, LATER_MESSAGE, LATER_HASH)


async def test_clear_message_if_clears_only_the_message_it_names(db: Database) -> None:
    await db.run(lambda c: board.set_message(c, CHANNEL, MESSAGE, HASH))

    assert await db.run(lambda c: board.clear_message_if(c, CHANNEL, LATER_MESSAGE)) is False
    assert await db.run(lambda c: board.clear_message_if(c, OTHER_CHANNEL, MESSAGE)) is False
    assert await db.run(board.get) == board.BoardState(CHANNEL, MESSAGE, HASH)

    assert await db.run(lambda c: board.clear_message_if(c, CHANNEL, MESSAGE)) is True
    assert await db.run(board.get) == board.BoardState(None, None, None)


async def test_clear_message_if_on_an_empty_board_changes_nothing(db: Database) -> None:
    assert await db.run(lambda c: board.clear_message_if(c, CHANNEL, MESSAGE)) is False
    assert await db.run(board.get) == board.BoardState(None, None, None)


async def test_set_hash_if_writes_only_while_the_board_still_holds_that_message(db: Database) -> None:
    await db.run(lambda c: board.set_message(c, CHANNEL, MESSAGE, HASH))

    assert await db.run(lambda c: board.set_hash_if(c, CHANNEL, MESSAGE, LATER_HASH)) is True
    assert await db.run(board.get) == board.BoardState(CHANNEL, MESSAGE, LATER_HASH)

    assert await db.run(lambda c: board.set_hash_if(c, CHANNEL, LATER_MESSAGE, HASH)) is False
    assert await db.run(lambda c: board.set_hash_if(c, OTHER_CHANNEL, MESSAGE, HASH)) is False
    assert await db.run(board.get) == board.BoardState(CHANNEL, MESSAGE, LATER_HASH)


async def test_a_hash_write_after_the_message_was_cleared_is_not_stored(db: Database) -> None:
    await db.run(lambda c: board.set_message(c, CHANNEL, MESSAGE, HASH))
    await db.run(lambda c: board.clear_message_if(c, CHANNEL, MESSAGE))

    assert await db.run(lambda c: board.set_hash_if(c, CHANNEL, MESSAGE, LATER_HASH)) is False
    assert await db.run(board.get) == board.BoardState(None, None, None)


# Schema: the CHECKs refuse malformed rows, whichever statement writes them.

REJECTED: list[tuple[str, str, tuple[object, ...]]] = [
    ("the row id is two", "INSERT INTO board (id) VALUES (2)", ()),
    ("the channel id has a letter", "UPDATE board SET channel_id = 'x1', message_id = '5001' WHERE id = 1", ()),
    ("the channel id is empty", "UPDATE board SET channel_id = '', message_id = '5001' WHERE id = 1", ()),
    ("the message id is 26 digits", "UPDATE board SET channel_id = '3001', message_id = ? WHERE id = 1",
     ("2" * 26,)),
    ("a channel is set without a message", "UPDATE board SET channel_id = '3001' WHERE id = 1", ()),
    ("a message is set without a channel", "UPDATE board SET message_id = '5001' WHERE id = 1", ()),
    ("a hash is set without a message", "UPDATE board SET content_hash = ? WHERE id = 1", (HASH,)),
    ("the hash is not hex", "UPDATE board SET channel_id = '3001', message_id = '5001', content_hash = ? "
                            "WHERE id = 1", ("G" * 64,)),
    ("the hash is 63 characters", "UPDATE board SET channel_id = '3001', message_id = '5001', content_hash = ? "
                                  "WHERE id = 1", ("a" * 63,)),
]


@pytest.mark.parametrize(("label", "sql", "params"), REJECTED, ids=[case[0] for case in REJECTED])
async def test_schema_rejects_a_malformed_board_row(db: Database, label: str, sql: str,
                                                    params: tuple[object, ...]) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        await _exec(db, sql, params)
