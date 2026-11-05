# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wake requests: a member asks for a card to be started. Synchronous SQL over one sqlite3 connection, run through
`await db.run(fn)` like the card store.

A requester has at most one open request per card (the partial unique index). `close` moves every open row of a card
to a closed status in one UPDATE ... RETURNING, so each request is closed, and its DM sent, exactly once.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

CLOSE_STATUSES = frozenset({"ready", "expired", "cleared"})


@dataclass(frozen=True)
class OpenSummary:
    card_id: int
    count: int        # distinct open requesters
    oldest_at: int    # min(created_at) of the open rows


@dataclass(frozen=True)
class AdminDm:
    card_id: int
    admin_id: str
    kind: str         # 'request' | 'signal'
    channel_id: str
    message_id: str
    opened_at: int


def add_open(conn: sqlite3.Connection, card_id: int, requester_id: str, now: int) -> bool:
    """Insert an open request. Returns False (no row) when this requester already has one open on this card."""
    cur = conn.execute(
        "INSERT INTO requests (kind, card_id, requester_id, status, created_at) VALUES ('wake', ?, ?, 'open', ?) "
        "ON CONFLICT (card_id, requester_id) WHERE status = 'open' DO NOTHING",
        (card_id, requester_id, now))
    return cur.rowcount == 1


def open_requesters(conn: sqlite3.Connection, card_id: int) -> list[str]:
    rows = conn.execute("SELECT requester_id FROM requests WHERE card_id = ? AND status = 'open' "
                        "ORDER BY created_at, id", (card_id,)).fetchall()
    return [row[0] for row in rows]


def open_counts(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute("SELECT card_id, COUNT(*) FROM requests WHERE status = 'open' GROUP BY card_id").fetchall()
    return {card_id: count for card_id, count in rows}


def open_summaries(conn: sqlite3.Connection) -> list[OpenSummary]:
    rows = conn.execute("SELECT card_id, COUNT(*), MIN(created_at) FROM requests WHERE status = 'open' "
                        "GROUP BY card_id ORDER BY card_id").fetchall()
    return [OpenSummary(card_id, count, oldest_at) for card_id, count, oldest_at in rows]


def close(conn: sqlite3.Connection, card_id: int, status: str, now: int) -> list[str]:
    """Close every open row of the card as `status`, atomically. Returns the requester ids closed (sorted).
    A second call returns []. This is the exactly-once source for close DMs."""
    if status not in CLOSE_STATUSES:
        raise ValueError(status)
    rows = conn.execute("UPDATE requests SET status = ?, closed_at = ? WHERE card_id = ? AND status = 'open' "
                        "RETURNING requester_id", (status, now, card_id)).fetchall()
    return sorted(row[0] for row in rows)


def get_dms(conn: sqlite3.Connection, card_id: int) -> list[AdminDm]:
    rows = conn.execute("SELECT card_id, admin_id, kind, channel_id, message_id, opened_at FROM wake_request_dm "
                        "WHERE card_id = ? ORDER BY admin_id", (card_id,)).fetchall()
    return [AdminDm(*row) for row in rows]


def put_dm(conn: sqlite3.Connection, dm: AdminDm) -> None:
    conn.execute(
        "INSERT INTO wake_request_dm (card_id, admin_id, kind, channel_id, message_id, opened_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (card_id, admin_id) DO UPDATE SET kind = excluded.kind, channel_id = excluded.channel_id, "
        "message_id = excluded.message_id, opened_at = excluded.opened_at",
        (dm.card_id, dm.admin_id, dm.kind, dm.channel_id, dm.message_id, dm.opened_at))


def drop_dms(conn: sqlite3.Connection, card_id: int) -> None:
    conn.execute("DELETE FROM wake_request_dm WHERE card_id = ?", (card_id,))
