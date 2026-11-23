# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Play-now posts: one row per press, live until its expiry, then expired. Synchronous SQL over one sqlite3
connection, run through `await db.run(fn)` like the card store.

`expire_if` changes a row only while it is live, so a row is expired once, whichever caller gets there first.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

_COLUMNS = ("id, card_id, guild_id, channel_id, message_id, presser_id, offers_wake, state, created_at, expires_at, "
            "scope_sub_id")


@dataclass(frozen=True)
class PlayNow:
    id: int
    card_id: int
    guild_id: str
    channel_id: str
    message_id: str
    presser_id: str
    offers_wake: bool
    state: str          # 'live' | 'expired'
    created_at: int
    expires_at: int
    scope_sub_id: int | None = None   # None is the home card; otherwise the subscription the post belongs to


def _load(row: tuple) -> PlayNow:
    (row_id, card_id, guild_id, channel_id, message_id, presser_id, offers_wake, state,
     created_at, expires_at, scope_sub_id) = row
    return PlayNow(row_id, card_id, guild_id, channel_id, message_id, presser_id, bool(offers_wake), state,
                   created_at, expires_at, scope_sub_id)


def add(conn: sqlite3.Connection, *, card_id: int, guild_id: str, channel_id: str, message_id: str,
        presser_id: str, offers_wake: bool, now: int, expires_at: int, scope_sub_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO play_now (card_id, guild_id, channel_id, message_id, presser_id, offers_wake, state, "
        "created_at, expires_at, scope_sub_id) VALUES (?, ?, ?, ?, ?, ?, 'live', ?, ?, ?)",
        (card_id, guild_id, channel_id, message_id, presser_id, int(offers_wake), now, expires_at, scope_sub_id))
    return int(cur.lastrowid)


def get_live_by_message(conn: sqlite3.Connection, message_id: str) -> PlayNow | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM play_now WHERE message_id = ? AND state = 'live'",
                       (message_id,)).fetchone()
    return None if row is None else _load(row)


def latest_for_scope(conn: sqlite3.Connection, card_id: int, scope_sub_id: int | None) -> PlayNow | None:
    """The newest play-now row of one scope of the card, in any state (NULL is the home card)."""
    row = conn.execute(f"SELECT {_COLUMNS} FROM play_now WHERE card_id = ? AND scope_sub_id IS ? "
                       "ORDER BY created_at DESC, id DESC LIMIT 1", (card_id, scope_sub_id)).fetchone()
    return None if row is None else _load(row)


def latest_for_card(conn: sqlite3.Connection, card_id: int) -> PlayNow | None:
    """The newest home play-now row of the card. A subscription's post never shows here."""
    return latest_for_scope(conn, card_id, None)


def live_for_scope(conn: sqlite3.Connection, card_id: int, scope_sub_id: int | None) -> list[PlayNow]:
    rows = conn.execute(f"SELECT {_COLUMNS} FROM play_now WHERE card_id = ? AND scope_sub_id IS ? AND state = 'live' "
                        "ORDER BY id", (card_id, scope_sub_id)).fetchall()
    return [_load(row) for row in rows]


def live_for_card(conn: sqlite3.Connection, card_id: int) -> list[PlayNow]:
    """Every live play-now row of the card, in every scope (archive)."""
    rows = conn.execute(f"SELECT {_COLUMNS} FROM play_now WHERE card_id = ? AND state = 'live' ORDER BY id",
                        (card_id,)).fetchall()
    return [_load(row) for row in rows]


def list_for_scope(conn: sqlite3.Connection, scope_sub_id: int) -> list[PlayNow]:
    """Every play-now row of one subscription scope, in any state, in id order."""
    rows = conn.execute(f"SELECT {_COLUMNS} FROM play_now WHERE scope_sub_id = ? ORDER BY id",
                        (scope_sub_id,)).fetchall()
    return [_load(row) for row in rows]


def due(conn: sqlite3.Connection, now: int) -> list[PlayNow]:
    rows = conn.execute(f"SELECT {_COLUMNS} FROM play_now WHERE state = 'live' AND expires_at <= ? ORDER BY id",
                        (now,)).fetchall()
    return [_load(row) for row in rows]


def expire_if(conn: sqlite3.Connection, play_now_id: int) -> bool:
    """Expires the row only while it is live. Returns whether it changed."""
    cur = conn.execute("UPDATE play_now SET state = 'expired' WHERE id = ? AND state = 'live'", (play_now_id,))
    return cur.rowcount == 1
