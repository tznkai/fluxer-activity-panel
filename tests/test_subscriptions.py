# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The subscription store against a real SQLite file: one row per card and channel, the cap of twenty per card, pause and
resume, the conditional message writes, modes and local channels, and the cascade when a card is deleted."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fluxer_panel import cards, subscriptions
from fluxer_panel.db import Database

GUILD = "1001"
CHANNELS = tuple(str(2000 + n) for n in range(1, 7))   # 2001 to 2006: six channels
CAP_CHANNELS = tuple(str(2100 + n) for n in range(1, 22))   # 2101 to 2121: one more than the cap of twenty
MESSAGE = "5001"
OTHER_MESSAGE = "5002"
HASH = "a" * 64
OTHER_HASH = "b" * 64


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def card_id(db: Database) -> int:
    return await db.run(lambda c: cards.create_card(c, slug="mc", title="Minecraft", author_id="1001", now=1))


async def fetch(db: Database, card_id: int, channel_id: str) -> subscriptions.Subscription | None:
    return await db.run(lambda c: subscriptions.get(c, card_id, channel_id))


async def test_add_stores_an_active_subscription_with_no_message(db: Database, card_id: int) -> None:
    sub_id = await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))
    assert await fetch(db, card_id, CHANNELS[0]) == subscriptions.Subscription(
        id=sub_id, card_id=card_id, guild_id=GUILD, channel_id=CHANNELS[0], message_id=None, content_hash=None,
        paused=False, mode="redirect", local_channel_id=None)


async def test_add_returns_an_increasing_id_for_each_channel(db: Database, card_id: int) -> None:
    first = await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))
    second = await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[1]))
    assert 0 < first < second


async def test_a_card_takes_twenty_channels_and_refuses_a_twenty_first(db: Database, card_id: int) -> None:
    for channel in CAP_CHANNELS[:20]:
        await db.run(lambda c, ch=channel: subscriptions.add(c, card_id, GUILD, ch))
    with pytest.raises(subscriptions.SubscriptionFull):
        await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CAP_CHANNELS[20]))
    assert await db.run(lambda c: subscriptions.count(c, card_id)) == 20
    assert await fetch(db, card_id, CAP_CHANNELS[20]) is None


def test_the_cap_is_twenty() -> None:
    assert subscriptions.MAX_SUBSCRIPTIONS == 20


async def test_the_cap_counts_paused_subscriptions_too(db: Database, card_id: int) -> None:
    for channel in CAP_CHANNELS[:20]:
        await db.run(lambda c, ch=channel: subscriptions.add(c, card_id, GUILD, ch))
    await db.run(lambda c: subscriptions.pause(c, card_id, CAP_CHANNELS[0]))
    with pytest.raises(subscriptions.SubscriptionFull):
        await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CAP_CHANNELS[20]))


async def test_the_cap_is_per_card(db: Database, card_id: int) -> None:
    other = await db.run(lambda c: cards.create_card(c, slug="ark", title="Ark", author_id="1001", now=1))
    for channel in CAP_CHANNELS[:20]:
        await db.run(lambda c, ch=channel: subscriptions.add(c, card_id, GUILD, ch))
    await db.run(lambda c: subscriptions.add(c, other, GUILD, CHANNELS[0]))
    assert await db.run(lambda c: subscriptions.count(c, other)) == 1


async def test_a_channel_already_subscribed_to_the_card_is_refused_by_the_key(db: Database, card_id: int) -> None:
    await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))
    with pytest.raises(sqlite3.IntegrityError):
        await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))


async def test_pause_marks_an_active_subscription_and_only_the_first_call_changes_it(db: Database,
                                                                                    card_id: int) -> None:
    await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))
    assert await db.run(lambda c: subscriptions.pause(c, card_id, CHANNELS[0])) is True
    assert await db.run(lambda c: subscriptions.pause(c, card_id, CHANNELS[0])) is False
    sub = await fetch(db, card_id, CHANNELS[0])
    assert sub is not None and sub.paused is True
    assert await db.run(lambda c: subscriptions.list_active(c, card_id)) == []


async def test_resume_clears_the_pause_and_keeps_the_message_it_had(db: Database, card_id: int) -> None:
    await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))
    await db.run(lambda c: subscriptions.set_message(c, card_id, CHANNELS[0], MESSAGE, HASH))
    await db.run(lambda c: subscriptions.pause(c, card_id, CHANNELS[0]))
    assert await db.run(lambda c: subscriptions.resume(c, card_id, CHANNELS[0])) is True
    assert await db.run(lambda c: subscriptions.resume(c, card_id, CHANNELS[0])) is False
    sub = await fetch(db, card_id, CHANNELS[0])
    assert sub is not None
    assert (sub.paused, sub.message_id, sub.content_hash) == (False, MESSAGE, HASH)
    assert [s.channel_id for s in await db.run(lambda c: subscriptions.list_active(c, card_id))] == [CHANNELS[0]]


async def test_set_hash_if_stores_the_hash_only_while_the_message_is_held(db: Database, card_id: int) -> None:
    await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))
    await db.run(lambda c: subscriptions.set_message(c, card_id, CHANNELS[0], MESSAGE, HASH))
    assert await db.run(lambda c: subscriptions.set_hash_if(c, card_id, CHANNELS[0], MESSAGE, OTHER_HASH)) is True
    assert await db.run(lambda c: subscriptions.set_hash_if(c, card_id, CHANNELS[0], OTHER_MESSAGE, HASH)) is False
    sub = await fetch(db, card_id, CHANNELS[0])
    assert sub is not None and sub.content_hash == OTHER_HASH


async def test_clear_message_if_forgets_only_the_message_it_names(db: Database, card_id: int) -> None:
    await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))
    await db.run(lambda c: subscriptions.set_message(c, card_id, CHANNELS[0], MESSAGE, HASH))
    assert await db.run(lambda c: subscriptions.clear_message_if(c, CHANNELS[0], OTHER_MESSAGE)) is None
    assert await db.run(lambda c: subscriptions.clear_message_if(c, CHANNELS[0], MESSAGE)) == card_id
    sub = await fetch(db, card_id, CHANNELS[0])
    assert sub is not None and (sub.message_id, sub.content_hash) == (None, None)
    # A hash write that was in flight for the forgotten message changes nothing.
    assert await db.run(lambda c: subscriptions.set_hash_if(c, card_id, CHANNELS[0], MESSAGE, OTHER_HASH)) is False


async def test_clear_messages_forgets_every_copy_and_keeps_the_rows(db: Database, card_id: int) -> None:
    for channel, message in ((CHANNELS[0], MESSAGE), (CHANNELS[1], OTHER_MESSAGE)):
        await db.run(lambda c, ch=channel: subscriptions.add(c, card_id, GUILD, ch))
        await db.run(lambda c, ch=channel, mid=message: subscriptions.set_message(c, card_id, ch, mid, HASH))
    await db.run(lambda c: subscriptions.clear_messages(c, card_id))
    for channel in (CHANNELS[0], CHANNELS[1]):
        sub = await fetch(db, card_id, channel)
        assert sub is not None and (sub.message_id, sub.content_hash) == (None, None)


async def test_remove_deletes_only_the_named_channel(db: Database, card_id: int) -> None:
    await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))
    await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[1]))
    assert await db.run(lambda c: subscriptions.remove(c, card_id, CHANNELS[0])) is True
    assert await db.run(lambda c: subscriptions.remove(c, card_id, CHANNELS[0])) is False
    assert await fetch(db, card_id, CHANNELS[0]) is None
    assert await fetch(db, card_id, CHANNELS[1]) is not None


async def test_list_for_card_keeps_the_order_the_channels_were_added(db: Database, card_id: int) -> None:
    for channel in (CHANNELS[2], CHANNELS[0], CHANNELS[1]):
        await db.run(lambda c, ch=channel: subscriptions.add(c, card_id, GUILD, ch))
    listed = await db.run(lambda c: subscriptions.list_for_card(c, card_id))
    assert [sub.channel_id for sub in listed] == [CHANNELS[2], CHANNELS[0], CHANNELS[1]]


async def test_deleting_the_card_removes_its_subscriptions(db: Database, card_id: int) -> None:
    await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))
    await db.run(lambda c: c.execute("DELETE FROM cards WHERE id = ?", (card_id,)))
    assert await db.run(lambda c: c.execute("SELECT COUNT(*) FROM card_subscription").fetchone()[0]) == 0


async def test_get_by_message_id_finds_the_copy_holding_that_message(db: Database, card_id: int) -> None:
    await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))
    await db.run(lambda c: subscriptions.set_message(c, card_id, CHANNELS[0], MESSAGE, HASH))

    found = await db.run(lambda c: subscriptions.get_by_message_id(c, MESSAGE))

    assert found is not None
    assert found == subscriptions.Subscription(id=found.id, card_id=card_id, guild_id=GUILD, channel_id=CHANNELS[0],
                                               message_id=MESSAGE, content_hash=HASH, paused=False,
                                               mode="redirect", local_channel_id=None)


async def test_get_by_message_id_returns_none_for_an_unknown_message(db: Database, card_id: int) -> None:
    await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))
    await db.run(lambda c: subscriptions.set_message(c, card_id, CHANNELS[0], MESSAGE, HASH))

    assert await db.run(lambda c: subscriptions.get_by_message_id(c, OTHER_MESSAGE)) is None


# Modes and local channels ----------------------------------------------------------------------------

LOCAL_CHANNEL = "2901"


async def test_get_by_id_round_trips_the_mode_and_the_local_channel(db: Database, card_id: int) -> None:
    sub_id = await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))
    assert await db.run(lambda c: subscriptions.set_mode_if(c, sub_id, "redirect", "local")) is True
    await db.run(lambda c: subscriptions.set_local_channel(c, sub_id, LOCAL_CHANNEL))

    found = await db.run(lambda c: subscriptions.get_by_id(c, sub_id))

    assert found is not None
    assert (found.id, found.mode, found.local_channel_id) == (sub_id, "local", LOCAL_CHANNEL)
    assert await db.run(lambda c: subscriptions.get_by_id(c, sub_id + 100)) is None


async def test_set_mode_if_applies_only_when_the_expected_mode_matches(db: Database, card_id: int) -> None:
    sub_id = await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))

    assert await db.run(lambda c: subscriptions.set_mode_if(c, sub_id, "local", "both")) is False
    sub = await db.run(lambda c: subscriptions.get_by_id(c, sub_id))
    assert sub is not None and sub.mode == "redirect"

    assert await db.run(lambda c: subscriptions.set_mode_if(c, sub_id, "redirect", "both")) is True
    assert await db.run(lambda c: subscriptions.set_mode_if(c, sub_id, "redirect", "local")) is False
    sub = await db.run(lambda c: subscriptions.get_by_id(c, sub_id))
    assert sub is not None and sub.mode == "both"


async def test_set_local_channel_sets_and_clears(db: Database, card_id: int) -> None:
    sub_id = await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))

    await db.run(lambda c: subscriptions.set_local_channel(c, sub_id, LOCAL_CHANNEL))
    sub = await db.run(lambda c: subscriptions.get_by_id(c, sub_id))
    assert sub is not None and sub.local_channel_id == LOCAL_CHANNEL

    await db.run(lambda c: subscriptions.set_local_channel(c, sub_id, None))
    sub = await db.run(lambda c: subscriptions.get_by_id(c, sub_id))
    assert sub is not None and sub.local_channel_id is None


async def test_list_scopes_and_list_both_keep_only_active_local_and_both_rows(db: Database, card_id: int) -> None:
    redirect_id = await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))
    local_id = await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[1]))
    both_id = await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[2]))
    paused_both_id = await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[3]))
    await db.run(lambda c: subscriptions.set_mode_if(c, local_id, "redirect", "local"))
    await db.run(lambda c: subscriptions.set_mode_if(c, both_id, "redirect", "both"))
    await db.run(lambda c: subscriptions.set_mode_if(c, paused_both_id, "redirect", "both"))
    await db.run(lambda c: subscriptions.pause(c, card_id, CHANNELS[3]))

    scopes = await db.run(lambda c: subscriptions.list_scopes(c, card_id))
    both = await db.run(lambda c: subscriptions.list_both(c, card_id))

    assert [sub.id for sub in scopes] == [local_id, both_id]
    assert [sub.id for sub in both] == [both_id]
    assert redirect_id not in {sub.id for sub in scopes}


async def test_remove_by_id_deletes_only_that_row(db: Database, card_id: int) -> None:
    first = await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[0]))
    second = await db.run(lambda c: subscriptions.add(c, card_id, GUILD, CHANNELS[1]))

    assert await db.run(lambda c: subscriptions.remove_by_id(c, first)) is True
    assert await db.run(lambda c: subscriptions.remove_by_id(c, first)) is False
    assert await db.run(lambda c: subscriptions.get_by_id(c, second)) is not None
