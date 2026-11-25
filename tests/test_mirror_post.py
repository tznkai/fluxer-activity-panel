# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""MirrorPost against the fake REST and a real database: post, hash-gated edit, the 404 drop and the 403 log (AC3.8),
the games-channel guard, the delete path, and the log rule. Every mirror post and edit runs through this module."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, mirrors, sessions, subscriptions
from fluxer_panel.cards import Card
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games_guard import GamesChannelRefused
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.mirror_post import MirrorPost
from fluxer_panel.subscriptions import Subscription
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "mirror-post-token"
GUILD = "4000"          # the both subscriptions' community
GAMES = "3001"          # the games channel, refused for every mirror
HOME_CHANNEL = "2001"
CHANNEL_A = "4101"
CHANNEL_B = "4102"
CHANNEL_L = "4103"      # a local subscription, never mirrored into
ROLE_A = "5101"
PAYLOAD = {"embeds": [{"title": "Lobby", "fields": [{"name": "When", "value": "soon"}]}],
           "allowed_mentions": {"parse": []}}
CHANGED = {"embeds": [{"title": "Lobby", "fields": [{"name": "When", "value": "later"}]}],
           "allowed_mentions": {"parse": []}}
ROLE_MENTIONS = {"parse": [], "roles": [ROLE_A], "replied_user": False}
POST_PATH = f"/api/v1/channels/{CHANNEL_A}/messages"


def config_for() -> Config:
    return Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC",
                            "FLUXER_INSTANCE_URL": "https://fluxer.invalid", "ADMIN_USER_IDS": "1001",
                            "GAMES_CHANNEL_ID": GAMES, "POLL_INTERVAL_SECONDS": "3600"})


@pytest.fixture
async def world(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[SimpleNamespace]:
    database = Database.open(tmp_path / "mirror_post.db")
    state = FakeFluxerState(bot_user_id="1900", next_message_id=9001)
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    try:
        async with aiohttp.ClientSession() as client:
            rest = FluxerREST(client, f"{base}/api", TOKEN)
            mirror = MirrorPost(db=database, rest=rest, config=config_for())

            def build(conn: sqlite3.Connection) -> dict[str, int]:
                card_id = cards.create_card(conn, slug="mc", title="Minecraft", author_id="1001", now=1)
                sub_a = subscriptions.add(conn, card_id, GUILD, CHANNEL_A)
                assert subscriptions.set_mode_if(conn, sub_a, "redirect", "both")
                sub_b = subscriptions.add(conn, card_id, GUILD, CHANNEL_B)
                assert subscriptions.set_mode_if(conn, sub_b, "redirect", "both")
                sub_l = subscriptions.add(conn, card_id, GUILD, CHANNEL_L)
                assert subscriptions.set_mode_if(conn, sub_l, "redirect", "local")
                session_id = sessions.add(conn, card_id=card_id, guild_id=GUILD, channel_id=HOME_CHANNEL,
                                          message_id="5001", starts_at=1000, ends_at=2000, proposer_id="1101",
                                          now=1)
                return {"card": card_id, "sub_a": sub_a, "sub_b": sub_b, "sub_l": sub_l, "session": session_id}

            ids = await database.run(build)
            yield SimpleNamespace(db=database, state=state, rest=rest, mirror=mirror, **ids)
    finally:
        database.close()


async def load_sub(world: SimpleNamespace, sub_id: int) -> Subscription:
    sub = await world.db.run(lambda c: subscriptions.get_by_id(c, sub_id))
    assert sub is not None
    return sub


async def load_card(world: SimpleNamespace) -> Card:
    card = await world.db.run(lambda c: cards.get_by_id(c, world.card))
    assert card is not None
    return card


async def stored(world: SimpleNamespace, sub_id: int) -> mirrors.Mirror | None:
    return await world.db.run(lambda c: mirrors.get(c, "session", world.session, sub_id))


def requests(world: SimpleNamespace, method: str) -> list[Any]:
    return [r for r in world.state.requests if r.method == method]


def logged(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, object]]:
    return [getattr(record, FIELDS_ATTR, {}) for record in caplog.records if record.getMessage() == name]


async def post_for(world: SimpleNamespace, sub_id: int, channel: str, payload: dict[str, Any] = PAYLOAD,
                   allowed: dict[str, Any] | None = None) -> str | None:
    return await world.mirror.sync(sub=await load_sub(world, sub_id), channel_id=channel, kind="session",
                                   object_id=world.session, payload=payload, allowed_mentions=allowed)


async def test_a_new_mirror_posts_once_and_stores_its_row(world: SimpleNamespace) -> None:
    message_id = await post_for(world, world.sub_a, CHANNEL_A)
    row = await stored(world, world.sub_a)
    assert message_id is not None and row is not None and row.message_id == message_id
    assert row.channel_id == CHANNEL_A and row.content_hash is not None
    assert len(requests(world, "POST")) == 1


async def test_the_same_payload_again_makes_no_patch(world: SimpleNamespace) -> None:
    await post_for(world, world.sub_a, CHANNEL_A)
    await post_for(world, world.sub_a, CHANNEL_A)
    assert requests(world, "PATCH") == []


async def test_a_changed_payload_patches_and_updates_the_hash(world: SimpleNamespace) -> None:
    await post_for(world, world.sub_a, CHANNEL_A)
    before = (await stored(world, world.sub_a)).content_hash
    message_id = await post_for(world, world.sub_a, CHANNEL_A, CHANGED)
    after = await stored(world, world.sub_a)
    assert message_id == after.message_id and after.content_hash != before
    assert len(requests(world, "PATCH")) == 1


async def test_a_post_carries_the_given_allowed_mentions_and_its_edit_carries_none(world: SimpleNamespace) -> None:
    await post_for(world, world.sub_a, CHANNEL_A, allowed=ROLE_MENTIONS)
    await post_for(world, world.sub_a, CHANNEL_A, CHANGED, allowed=ROLE_MENTIONS)
    [post] = requests(world, "POST")
    [patch] = requests(world, "PATCH")
    assert post.payload["allowed_mentions"] == ROLE_MENTIONS
    assert patch.payload["allowed_mentions"] == {"parse": []}


async def test_the_allowed_mentions_are_not_part_of_the_hash(world: SimpleNamespace) -> None:
    # The same content posted with a role ping, then sent again without one: an unchanged hash, so no PATCH.
    await post_for(world, world.sub_a, CHANNEL_A, allowed=ROLE_MENTIONS)
    await post_for(world, world.sub_a, CHANNEL_A)
    assert requests(world, "PATCH") == []


async def test_a_404_on_an_edit_drops_only_that_row_and_reposts_nothing(world: SimpleNamespace,
                                                                        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    message_id = await post_for(world, world.sub_a, CHANNEL_A)
    world.state.overrides[f"PATCH /api/v1/channels/{CHANNEL_A}/messages/{message_id}"] = Override(
        404, b'{"code": "UNKNOWN_MESSAGE", "message": "Unknown Message"}')
    result = await post_for(world, world.sub_a, CHANNEL_A, CHANGED)
    assert result is None
    assert await stored(world, world.sub_a) is None
    assert len(requests(world, "POST")) == 1, "a 404 on an edit must not repost"
    assert logged(caplog, "mirror_dropped")


async def test_a_403_on_an_edit_keeps_the_row_and_logs_forbidden(world: SimpleNamespace,
                                                                  caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    message_id = await post_for(world, world.sub_a, CHANNEL_A)
    before = await stored(world, world.sub_a)
    world.state.overrides[f"PATCH /api/v1/channels/{CHANNEL_A}/messages/{message_id}"] = Override(
        403, b'{"code": "MISSING_ACCESS", "message": "no"}')
    await post_for(world, world.sub_a, CHANNEL_A, CHANGED)
    after = await stored(world, world.sub_a)
    assert after is not None and after.content_hash == before.content_hash, "the hash must not move on a failure"
    [record] = logged(caplog, "mirror_failed")
    assert record["reason"] == "forbidden"


async def test_another_error_on_an_edit_keeps_the_row(world: SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    message_id = await post_for(world, world.sub_a, CHANNEL_A)
    world.state.overrides[f"PATCH /api/v1/channels/{CHANNEL_A}/messages/{message_id}"] = Override(
        500, b'{"code": "X", "message": "boom"}')
    assert await post_for(world, world.sub_a, CHANNEL_A, CHANGED) == message_id
    assert await stored(world, world.sub_a) is not None
    assert logged(caplog, "mirror_failed")


async def test_a_failed_post_logs_and_stores_nothing(world: SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    world.state.overrides[f"POST {POST_PATH}"] = Override(403, b'{"code": "MISSING_ACCESS", "message": "no"}')
    assert await post_for(world, world.sub_a, CHANNEL_A) is None
    assert await stored(world, world.sub_a) is None
    [record] = logged(caplog, "mirror_failed")
    assert record["reason"] == "forbidden"


async def test_a_404_on_one_mirror_leaves_the_other_subscriptions_mirror_editable(world: SimpleNamespace) -> None:
    message_a = await post_for(world, world.sub_a, CHANNEL_A)
    message_b = await post_for(world, world.sub_b, CHANNEL_B)
    world.state.overrides[f"PATCH /api/v1/channels/{CHANNEL_A}/messages/{message_a}"] = Override(
        404, b'{"code": "UNKNOWN_MESSAGE", "message": "Unknown Message"}')
    await post_for(world, world.sub_a, CHANNEL_A, CHANGED)
    assert await post_for(world, world.sub_b, CHANNEL_B, CHANGED) == message_b
    row_b = await stored(world, world.sub_b)
    assert row_b is not None and row_b.message_id == message_b
    assert len(requests(world, "PATCH")) == 2


async def test_the_games_channel_is_refused_before_any_request(world: SimpleNamespace) -> None:
    with pytest.raises(GamesChannelRefused):
        await post_for(world, world.sub_a, GAMES)
    assert world.state.requests == []
    assert await stored(world, world.sub_a) is None


async def test_a_delete_of_a_live_mirror_removes_the_message_and_the_row(world: SimpleNamespace) -> None:
    await post_for(world, world.sub_a, CHANNEL_A)
    mirror = await stored(world, world.sub_a)
    assert await world.mirror.delete(mirror) is True
    assert await stored(world, world.sub_a) is None
    assert requests(world, "DELETE")[0].status == 204


async def test_a_404_on_delete_counts_as_deleted_and_removes_the_row(world: SimpleNamespace) -> None:
    message_id = await post_for(world, world.sub_a, CHANNEL_A)
    mirror = await stored(world, world.sub_a)
    world.state.overrides[f"DELETE /api/v1/channels/{CHANNEL_A}/messages/{message_id}"] = Override(
        404, b'{"code": "UNKNOWN_MESSAGE", "message": "Unknown Message"}')
    assert await world.mirror.delete(mirror) is True
    assert await stored(world, world.sub_a) is None


async def test_a_403_on_delete_returns_false_and_still_removes_the_row(world: SimpleNamespace,
                                                                        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    message_id = await post_for(world, world.sub_a, CHANNEL_A)
    mirror = await stored(world, world.sub_a)
    world.state.overrides[f"DELETE /api/v1/channels/{CHANNEL_A}/messages/{message_id}"] = Override(
        403, b'{"code": "MISSING_ACCESS", "message": "no"}')
    assert await world.mirror.delete(mirror) is False
    assert await stored(world, world.sub_a) is None
    [record] = logged(caplog, "mirror_failed")
    assert record["reason"] == "forbidden"


async def test_targets_are_the_active_both_subscriptions_only(world: SimpleNamespace) -> None:
    card = await load_card(world)
    targets = await world.db.run(lambda c: world.mirror.targets(c, card))
    assert [(sub.id, channel) for sub, channel in targets] == [(world.sub_a, CHANNEL_A), (world.sub_b, CHANNEL_B)]


async def test_no_log_record_carries_a_user_id(world: SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    message_id = await post_for(world, world.sub_a, CHANNEL_A)
    world.state.overrides[f"PATCH /api/v1/channels/{CHANNEL_A}/messages/{message_id}"] = Override(
        403, b'{"code": "MISSING_ACCESS", "message": "no"}')
    await post_for(world, world.sub_a, CHANNEL_A, CHANGED)
    records = [record for record in caplog.records if record.name.startswith("fluxer_panel")]
    assert records, "the failure must have been logged"
    for record in records:
        assert "user_id" not in getattr(record, FIELDS_ATTR, {})



async def wait_gated(world: SimpleNamespace, key: str) -> None:
    """Waits until a request on `key` has reached its gate, so the test can act while it is held."""
    for _ in range(1000):
        if key in world.state.gated:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the request never reached its gate")


def refused_reasons(caplog: pytest.LogCaptureFixture) -> list[object]:
    return [fields.get("reason") for fields in logged(caplog, "mirror_failed")]


async def test_a_copy_that_leaves_both_during_the_post_gets_no_row_and_its_message_is_removed(
        world: SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
    # The post is held in flight; the copy leaves `both` after the targets were read and before the row is stored.
    gate_key = f"POST {POST_PATH}"
    gate = asyncio.Event()
    world.state.gates[gate_key] = gate
    caplog.set_level(logging.WARNING)
    try:
        task = asyncio.create_task(post_for(world, world.sub_a, CHANNEL_A))
        await wait_gated(world, gate_key)
        assert await world.db.run(lambda c: subscriptions.set_mode_if(c, world.sub_a, "both", "local"))
    finally:
        gate.set()
    result = await asyncio.wait_for(task, timeout=5)
    assert result is None
    assert await stored(world, world.sub_a) is None
    [sent] = [key for key in world.state.messages if key[0] == CHANNEL_A]
    assert sent in world.state.deleted
    assert "row_refused" in refused_reasons(caplog)


async def test_a_row_stored_by_a_concurrent_post_wins_and_the_sent_message_is_removed(
        world: SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
    # Another path stores this object's row while the post is in flight: the store fails on the unique row, so the
    # message just sent is removed and the stored row is left as it is.
    gate_key = f"POST {POST_PATH}"
    gate = asyncio.Event()
    world.state.gates[gate_key] = gate
    caplog.set_level(logging.WARNING)
    try:
        task = asyncio.create_task(post_for(world, world.sub_a, CHANNEL_A))
        await wait_gated(world, gate_key)
        await world.db.run(lambda c: mirrors.add(c, sub_id=world.sub_a, channel_id=CHANNEL_A, message_id="7777",
                                                 kind="session", object_id=world.session, content_hash=None))
    finally:
        gate.set()
    result = await asyncio.wait_for(task, timeout=5)
    assert result is None
    row = await stored(world, world.sub_a)
    assert row is not None and row.message_id == "7777"
    [sent] = [key for key in world.state.messages if key[0] == CHANNEL_A]
    assert sent in world.state.deleted
    assert "row_refused" in refused_reasons(caplog)


async def test_a_copy_paused_during_the_post_gets_no_row_and_its_message_is_removed(
        world: SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
    # The copy is paused while its post is in flight: a paused copy is not active, so no row may be stored for it.
    gate_key = f"POST {POST_PATH}"
    gate = asyncio.Event()
    world.state.gates[gate_key] = gate
    caplog.set_level(logging.WARNING)
    try:
        task = asyncio.create_task(post_for(world, world.sub_a, CHANNEL_A))
        await wait_gated(world, gate_key)
        assert await world.db.run(lambda c: subscriptions.pause(c, world.card, CHANNEL_A))
    finally:
        gate.set()
    result = await asyncio.wait_for(task, timeout=5)
    assert result is None
    assert await stored(world, world.sub_a) is None
    [sent] = [key for key in world.state.messages if key[0] == CHANNEL_A]
    assert sent in world.state.deleted
    assert "row_refused" in refused_reasons(caplog)
