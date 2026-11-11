# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Schema v2 constraints against a real SQLite file: id shapes, enums, ranges, cascades and column allowlist."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fluxer_panel.db import Database

# Copied from the schema after src/fluxer_panel/migrations/0002_cards.sql. Adding a column means updating this on purpose.
ALLOWED_COLUMNS: dict[str, set[str]] = {
    "cards": {"id", "title", "description", "channel_id", "message_id", "created_by", "approved_by",
              "created_at", "updated_at", "slug", "how_to_join", "first_time", "sort_order",
              "show_details", "state"},
    "card_location": {"card_id", "location_text"},
    "card_seats": {"card_id", "seats"},
    "card_voice": {"card_id", "slot", "guild_id", "channel_id", "channel_name", "verified", "suffix"},
    "card_text_channel": {"card_id", "slot", "guild_id", "channel_id", "channel_name", "verified", "suffix"},
    "card_address": {"card_id", "slot", "host", "port", "public", "suffix"},
    "card_manager": {"card_id", "kind", "ref"},
    "card_probe": {"card_id", "kind", "host", "port"},
    "card_runtime": {"card_id", "rendered_hash", "probe_failing_since", "probe_alert_sent"},
    "board": {"id", "channel_id", "message_id", "content_hash"},
    "card_subscription": {"card_id", "guild_id", "channel_id", "message_id", "content_hash", "paused"},
    "families": {"id", "created_at"},
    "family_members": {"family_id", "user_id", "relation"},
    "sessions": {"id", "card_id", "group_role_id", "starts_at", "ends_at", "location_text", "seats",
                 "recurrence", "recurrence_until", "proposer_id", "created_at", "channel_id", "message_id"},
    "rsvps": {"session_id", "user_id", "status", "responded_at"},
    "requests": {"id", "kind", "card_id", "requester_id", "status", "created_at", "closed_at"},
    "wake_request_dm": {"card_id", "admin_id", "kind", "channel_id", "message_id", "opened_at"},
    "card_follow_role": {"card_id", "guild_id", "role_id"},
    "card_play_now_channel": {"card_id", "guild_id", "channel_id", "channel_name", "verified"},
    "play_now": {"id", "card_id", "guild_id", "channel_id", "message_id", "presser_id", "offers_wake", "state",
                 "created_at", "expires_at"},
    "interactions": {"id", "enabled"},
    "card_interactions_off": {"card_id"},
    "reaction_degraded": {"channel_id", "alerted_at", "last_retry_at"},
}

STRICT_TABLES = {"card_location", "card_voice", "card_text_channel", "card_address", "card_manager", "card_probe",
                 "card_subscription",
                 "card_runtime", "board", "wake_request_dm", "card_follow_role", "card_play_now_channel",
                 "play_now", "interactions", "card_interactions_off", "reaction_degraded"}

FORBIDDEN_TOKENS = {"nick", "nickname", "age", "birth", "birthday", "dob", "gender",
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
    await _exec(db, "INSERT INTO cards (title, created_by, created_at, updated_at) "
                    "VALUES ('t', '1001', 1, 1)")
    await _exec(db, "INSERT INTO sessions (card_id, group_role_id, starts_at, ends_at, proposer_id, created_at) "
                    "VALUES (1, '2001', 10, 20, '1001', 1)")


@pytest.fixture
async def seeded(db: Database) -> Database:
    await _seed(db)
    return db


# AC1.3 ----------------------------------------------------------------------------------------------

# The suffix CHECK, the second layer behind card_fields.suffix: one insert per table, each with a bound suffix.
SUFFIX_TABLES = [
    ("card_voice", "(card_id,slot,guild_id,channel_id,verified,suffix)", "(1,1,'1001','1002',1,?)"),
    ("card_text_channel", "(card_id,slot,guild_id,channel_id,verified,suffix)", "(1,1,'1001','1002',1,?)"),
    ("card_address", "(card_id,slot,host,port,suffix)", "(1,1,'h.invalid',1,?)"),
]
BAD_SUFFIXES = [("is empty", ""), ("is 21 characters", "s" * 21), ("has a leading space", " Java"),
                ("has a double space", "Raid  night"), ("has a newline", "Raid\nnight"),
                *[(f"has {ch!r}", f"a{ch}b") for ch in "()`*_~|>#[]@<:\\"]]


REJECTED: list[tuple[str, str, tuple[object, ...]]] = [
    ("created_by is letters", "INSERT INTO cards (title,created_by,created_at,updated_at) "
                              "VALUES ('t',?,1,1)", ("abc",)),
    ("created_by is empty", "INSERT INTO cards (title,created_by,created_at,updated_at) "
                            "VALUES ('t',?,1,1)", ("",)),
    ("created_by is 26 digits", "INSERT INTO cards (title,created_by,created_at,updated_at) "
                                "VALUES ('t',?,1,1)", ("9" * 26,)),
    ("created_by has a space", "INSERT INTO cards (title,created_by,created_at,updated_at) "
                               "VALUES ('t',?,1,1)", ("12 3",)),
    ("slug has an uppercase letter", "INSERT INTO cards (title,created_by,created_at,updated_at,slug) "
                                     "VALUES ('t','1001',1,1,?)", ("Bad",)),
    ("slug has an underscore", "INSERT INTO cards (title,created_by,created_at,updated_at,slug) "
                               "VALUES ('t','1001',1,1,?)", ("a_b",)),
    ("slug is empty", "INSERT INTO cards (title,created_by,created_at,updated_at,slug) "
                      "VALUES ('t','1001',1,1,?)", ("",)),
    ("slug is 33 characters", "INSERT INTO cards (title,created_by,created_at,updated_at,slug) "
                              "VALUES ('t','1001',1,1,?)", ("a" * 33,)),
    ("state is gone", "INSERT INTO cards (title,created_by,created_at,updated_at,state) "
                      "VALUES ('t','1001',1,1,'gone')", ()),
    ("show_details is an unknown item", "INSERT INTO cards (title,created_by,created_at,updated_at,show_details) "
                                        "VALUES ('t','1001',1,1,'players')", ()),
    ("show_details is out of canonical order", "INSERT INTO cards (title,created_by,created_at,updated_at,show_details) "
                                               "VALUES ('t','1001',1,1,'version,map')", ()),
    ("sort_order is negative", "INSERT INTO cards (title,created_by,created_at,updated_at,sort_order) "
                               "VALUES ('t','1001',1,1,-1)", ()),
    ("rsvp status is unknown", "INSERT INTO rsvps (session_id,user_id,status,responded_at) "
                               "VALUES (1,'1001','perhaps',1)", ()),
    ("family relation is unknown", "INSERT INTO family_members (family_id,user_id,relation) "
                                   "VALUES (1,'1001','cousin')", ()),
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
    ("location text is empty", "INSERT INTO card_location (card_id,location_text) VALUES (1,'')", ()),
    ("voice guild id has a letter", "INSERT INTO card_voice (card_id,slot,guild_id,channel_id,verified) "
                                    "VALUES (1,1,'12a','1002',1)", ()),
    ("voice channel id has a letter", "INSERT INTO card_voice (card_id,slot,guild_id,channel_id,verified) "
                                      "VALUES (1,1,'1001','12a',1)", ()),
    ("voice is unverified but named", "INSERT INTO card_voice (card_id,slot,guild_id,channel_id,channel_name,verified) "
                                      "VALUES (1,1,'1001','1002','SENTINEL-VOICE',0)", ()),
    ("voice name is 101 characters", "INSERT INTO card_voice (card_id,slot,guild_id,channel_id,channel_name,verified) "
                                     "VALUES (1,1,'1001','1002',?,1)", ("n" * 101,)),
    ("voice name is empty", "INSERT INTO card_voice (card_id,slot,guild_id,channel_id,channel_name,verified) "
                            "VALUES (1,1,'1001','1002','',1)", ()),
    ("voice channel id is 26 digits", "INSERT INTO card_voice (card_id,slot,guild_id,channel_id,verified) "
                                      "VALUES (1,1,'1001',?,1)", ("2" * 26,)),
    ("voice verified is two", "INSERT INTO card_voice (card_id,slot,guild_id,channel_id,verified) "
                              "VALUES (1,1,'1001','1002',2)", ()),
    ("text channel slot is zero", "INSERT INTO card_text_channel (card_id,slot,guild_id,channel_id,verified) "
                                  "VALUES (1,0,'1001','1002',1)", ()),
    ("text channel slot is four", "INSERT INTO card_text_channel (card_id,slot,guild_id,channel_id,verified) "
                                  "VALUES (1,4,'1001','1002',1)", ()),
    ("text channel guild id has a letter", "INSERT INTO card_text_channel (card_id,slot,guild_id,channel_id,verified) "
                                           "VALUES (1,1,'12a','1002',1)", ()),
    ("text channel is unverified but named", "INSERT INTO card_text_channel (card_id,slot,guild_id,channel_id,channel_name,verified) "
                                             "VALUES (1,1,'1001','1002','SENTINEL-TEXT',0)", ()),
    ("text channel name is 101 characters", "INSERT INTO card_text_channel (card_id,slot,guild_id,channel_id,channel_name,verified) "
                                            "VALUES (1,1,'1001','1002',?,1)", ("n" * 101,)),
    ("text channel name is empty", "INSERT INTO card_text_channel (card_id,slot,guild_id,channel_id,channel_name,verified) "
                                   "VALUES (1,1,'1001','1002','',1)", ()),
    ("text channel id is 26 digits", "INSERT INTO card_text_channel (card_id,slot,guild_id,channel_id,verified) "
                                     "VALUES (1,1,'1001',?,1)", ("2" * 26,)),
    ("text channel verified is two", "INSERT INTO card_text_channel (card_id,slot,guild_id,channel_id,verified) "
                                     "VALUES (1,1,'1001','1002',2)", ()),
    ("address port is zero", "INSERT INTO card_address (card_id,slot,host,port) VALUES (1,1,'h.invalid',0)", ()),
    ("address port is 65536", "INSERT INTO card_address (card_id,slot,host,port) VALUES (1,1,'h.invalid',65536)", ()),
    ("address public is two", "INSERT INTO card_address (card_id,slot,host,port,public) VALUES (1,1,'h.invalid',1,2)",
     ()),
    ("voice slot is four", "INSERT INTO card_voice (card_id,slot,guild_id,channel_id,verified) "
                           "VALUES (1,4,'1001','1002',1)", ()),
    ("address slot is zero", "INSERT INTO card_address (card_id,slot,host,port) VALUES (1,0,'h.invalid',1)", ()),
    *[(f"{table} suffix {label}", f"INSERT INTO {table} {columns} VALUES {values}", (suffix,))
      for table, columns, values in SUFFIX_TABLES for label, suffix in BAD_SUFFIXES],
    ("manager kind is uppercase", "INSERT INTO card_manager (card_id,kind,ref) VALUES (1,'Ptero','r')", ()),
    ("probe port is zero", "INSERT INTO card_probe (card_id,kind,host,port) VALUES (1,'a2s','h.invalid',0)", ()),
    ("probe port is 65536", "INSERT INTO card_probe (card_id,kind,host,port) VALUES (1,'a2s','h.invalid',65536)", ()),
    ("runtime hash is not hex", "INSERT INTO card_runtime (card_id,rendered_hash) VALUES (1,?)", ("G" * 64,)),
    ("session channel id has a letter", "INSERT INTO sessions (card_id,group_role_id,starts_at,ends_at,channel_id,proposer_id,created_at) "
                                        "VALUES (1,'2001',10,20,'x1','1001',1)", ()),
    ("session message id has a letter", "INSERT INTO sessions (card_id,group_role_id,starts_at,ends_at,message_id,proposer_id,created_at) "
                                        "VALUES (1,'2001',10,20,'x1','1001',1)", ()),
    ("subscription guild id has a letter", "INSERT INTO card_subscription (card_id,guild_id,channel_id,paused) "
                                           "VALUES (1,'12a','2001',0)", ()),
    ("subscription guild id is 26 digits", "INSERT INTO card_subscription (card_id,guild_id,channel_id,paused) "
                                           "VALUES (1,?,'2001',0)", ("1" * 26,)),
    ("subscription channel id has a letter", "INSERT INTO card_subscription (card_id,guild_id,channel_id,paused) "
                                             "VALUES (1,'1001','20x1',0)", ()),
    ("subscription channel id is 26 digits", "INSERT INTO card_subscription (card_id,guild_id,channel_id,paused) "
                                             "VALUES (1,'1001',?,0)", ("2" * 26,)),
    ("subscription message id has a letter", "INSERT INTO card_subscription (card_id,guild_id,channel_id,message_id,paused) "
                                             "VALUES (1,'1001','2001','x1',0)", ()),
    ("subscription hash without a message", "INSERT INTO card_subscription (card_id,guild_id,channel_id,content_hash,paused) "
                                            "VALUES (1,'1001','2001',?,0)", ("a" * 64,)),
    ("subscription hash is not hex", "INSERT INTO card_subscription (card_id,guild_id,channel_id,message_id,content_hash,paused) "
                                     "VALUES (1,'1001','2001','5001',?,0)", ("G" * 64,)),
    ("subscription paused is two", "INSERT INTO card_subscription (card_id,guild_id,channel_id,paused) "
                                   "VALUES (1,'1001','2001',2)", ()),
    ("request status is done", "INSERT INTO requests (kind,card_id,requester_id,status,created_at) "
                               "VALUES ('wake',1,'1001','done',1)", ()),
    ("open request has a close time", "INSERT INTO requests (kind,card_id,requester_id,status,created_at,closed_at) "
                                      "VALUES ('wake',1,'1001','open',1,2)", ()),
    ("closed request has no close time", "INSERT INTO requests (kind,card_id,requester_id,status,created_at) "
                                         "VALUES ('wake',1,'1001','cleared',1)", ()),
    ("play-now state is open", "INSERT INTO play_now (card_id,guild_id,channel_id,message_id,presser_id,offers_wake,"
                               "state,created_at,expires_at) VALUES (1,'1001','1002','5001','1001',0,'open',1,2)", ()),
    ("play-now expires before it is created", "INSERT INTO play_now (card_id,guild_id,channel_id,message_id,presser_id,"
                                              "offers_wake,state,created_at,expires_at) "
                                              "VALUES (1,'1001','1002','5001','1001',0,'live',10,5)", ()),
    ("interactions row id is two", "INSERT INTO interactions (id,enabled) VALUES (2,1)", ()),
    ("degraded channel id has a letter", "INSERT INTO reaction_degraded (channel_id,alerted_at,last_retry_at) "
                                         "VALUES ('12a',1,1)", ()),
    ("play-now channel is unverified but named", "INSERT INTO card_play_now_channel (card_id,guild_id,channel_id,"
                                                 "channel_name,verified) VALUES (1,'1001','1002','SENTINEL-PN',0)", ()),
]


@pytest.mark.parametrize(("label", "sql", "params"), REJECTED, ids=[case[0] for case in REJECTED])
async def test_schema_rejects_invalid_row(seeded: Database, label: str, sql: str, params: tuple[object, ...]) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        await _exec(seeded, sql, params)


async def test_text_channel_slots_are_unique_per_card_and_all_three_fit(seeded: Database) -> None:
    for slot in (1, 2, 3):
        await _exec(seeded, "INSERT INTO card_text_channel (card_id,slot,guild_id,channel_id,verified) "
                            "VALUES (1,?,'1001','1002',1)", (slot,))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        await _exec(seeded, "INSERT INTO card_text_channel (card_id,slot,guild_id,channel_id,verified) "
                            "VALUES (1,2,'1001','1003',1)")
    count = await seeded.run(lambda c: c.execute("SELECT COUNT(*) FROM card_text_channel").fetchone()[0])
    assert count == 3


async def test_deleting_a_card_removes_its_text_channels(seeded: Database) -> None:
    await _exec(seeded, "INSERT INTO cards (title,created_by,created_at,updated_at) VALUES ('t2','1001',1,1)")
    await _exec(seeded, "INSERT INTO card_text_channel (card_id,slot,guild_id,channel_id,verified) "
                        "VALUES (2,1,'1001','1002',1)")
    await _exec(seeded, "DELETE FROM cards WHERE id = 2")
    count = await seeded.run(lambda c: c.execute("SELECT COUNT(*) FROM card_text_channel").fetchone()[0])
    assert count == 0


async def test_duplicate_slug_is_rejected(seeded: Database) -> None:
    await _exec(seeded, "UPDATE cards SET slug = 'same-slug' WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        await _exec(seeded, "INSERT INTO cards (title,created_by,created_at,updated_at,slug) "
                            "VALUES ('t2','1002',1,1,'same-slug')")


async def test_strict_table_rejects_text_for_integer_column(seeded: Database) -> None:
    with pytest.raises(sqlite3.Error):
        await _exec(seeded, "INSERT INTO sessions (card_id,group_role_id,starts_at,ends_at,proposer_id,created_at) "
                            "VALUES (1,'2001','soon',20,'1001',1)")


async def test_valid_base_rows_insert_and_null_approved_by_is_accepted(seeded: Database) -> None:
    count = await seeded.run(lambda c: c.execute("SELECT COUNT(*) FROM cards WHERE approved_by IS NULL").fetchone()[0])
    assert count == 1
    assert await seeded.run(lambda c: c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]) == 1


async def test_card_state_defaults_to_draft(seeded: Database) -> None:
    state = await seeded.run(lambda c: c.execute("SELECT state FROM cards WHERE id = 1").fetchone()[0])
    assert state == "draft"


# AC1.2 ----------------------------------------------------------------------------------------------

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


async def test_retired_columns_and_tables_are_gone(seeded: Database) -> None:
    def introspect(c: sqlite3.Connection) -> tuple[set[str], set[str]]:
        card_columns = {row[1] for row in c.execute("PRAGMA table_info(cards)")}
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return card_columns, names

    card_columns, names = await seeded.run(introspect)

    assert "kind" not in card_columns
    assert "archived" not in card_columns
    assert "card_server" not in names


async def test_every_v1_and_v2_table_is_strict(seeded: Database) -> None:
    rows = await seeded.run(lambda c: c.execute("PRAGMA table_list").fetchall())
    strict = {row[1]: bool(row[-1]) for row in rows if not row[1].startswith("sqlite_")}

    assert {name for name, is_strict in strict.items() if is_strict} >= STRICT_TABLES | {
        "cards", "card_seats", "families", "family_members", "sessions", "rsvps", "requests"}
    assert all(strict[name] for name in STRICT_TABLES)


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


async def test_deleting_card_cascades_its_slot_and_runtime_rows(seeded: Database) -> None:
    await _exec(seeded, "INSERT INTO card_location (card_id,location_text) VALUES (1,'SENTINEL-LOC')")
    await _exec(seeded, "INSERT INTO card_runtime (card_id) VALUES (1)")
    await _exec(seeded, "DELETE FROM sessions WHERE id = 1")

    await _exec(seeded, "DELETE FROM cards WHERE id = 1")

    location_rows = await seeded.run(lambda c: c.execute("SELECT COUNT(*) FROM card_location").fetchone()[0])
    runtime_rows = await seeded.run(lambda c: c.execute("SELECT COUNT(*) FROM card_runtime").fetchone()[0])
    assert (location_rows, runtime_rows) == (0, 0)


async def test_deleting_family_removes_its_members(seeded: Database) -> None:
    await _exec(seeded, "INSERT INTO family_members (family_id,user_id,relation) VALUES (1,'1001','parent')")
    await _exec(seeded, "INSERT INTO family_members (family_id,user_id,relation) VALUES (1,'1002','child')")

    await _exec(seeded, "DELETE FROM families WHERE id = 1")

    assert await seeded.run(lambda c: c.execute("SELECT COUNT(*) FROM family_members").fetchone()[0]) == 0


async def test_request_without_a_card_is_rejected(seeded: Database) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL constraint failed"):
        await _exec(seeded, "INSERT INTO requests (kind,card_id,requester_id,status,created_at) "
                            "VALUES ('wake',NULL,'1001','open',1)")


async def test_second_open_request_by_the_same_requester_on_a_card_is_rejected(seeded: Database) -> None:
    await _exec(seeded, "INSERT INTO requests (kind,card_id,requester_id,status,created_at) "
                        "VALUES ('wake',1,'1002','open',1)")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        await _exec(seeded, "INSERT INTO requests (kind,card_id,requester_id,status,created_at) "
                            "VALUES ('wake',1,'1002','open',2)")


async def test_a_closed_request_does_not_block_a_new_open_one(seeded: Database) -> None:
    await _exec(seeded, "INSERT INTO requests (kind,card_id,requester_id,status,created_at,closed_at) "
                        "VALUES ('wake',1,'1002','cleared',1,1)")
    await _exec(seeded, "INSERT INTO requests (kind,card_id,requester_id,status,created_at) "
                        "VALUES ('wake',1,'1002','open',2)")
    count = await seeded.run(lambda c: c.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
    assert count == 2


async def test_follow_role_id_is_unique_across_cards(seeded: Database) -> None:
    await _exec(seeded, "INSERT INTO cards (title,created_by,created_at,updated_at) VALUES ('t2','1001',1,1)")
    await _exec(seeded, "INSERT INTO card_follow_role (card_id,guild_id,role_id) VALUES (1,'1001','3001')")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        await _exec(seeded, "INSERT INTO card_follow_role (card_id,guild_id,role_id) VALUES (2,'1001','3001')")


async def test_deleting_a_card_removes_its_reaction_rows(seeded: Database) -> None:
    await _exec(seeded, "DELETE FROM sessions WHERE id = 1")
    await _exec(seeded, "INSERT INTO requests (kind,card_id,requester_id,status,created_at) "
                        "VALUES ('wake',1,'1002','open',1)")
    await _exec(seeded, "INSERT INTO wake_request_dm (card_id,admin_id,kind,channel_id,message_id,opened_at) "
                        "VALUES (1,'1003','request','1004','1005',1)")
    await _exec(seeded, "INSERT INTO card_follow_role (card_id,guild_id,role_id) VALUES (1,'1001','3001')")
    await _exec(seeded, "INSERT INTO card_play_now_channel (card_id,guild_id,channel_id,verified) "
                        "VALUES (1,'1001','1002',0)")
    await _exec(seeded, "INSERT INTO play_now (card_id,guild_id,channel_id,message_id,presser_id,offers_wake,state,"
                        "created_at,expires_at) VALUES (1,'1001','1002','5001','1001',0,'live',1,2)")
    await _exec(seeded, "INSERT INTO card_interactions_off (card_id) VALUES (1)")

    await _exec(seeded, "DELETE FROM cards WHERE id = 1")

    def counts(c: sqlite3.Connection) -> list[int]:
        tables = ["requests", "wake_request_dm", "card_follow_role", "card_play_now_channel", "play_now",
                  "card_interactions_off"]
        return [c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables]

    assert await seeded.run(counts) == [0, 0, 0, 0, 0, 0]
