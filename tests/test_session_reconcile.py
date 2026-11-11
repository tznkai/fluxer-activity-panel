# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The startup reconcile of session RSVPs: `Interactions.reconcile_sessions` corrects the stored presence rows from the
reactions on each upcoming session's message, then recomputes, and `Poller.reconcile` runs it last. The fake REST app
holds the reactions; the tests read the rows, the PATCHes, the DMs and the log. Emoji come from rsvp, never as literals."""

from __future__ import annotations

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
from fluxer_panel.poller import Poller
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_post import SessionPost
from fluxer_panel.sources.registry import Sources
from tests.fakes import FakeFluxerState, Override, Recorded, make_rest_app

TOKEN = "SENTINEL-TOKEN-session-reconcile"
BOT_ID = "1900"
ADMIN = "1001"
PROPOSER = "6001"
ALPHA = "6002"
BETA = "6003"
GAMMA = "6004"
GUILD = "2001"
GAMES = "3100"
SESSION_CH = "3200"
SESSION_MSG = "5100"
CARD_MSG = "5001"
INSTANCE = "https://fluxer.invalid"
STARTS = 1_800_000_000
PATCH_PATH = f"/api/v1/channels/{SESSION_CH}/messages/{SESSION_MSG}"
REACTORS_PATH = f"/api/v1/channels/{SESSION_CH}/messages/{SESSION_MSG}/reactions"
JUMP = f"/channels/{GUILD}/{SESSION_CH}/{SESSION_MSG}"


@dataclass
class World:
    state: FakeFluxerState
    db: Database
    rest: FluxerREST
    config: Config
    interactions: Interactions
    session_id: int


def seed(conn: Any) -> tuple[int, int]:
    card = cards.create_card(conn, slug="mc", title="Lobby", author_id=ADMIN, now=1)
    cards.set_state(conn, card, "live", 1)
    cards.set_message(conn, card, GAMES, CARD_MSG, "a" * 64)
    session_id = sessions.add(conn, card_id=card, guild_id=GUILD, channel_id=SESSION_CH, message_id=SESSION_MSG,
                              starts_at=STARTS, ends_at=STARTS + 3600, proposer_id=PROPOSER, now=1,
                              location_text="Hall B", seats=1)
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
    database = Database.open(tmp_path / "reconcile.db")
    try:
        _, session_id = await database.run(seed)
        async with aiohttp.ClientSession() as client:
            rest = FluxerREST(client, f"{base}/api", TOKEN)
            config = Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": INSTANCE,
                                      "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES})
            pacer = ChannelPacer(max_calls=1000)
            interactions = Interactions(db=database, rest=rest, config=config, locks=CardLocks(),
                                        games_guild=GamesGuild(rest, config),
                                        seeder=Seeder(rest=rest, pacer=pacer, db=database), pacer=pacer,
                                        identity=BotIdentity(user_id=BOT_ID), roles=RoleCache(rest),
                                        clock=lambda: float(STARTS - 1000), kick=lambda: None,
                                        session_post=SessionPost(db=database, rest=rest, config=config, pacer=pacer))
            yield World(state=state, db=database, rest=rest, config=config, interactions=interactions,
                        session_id=session_id)
    finally:
        database.close()


async def add_rows(w: World, rows: list[tuple[str, str]]) -> None:
    """Stores real presence rows the way the RSVP path does, then recomputes the states."""
    def tx(c: Any) -> None:
        session = sessions.get(c, w.session_id)
        assert session is not None
        for user_id, choice in rows:
            sessions.add_presence(c, session, user_id, choice, 1)
        sessions.recompute(c, session, 1, 1)
    await w.db.run(tx)


def set_reactors(w: World, *, going: tuple[str, ...] = (), maybe: tuple[str, ...] = (),
                 no: tuple[str, ...] = ()) -> None:
    """Puts these reactors on the session message for each RSVP emoji, after the bot's own."""
    message = w.state.reactions.setdefault((SESSION_CH, SESSION_MSG), {})
    for emoji, users in ((rsvp.GOING_EMOJI, going), (rsvp.MAYBE_EMOJI, maybe), (rsvp.NO_EMOJI, no)):
        message[emoji] = [BOT_ID, *users]


async def rows(w: World) -> list[tuple[str, str, bool]]:
    """The presence rows in seq order: (user, choice, implicit)."""
    presence = await w.db.run(lambda c: sessions.presence_of(c, w.session_id))
    return [(p.user_id, p.choice, p.implicit) for p in presence]


async def states(w: World) -> rsvp.Rsvps:
    return await w.db.run(lambda c: sessions.rsvps_of(c, w.session_id))


def patches(w: World) -> list[Recorded]:
    return [r for r in w.state.requests if r.method == "PATCH"]


def dm_contents(w: World, user: str) -> list[str]:
    channel = w.state.dm_channels.get(user)
    if channel is None:
        return []
    return [r.payload["content"] for r in w.state.requests
            if r.method == "POST" and r.path == f"/api/v1/channels/{channel}/messages"]


# AC4.11: missing rows are added, rows whose reaction is gone are deleted, and the states are recomputed.

async def test_reconcile_adds_missing_rows_deletes_gone_reactions_and_recomputes(world: World) -> None:
    await add_rows(world, [(ALPHA, "going"), (BETA, "maybe")])
    set_reactors(world, going=[ALPHA, GAMMA], no=[BETA])
    await world.interactions.reconcile_sessions()
    # The proposer's implicit row stays. B's maybe is gone; C's going is added before B's no (seq order).
    assert await rows(world) == [(PROPOSER, "going", True), (ALPHA, "going", False),
                                 (GAMMA, "going", False), (BETA, "no", False)]
    stored = await states(world)
    engine = rsvp.apply(stored, await world.db.run(lambda c: sessions.presence_of(c, world.session_id)), 1)
    assert engine.rsvps == stored
    assert len(patches(world)) == 1


# The proposer's implicit row is kept while their reaction is missing from the message.

async def test_the_proposers_implicit_row_stays_when_their_reaction_is_not_on_the_message(world: World) -> None:
    set_reactors(world, going=[ALPHA])
    await world.interactions.reconcile_sessions()
    assert await rows(world) == [(PROPOSER, "going", True), (ALPHA, "going", False)]
    assert (await states(world))[PROPOSER] == ("going", None)


# A real reaction from the proposer replaces the implicit row: exactly one going row remains, and it is real.

async def test_the_proposers_real_reaction_replaces_their_implicit_row(world: World) -> None:
    set_reactors(world, going=[PROPOSER])
    await world.interactions.reconcile_sessions()
    assert await rows(world) == [(PROPOSER, "going", False)]
    assert (await states(world))[PROPOSER][0] == "going"


# A failed reactor read leaves the stored rows alone: a partial read never deletes a row.

async def test_a_failed_reactor_read_leaves_the_rows_untouched_and_skips_the_session(
        world: World, caplog: pytest.LogCaptureFixture) -> None:
    await add_rows(world, [(ALPHA, "maybe")])
    set_reactors(world, going=[ALPHA])
    world.state.overrides[f"GET {REACTORS_PATH}/{rsvp.MAYBE_EMOJI}/users"] = Override(
        500, b'{"code": "X", "message": "boom"}')
    before = await rows(world)
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.interactions")
    await world.interactions.reconcile_sessions()
    assert await rows(world) == before
    assert patches(world) == []
    assert "session_reconcile_skipped" in [r.getMessage() for r in caplog.records]


# Ended and cancelled sessions are not read at all: no request reaches their message.

async def test_an_ended_session_is_not_read_at_all(world: World) -> None:
    await world.db.run(lambda c: sessions.end_if(c, world.session_id, STARTS + 10))
    set_reactors(world, going=[ALPHA])
    await world.interactions.reconcile_sessions()
    assert world.state.requests == []


# A reconcile that promotes a waiting user DMs that user once, and a second reconcile with nothing to change DMs no one.

async def test_a_reconcile_that_promotes_a_waiting_user_dms_them_once(world: World) -> None:
    await add_rows(world, [(PROPOSER, "going"), (ALPHA, "going")])   # ALPHA waits behind the proposer's seat
    set_reactors(world, going=[ALPHA])                               # the proposer's reaction is gone
    await world.interactions.reconcile_sessions()
    [dm] = dm_contents(world, ALPHA)
    assert f"[{text.SESSION_LINK_LABEL}]({INSTANCE}{JUMP})" in dm
    patches_before = len(patches(world))
    await world.interactions.reconcile_sessions()
    assert dm_contents(world, ALPHA) == [dm]
    assert len(patches(world)) == patches_before


# The reconcile logs each session it read, as changed or unchanged, with ids only.

async def test_the_reconcile_logs_each_session_as_changed_or_unchanged(world: World,
                                                                        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.interactions")
    set_reactors(world, going=[PROPOSER])
    await world.interactions.reconcile_sessions()
    await world.interactions.reconcile_sessions()
    outcomes = [getattr(r, FIELDS_ATTR, {}).get("outcome") for r in caplog.records
                if r.getMessage() == "session_reconciled"]
    assert outcomes == ["changed", "unchanged"]


# The poller runs the session reconcile last, after its card and copy checks, so one startup repairs both.

async def test_the_poller_reconcile_runs_the_session_reconcile(world: World) -> None:
    await add_rows(world, [(ALPHA, "maybe")])
    set_reactors(world, going=[ALPHA])
    poller = Poller(db=world.db, rest=world.rest, sources=Sources(managers={}, probes={}), config=world.config,
                    locks=CardLocks(), clock=lambda: float(STARTS - 1000), games_guild=GamesGuild(world.rest, world.config),
                    seeder=None)
    poller.set_interactions(world.interactions)
    await poller.reconcile()
    assert await rows(world) == [(PROPOSER, "going", True), (ALPHA, "going", False)]


# The reactor lists are paged like the play-now count: reactors past the first page are read too.

async def test_reactors_past_the_first_page_are_read(world: World) -> None:
    many = tuple(f"7{n:03d}" for n in range(150))
    set_reactors(world, going=many)
    await world.interactions.reconcile_sessions()
    going = [user for user, choice, implicit in await rows(world) if choice == "going" and not implicit]
    assert sorted(going) == sorted(many)
