# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The startup reconcile of session RSVPs: `Interactions.reconcile_sessions` corrects the stored presence rows from the
reactions on each upcoming session's message, then recomputes, and `Poller.reconcile` runs it last. The fake REST app
holds the reactions; the tests read the rows, the PATCHes, the DMs and the log. Emoji come from rsvp, never as literals."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST, ReactionUsersPage
from fluxer_client.roles import RoleCache
from fluxer_panel import cards, rsvp, sessions, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import MAX_REACTOR_PAGES, Interactions
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
SECOND_MSG = "5101"
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
                 no: tuple[str, ...] = (), message_id: str = SESSION_MSG) -> None:
    """Puts these reactors on the session message for each RSVP emoji, after the bot's own."""
    message = w.state.reactions.setdefault((SESSION_CH, message_id), {})
    for emoji, users in ((rsvp.GOING_EMOJI, going), (rsvp.MAYBE_EMOJI, maybe), (rsvp.NO_EMOJI, no)):
        message[emoji] = [BOT_ID, *users]


def script_reactors(w: World, make_page: Callable[[int], ReactionUsersPage], *, message_id: str = SESSION_MSG,
                    emoji: str = rsvp.GOING_EMOJI) -> list[int]:
    """Answers the reactor reads of one reaction on one message with `make_page(n)` for the n-th call, counting from 0.
    Every other read goes to the fake. Returns the call numbers, so a test can count the pages read."""
    original = w.rest.get_reaction_users_page
    calls: list[int] = []

    async def page(channel_id: str, msg: str, em: str, **kw: Any) -> ReactionUsersPage:
        if msg == message_id and em == emoji:
            calls.append(len(calls))
            return make_page(calls[-1])
        return await original(channel_id, msg, em, **kw)

    w.rest.get_reaction_users_page = page
    return calls


async def add_second_session(w: World) -> int:
    """A second upcoming session on the same card, one hour after the first, with its own message."""
    def tx(c: Any) -> int:
        first = sessions.get(c, w.session_id)
        assert first is not None
        return sessions.add(c, card_id=first.card_id, guild_id=GUILD, channel_id=SESSION_CH, message_id=SECOND_MSG,
                            starts_at=STARTS + 3600, ends_at=STARTS + 7200, proposer_id=PROPOSER, now=1, seats=1)
    return await w.db.run(tx)


async def presence_rows(w: World, session_id: int) -> list[tuple[str, str, bool]]:
    presence = await w.db.run(lambda c: sessions.presence_of(c, session_id))
    return [(p.user_id, p.choice, p.implicit) for p in presence]


def event_fields(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    """The allowlisted fields of each logged record with this event name, in the order logged."""
    return [getattr(r, FIELDS_ATTR, {}) for r in caplog.records if r.getMessage() == name]


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


# A voter scheduled by a poll holds an implicit maybe (their session is a second one, with its own message).

async def add_voted_session(w: World, maybe: list[str]) -> int:
    """A second upcoming session on the same card, scheduled with these voters as implicit maybes."""
    def tx(c: Any) -> int:
        first = sessions.get(c, w.session_id)
        assert first is not None
        return sessions.add(c, card_id=first.card_id, guild_id=GUILD, channel_id=SESSION_CH, message_id=SECOND_MSG,
                            starts_at=STARTS + 3600, ends_at=STARTS + 7200, proposer_id=PROPOSER, now=1, maybe=maybe)
    return await w.db.run(tx)


# The implicit maybe survives a reconcile while the voter has no reaction on the message.

async def test_a_voters_implicit_maybe_survives_a_reconcile_with_no_reaction_from_them(world: World) -> None:
    second = await add_voted_session(world, [BETA])
    set_reactors(world, message_id=SECOND_MSG)   # the message is read in full, and nobody has reacted
    await world.interactions.reconcile_sessions()
    assert await presence_rows(world, second) == [(PROPOSER, "going", True), (BETA, "maybe", True)]


# A voter's real maybe reaction on the message replaces the implicit row, so the row is real and counted.

async def test_a_voters_maybe_reaction_on_the_message_replaces_their_implicit_maybe(world: World) -> None:
    second = await add_voted_session(world, [BETA])
    set_reactors(world, maybe=[BETA], message_id=SECOND_MSG)
    await world.interactions.reconcile_sessions()
    assert await presence_rows(world, second) == [(PROPOSER, "going", True), (BETA, "maybe", False)]
    assert (await world.db.run(lambda c: sessions.rsvps_of(c, second)))[BETA] == ("maybe", None)


# A voter's no reaction on the message replaces the implicit maybe too: the voter is no, and nothing else remains.

async def test_a_voters_no_reaction_on_the_message_replaces_their_implicit_maybe(world: World) -> None:
    second = await add_voted_session(world, [BETA])
    set_reactors(world, no=[BETA], message_id=SECOND_MSG)
    await world.interactions.reconcile_sessions()
    assert await presence_rows(world, second) == [(PROPOSER, "going", True), (BETA, "no", False)]
    assert (await world.db.run(lambda c: sessions.rsvps_of(c, second)))[BETA] == ("no", None)


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
    assert event_fields(caplog, "session_reconcile_skipped") == [{"session_id": world.session_id, "status": 500}]


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


# A live RSVP that starts while a reactor read is in flight is never undone by the reconcile. The live change is
# started as a task inside the last read: it waits for the card lock the reconcile holds, so it commits after the
# reconcile's transaction and its row survives. Before the reads were under the lock, it committed in between and the
# reconcile deleted it, because the reactor snapshot did not contain it.

async def test_a_live_rsvp_started_during_a_reactor_read_survives_the_reconcile(world: World) -> None:
    set_reactors(world, going=[BETA])
    original = world.rest.get_reaction_users_page
    live: list[asyncio.Task[None]] = []

    async def spy(channel_id: str, message_id: str, emoji: str, **kw: Any) -> Any:
        page = await original(channel_id, message_id, emoji, **kw)
        if emoji == rsvp.NO_EMOJI and not live:
            # ALPHA's going reaction lands on the message after the going list was read, and its live change starts now.
            world.state.reactions[(channel_id, message_id)][rsvp.GOING_EMOJI].append(ALPHA)
            live.append(asyncio.create_task(world.interactions.rsvp_changed(world.session_id, ALPHA, "going", True)))
            await asyncio.sleep(0.1)   # the live change runs as far as it can while the read is still in flight
        return page

    world.rest.get_reaction_users_page = spy
    await world.interactions.reconcile_sessions()
    await asyncio.gather(*live)
    going = [user for user, choice, implicit in await rows(world) if choice == "going" and not implicit]
    assert ALPHA in going, "a live RSVP was undone by the reconcile"
    assert BETA in going


# A reactor page that is not a user with a snowflake id skips its own session only. The second session, one hour later
# on the same card, is still reconciled.

@pytest.mark.parametrize("item", [{"id": "abc"}, "6002", {"bot": True}],
                         ids=["non_snowflake_id", "not_an_object", "missing_id"])
async def test_a_malformed_reactor_page_skips_only_its_own_session(world: World, caplog: pytest.LogCaptureFixture,
                                                                   item: Any) -> None:
    second_id = await add_second_session(world)
    set_reactors(world, going=[ALPHA])
    set_reactors(world, going=[BETA], message_id=SECOND_MSG)
    await add_rows(world, [(GAMMA, "maybe")])
    before = await rows(world)
    script_reactors(world, lambda n: ReactionUsersPage(users=[item], has_more=False, next_after=None))
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.interactions")
    await world.interactions.reconcile_sessions()
    assert await rows(world) == before
    assert event_fields(caplog, "session_reconcile_skipped") == [
        {"session_id": world.session_id, "reason": "reactors_malformed"}]
    assert await presence_rows(world, second_id) == [(PROPOSER, "going", True), (BETA, "going", False)]
    [patch] = patches(world)
    assert patch.path == f"/api/v1/channels/{SESSION_CH}/messages/{SECOND_MSG}"


# An exception in one session's reconcile is logged, and the next session is still reconciled.

async def test_a_session_that_raises_does_not_stop_the_next_session(world: World, caplog: pytest.LogCaptureFixture,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    second_id = await add_second_session(world)
    set_reactors(world, going=[ALPHA])
    set_reactors(world, going=[BETA], message_id=SECOND_MSG)
    before = await rows(world)
    real = sessions.reconcile_presence

    def failing_for_the_first(conn: sqlite3.Connection, session: sessions.Session, actual: Any, now: int,
                              via: Any = None, kept: Any = frozenset()) -> bool:
        if session.id == world.session_id:
            raise sqlite3.IntegrityError("simulated")
        return real(conn, session, actual, now, via=via, kept=kept)

    monkeypatch.setattr(sessions, "reconcile_presence", failing_for_the_first)
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.interactions")
    await world.interactions.reconcile_sessions()
    assert await rows(world) == before
    assert event_fields(caplog, "session_reconcile_failed") == [
        {"session_id": world.session_id, "reason": "IntegrityError"}]
    assert await presence_rows(world, second_id) == [(PROPOSER, "going", True), (BETA, "going", False)]


# A reactor list that stalls leaves the rows untouched: the cursor repeats, or a page adds no new user.

@pytest.mark.parametrize("make_page", [
    lambda n: ReactionUsersPage(users=[{"id": ALPHA}], has_more=True, next_after="6100"),
    lambda n: ReactionUsersPage(users=[{"id": ALPHA}], has_more=True, next_after=f"610{n}"),
], ids=["repeated_cursor", "page_with_nothing_new"])
async def test_a_stalled_reactor_list_leaves_the_rows_untouched(world: World, caplog: pytest.LogCaptureFixture,
                                                               make_page: Callable[[int], ReactionUsersPage]) -> None:
    await add_rows(world, [(ALPHA, "maybe")])
    set_reactors(world, going=[ALPHA])
    script_reactors(world, make_page)
    before = await rows(world)
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.interactions")
    await world.interactions.reconcile_sessions()
    assert await rows(world) == before
    assert patches(world) == []
    assert event_fields(caplog, "session_reconcile_skipped") == [
        {"session_id": world.session_id, "reason": "reactors_stalled"}]


# A reactor list that runs past MAX_REACTOR_PAGES leaves the rows untouched: every page has new users and more to come.

async def test_a_reactor_list_past_the_page_cap_leaves_the_rows_untouched(world: World,
                                                                          caplog: pytest.LogCaptureFixture) -> None:
    await add_rows(world, [(ALPHA, "maybe")])
    set_reactors(world, going=[ALPHA])
    calls = script_reactors(world, lambda n: ReactionUsersPage(
        users=[{"id": f"7{n:03d}"}], has_more=True, next_after=f"7{n:03d}"))
    before = await rows(world)
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.interactions")
    await world.interactions.reconcile_sessions()
    assert len(calls) == MAX_REACTOR_PAGES
    assert await rows(world) == before
    assert patches(world) == []
    assert event_fields(caplog, "session_reconcile_skipped") == [
        {"session_id": world.session_id, "reason": "reactors_truncated"}]


# A reactor flagged as a bot, who is not the bot itself, gets no row.

async def test_a_bot_flagged_reactor_gets_no_row(world: World) -> None:
    script_reactors(world, lambda n: ReactionUsersPage(
        users=[{"id": BETA, "bot": True}, {"id": ALPHA}], has_more=False, next_after=None))
    await world.interactions.reconcile_sessions()
    assert await rows(world) == [(PROPOSER, "going", True), (ALPHA, "going", False)]
