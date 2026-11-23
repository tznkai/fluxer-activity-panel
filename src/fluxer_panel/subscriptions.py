# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Card subscriptions: the copies of a card in other text channels. Synchronous SQL over one sqlite3 connection, run
through `await db.run(fn)` like the card store.

The poller and the commands both write a subscription's message columns, always under the card's lock. A
MESSAGE_DELETE does not take the lock, so the clear and the hash write are compare-and-set, as the card's are.
A paused subscription keeps its message and hash, so a resume edits the copy only when its content changed.
A subscription's mode says what it posts: `redirect` (a link to the home card), `local` (its own scoped sessions,
polls and play-now posts) or `both` (the local scope and mirrors of the home objects).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

MAX_SUBSCRIPTIONS = 20

Mode = Literal["redirect", "local", "both"]
MODES: tuple[str, ...] = ("redirect", "local", "both")
LOCAL_MODES = frozenset({"local", "both"})


class SubscriptionFull(Exception):
    """The card already has MAX_SUBSCRIPTIONS channels, paused ones included."""


@dataclass(frozen=True)
class Subscription:
    id: int
    card_id: int
    guild_id: str
    channel_id: str
    message_id: str | None
    content_hash: str | None
    paused: bool
    mode: str
    local_channel_id: str | None

    @property
    def is_scope(self) -> bool:
        """Whether this copy owns scoped sessions, polls and play-now posts."""
        return self.mode in LOCAL_MODES


_COLUMNS = "id, card_id, guild_id, channel_id, message_id, content_hash, paused, mode, local_channel_id"


def _load(row: tuple) -> Subscription:
    sub_id, card_id, guild_id, channel_id, message_id, content_hash, paused, mode, local_channel_id = row
    return Subscription(sub_id, card_id, guild_id, channel_id, message_id, content_hash, bool(paused), mode,
                        local_channel_id)


def get(conn: sqlite3.Connection, card_id: int, channel_id: str) -> Subscription | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM card_subscription WHERE card_id = ? AND channel_id = ?",
                       (card_id, channel_id)).fetchone()
    return None if row is None else _load(row)


def get_by_id(conn: sqlite3.Connection, sub_id: int) -> Subscription | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM card_subscription WHERE id = ?", (sub_id,)).fetchone()
    return None if row is None else _load(row)


def get_by_message_id(conn: sqlite3.Connection, message_id: str) -> Subscription | None:
    """The copy that holds this message, for a lookup from a reaction or a message event."""
    row = conn.execute(f"SELECT {_COLUMNS} FROM card_subscription WHERE message_id = ? ORDER BY id LIMIT 1",
                       (message_id,)).fetchone()
    return None if row is None else _load(row)


def list_for_card(conn: sqlite3.Connection, card_id: int) -> list[Subscription]:
    """Every subscription of the card, paused ones included, in the order the channels were added."""
    rows = conn.execute(f"SELECT {_COLUMNS} FROM card_subscription WHERE card_id = ? ORDER BY id",
                        (card_id,)).fetchall()
    return [_load(row) for row in rows]


def list_active(conn: sqlite3.Connection, card_id: int) -> list[Subscription]:
    """The card's subscriptions that are not paused, in the order the channels were added."""
    return [sub for sub in list_for_card(conn, card_id) if not sub.paused]


def list_scopes(conn: sqlite3.Connection, card_id: int) -> list[Subscription]:
    """The active local and both subscriptions of the card, in the order they were added."""
    rows = conn.execute(f"SELECT {_COLUMNS} FROM card_subscription WHERE card_id = ? AND paused = 0 "
                        "AND mode IN ('local', 'both') ORDER BY id", (card_id,)).fetchall()
    return [_load(row) for row in rows]


def list_both(conn: sqlite3.Connection, card_id: int) -> list[Subscription]:
    """The active both subscriptions of the card, in the order they were added."""
    rows = conn.execute(f"SELECT {_COLUMNS} FROM card_subscription WHERE card_id = ? AND paused = 0 "
                        "AND mode = 'both' ORDER BY id", (card_id,)).fetchall()
    return [_load(row) for row in rows]


def count(conn: sqlite3.Connection, card_id: int) -> int:
    row = conn.execute("SELECT COUNT(*) FROM card_subscription WHERE card_id = ?", (card_id,)).fetchone()
    return int(row[0])


def add(conn: sqlite3.Connection, card_id: int, guild_id: str, channel_id: str) -> int:
    """Adds an active redirect subscription with no message and returns its id. The cap is checked here, in the same
    transaction as the insert. A channel already subscribed to the card is refused by the primary key."""
    if count(conn, card_id) >= MAX_SUBSCRIPTIONS:
        raise SubscriptionFull()
    cur = conn.execute("INSERT INTO card_subscription (card_id, guild_id, channel_id, paused) VALUES (?, ?, ?, 0)",
                       (card_id, guild_id, channel_id))
    return int(cur.lastrowid)


def remove(conn: sqlite3.Connection, card_id: int, channel_id: str) -> bool:
    cur = conn.execute("DELETE FROM card_subscription WHERE card_id = ? AND channel_id = ?", (card_id, channel_id))
    return cur.rowcount == 1


def remove_by_id(conn: sqlite3.Connection, sub_id: int) -> bool:
    cur = conn.execute("DELETE FROM card_subscription WHERE id = ?", (sub_id,))
    return cur.rowcount == 1


def set_mode_if(conn: sqlite3.Connection, sub_id: int, expected: str, mode: str) -> bool:
    """Changes the mode only while it is still the expected one. Returns whether the row changed."""
    cur = conn.execute("UPDATE card_subscription SET mode = ? WHERE id = ? AND mode = ?", (mode, sub_id, expected))
    return cur.rowcount == 1


def set_local_channel(conn: sqlite3.Connection, sub_id: int, channel_id: str | None) -> None:
    conn.execute("UPDATE card_subscription SET local_channel_id = ? WHERE id = ?", (channel_id, sub_id))


def pause(conn: sqlite3.Connection, card_id: int, channel_id: str) -> bool:
    """Pauses an active subscription. Returns whether it changed, so the caller alerts the admins once per pause."""
    cur = conn.execute("UPDATE card_subscription SET paused = 1 WHERE card_id = ? AND channel_id = ? AND paused = 0",
                       (card_id, channel_id))
    return cur.rowcount == 1


def resume(conn: sqlite3.Connection, card_id: int, channel_id: str) -> bool:
    """Unpauses a paused subscription. Its message and hash are kept. Returns whether it changed."""
    cur = conn.execute("UPDATE card_subscription SET paused = 0 WHERE card_id = ? AND channel_id = ? AND paused = 1",
                       (card_id, channel_id))
    return cur.rowcount == 1


def set_message(conn: sqlite3.Connection, card_id: int, channel_id: str, message_id: str, content_hash: str) -> None:
    conn.execute("UPDATE card_subscription SET message_id = ?, content_hash = ? WHERE card_id = ? AND channel_id = ?",
                 (message_id, content_hash, card_id, channel_id))


def set_hash_if(conn: sqlite3.Connection, card_id: int, channel_id: str, message_id: str, content_hash: str) -> bool:
    """Stores the hash only while the subscription still holds this message. Returns whether a row changed."""
    cur = conn.execute("UPDATE card_subscription SET content_hash = ? WHERE card_id = ? AND channel_id = ? "
                       "AND message_id = ?", (content_hash, card_id, channel_id, message_id))
    return cur.rowcount == 1


def clear_message_if(conn: sqlite3.Connection, channel_id: str, message_id: str) -> int | None:
    """Forgets a subscription's message only if it is still this one. Returns the card id, or None when it moved on."""
    rows = conn.execute(
        "UPDATE card_subscription SET message_id = NULL, content_hash = NULL "
        "WHERE channel_id = ? AND message_id = ? RETURNING card_id", (channel_id, message_id)).fetchall()
    return rows[0][0] if rows else None


def clear_messages(conn: sqlite3.Connection, card_id: int) -> None:
    """Forgets the messages of every copy of the card. The rows stay, so publishing again posts them afresh."""
    conn.execute("UPDATE card_subscription SET message_id = NULL, content_hash = NULL WHERE card_id = ?", (card_id,))
