# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The summary board's store: one row, with the message it posts and the hash of that message's content.

The poller is the only writer of the message columns. A MESSAGE_DELETE can race it, so the clear and the hash
write are compare-and-set: each one names the message it expects and changes nothing when that is no longer stored.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class BoardState:
    channel_id: str | None
    message_id: str | None
    content_hash: str | None


def get(conn: sqlite3.Connection) -> BoardState:
    row = conn.execute("SELECT channel_id, message_id, content_hash FROM board WHERE id = 1").fetchone()
    return BoardState(row[0], row[1], row[2])


def set_message(conn: sqlite3.Connection, channel_id: str, message_id: str, content_hash: str) -> None:
    """Stores a newly posted message. It replaces whatever was stored before, including another channel's message."""
    conn.execute("UPDATE board SET channel_id = ?, message_id = ?, content_hash = ? WHERE id = 1",
                 (channel_id, message_id, content_hash))


def set_hash_if(conn: sqlite3.Connection, channel_id: str, message_id: str, content_hash: str) -> bool:
    """Stores the content hash only while the board still holds this message. Returns whether a row changed."""
    cur = conn.execute("UPDATE board SET content_hash = ? WHERE id = 1 AND channel_id = ? AND message_id = ?",
                       (content_hash, channel_id, message_id))
    return cur.rowcount == 1


def clear_message_if(conn: sqlite3.Connection, channel_id: str, message_id: str) -> bool:
    """Forgets the message only if it is still the stored one. Returns whether it was forgotten."""
    cur = conn.execute("UPDATE board SET channel_id = NULL, message_id = NULL, content_hash = NULL "
                       "WHERE id = 1 AND channel_id = ? AND message_id = ?", (channel_id, message_id))
    return cur.rowcount == 1
