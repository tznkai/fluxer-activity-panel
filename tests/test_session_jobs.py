# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Timed session jobs (AC5, AC10.2): ending, the weekly series and the asleep heads-up, against the fake REST server. A
real SessionJobs, SessionPost, SessionFlow and Interactions run on a SQLite file. The clock is a stub that moves only
when a test moves it, and the pacer never sleeps. Nothing waits on the wall clock. Times are in America/New_York, so
the weekly series is checked across the end of daylight time."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from fluxer_client.roles import RoleCache
from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, sessions, subscriptions, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import ManagerRef
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.dm_router import NonAdminLimiter
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.resolver import CardStatus, StatusKind
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_flow import SessionFlow
from fluxer_panel.session_jobs import SessionJobs
from fluxer_panel.session_post import SessionPost
from fluxer_panel.sessions import Session
from tests.fakes import FakeFluxerState, Override, Recorded, make_rest_app

TOKEN = "session-jobs-token"
URL = "https://fluxer.invalid"
BOT_ID = "1900"
ADMIN = "6101"
ADMIN_2 = "6104"
PROPOSER = "6102"
GOING = "6103"
GUILD = "2001"
SESSION_CH = "3200"      # the session channel, in GUILD
GAMES = "3100"           # the games channel, which no job may touch
HOUR = 3600
DAY = 86400
ZONE = ZoneInfo("America/New_York")
# Saturday 2026-10-31 14:00 EDT, and the next week's 14:00 EST, one hour later in UTC once the clocks have gone back.
S = int(datetime(2026, 10, 31, 14, 0, tzinfo=ZONE).timestamp())
NEXT = int(datetime(2026, 11, 7, 14, 0, tzinfo=ZONE).timestamp())
SERVER_ERROR = b'{"code": "INTERNAL", "message": "x"}'
SESSION_POSTS = f"/api/v1/channels/{SESSION_CH}/messages"
POST_OVERRIDE = f"POST {SESSION_POSTS}"


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@dataclass
class World:
    db: Database
    state: FakeFluxerState
    rest: FluxerREST
    clock: Clock
    config: Config
    post: SessionPost
    flow: SessionFlow
    jobs: SessionJobs
    locks: CardLocks
    statuses: dict[int, CardStatus | None]
    card_id: int
    kicks: list[int]        # one entry per poller kick the jobs ask for
    backoff: DmBackoff


def config_for() -> Config:
    return Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "America/New_York",
                            "FLUXER_INSTANCE_URL": URL, "ADMIN_USER_IDS": f"{ADMIN},{ADMIN_2}",
                            "GAMES_CHANNEL_ID": GAMES, "POLL_INTERVAL_SECONDS": "3600"})


@pytest.fixture
async def world(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[World]:
    database = Database.open(tmp_path / "session_jobs.db")
    state = FakeFluxerState(bot_user_id=BOT_ID)
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    config = config_for()
    clock = Clock(float(S))
    statuses: dict[int, CardStatus | None] = {}
    kicks: list[int] = []
    try:
        async with aiohttp.ClientSession() as client:
            rest = FluxerREST(client, f"{base}/api", TOKEN)
            pacer = ChannelPacer(max_calls=1000)
            backoff = DmBackoff(config.dm_backoff_seconds, clock=clock)
            locks = CardLocks()
            post = SessionPost(db=database, rest=rest, config=config, pacer=pacer, backoff=backoff)
            flow = SessionFlow(db=database, rest=rest, config=config, locks=locks, session_post=post,
                               games_guild=GamesGuild(rest, config), pacer=pacer,
                               identity=BotIdentity(user_id=BOT_ID), backoff=backoff,
                               limiter=NonAdminLimiter(config, clock=clock), clock=clock)
            jobs = SessionJobs(db=database, rest=rest, config=config, locks=locks, session_post=post,
                               session_flow=flow, status_of=statuses.get, clock=clock, backoff=backoff,
                               kick=lambda: kicks.append(1))
            card_id = await database.run(_seed_card)
            yield World(db=database, state=state, rest=rest, clock=clock, config=config, post=post, flow=flow,
                        jobs=jobs, locks=locks, statuses=statuses, card_id=card_id, kicks=kicks, backoff=backoff)
    finally:
        database.close()


def _seed_card(conn: Any) -> int:
    card_id = cards.create_card(conn, slug="lobby", title="Lobby", author_id=ADMIN, now=1)
    cards.set_state(conn, card_id, "live", 1)
    return card_id


_message_ids = iter(range(7001, 10_000))


async def add_session(world: World, *, starts: int = S, ends: int | None = None, recurrence: str | None = None,
                      location: str | None = None, seats: int | None = None, proposer: str = PROPOSER,
                      proposer_cap: bool = True) -> int:
    """An upcoming session in the session channel, with a message id no other session in the test has."""
    message = str(next(_message_ids))
    end = starts + HOUR if ends is None else ends

    def tx(conn: Any) -> int:
        return sessions.add(conn, card_id=world.card_id, guild_id=GUILD, channel_id=SESSION_CH, message_id=message,
                            starts_at=starts, ends_at=end, proposer_id=proposer, now=1, location_text=location,
                            seats=seats, recurrence=recurrence, proposer_cap=proposer_cap)

    return await world.db.run(tx)


async def row(world: World, session_id: int) -> Session:
    found = await world.db.run(lambda c: sessions.get(c, session_id))
    assert found is not None
    return found


async def row_by_message(world: World, message_id: str) -> Session:
    found = await world.db.run(lambda c: sessions.get_by_message(c, message_id))
    assert found is not None
    return found


async def set_rsvps(world: World, session_id: int, rows: list[tuple[str, str]]) -> None:
    def tx(conn: Any) -> None:
        session = sessions.get(conn, session_id)
        assert session is not None
        for user_id, choice in rows:
            sessions.add_presence(conn, session, user_id, choice, 1)
        sessions.recompute(conn, session, None, 1)

    await world.db.run(tx)


async def set_manager(world: World) -> None:
    await world.db.run(lambda c: cards.set_manager(c, world.card_id, ManagerRef("pterodactyl", "srv")))


def posts(world: World) -> list[Recorded]:
    return [r for r in world.state.requests if r.method == "POST" and r.path == SESSION_POSTS]


def patches(world: World) -> list[Recorded]:
    return [r for r in world.state.requests
            if r.method == "PATCH" and r.path.startswith(f"/api/v1/channels/{SESSION_CH}/messages/")]


def deletes(world: World) -> list[Recorded]:
    return [r for r in world.state.requests
            if r.method == "DELETE" and r.path.startswith(f"/api/v1/channels/{SESSION_CH}/messages/")]


def dms_to(world: World, user_id: str) -> list[str]:
    channel = world.state.dm_channels.get(user_id)
    if channel is None:
        return []
    path = f"/api/v1/channels/{channel}/messages"
    return [r.payload["content"] for r in world.state.requests if r.method == "POST" and r.path == path]


def field_value(payload: dict[str, Any], name: str) -> str | None:
    for embed in payload["embeds"]:
        for field in embed.get("fields", []):
            if field["name"] == name:
                return field["value"]
    return None


def headsup_text(n: int) -> str:
    return text.SESSION_HEADSUP.format(card="Lobby", ts=S, n=n)


def make_interactions(world: World) -> Interactions:
    pacer = ChannelPacer(max_calls=1000)
    return Interactions(db=world.db, rest=world.rest, config=world.config, locks=world.locks,
                        games_guild=GamesGuild(world.rest, world.config),
                        seeder=Seeder(rest=world.rest, pacer=pacer, db=world.db), pacer=pacer,
                        identity=BotIdentity(user_id=BOT_ID), roles=RoleCache(world.rest),
                        clock=world.clock, kick=lambda: None, session_post=world.post,
                        backoff=DmBackoff(world.config.dm_backoff_seconds, clock=world.clock),
                        session_flow=world.flow)


# Ending (AC5.1) ------------------------------------------------------------------------------------------------------

async def test_a_session_ends_at_its_end_time_and_its_message_says_ended(world: World) -> None:
    sid = await add_session(world)
    world.clock.now = S + HOUR - 1
    await world.jobs.run()
    assert (await row(world, sid)).state == "upcoming"
    assert patches(world) == []

    world.clock.now = S + HOUR
    await world.jobs.run()
    ended = await row(world, sid)
    assert ended.state == "ended"
    assert ended.closed_at == S + HOUR
    [patch] = patches(world)
    assert patch.path == f"/api/v1/channels/{SESSION_CH}/messages/{ended.message_id}"
    assert field_value(patch.payload, text.SESSION_FIELD_STATUS) == text.SESSION_ENDED


async def test_an_rsvp_after_the_end_changes_nothing(world: World) -> None:
    sid = await add_session(world)
    world.clock.now = S + HOUR
    await world.jobs.run()
    patched = len(patches(world))
    before = await world.db.run(lambda c: sessions.rsvps_of(c, sid))

    await make_interactions(world).rsvp_changed(sid, GOING, "going", True)

    assert await world.db.run(lambda c: sessions.rsvps_of(c, sid)) == before
    assert len(patches(world)) == patched


async def test_the_end_is_read_again_under_the_lock(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = await add_session(world)
    original = sessions.due_to_end

    def move_after_the_candidate_read(conn: Any, now: int) -> list[Session]:
        due = original(conn, now)
        assert sessions.move_if(conn, sid, starts_at=S + HOUR, ends_at=S + 3 * HOUR)   # the end is now in the future
        return due

    monkeypatch.setattr(sessions, "due_to_end", move_after_the_candidate_read)
    world.clock.now = S + HOUR
    await world.jobs.run()
    assert (await row(world, sid)).state == "upcoming"
    assert patches(world) == []
    assert world.kicks == []


async def test_an_end_kicks_the_poller_once(world: World) -> None:
    await add_session(world)
    world.clock.now = S + HOUR
    await world.jobs.run()
    assert world.kicks == [1]


async def test_an_ended_session_whose_message_is_gone_stays_ended_and_cancels_nothing(
        world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="fluxer_panel.session_jobs")
    sid = await add_session(world)
    await set_rsvps(world, sid, [(GOING, "going")])
    gone = []
    world.post.set_on_gone(lambda session_id: _record_gone(gone, session_id))   # the cancel hook, as in production
    message = (await row(world, sid)).message_id
    world.state.overrides[f"PATCH /api/v1/channels/{SESSION_CH}/messages/{message}"] = Override(404, SERVER_ERROR)
    world.clock.now = S + HOUR
    await world.jobs.run()

    assert (await row(world, sid)).state == "ended"
    assert gone == []                              # the hook runs only for an upcoming session
    assert dms_to(world, GOING) == []
    failed = [r for r in caplog.records if r.name == "fluxer_panel.session_jobs"
              and getattr(r, FIELDS_ATTR, {}).get("session_id") == sid]
    assert [r.getMessage() for r in failed] == ["session_edit_failed"]


async def _record_gone(gone: list[int], session_id: int) -> None:
    gone.append(session_id)


# The weekly series (AC5.2, AC5.3, AC5.4, AC5.5) ----------------------------------------------------------------------

async def test_a_weekly_session_posts_its_next_occurrence_one_day_after_its_start(world: World) -> None:
    sid = await add_session(world, recurrence="weekly", location="X", seats=4)
    await set_rsvps(world, sid, [("6201", "going"), ("6202", "going")])
    world.clock.now = S + DAY - 1
    await world.jobs.run()
    assert posts(world) == []

    world.clock.now = S + DAY
    await world.jobs.run()
    [sent] = posts(world)
    old = await row(world, sid)
    assert old.next_posted
    new = await row_by_message(world, _posted_id(world))
    assert new.id != sid
    assert sent.path == SESSION_POSTS
    assert new.starts_at == NEXT
    assert new.ends_at == NEXT + HOUR
    assert new.series_id == old.series_id
    assert new.recurrence == "weekly"
    assert new.location_text == "X"
    assert new.seats == 4
    assert new.state == "upcoming"
    assert await world.db.run(lambda c: sessions.rsvps_of(c, new.id)) == {PROPOSER: ("going", None)}


LOCAL_CH = "3950"      # a local subscription's channel, in GUILD


async def test_a_weekly_local_session_posts_its_next_occurrence_in_the_same_scope_and_channel(world: World) -> None:
    def tx(conn: Any) -> tuple[int, int]:
        sub_id = subscriptions.add(conn, world.card_id, GUILD, LOCAL_CH)
        assert subscriptions.set_mode_if(conn, sub_id, "redirect", "local")
        subscriptions.set_local_channel(conn, sub_id, LOCAL_CH)
        session_id = sessions.add(conn, card_id=world.card_id, guild_id=GUILD, channel_id=LOCAL_CH, message_id="7900",
                                  starts_at=S, ends_at=S + HOUR, proposer_id=PROPOSER, now=1, recurrence="weekly",
                                  scope_sub_id=sub_id)
        return sub_id, session_id

    sub_id, sid = await world.db.run(tx)
    world.clock.now = S + DAY
    await world.jobs.run()

    local_posts = f"/api/v1/channels/{LOCAL_CH}/messages"
    [sent] = [r for r in world.state.requests if r.method == "POST" and r.path == local_posts]
    assert sent.path == local_posts and posts(world) == []
    [posted] = [mid for (cid, mid) in world.state.messages if cid == LOCAL_CH and mid != "7900"]
    new = await row_by_message(world, posted)
    assert new.id != sid
    assert new.scope_sub_id == sub_id
    assert new.channel_id == LOCAL_CH


async def test_the_new_occurrence_gets_its_three_reactions_on_its_message(world: World) -> None:
    await add_session(world, recurrence="weekly")
    world.clock.now = S + DAY
    await world.jobs.run()
    message_id = _posted_id(world)
    seeds = [r for r in world.state.requests if r.method == "PUT" and "/reactions/" in r.path]
    assert len(seeds) == 3
    assert all(r.path.startswith(f"{SESSION_POSTS}/{message_id}/reactions/") for r in seeds)


def _posted_id(world: World) -> str:
    """The id of the last message the fake stored in the session channel: the newest session post."""
    return max((key[1] for key in world.state.messages if key[0] == SESSION_CH), key=int)


async def test_stopping_the_series_stops_the_later_postings_and_keeps_the_posted_one(world: World) -> None:
    sid = await add_session(world, recurrence="weekly")
    world.clock.now = S + DAY
    await world.jobs.run()
    second = await row_by_message(world, _posted_id(world))
    await world.db.run(lambda c: sessions.set_series_recurrence(c, second.series_id, None))

    world.clock.now = NEXT + 60
    await world.jobs.run()
    assert (await row(world, second.id)).state == "upcoming"   # the posted occurrence stays

    world.clock.now = NEXT + DAY
    await world.jobs.run()
    assert len(posts(world)) == 1
    assert (await row(world, sid)).series_id == second.series_id


async def test_the_job_reads_the_occurrence_again_under_the_lock(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = await add_session(world, ends=S + 2 * DAY, recurrence="weekly")
    moved_to = S + HOUR
    original = sessions.recurrence_due

    def move_after_the_candidate_read(conn: Any, now: int) -> list[Session]:
        due = original(conn, now)
        assert sessions.move_if(conn, sid, starts_at=moved_to, ends_at=moved_to + 2 * DAY)
        return due

    monkeypatch.setattr(sessions, "recurrence_due", move_after_the_candidate_read)
    world.clock.now = S + DAY
    await world.jobs.run()
    assert posts(world) == []                 # the moved occurrence is not due yet, so nothing is posted
    monkeypatch.setattr(sessions, "recurrence_due", original)

    world.clock.now = moved_to + DAY
    await world.jobs.run()
    [sent] = posts(world)
    new = await row_by_message(world, _posted_id(world))
    assert new.starts_at == int(datetime(2026, 11, 7, 15, 0, tzinfo=ZONE).timestamp())
    assert sent.path == SESSION_POSTS


async def test_concurrent_runs_post_each_occurrence_once(world: World) -> None:
    sid = await add_session(world, recurrence="weekly")
    world.clock.now = S + DAY
    await asyncio.gather(world.jobs.run(), world.jobs.run())
    assert len(posts(world)) == 1
    assert len(await world.db.run(sessions.all_upcoming)) == 1

    await world.jobs.run()
    assert len(posts(world)) == 1
    assert (await row(world, sid)).next_posted


async def test_a_failed_store_deletes_the_posted_message_and_the_next_run_posts_once(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = await add_session(world, recurrence="weekly")
    original_add = sessions.add
    failures: list[int] = []

    def fail_once(conn: Any, **kwargs: Any) -> int:
        if not failures:
            failures.append(1)
            raise RuntimeError("store failed")
        return original_add(conn, **kwargs)

    monkeypatch.setattr(sessions, "add", fail_once)
    world.clock.now = S + DAY
    await world.jobs.run()
    assert len(posts(world)) == 1
    assert [r.path for r in deletes(world)] == [f"{SESSION_POSTS}/{_posted_id(world)}"]
    assert not (await row(world, sid)).next_posted          # the mark rolled back with the failed store
    assert await world.db.run(sessions.all_upcoming) == []  # no row for the failed occurrence

    await world.jobs.run()
    assert len(posts(world)) == 2
    assert (await row(world, sid)).next_posted
    assert [s.starts_at for s in await world.db.run(sessions.all_upcoming)] == [NEXT]


async def test_a_posted_occurrence_kicks_the_poller_once(world: World) -> None:
    await add_session(world, ends=S + 2 * DAY, recurrence="weekly")   # still running when its successor is posted
    world.clock.now = S + DAY
    await world.jobs.run()
    assert len(posts(world)) == 1
    assert world.kicks == [1]


async def test_an_occurrence_of_an_archived_card_clears_the_series_and_posts_nothing(world: World) -> None:
    sid = await add_session(world, recurrence="weekly")
    await world.db.run(lambda c: cards.set_state(c, world.card_id, "archived", 1))
    world.clock.now = S + DAY
    await world.jobs.run()
    assert posts(world) == []
    assert (await row(world, sid)).recurrence is None


async def test_the_cap_stops_the_series_and_dms_the_proposer_once(world: World) -> None:
    # The due occurrence is still upcoming (it runs two days), so with nine more it is the card's tenth.
    sid = await add_session(world, ends=S + 2 * DAY, recurrence="weekly")
    for n in range(9):
        await add_session(world, starts=S + 30 * DAY + n * HOUR, proposer=f"63{n:02d}", proposer_cap=False)
    world.clock.now = S + DAY
    world.backoff.failed(PROPOSER)            # the proposer is in DM backoff: the stop note still goes out (bypass)
    await world.jobs.run()
    assert posts(world) == []
    assert (await row(world, sid)).recurrence is None
    expected = text.SESSION_SERIES_STOPPED.format(card="Lobby")
    assert dms_to(world, PROPOSER) == [expected]

    await world.jobs.run()
    assert dms_to(world, PROPOSER) == [expected]
    assert posts(world) == []


# The asleep heads-up (AC5.6, AC5.7) ----------------------------------------------------------------------------------

async def test_an_asleep_card_with_someone_going_gets_one_heads_up_to_each_admin(world: World) -> None:
    await set_manager(world)
    world.statuses[world.card_id] = CardStatus(StatusKind.ASLEEP)
    await add_session(world)
    world.clock.now = S - 29 * 60
    await world.jobs.run()
    assert dms_to(world, ADMIN) == [headsup_text(1)]
    assert dms_to(world, ADMIN_2) == [headsup_text(1)]
    content = dms_to(world, ADMIN)[0]
    assert "<@" not in content and PROPOSER not in content          # AC11.2

    world.clock.now = S - 28 * 60
    await world.jobs.run()
    assert dms_to(world, ADMIN) == [headsup_text(1)]


async def test_no_heads_up_with_nobody_going(world: World) -> None:
    await set_manager(world)
    world.statuses[world.card_id] = CardStatus(StatusKind.ASLEEP)
    sid = await add_session(world)

    def proposer_declines(conn: Any) -> None:
        session = sessions.get(conn, sid)
        assert session is not None
        sessions.add_presence(conn, session, PROPOSER, "no", 1)
        sessions.recompute(conn, session, None, 1)

    await world.db.run(proposer_declines)
    world.clock.now = S - 29 * 60
    await world.jobs.run()
    assert dms_to(world, ADMIN) == []


@pytest.mark.parametrize("managed, status", [
    (False, CardStatus(StatusKind.ASLEEP)),   # no manager
    (True, CardStatus(StatusKind.READY)),     # awake
    (True, None),                             # no status at all
])
async def test_no_heads_up_without_a_manager_or_when_the_server_is_not_asleep(
        world: World, managed: bool, status: CardStatus | None) -> None:
    if managed:
        await set_manager(world)
    world.statuses[world.card_id] = status
    await add_session(world)
    world.clock.now = S - 29 * 60
    await world.jobs.run()
    assert dms_to(world, ADMIN) == []


async def test_no_heads_up_thirty_one_minutes_before_the_start(world: World) -> None:
    await set_manager(world)
    world.statuses[world.card_id] = CardStatus(StatusKind.ASLEEP)
    await add_session(world)
    world.clock.now = S - 31 * 60
    await world.jobs.run()
    assert dms_to(world, ADMIN) == []


async def test_a_move_gives_the_new_time_its_own_heads_up(world: World) -> None:
    await set_manager(world)
    world.statuses[world.card_id] = CardStatus(StatusKind.ASLEEP)
    sid = await add_session(world)
    world.clock.now = S - 29 * 60
    await world.jobs.run()
    assert dms_to(world, ADMIN) == [headsup_text(1)]

    moved = S + 2 * HOUR
    await world.db.run(lambda c: sessions.move_if(c, sid, starts_at=moved, ends_at=moved + HOUR))
    world.clock.now = moved - 29 * 60
    await world.jobs.run()
    assert dms_to(world, ADMIN) == [headsup_text(1), text.SESSION_HEADSUP.format(card="Lobby", ts=moved, n=1)]


async def test_a_session_moved_out_of_its_window_after_the_listing_gets_no_heads_up(
        world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await set_manager(world)
    world.statuses[world.card_id] = CardStatus(StatusKind.ASLEEP)
    sid = await add_session(world)
    await set_rsvps(world, sid, [(GOING, "going")])
    original = sessions.headsup_candidates

    def move_after_the_listing(conn: Any, now: int) -> list[Session]:
        due = original(conn, now)
        moved = S + 2 * HOUR                   # two hours out: the window the listing saw is now over
        assert sessions.move_if(conn, sid, starts_at=moved, ends_at=moved + HOUR)
        return due

    monkeypatch.setattr(sessions, "headsup_candidates", move_after_the_listing)
    world.clock.now = S - 29 * 60
    await world.jobs.run()
    assert dms_to(world, ADMIN) == []
    assert not (await row(world, sid)).headsup_sent


async def test_a_run_without_the_heads_up_step_sends_none_and_the_next_run_sends_it_once(world: World) -> None:
    # The poller skips the heads-up on a tick whose card pass did not finish. The heads-up stays pending, `due` is still
    # read, and the next run that includes the step sends it.
    await set_manager(world)
    world.statuses[world.card_id] = CardStatus(StatusKind.ASLEEP)
    sid = await add_session(world)
    world.clock.now = S - 29 * 60
    await world.jobs.run(headsup=False)
    assert dms_to(world, ADMIN) == []
    assert not (await row(world, sid)).headsup_sent
    assert world.jobs.due == S + HOUR                 # the session's end: the window is open, so it is not a wake-up
    await world.jobs.run()
    assert dms_to(world, ADMIN) == [headsup_text(1)]
    await world.jobs.run()
    assert dms_to(world, ADMIN) == [headsup_text(1)]


# Stored channel only (AC10.2) ----------------------------------------------------------------------------------------

async def test_the_jobs_touch_only_the_session_channel(world: World) -> None:
    await set_manager(world)
    world.statuses[world.card_id] = CardStatus(StatusKind.ASLEEP)
    await add_session(world, recurrence="weekly")
    world.clock.now = S - 29 * 60
    await world.jobs.run()
    world.clock.now = S + DAY
    await world.jobs.run()
    assert len(posts(world)) == 1
    assert not any(f"/channels/{GAMES}" in r.path for r in world.state.requests)


# The due time (run loop wake-ups) ------------------------------------------------------------------------------------

async def test_due_is_the_end_of_a_session_that_is_running(world: World) -> None:
    await add_session(world)
    world.clock.now = S + 60
    await world.jobs.run()
    assert world.jobs.due == S + HOUR


async def test_due_is_the_heads_up_window_of_a_session_ahead(world: World) -> None:
    await set_manager(world)
    await add_session(world)
    world.clock.now = S - HOUR
    await world.jobs.run()
    assert world.jobs.due == S - 30 * 60


async def test_due_is_the_draft_expiry_when_it_comes_first(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_session(world)
    world.clock.now = S + 60
    monkeypatch.setattr(world.flow, "next_expiry", lambda: S + 160.0)
    await world.jobs.run()
    assert world.jobs.due == S + 160.0


async def test_due_never_sits_in_the_past_when_a_draft_expiry_is_stuck(world: World,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail() -> None:
        raise RuntimeError("draft sweep failed")

    await add_session(world)
    monkeypatch.setattr(world.flow, "expire_drafts", fail)
    monkeypatch.setattr(world.flow, "next_expiry", lambda: world.clock.now - 10)
    world.clock.now = S + 60
    await world.jobs.run()
    assert world.jobs.due == S + HOUR                     # the session's end, not the stuck expiry, which is past


async def test_due_is_not_held_by_an_open_heads_up_window_without_a_manager(world: World) -> None:
    await add_session(world)
    world.clock.now = S - 1000
    await world.jobs.run()
    assert world.jobs.due == S + HOUR
    assert dms_to(world, ADMIN) == []


async def test_due_is_not_held_by_a_recurrence_that_keeps_failing(world: World) -> None:
    sid = await add_session(world, recurrence="weekly")
    world.state.overrides[POST_OVERRIDE] = Override(500, SERVER_ERROR)
    world.clock.now = S + DAY
    await world.jobs.run()
    assert len(posts(world)) == 1                              # the refused attempt, and nothing stored
    assert (await row(world, sid)).next_posted is False
    assert world.jobs.due is None or world.jobs.due > world.clock.now

    del world.state.overrides[POST_OVERRIDE]
    await world.jobs.run()
    assert len(posts(world)) == 2                              # the next tick retries the same occurrence
    assert (await row(world, sid)).next_posted
    assert [s.starts_at for s in await world.db.run(sessions.all_upcoming)] == [NEXT]


async def test_due_is_the_later_end_when_an_earlier_end_is_still_pending(world: World,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    # The end job fails, so the earlier session stays upcoming past its end: a pending time that is not a wake-up.
    earlier = await add_session(world, proposer_cap=False)
    later_start = S + 3 * DAY
    later = await add_session(world, starts=later_start, proposer=GOING, proposer_cap=False)
    await world.db.run(lambda c: sessions.mark_headsup_if(c, later))   # no heads-up wake for the later one either

    def broken(conn: Any, now: int) -> list[Session]:
        raise RuntimeError("end query failed")

    monkeypatch.setattr(sessions, "due_to_end", broken)
    world.clock.now = S + HOUR
    await world.jobs.run()
    assert (await row(world, earlier)).state == "upcoming"
    assert (await row(world, later)).state == "upcoming"
    assert world.jobs.due == later_start + HOUR


async def test_due_is_the_later_heads_up_window_when_an_earlier_one_is_open_without_a_manager(world: World) -> None:
    await add_session(world, ends=S + DAY, proposer_cap=False)          # its window is open, but no manager: pending
    later = S + 2 * HOUR
    await add_session(world, starts=later, ends=later + DAY, proposer=GOING, proposer_cap=False)
    world.clock.now = S - 1000
    await world.jobs.run()
    assert dms_to(world, ADMIN) == []
    assert world.jobs.due == later - sessions.HEADSUP_WINDOW


async def test_due_is_the_later_recurrence_when_an_earlier_one_keeps_failing(world: World) -> None:
    await add_session(world, recurrence="weekly", proposer_cap=False)  # due at S + DAY, and its POST keeps failing
    later = S + 3 * HOUR
    await add_session(world, starts=later, ends=later + 2 * DAY, recurrence="weekly", proposer=GOING,
                      proposer_cap=False)
    world.state.overrides[POST_OVERRIDE] = Override(500, SERVER_ERROR)
    world.clock.now = S + DAY
    await world.jobs.run()
    assert len(posts(world)) == 1                 # the earlier occurrence's refused attempt, and nothing stored
    assert world.jobs.due == later + DAY


async def test_draft_expiry_still_runs_through_run(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def record() -> None:
        calls.append(1)

    monkeypatch.setattr(world.flow, "expire_drafts", record)
    await world.jobs.run()
    assert calls == [1]


async def test_a_failing_step_does_not_stop_the_others(world: World, monkeypatch: pytest.MonkeyPatch,
                                                       caplog: pytest.LogCaptureFixture) -> None:
    sid = await add_session(world, recurrence="weekly")
    caplog.set_level(logging.WARNING, logger="fluxer_panel.session_jobs")

    def broken(conn: Any, now: int) -> list[Session]:
        raise RuntimeError("end query failed")

    monkeypatch.setattr(sessions, "due_to_end", broken)
    world.clock.now = S + DAY
    await world.jobs.run()
    assert len(posts(world)) == 1
    assert (await row(world, sid)).next_posted
    failed = [r for r in caplog.records if r.name == "fluxer_panel.session_jobs"
              and getattr(r, FIELDS_ATTR, {}).get("kind") == "end"]
    assert len(failed) == 1
