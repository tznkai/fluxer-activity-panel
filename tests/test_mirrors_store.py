# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The mirror_message store against a real SQLite file: one row per home object and both subscription, found by
object, by message and by subscription, with compare-and-set writes and cascades from the home object."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fluxer_panel import cards, mirrors, play_now, polls, sessions, subscriptions
from fluxer_panel.db import Database

GUILD = "1001"
HOME_CHANNEL = "2001"
CHANNEL_A = "2002"
CHANNEL_B = "2003"
HASH = "a" * 64
NEW_HASH = "b" * 64


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


def _both_sub(conn: sqlite3.Connection, card_id: int, channel_id: str) -> int:
    sub_id = subscriptions.add(conn, card_id, GUILD, channel_id)
    assert subscriptions.set_mode_if(conn, sub_id, "redirect", "both")
    return sub_id


@pytest.fixture
async def world(db: Database) -> dict[str, int]:
    """A card with two both subscriptions and one home object of each kind."""
    def build(conn: sqlite3.Connection) -> dict[str, int]:
        card_id = cards.create_card(conn, slug="mc", title="Minecraft", author_id="1001", now=1)
        sub_a = _both_sub(conn, card_id, CHANNEL_A)
        sub_b = _both_sub(conn, card_id, CHANNEL_B)
        session_id = sessions.add(conn, card_id=card_id, guild_id=GUILD, channel_id=HOME_CHANNEL,
                                  message_id="5001", starts_at=1000, ends_at=2000, proposer_id="1101", now=1)
        poll_id = polls.create(conn, card_id=card_id, guild_id=GUILD, channel_id=HOME_CHANNEL, message_id="5002",
                               starter_id="1101", kind="approval", parent_poll_id=None, now=1, closes_at=9000,
                               ballot_size=1, options=[(1000, "offered")], recipients=[])
        play_now_id = play_now.add(conn, card_id=card_id, guild_id=GUILD, channel_id=HOME_CHANNEL,
                                   message_id="5003", presser_id="1101", offers_wake=False, now=1, expires_at=9000)
        return {"card": card_id, "sub_a": sub_a, "sub_b": sub_b, "session": session_id, "poll": poll_id,
                "play_now": play_now_id}
    return await db.run(build)


def _add(conn: sqlite3.Connection, *, sub_id: int, channel_id: str, message_id: str, kind: str, object_id: int,
         content_hash: str | None = HASH) -> int:
    return mirrors.add(conn, sub_id=sub_id, channel_id=channel_id, message_id=message_id, kind=kind,
                       object_id=object_id, content_hash=content_hash)


async def test_a_session_mirror_round_trips(db: Database, world: dict[str, int]) -> None:
    mirror_id = await db.run(lambda c: _add(c, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6001",
                                            kind="session", object_id=world["session"]))
    found = await db.run(lambda c: mirrors.get(c, "session", world["session"], world["sub_a"]))
    assert found == mirrors.Mirror(id=mirror_id, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6001",
                                   content_hash=HASH, kind="session", object_id=world["session"])


async def test_a_poll_mirror_round_trips_with_no_hash(db: Database, world: dict[str, int]) -> None:
    mirror_id = await db.run(lambda c: _add(c, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6002",
                                            kind="poll", object_id=world["poll"], content_hash=None))
    found = await db.run(lambda c: mirrors.get(c, "poll", world["poll"], world["sub_a"]))
    assert found == mirrors.Mirror(id=mirror_id, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6002",
                                   content_hash=None, kind="poll", object_id=world["poll"])


async def test_a_play_now_mirror_round_trips(db: Database, world: dict[str, int]) -> None:
    mirror_id = await db.run(lambda c: _add(c, sub_id=world["sub_b"], channel_id=CHANNEL_B, message_id="6003",
                                            kind="play_now", object_id=world["play_now"]))
    found = await db.run(lambda c: mirrors.get(c, "play_now", world["play_now"], world["sub_b"]))
    assert found == mirrors.Mirror(id=mirror_id, sub_id=world["sub_b"], channel_id=CHANNEL_B, message_id="6003",
                                   content_hash=HASH, kind="play_now", object_id=world["play_now"])


async def test_get_by_message_id_finds_the_mirror_and_its_kind(db: Database, world: dict[str, int]) -> None:
    await db.run(lambda c: _add(c, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6004",
                                kind="poll", object_id=world["poll"]))
    found = await db.run(lambda c: mirrors.get_by_message_id(c, "6004"))
    assert found is not None
    assert (found.kind, found.object_id, found.sub_id) == ("poll", world["poll"], world["sub_a"])
    assert await db.run(lambda c: mirrors.get_by_message_id(c, "6999")) is None


async def test_for_object_returns_only_that_objects_mirrors_in_sub_order(db: Database, world: dict[str, int]) -> None:
    def build(conn: sqlite3.Connection) -> None:
        _add(conn, sub_id=world["sub_b"], channel_id=CHANNEL_B, message_id="6010", kind="session",
             object_id=world["session"])
        _add(conn, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6011", kind="session",
             object_id=world["session"])
        _add(conn, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6012", kind="poll",
             object_id=world["poll"])
    await db.run(build)
    found = await db.run(lambda c: mirrors.for_object(c, "session", world["session"]))
    assert [m.message_id for m in found] == ["6011", "6010"]
    assert [m.sub_id for m in found] == sorted(m.sub_id for m in found)


async def test_for_sub_returns_every_mirror_of_the_subscription(db: Database, world: dict[str, int]) -> None:
    def build(conn: sqlite3.Connection) -> None:
        _add(conn, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6020", kind="session",
             object_id=world["session"])
        _add(conn, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6021", kind="play_now",
             object_id=world["play_now"])
        _add(conn, sub_id=world["sub_b"], channel_id=CHANNEL_B, message_id="6022", kind="poll",
             object_id=world["poll"])
    await db.run(build)
    found = await db.run(lambda c: mirrors.for_sub(c, world["sub_a"]))
    assert sorted(m.message_id for m in found) == ["6020", "6021"]


async def test_set_hash_if_refuses_a_stale_message(db: Database, world: dict[str, int]) -> None:
    mirror_id = await db.run(lambda c: _add(c, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6030",
                                            kind="session", object_id=world["session"]))
    assert await db.run(lambda c: mirrors.set_hash_if(c, mirror_id, "6031", NEW_HASH)) is False
    assert await db.run(lambda c: mirrors.set_hash_if(c, mirror_id, "6030", NEW_HASH)) is True
    found = await db.run(lambda c: mirrors.get(c, "session", world["session"], world["sub_a"]))
    assert found is not None and found.content_hash == NEW_HASH


async def test_delete_if_refuses_a_stale_message(db: Database, world: dict[str, int]) -> None:
    mirror_id = await db.run(lambda c: _add(c, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6040",
                                            kind="session", object_id=world["session"]))
    assert await db.run(lambda c: mirrors.delete_if(c, mirror_id, "6041")) is False
    assert await db.run(lambda c: mirrors.get_by_message_id(c, "6040")) is not None
    assert await db.run(lambda c: mirrors.delete_if(c, mirror_id, "6040")) is True
    assert await db.run(lambda c: mirrors.get_by_message_id(c, "6040")) is None


async def test_delete_for_sub_removes_only_that_subscriptions_mirrors(db: Database, world: dict[str, int]) -> None:
    def build(conn: sqlite3.Connection) -> None:
        _add(conn, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6050", kind="session",
             object_id=world["session"])
        _add(conn, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6051", kind="poll",
             object_id=world["poll"])
        _add(conn, sub_id=world["sub_b"], channel_id=CHANNEL_B, message_id="6052", kind="poll",
             object_id=world["poll"])
    await db.run(build)
    assert await db.run(lambda c: mirrors.delete_for_sub(c, world["sub_a"])) == 2
    remaining = await db.run(lambda c: mirrors.for_object(c, "poll", world["poll"]))
    assert [m.message_id for m in remaining] == ["6052"]


async def test_a_duplicate_object_and_subscription_is_refused(db: Database, world: dict[str, int]) -> None:
    await db.run(lambda c: _add(c, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6060",
                                kind="session", object_id=world["session"]))
    with pytest.raises(sqlite3.IntegrityError):
        await db.run(lambda c: _add(c, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6061",
                                    kind="session", object_id=world["session"]))


async def test_deleting_the_home_session_cascades_its_mirrors(db: Database, world: dict[str, int]) -> None:
    await db.run(lambda c: _add(c, sub_id=world["sub_a"], channel_id=CHANNEL_A, message_id="6070",
                                kind="session", object_id=world["session"]))
    await db.run(lambda c: c.execute("DELETE FROM sessions WHERE id = ?", (world["session"],)))
    assert await db.run(lambda c: mirrors.get_by_message_id(c, "6070")) is None
