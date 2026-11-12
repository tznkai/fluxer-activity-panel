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
from fluxer_panel import cards, render, rsvp, sessions
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
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
# No markdown control character, so `render.escape` leaves it as it is and the raw absence check is real.
LOCATION_SENTINEL = "SESSION POST LOCATION SENTINEL"
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
            backoff = DmBackoff(3600)
            post = SessionPost(db=database, rest=rest, config=config_for(), pacer=ChannelPacer(), backoff=backoff)
            yield SimpleNamespace(db=database, state=state, rest=rest, post=post, backoff=backoff)
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
    assert render.escape(LOCATION_SENTINEL) == LOCATION_SENTINEL, "the sentinel must survive render.escape"
    session_id = await seed(world, location=LOCATION_SENTINEL)
    # Positive control: the upcoming form carries the location, so the absence check after the scrub can fail.
    await world.post.edit_session(session_id)
    [control] = writes(world, "PATCH")
    assert LOCATION_SENTINEL in json.dumps(control.payload)
    await world.db.run(lambda conn: sessions.end_if(conn, session_id, 2))
    session = await world.db.run(lambda conn: sessions.get(conn, session_id))
    assert session is not None
    await world.post.scrub(session)
    patches = writes(world, "PATCH")
    assert len(patches) == 2
    names = [f["name"] for f in patches[-1].payload["embeds"][0]["fields"]]
    assert "Location" not in names
    assert LOCATION_SENTINEL not in json.dumps(patches[-1].payload)


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


async def test_delete_unstored_refuses_the_games_channel_before_any_request_and_logs_it(
        world: SimpleNamespace, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.session_post")
    before = len(world.state.requests)
    await world.post.delete_unstored(GAMES, GAMES_MSG)
    assert len(world.state.requests) == before
    [refused] = [r for r in panel_records(caplog) if r.getMessage() == "session_discard_failed"]
    assert getattr(refused, FIELDS_ATTR) == {"channel_id": GAMES, "reason": "GamesChannelRefused"}


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
    # Deterministic, no wall clock. One turn lets the second task start: if the lock were free it would submit its
    # _load to the single-thread DB executor now. The no-op run below is submitted after that _load, so awaiting it
    # means the _load has finished, and a few more turns let an unlocked second edit reach gated_edit.
    await asyncio.sleep(0)
    await world.db.run(lambda conn: None)
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(calls) == 1, "the second edit must wait for the first"
    assert world.post.edit_lock(session_id).locked()
    release.set()
    await asyncio.gather(first, second)
    assert len(calls) == 2
    last = calls[1]["embeds"][0]
    names = [f["name"] for f in last["fields"]]
    assert "Location" not in names
    assert ("Status", "Cancelled") in [(f["name"], f["value"]) for f in last["fields"]]


# AC7.2 and the deleted-message hook. The attendee DMs go to going and waiting users, except the excluded user, and
# bypass the DM backoff. A 404 on a session edit calls the hook only while the session is still upcoming.

async def _rsvp(world: SimpleNamespace, session_id: int, rows: list[tuple[str, str, int | None]]) -> None:
    def seed(conn: Any) -> None:
        for user_id, choice, pos in rows:
            conn.execute("INSERT INTO rsvps (session_id, user_id, choice, responded_at, waitlist_pos) "
                         "VALUES (?, ?, ?, ?, ?)", (session_id, user_id, choice, STARTS, pos))

    await world.db.run(seed)


def dm_sends(world: SimpleNamespace, content: str) -> list[Any]:
    return [r for r in writes(world, "POST") if r.payload and r.payload.get("content") == content]


async def test_dm_attendees_sends_one_dm_to_each_going_and_waiting_user_except_the_excluded_user(
        world: SimpleNamespace) -> None:
    session_id = await seed(world)
    await _rsvp(world, session_id, [("1003", "going", None), ("1004", "waiting", 1), ("1005", "maybe", None),
                                    ("1006", "no", None)])
    session = await world.db.run(lambda conn: sessions.get(conn, session_id))
    assert session is not None

    attempted = await world.post.dm_attendees(session, "dm-text", exclude=PROPOSER)

    assert attempted == 2
    assert set(world.state.dm_channels) == {"1003", "1004"}
    assert len(dm_sends(world, "dm-text")) == 2


async def test_dm_attendees_attempts_a_user_who_is_in_dm_backoff(world: SimpleNamespace) -> None:
    session_id = await seed(world)
    await _rsvp(world, session_id, [("1003", "going", None)])
    session = await world.db.run(lambda conn: sessions.get(conn, session_id))
    assert session is not None
    world.backoff.failed("1003")

    attempted = await world.post.dm_attendees(session, "dm-text", exclude=None)

    assert attempted == 2   # the proposer and 1003
    assert "1003" in world.state.dm_channels
    assert len(dm_sends(world, "dm-text")) == 2


async def test_a_404_on_an_upcoming_session_edit_calls_the_hook_once_and_still_raises(
        world: SimpleNamespace) -> None:
    session_id = await seed(world)
    calls: list[int] = []

    async def hook(gone_id: int) -> None:
        calls.append(gone_id)

    world.post.set_on_gone(hook)
    world.state.overrides[f"PATCH {PATCH}"] = Override(404, b'{"code":"UNKNOWN_MESSAGE","message":"x"}')
    with pytest.raises(FluxerHTTPError) as raised:
        await world.post.edit_session(session_id)
    assert raised.value.status == 404
    assert calls == [session_id]


async def test_a_404_on_an_ended_session_edit_does_not_call_the_hook(world: SimpleNamespace) -> None:
    session_id = await seed(world)
    await world.db.run(lambda conn: sessions.end_if(conn, session_id, 2))
    calls: list[int] = []

    async def hook(gone_id: int) -> None:
        calls.append(gone_id)

    world.post.set_on_gone(hook)
    world.state.overrides[f"PATCH {PATCH}"] = Override(404, b'{"code":"UNKNOWN_MESSAGE","message":"x"}')
    with pytest.raises(FluxerHTTPError):
        await world.post.edit_session(session_id)
    assert calls == []


async def test_a_non_404_failure_does_not_call_the_hook(world: SimpleNamespace) -> None:
    session_id = await seed(world)
    calls: list[int] = []

    async def hook(gone_id: int) -> None:
        calls.append(gone_id)

    world.post.set_on_gone(hook)
    world.state.overrides[f"PATCH {PATCH}"] = Override(403, b'{"code":"MISSING_PERMISSIONS","message":"x"}')
    with pytest.raises(FluxerHTTPError):
        await world.post.edit_session(session_id)
    assert calls == []


async def test_a_failing_hook_does_not_replace_the_edit_failure(world: SimpleNamespace) -> None:
    session_id = await seed(world)

    async def broken_hook(gone_id: int) -> None:
        raise RuntimeError("boom")

    world.post.set_on_gone(broken_hook)
    world.state.overrides[f"PATCH {PATCH}"] = Override(404, b'{"code":"UNKNOWN_MESSAGE","message":"x"}')
    with pytest.raises(FluxerHTTPError) as raised:
        await world.post.edit_session(session_id)
    assert raised.value.status == 404
