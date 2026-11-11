# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RSVP changes end to end: `Interactions.rsvp_changed` against a real SQLite file, a real SessionPost and the fake REST
app. The session message's edits, the promotion DMs and the games channel's silence are what the tests read. Emoji are
written as escapes or taken from rsvp; the tests never contain a literal emoji."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import cards, rsvp, sessions, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_post import SessionPost
from tests.fakes import FakeFluxerState, Recorded, make_rest_app

TOKEN = "SENTINEL-TOKEN-interactions-rsvp"
BOT_ID = "1900"
ADMIN = "1001"
PROPOSER = "6001"
ALPHA = "6002"
BETA = "6003"
GUILD = "2001"
GAMES = "3100"          # the games channel: no session request may touch it (AC10.3)
SESSION_CH = "3200"
SESSION_MSG = "5100"
CARD_MSG = "5001"
INSTANCE = "https://fluxer.invalid"
STARTS = 1_800_000_000
PATCH_PATH = f"/api/v1/channels/{SESSION_CH}/messages/{SESSION_MSG}"
JUMP = f"/channels/{GUILD}/{SESSION_CH}/{SESSION_MSG}"
TEN_USERS = [f"6{n:03d}" for n in range(10, 20)]   # ten distinct users for the concurrency test


@dataclass
class World:
    state: FakeFluxerState
    db: Database
    rest: FluxerREST
    post: SessionPost
    interactions: Interactions
    card_id: int
    session_id: int
    kicks: list[int]


def seed(conn, *, seats: int | None = 1) -> tuple[int, int]:
    card = cards.create_card(conn, slug="mc", title="Lobby", author_id=ADMIN, now=1)
    cards.set_state(conn, card, "live", 1)
    cards.set_message(conn, card, GAMES, CARD_MSG, "a" * 64)
    session_id = sessions.add(conn, card_id=card, guild_id=GUILD, channel_id=SESSION_CH, message_id=SESSION_MSG,
                              starts_at=STARTS, ends_at=STARTS + 3600, proposer_id=PROPOSER, now=1,
                              location_text="Hall B", seats=seats)
    return card, session_id


@pytest.fixture
async def world(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[World]:
    state = FakeFluxerState(bot_user_id=BOT_ID)
    state.channels.update({
        GAMES: {"id": GAMES, "guild_id": GUILD, "type": 0},
        SESSION_CH: {"id": SESSION_CH, "guild_id": GUILD, "type": 0},
    })
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    database = Database.open(tmp_path / "rsvp.db")
    try:
        card_id, session_id = await database.run(seed)
        async with aiohttp.ClientSession() as client:
            rest = FluxerREST(client, f"{base}/api", TOKEN)
            config = Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": INSTANCE,
                                      "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES})
            pacer = ChannelPacer(max_calls=1000)
            post = SessionPost(db=database, rest=rest, config=config, pacer=pacer)
            kicks: list[int] = []
            interactions = Interactions(db=database, rest=rest, config=config, locks=CardLocks(),
                                        games_guild=GamesGuild(rest, config),
                                        seeder=Seeder(rest=rest, pacer=pacer, db=database), pacer=pacer,
                                        identity=BotIdentity(user_id=BOT_ID), roles=RoleCache(rest),
                                        clock=lambda: float(STARTS - 1000), kick=lambda: kicks.append(1),
                                        session_post=post)
            yield World(state=state, db=database, rest=rest, post=post, interactions=interactions,
                        card_id=card_id, session_id=session_id, kicks=kicks)
    finally:
        database.close()


def patches(w: World) -> list[Recorded]:
    return [r for r in w.state.requests if r.method == "PATCH"]


def rsvp_field(patch: Recorded) -> str:
    return next(f["value"] for f in patch.payload["embeds"][0]["fields"] if f["name"] == text.SESSION_FIELD_RSVPS)


def dm_contents(w: World, user: str) -> list[str]:
    channel = w.state.dm_channels.get(user)
    if channel is None:
        return []
    return [r.payload["content"] for r in w.state.requests
            if r.method == "POST" and r.path == f"/api/v1/channels/{channel}/messages"]


def counts_text(going: int, maybe: int, waiting: int = 0) -> str:
    line = text.SESSION_COUNTS.format(going=going, maybe=maybe)
    return line + (text.SESSION_COUNTS_WAITING.format(waiting=waiting) if waiting else "")


async def presence(w: World) -> list[rsvp.Presence]:
    return await w.db.run(lambda c: sessions.presence_of(c, w.session_id))


# AC4.8: a waiting user is counted, and a promoted user gets exactly one DM with the jump link.

async def test_a_user_going_while_the_seat_is_taken_waits_and_the_count_shows_it(world: World) -> None:
    await world.interactions.rsvp_changed(world.session_id, ALPHA, "going", True)
    [patch] = patches(world)
    assert patch.path == PATCH_PATH
    assert rsvp_field(patch) == counts_text(going=1, maybe=0, waiting=1)


async def test_the_proposer_stepping_out_promotes_the_waiting_user_once_with_a_jump_link(world: World) -> None:
    await world.interactions.rsvp_changed(world.session_id, ALPHA, "going", True)
    await world.interactions.rsvp_changed(world.session_id, PROPOSER, "no", True)
    [dm] = dm_contents(world, ALPHA)
    assert dm.startswith(text.SESSION_PROMOTED.split("{")[0])
    assert f"[{text.SESSION_LINK_LABEL}]({INSTANCE}{JUMP})" in dm
    assert dm_contents(world, PROPOSER) == []
    assert rsvp_field(patches(world)[-1]) == counts_text(going=1, maybe=0)


async def test_a_no_op_repeat_sends_no_second_dm_and_no_edit(world: World) -> None:
    await world.interactions.rsvp_changed(world.session_id, ALPHA, "going", True)
    await world.interactions.rsvp_changed(world.session_id, PROPOSER, "no", True)
    edits_before = len(patches(world))
    await world.interactions.rsvp_changed(world.session_id, PROPOSER, "no", True)
    assert len(dm_contents(world, ALPHA)) == 1
    assert len(patches(world)) == edits_before


# AC4.10: a session that has ended or been cancelled changes nothing and sends no request.

async def test_a_reaction_on_an_ended_session_changes_no_rows_and_sends_no_request(world: World) -> None:
    await world.db.run(lambda c: sessions.end_if(c, world.session_id, STARTS + 10))
    before = await presence(world)
    requests_before = len(world.state.requests)
    await world.interactions.rsvp_changed(world.session_id, ALPHA, "going", True)
    assert await presence(world) == before
    assert len(world.state.requests) == requests_before


async def test_a_reaction_on_a_cancelled_session_changes_no_rows_and_sends_no_request(world: World) -> None:
    await world.db.run(lambda c: sessions.cancel_if(c, world.session_id, STARTS + 10))
    before = await presence(world)
    requests_before = len(world.state.requests)
    await world.interactions.rsvp_changed(world.session_id, ALPHA, "going", True)
    assert await presence(world) == before
    assert len(world.state.requests) == requests_before


# A late RSVP edit never undoes a cancel: the RSVP's own PATCH is held, the cancel's edit waits behind it, and the last
# PATCH sent is the cancelled form.

async def test_a_late_rsvp_edit_never_restores_the_upcoming_form_after_a_cancel(world: World) -> None:
    release = asyncio.Event()
    calls: list[dict[str, Any]] = []
    original = world.rest.edit_message

    async def gated_edit(channel_id: str, message_id: str, **kwargs: Any) -> Any:
        calls.append(kwargs)
        if len(calls) == 1:
            await release.wait()
        return await original(channel_id, message_id, **kwargs)

    world.rest.edit_message = gated_edit   # the instance attribute shadows the method, for SessionPost too
    first = asyncio.create_task(world.interactions.rsvp_changed(world.session_id, ALPHA, "going", True))
    while not calls:
        await asyncio.sleep(0)
    await world.db.run(lambda c: sessions.cancel_if(c, world.session_id, STARTS + 10))
    second = asyncio.create_task(world.post.edit_session(world.session_id))   # the cancel's edit
    await asyncio.sleep(0)
    await world.db.run(lambda c: None)   # a no-op DB turn: the second edit has reached its lock by now
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(calls) == 1, "the cancel's edit must wait for the RSVP's edit"
    release.set()
    await asyncio.gather(first, second)
    last = patches(world)[-1]
    names = [f["name"] for f in last.payload["embeds"][0]["fields"]]
    assert "Location" not in names
    assert (text.SESSION_FIELD_STATUS, text.SESSION_CANCELLED) in [
        (f["name"], f["value"]) for f in last.payload["embeds"][0]["fields"]]


# An RSVP change never kicks the poller: RSVPs bypass every rate limit, so a kick would drive back-to-back ticks.

async def test_an_rsvp_change_does_not_kick_the_poller(world: World) -> None:
    await world.interactions.rsvp_changed(world.session_id, ALPHA, "going", True)
    assert world.kicks == []


# AC4.12: concurrent RSVP events for one card are processed in order, with no lost update.

async def test_ten_concurrent_rsvps_on_one_card_store_every_row_with_contiguous_seqs(world: World) -> None:
    await asyncio.gather(*(world.interactions.rsvp_changed(world.session_id, user, "going", True)
                           for user in TEN_USERS))
    rows = await presence(world)
    # Each new row takes the next seq under the card lock, so the proposer's implicit row (seq 1) and the ten rows are
    # 1 to 11 with no gap and no duplicate.
    assert [p.seq for p in rows] == list(range(1, 12))
    stored = await world.db.run(lambda c: sessions.rsvps_of(c, world.session_id))
    assert rsvp.apply(stored, rows, 1).rsvps == stored   # the stored states are a fixed point of the engine
    counts = await world.db.run(lambda c: sessions.counts_of(c, world.session_id))
    assert rsvp_field(patches(world)[-1]) == counts_text(counts.going, counts.maybe, counts.waiting)


async def test_two_rapid_rsvps_send_at_most_two_edits_and_the_last_matches_the_db(world: World) -> None:
    await asyncio.gather(world.interactions.rsvp_changed(world.session_id, ALPHA, "going", True),
                         world.interactions.rsvp_changed(world.session_id, BETA, "maybe", True))
    assert 1 <= len(patches(world)) <= 2
    counts = await world.db.run(lambda c: sessions.counts_of(c, world.session_id))
    assert rsvp_field(patches(world)[-1]) == counts_text(counts.going, counts.maybe, counts.waiting)


# AC10.3: every session request is to the stored channel, and none reaches the games channel.

async def test_every_session_request_targets_the_stored_channel_and_never_the_games_channel(world: World) -> None:
    await world.interactions.rsvp_changed(world.session_id, ALPHA, "going", True)
    await world.interactions.rsvp_changed(world.session_id, PROPOSER, "no", True)
    assert patches(world)
    assert all(r.path == PATCH_PATH for r in patches(world))
    assert not [r for r in world.state.requests if GAMES in r.path]


# The RSVP log line carries the choice and outcome, and never the user id.

async def test_the_rsvp_changed_log_carries_no_user_id(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.interactions")
    await world.interactions.rsvp_changed(world.session_id, ALPHA, "going", True)
    [logged] = [r for r in caplog.records if r.getMessage() == "rsvp_changed"]
    fields = getattr(logged, FIELDS_ATTR)
    assert "user_id" not in fields
    assert ALPHA not in repr(fields)
