# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Card store: synchronous SQL over one sqlite3 connection. Callers run each function through `await db.run(fn)`.

Only rows with a slug are visible here. Rows without one (v1 test data) are never read or listed.
This module never logs card text.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

SHOW_ITEMS = ("map", "version", "game_mode")      # canonical order
CARD_STATES = ("draft", "live", "archived")

# Slot name -> table. Fixed here so no user input ever reaches a table name.
SLOT_TABLES = {
    "voice": "card_voice",
    "address": "card_address",
    "location": "card_location",
    "seats": "card_seats",
    "manager": "card_manager",
    "probe": "card_probe",
}

# Base columns that update_fields may write. Anything else is a programming error.
UPDATABLE = frozenset({"title", "description", "how_to_join", "first_time", "sort_order", "show_details"})


class CardFieldError(ValueError):
    """A field value is invalid. Carries the field and a rule key; never the value."""

    def __init__(self, field: str, rule: str) -> None:
        super().__init__(f"{field}: {rule}")
        self.field = field
        self.rule = rule


@dataclass(frozen=True)
class Voice:
    guild_id: str
    channel_id: str
    channel_name: str | None
    verified: bool


@dataclass(frozen=True)
class Address:
    host: str
    port: int
    public: bool


@dataclass(frozen=True)
class Location:
    text: str = field(repr=False)


@dataclass(frozen=True)
class ManagerRef:
    kind: str
    ref: str


@dataclass(frozen=True)
class ProbeRef:
    kind: str
    host: str
    port: int


@dataclass(frozen=True)
class Runtime:
    rendered_hash: str | None = None
    probe_failing_since: int | None = None
    probe_alert_sent: bool = False


@dataclass(frozen=True)
class Card:
    id: int
    slug: str
    title: str
    description: str | None
    how_to_join: str | None
    first_time: str | None
    sort_order: int
    show_details: tuple[str, ...]   # subset of SHOW_ITEMS, canonical order
    state: str
    channel_id: str | None
    message_id: str | None
    created_by: str
    voice: Voice | None = None
    address: Address | None = None
    location: Location | None = None
    seats: int | None = None
    manager: ManagerRef | None = None
    probe: ProbeRef | None = None
    runtime: Runtime = Runtime()

    @property
    def has_status_source(self) -> bool:
        return self.manager is not None or self.probe is not None


_BASE_COLUMNS = ("id, slug, title, description, how_to_join, first_time, sort_order, show_details, state, "
                 "channel_id, message_id, created_by")


# Creation and reads ----------------------------------------------------------------------------------

def create_card(conn: sqlite3.Connection, *, slug: str, title: str, author_id: str, now: int) -> int:
    if conn.execute("SELECT 1 FROM cards WHERE slug = ?", (slug,)).fetchone() is not None:
        raise CardFieldError("slug", "taken")
    try:
        cur = conn.execute(
            "INSERT INTO cards (slug, title, created_by, approved_by, created_at, updated_at, state) "
            "VALUES (?, ?, ?, ?, ?, ?, 'draft')",
            (slug, title, author_id, author_id, now, now))
    except sqlite3.IntegrityError as exc:
        # A concurrent insert of the same slug can get past the check above. Other constraint failures
        # are not the caller's slug problem, so they propagate unchanged.
        if "cards.slug" in str(exc):
            raise CardFieldError("slug", "taken") from None
        raise
    card_id = cur.lastrowid
    conn.execute("INSERT INTO card_runtime (card_id) VALUES (?)", (card_id,))
    return card_id


def get_by_slug(conn: sqlite3.Connection, slug: str) -> Card | None:
    row = conn.execute(f"SELECT {_BASE_COLUMNS} FROM cards WHERE slug = ? AND slug IS NOT NULL",
                       (slug,)).fetchone()
    return None if row is None else _load(conn, row)


def get_by_id(conn: sqlite3.Connection, card_id: int) -> Card | None:
    row = conn.execute(f"SELECT {_BASE_COLUMNS} FROM cards WHERE id = ? AND slug IS NOT NULL",
                       (card_id,)).fetchone()
    return None if row is None else _load(conn, row)


def get_by_message_id(conn: sqlite3.Connection, message_id: str) -> Card | None:
    row = conn.execute(f"SELECT {_BASE_COLUMNS} FROM cards WHERE message_id = ? AND slug IS NOT NULL "
                       "ORDER BY id LIMIT 1", (message_id,)).fetchone()
    return None if row is None else _load(conn, row)


def list_cards(conn: sqlite3.Connection, state: str | None = None) -> list[Card]:
    if state is None:
        rows = conn.execute(f"SELECT {_BASE_COLUMNS} FROM cards WHERE slug IS NOT NULL "
                            "ORDER BY sort_order, id").fetchall()
    else:
        rows = conn.execute(f"SELECT {_BASE_COLUMNS} FROM cards WHERE slug IS NOT NULL AND state = ? "
                            "ORDER BY sort_order, id", (state,)).fetchall()
    return [_load(conn, row) for row in rows]


def _load(conn: sqlite3.Connection, row: tuple) -> Card:
    (card_id, slug, title, description, how_to_join, first_time, sort_order, show_details,
     state, channel_id, message_id, created_by) = row
    return Card(
        id=card_id,
        slug=slug,
        title=title,
        description=description,
        how_to_join=how_to_join,
        first_time=first_time,
        sort_order=sort_order,
        show_details=tuple(item for item in show_details.split(",") if item),
        state=state,
        channel_id=channel_id,
        message_id=message_id,
        created_by=created_by,
        voice=_load_voice(conn, card_id),
        address=_load_address(conn, card_id),
        location=_load_location(conn, card_id),
        seats=_load_seats(conn, card_id),
        manager=_load_manager(conn, card_id),
        probe=_load_probe(conn, card_id),
        runtime=_load_runtime(conn, card_id),
    )


def _load_voice(conn: sqlite3.Connection, card_id: int) -> Voice | None:
    row = conn.execute("SELECT guild_id, channel_id, channel_name, verified FROM card_voice WHERE card_id = ?",
                       (card_id,)).fetchone()
    return None if row is None else Voice(row[0], row[1], row[2], bool(row[3]))


def _load_address(conn: sqlite3.Connection, card_id: int) -> Address | None:
    row = conn.execute("SELECT host, port, public FROM card_address WHERE card_id = ?", (card_id,)).fetchone()
    return None if row is None else Address(row[0], row[1], bool(row[2]))


def _load_location(conn: sqlite3.Connection, card_id: int) -> Location | None:
    row = conn.execute("SELECT location_text FROM card_location WHERE card_id = ?", (card_id,)).fetchone()
    return None if row is None else Location(row[0])


def _load_seats(conn: sqlite3.Connection, card_id: int) -> int | None:
    row = conn.execute("SELECT seats FROM card_seats WHERE card_id = ?", (card_id,)).fetchone()
    return None if row is None else row[0]


def _load_manager(conn: sqlite3.Connection, card_id: int) -> ManagerRef | None:
    row = conn.execute("SELECT kind, ref FROM card_manager WHERE card_id = ?", (card_id,)).fetchone()
    return None if row is None else ManagerRef(row[0], row[1])


def _load_probe(conn: sqlite3.Connection, card_id: int) -> ProbeRef | None:
    row = conn.execute("SELECT kind, host, port FROM card_probe WHERE card_id = ?", (card_id,)).fetchone()
    return None if row is None else ProbeRef(row[0], row[1], row[2])


def _load_runtime(conn: sqlite3.Connection, card_id: int) -> Runtime:
    row = conn.execute("SELECT rendered_hash, probe_failing_since, probe_alert_sent FROM card_runtime "
                       "WHERE card_id = ?", (card_id,)).fetchone()
    return Runtime() if row is None else Runtime(row[0], row[1], bool(row[2]))


# Base fields and state ---------------------------------------------------------------------------------

def update_fields(conn: sqlite3.Connection, card_id: int, now: int, **cols: Any) -> None:
    if not cols:
        # A programming error: with no column the UPDATE would be malformed SQL. Checked first so no SQL is built.
        raise ValueError("no fields to update")
    unknown = set(cols) - UPDATABLE
    if unknown:
        raise KeyError(sorted(unknown)[0])   # a programming error: the key names a column, never a value
    if "show_details" in cols:
        cols["show_details"] = _canonical_show_details(cols["show_details"])
    assignments = ", ".join(f"{name} = ?" for name in cols)   # names come from UPDATABLE only
    params = [*cols.values(), now, card_id]
    conn.execute(f"UPDATE cards SET {assignments}, updated_at = ? WHERE id = ?", params)


def _canonical_show_details(items: Iterable[str]) -> str:
    chosen = tuple(items)
    if any(item not in SHOW_ITEMS for item in chosen):
        raise CardFieldError("show_details", "unknown item")
    return ",".join(item for item in SHOW_ITEMS if item in chosen)


def set_state(conn: sqlite3.Connection, card_id: int, state: str, now: int) -> None:
    if state not in CARD_STATES:
        raise ValueError("unknown card state")
    conn.execute("UPDATE cards SET state = ?, updated_at = ? WHERE id = ?", (state, now, card_id))


# Slots -------------------------------------------------------------------------------------------------

def set_voice(conn: sqlite3.Connection, card_id: int, voice: Voice) -> None:
    conn.execute(
        "INSERT INTO card_voice (card_id, guild_id, channel_id, channel_name, verified) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(card_id) DO UPDATE SET guild_id = excluded.guild_id, channel_id = excluded.channel_id, "
        "channel_name = excluded.channel_name, verified = excluded.verified",
        (card_id, voice.guild_id, voice.channel_id, voice.channel_name, int(voice.verified)))


def set_voice_name_if(conn: sqlite3.Connection, card_id: int, expected: Voice, channel_name: str) -> bool:
    """Writes a refreshed channel name, and only while the stored voice is still the `expected` one, verified and
    under the same name. Never inserts a row and never changes the channel. Returns whether a row changed."""
    cur = conn.execute(
        "UPDATE card_voice SET channel_name = ? WHERE card_id = ? AND guild_id = ? AND channel_id = ? "
        "AND verified = 1 AND channel_name IS ?",
        (channel_name, card_id, expected.guild_id, expected.channel_id, expected.channel_name))
    return cur.rowcount == 1


def set_address(conn: sqlite3.Connection, card_id: int, address: Address) -> None:
    conn.execute(
        "INSERT INTO card_address (card_id, host, port, public) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(card_id) DO UPDATE SET host = excluded.host, port = excluded.port, public = excluded.public",
        (card_id, address.host, address.port, int(address.public)))


def set_address_public(conn: sqlite3.Connection, card_id: int, public: bool) -> bool:
    cur = conn.execute("UPDATE card_address SET public = ? WHERE card_id = ?", (int(public), card_id))
    return cur.rowcount == 1


def set_location(conn: sqlite3.Connection, card_id: int, location: Location) -> None:
    conn.execute(
        "INSERT INTO card_location (card_id, location_text) VALUES (?, ?) "
        "ON CONFLICT(card_id) DO UPDATE SET location_text = excluded.location_text",
        (card_id, location.text))


def set_seats(conn: sqlite3.Connection, card_id: int, seats: int) -> None:
    conn.execute(
        "INSERT INTO card_seats (card_id, seats) VALUES (?, ?) "
        "ON CONFLICT(card_id) DO UPDATE SET seats = excluded.seats",
        (card_id, seats))


def set_manager(conn: sqlite3.Connection, card_id: int, manager: ManagerRef) -> None:
    conn.execute(
        "INSERT INTO card_manager (card_id, kind, ref) VALUES (?, ?, ?) "
        "ON CONFLICT(card_id) DO UPDATE SET kind = excluded.kind, ref = excluded.ref",
        (card_id, manager.kind, manager.ref))


def set_probe(conn: sqlite3.Connection, card_id: int, probe: ProbeRef) -> None:
    conn.execute(
        "INSERT INTO card_probe (card_id, kind, host, port) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(card_id) DO UPDATE SET kind = excluded.kind, host = excluded.host, port = excluded.port",
        (card_id, probe.kind, probe.host, probe.port))


def clear_slot(conn: sqlite3.Connection, card_id: int, slot: str) -> None:
    table = SLOT_TABLES[slot]   # KeyError for an unknown slot; the table name is never built from input
    conn.execute(f"DELETE FROM {table} WHERE card_id = ?", (card_id,))


# Messages and runtime ----------------------------------------------------------------------------------

def set_message(conn: sqlite3.Connection, card_id: int, channel_id: str | None, message_id: str | None,
                rendered_hash: str | None) -> None:
    conn.execute("UPDATE cards SET channel_id = ?, message_id = ? WHERE id = ?", (channel_id, message_id, card_id))
    conn.execute("UPDATE card_runtime SET rendered_hash = ? WHERE card_id = ?", (rendered_hash, card_id))


def clear_message_if(conn: sqlite3.Connection, channel_id: str, message_id: str) -> int | None:
    """Clear the posted message only if it is still this one. Returns the card id, or None when it moved on."""
    rows = conn.execute(
        "UPDATE cards SET channel_id = NULL, message_id = NULL WHERE channel_id = ? AND message_id = ? "
        "RETURNING id", (channel_id, message_id)).fetchall()
    if not rows:
        return None
    card_id = rows[0][0]
    conn.execute("UPDATE card_runtime SET rendered_hash = NULL WHERE card_id = ?", (card_id,))
    return card_id


def set_probe_tracking(conn: sqlite3.Connection, card_id: int, failing_since: int | None, alert_sent: bool) -> None:
    conn.execute("UPDATE card_runtime SET probe_failing_since = ?, probe_alert_sent = ? WHERE card_id = ?",
                 (failing_since, int(alert_sent), card_id))
