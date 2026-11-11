# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every REST call that touches a session message (AC10.1, AC3.2): the games channel is refused before any request,
every send and edit carries empty allowed_mentions, and an edit always renders the session as the database holds it."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_panel import cards, rsvp, sessions
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.interactions import GamesChannelRefused
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.session_post import SessionPost
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "session-post-token"
URL = "https://fluxer.invalid"
ADMIN = "1001"
PROPOSER = "1002"
GUILD = "2001"
CHANNEL = "2101"
MSG = "5101"
GAMES = "3100"
GAMES_MSG = "5102"
STARTS = 1_800_000_000
LOCATION_SENTINEL = "SESSION-POST-LOCATION-SENTINEL"
PATCH = f"/api/v1/channels/{CHANNEL}/messages/{MSG}"
PAYLOAD = {"embeds": [{"title": "Lobby", "fields": []}], "allowed_mentions": {"parse": []}}


def config_for() -> Config:
    return Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": URL,
                            "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES, "POLL_INTERVAL_SECONDS": "3600"})


@pytest.fixture
async def world(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[SimpleNamespace]:
    database = Database.open(tmp_path / "session_post.db")
    state = FakeFluxerState(bot_user_id="1900")
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    try:
        async with aiohttp.ClientSession() as client:
            rest = FluxerREST(client, f"{base}/api", TOKEN)
            post = SessionPost(db=database, rest=rest, config=config_for(), pacer=ChannelPacer())
            yield SimpleNamespace(db=database, state=state, rest=rest, post=post)
    finally:
        database.close()


def add_card_and_session(conn, *, channel: str = CHANNEL, message: str = MSG, location: str | None = "Hall B") -> tuple[int, int]:
    card = cards.create_card(conn, slug="mc", title="Lobby", author_id=ADMIN, now=1)
    cards.set_state(conn, card, "live", 1)
    session_id = sessions.add(conn, card_id=card, guild_id=GUILD, channel_id=channel, message_id=message,
                              starts_at=STARTS, ends_at=STARTS + 3600, proposer_id=PROPOSER, now=1,
                              location_text=location)
    return card, session_id


async def seed(world: SimpleNamespace, **kwargs: Any) -> int:
    _, session_id = await world.db.run(lambda conn: add_card_and_session(conn, **kwargs))
    return session_id


def writes(world: SimpleNamespace, method: str) -> list[Any]:
    return [r for r in world.state.requests if r.method == method]


def panel_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "fluxer_panel.session_post"]


# AC10.1: the games channel is refused before any REST call.

async def test_send_session_refuses_the_games_channel_before_any_request(world: SimpleNamespace) -> None:
    before = len(world.state.requests)
    with pytest.raises(GamesChannelRefused):
        await world.post.send_session(GAMES, PAYLOAD)
    assert len(world.state.requests) == before


async def test_edit_session_refuses_a_session_stored_in_the_games_channel(world: SimpleNamespace) -> None:
    session_id = await seed(world, channel=GAMES, message=GAMES_MSG)
    before = len(world.state.requests)
    with pytest.raises(GamesChannelRefused):
        await world.post.edit_session(session_id)
    assert len(world.state.requests) == before


async def test_seed_session_refuses_the_games_channel_before_any_request(world: SimpleNamespace) -> None:
    before = len(world.state.requests)
    with pytest.raises(GamesChannelRefused):
        await world.post.seed_session(GAMES, GAMES_MSG)
    assert len(world.state.requests) == before


# AC3.2: every send and edit carries empty allowed_mentions and no mention form.

async def test_send_session_posts_with_empty_allowed_mentions(world: SimpleNamespace) -> None:
    await world.post.send_session(CHANNEL, PAYLOAD)
    [post] = writes(world, "POST")
    assert post.payload["allowed_mentions"] == {"parse": []}


async def test_edit_session_patches_the_stored_message_with_empty_allowed_mentions(world: SimpleNamespace) -> None:
    session_id = await seed(world)
    await world.post.edit_session(session_id)
    [patch] = writes(world, "PATCH")
    assert patch.path == PATCH
    assert patch.payload["allowed_mentions"] == {"parse": []}
    assert "<@" not in json.dumps(patch.payload)


# Reactions: the three seeds, in order, and a failure stops them without raising.

async def test_seed_session_adds_the_three_rsvp_reactions_in_order_to_the_stored_channel(
        world: SimpleNamespace) -> None:
    await world.post.seed_session(CHANNEL, MSG)
    puts = writes(world, "PUT")
    assert len(puts) == 3
    assert all(f"/channels/{CHANNEL}/messages/{MSG}/reactions/" in r.path for r in puts)
    assert [next(e for e in rsvp.RSVP_EMOJI if e in r.path) for r in puts] == list(rsvp.RSVP_EMOJI)


async def test_a_failed_seed_logs_and_stops_without_raising(world: SimpleNamespace, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.session_post")
    world.state.overrides[f"PUT /api/v1/channels/{CHANNEL}/messages/{MSG}/reactions/{rsvp.GOING_EMOJI}/@me"] = \
        Override(403, b'{"code":"MISSING_PERMISSIONS","message":"x"}')
    await world.post.seed_session(CHANNEL, MSG)
    assert len(writes(world, "PUT")) == 1
    [failed] = [r for r in panel_records(caplog) if r.getMessage() == "session_seed_failed"]
    assert getattr(failed, FIELDS_ATTR) == {"channel_id": CHANNEL, "reason": "FluxerHTTPError"}


# The purge's scrub: one PATCH, no location, and a REST failure reaches the caller.

async def test_scrub_sends_one_patch_with_no_location_field(world: SimpleNamespace) -> None:
    session_id = await seed(world, location=LOCATION_SENTINEL)
    await world.db.run(lambda conn: sessions.end_if(conn, session_id, 2))
    session = await world.db.run(lambda conn: sessions.get(conn, session_id))
    assert session is not None
    await world.post.scrub(session)
    [patch] = writes(world, "PATCH")
    names = [f["name"] for f in patch.payload["embeds"][0]["fields"]]
    assert "Location" not in names
    assert LOCATION_SENTINEL not in json.dumps(patch.payload)


async def test_scrub_raises_the_rest_error_to_the_caller(world: SimpleNamespace) -> None:
    session_id = await seed(world)
    session = await world.db.run(lambda conn: sessions.get(conn, session_id))
    assert session is not None
    world.state.overrides[f"PATCH {PATCH}"] = Override(404, b'{"code":"UNKNOWN_MESSAGE","message":"x"}')
    with pytest.raises(FluxerHTTPError) as raised:
        await world.post.scrub(session)
    assert raised.value.status == 404


# A posted message whose store failed is deleted, and a failed delete never raises.

async def test_delete_unstored_removes_the_message(world: SimpleNamespace) -> None:
    await world.post.delete_unstored(CHANNEL, MSG)
    assert [r.method for r in world.state.requests] == ["DELETE"]


async def test_delete_unstored_swallows_a_failed_delete(world: SimpleNamespace, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.session_post")
    world.state.overrides[f"DELETE {PATCH}"] = Override(403, b'{"code":"MISSING_PERMISSIONS","message":"x"}')
    await world.post.delete_unstored(CHANNEL, MSG)
    assert [r.getMessage() for r in panel_records(caplog)] == ["session_discard_failed"]


# An edit renders the session as the database holds it at send time, never the caller's view.

async def test_an_edit_renders_the_database_state_not_the_callers_view(world: SimpleNamespace) -> None:
    session_id = await seed(world)
    await world.db.run(lambda conn: sessions.end_if(conn, session_id, 2))
    await world.post.edit_session(session_id)
    [patch] = writes(world, "PATCH")
    status = next(f for f in patch.payload["embeds"][0]["fields"] if f["name"] == "Status")
    assert status["value"] == "Ended"


async def test_an_edit_of_a_missing_session_sends_nothing(world: SimpleNamespace) -> None:
    await world.post.edit_session(999_999)
    assert world.state.requests == []


async def test_an_edit_in_flight_cannot_restore_an_upcoming_form_after_a_cancel(world: SimpleNamespace) -> None:
    session_id = await seed(world)
    release = asyncio.Event()
    calls: list[dict[str, Any]] = []
    original = world.rest.edit_message

    async def gated_edit(channel_id: str, message_id: str, **kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) == 1:
            await release.wait()
        return await original(channel_id, message_id, **kwargs)

    world.rest.edit_message = gated_edit   # the instance attribute shadows the method
    first = asyncio.create_task(world.post.edit_session(session_id))   # the RSVP edit
    while not calls:
        await asyncio.sleep(0)
    await world.db.run(lambda conn: sessions.cancel_if(conn, session_id, 2))
    second = asyncio.create_task(world.post.edit_session(session_id))  # the cancel's edit
    # The second edit reads the database in a thread, so a few loop turns are not enough to reach its PATCH. A real
    # wait lets an unlocked second edit send, so this check fails without the lock.
    await asyncio.sleep(0.2)
    assert len(calls) == 1, "the second edit must wait for the first"
    release.set()
    await asyncio.gather(first, second)
    assert len(calls) == 2
    last = calls[1]["embeds"][0]
    names = [f["name"] for f in last["fields"]]
    assert "Location" not in names
    assert ("Status", "Cancelled") in [(f["name"], f["value"]) for f in last["fields"]]
