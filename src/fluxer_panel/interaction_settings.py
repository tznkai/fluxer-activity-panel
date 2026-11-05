# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reaction settings: the global switch, the per-card off switch, and the channels where adding a reaction has failed.
Synchronous SQL over one sqlite3 connection, run through `await db.run(fn)` like the card store.

Each write reports whether it changed a row, so the caller alerts the admins once per change.
"""

from __future__ import annotations

import sqlite3


def enabled(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute("SELECT enabled FROM interactions WHERE id = 1").fetchone()[0])


def set_enabled(conn: sqlite3.Connection, on: bool) -> bool:
    cur = conn.execute("UPDATE interactions SET enabled = ? WHERE id = 1 AND enabled <> ?", (int(on), int(on)))
    return cur.rowcount == 1


def card_off(conn: sqlite3.Connection, card_id: int) -> bool:
    return conn.execute("SELECT 1 FROM card_interactions_off WHERE card_id = ?", (card_id,)).fetchone() is not None


def set_card_off(conn: sqlite3.Connection, card_id: int, off: bool) -> bool:
    if off:
        cur = conn.execute("INSERT INTO card_interactions_off (card_id) VALUES (?) "
                           "ON CONFLICT (card_id) DO NOTHING", (card_id,))
    else:
        cur = conn.execute("DELETE FROM card_interactions_off WHERE card_id = ?", (card_id,))
    return cur.rowcount == 1


def is_degraded(conn: sqlite3.Connection, channel_id: str) -> bool:
    return conn.execute("SELECT 1 FROM reaction_degraded WHERE channel_id = ?", (channel_id,)).fetchone() is not None


def mark_degraded(conn: sqlite3.Connection, channel_id: str, now: int) -> bool:
    """Marks the channel degraded. True only when it was not already, so the admin DM fires once."""
    cur = conn.execute("INSERT INTO reaction_degraded (channel_id, alerted_at, last_retry_at) VALUES (?, ?, ?) "
                       "ON CONFLICT (channel_id) DO NOTHING", (channel_id, now, now))
    return cur.rowcount == 1


def claim_retry(conn: sqlite3.Connection, channel_id: str, now: int, min_interval: int) -> bool:
    """Claims the next retry for a degraded channel, at most once per `min_interval` seconds. True when claimed."""
    cur = conn.execute("UPDATE reaction_degraded SET last_retry_at = ? WHERE channel_id = ? AND last_retry_at <= ?",
                       (now, channel_id, now - min_interval))
    return cur.rowcount == 1


def release_retry(conn: sqlite3.Connection, channel_id: str, now: int, min_interval: int) -> bool:
    """Gives back a claimed retry whose answer said nothing about permission, so the next claim succeeds at once.
    True when the channel is degraded."""
    cur = conn.execute("UPDATE reaction_degraded SET last_retry_at = ? WHERE channel_id = ?",
                       (now - min_interval, channel_id))
    return cur.rowcount == 1


def clear_degraded(conn: sqlite3.Connection, channel_id: str) -> bool:
    cur = conn.execute("DELETE FROM reaction_degraded WHERE channel_id = ?", (channel_id,))
    return cur.rowcount == 1
