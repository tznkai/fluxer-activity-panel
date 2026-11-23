# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The mirror_message store: a home session, poll or play-now post re-posted in a both subscription's channel.
Synchronous SQL over one sqlite3 connection, run through `await db.run(fn)` like the subscription store.

A mirror holds one of the three object ids, never two. Its writes are compare-and-set on the message id, as the
subscription writes are, because a MESSAGE_DELETE does not take the card lock. The object column is picked from the
fixed `_OBJECT_COLUMN` map, never from caller text.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

Kind = Literal["session", "poll", "play_now"]
_OBJECT_COLUMN: dict[str, str] = {"session": "session_id", "poll": "poll_id", "play_now": "play_now_id"}

# The kind and the object id are derived from whichever of the three columns is set (the table's CHECK makes it one).
_KIND_SQL = ("CASE WHEN session_id IS NOT NULL THEN 'session' WHEN poll_id IS NOT NULL THEN 'poll' "
             "ELSE 'play_now' END")
_OBJECT_SQL = "COALESCE(session_id, poll_id, play_now_id)"
_COLUMNS = f"id, sub_id, channel_id, message_id, content_hash, {_KIND_SQL}, {_OBJECT_SQL}"


@dataclass(frozen=True)
class Mirror:
    id: int
    sub_id: int
    channel_id: str
    message_id: str
    content_hash: str | None
    kind: Kind
    object_id: int    # the home session, poll or play_now id


def _load(row: tuple) -> Mirror:
    mirror_id, sub_id, channel_id, message_id, content_hash, kind, object_id = row
    return Mirror(mirror_id, sub_id, channel_id, message_id, content_hash, kind, object_id)


def add(conn: sqlite3.Connection, *, sub_id: int, channel_id: str, message_id: str, kind: Kind, object_id: int,
        content_hash: str | None) -> int:
    """Stores a mirror of one home object in one both subscription and returns its id. A second mirror of the same
    object in the same subscription raises IntegrityError."""
    column = _OBJECT_COLUMN[kind]
    cur = conn.execute(
        f"INSERT INTO mirror_message (sub_id, channel_id, message_id, content_hash, {column}) VALUES (?, ?, ?, ?, ?)",
        (sub_id, channel_id, message_id, content_hash, object_id))
    return int(cur.lastrowid)


def get(conn: sqlite3.Connection, kind: Kind, object_id: int, sub_id: int) -> Mirror | None:
    column = _OBJECT_COLUMN[kind]
    row = conn.execute(f"SELECT {_COLUMNS} FROM mirror_message WHERE {column} = ? AND sub_id = ?",
                       (object_id, sub_id)).fetchone()
    return None if row is None else _load(row)


def get_by_message_id(conn: sqlite3.Connection, message_id: str) -> Mirror | None:
    """The mirror that holds this message, for a lookup from a reaction or a message event."""
    row = conn.execute(f"SELECT {_COLUMNS} FROM mirror_message WHERE message_id = ?", (message_id,)).fetchone()
    return None if row is None else _load(row)


def for_object(conn: sqlite3.Connection, kind: Kind, object_id: int) -> list[Mirror]:
    """Every mirror of one home object, in the order of the subscriptions."""
    column = _OBJECT_COLUMN[kind]
    rows = conn.execute(f"SELECT {_COLUMNS} FROM mirror_message WHERE {column} = ? ORDER BY sub_id",
                        (object_id,)).fetchall()
    return [_load(row) for row in rows]


def for_sub(conn: sqlite3.Connection, sub_id: int) -> list[Mirror]:
    rows = conn.execute(f"SELECT {_COLUMNS} FROM mirror_message WHERE sub_id = ? ORDER BY id", (sub_id,)).fetchall()
    return [_load(row) for row in rows]


def set_hash_if(conn: sqlite3.Connection, mirror_id: int, message_id: str, content_hash: str) -> bool:
    """Stores the hash only while the mirror still holds this message. Returns whether a row changed."""
    cur = conn.execute("UPDATE mirror_message SET content_hash = ? WHERE id = ? AND message_id = ?",
                       (content_hash, mirror_id, message_id))
    return cur.rowcount == 1


def delete_if(conn: sqlite3.Connection, mirror_id: int, message_id: str) -> bool:
    """Forgets a mirror only while it still holds this message (used on a 404 and on cleanup)."""
    cur = conn.execute("DELETE FROM mirror_message WHERE id = ? AND message_id = ?", (mirror_id, message_id))
    return cur.rowcount == 1


def delete_for_sub(conn: sqlite3.Connection, sub_id: int) -> int:
    """Deletes every mirror of a subscription. Returns the number of rows removed."""
    cur = conn.execute("DELETE FROM mirror_message WHERE sub_id = ?", (sub_id,))
    return int(cur.rowcount)
