# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The timed poll job (AC6.1, AC6.2, AC6.3, AC6.8): due polls close on time with their ranking and seeds, and each
unanswered recipient gets one halfway reminder. A real PollJobs and PollCloser run on a SQLite file against the fake
REST server. The clock is a stub that moves only when a test moves it, and the debounce never fires."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, polls, render, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.games import BotIdentity
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poll_close import PollCloser
from fluxer_panel.poll_jobs import PollJobs
from fluxer_panel.poll_post import POLL_RUNOFF_EMOJI, POLL_SCHEDULE_EMOJI, PollPost
from tests.fakes import FakeFluxerState, Recorded, make_rest_app

TOKEN = "SENTINEL-TOKEN-poll-jobs"
URL = "https://fluxer.invalid"
BOT = "1900"
ADMIN = "1001"
GUILD = "2001"
GAMES = "3001"
CHANNEL = "3010"
POLL_MSG = "5001"
RUNOFF_MSG = "5002"
SECOND_MSG = "5003"
NOW = 1_800_000_000
CLOSES = NOW + 7200                # two hours after the start, so the halfway time is an hour in
HALFWAY = NOW + 3600
TOO_SOON = CLOSES + 1800           # under an hour after the close: never usable
ONE = CLOSES + 7200
TWO = CLOSES + 10800
VOTERS = ("1101", "1102", "1103")
POLL_PATH = f"/api/v1/channels/{CHANNEL}/messages/{POLL_MSG}"
BACKOFF_SECONDS = 300              # shorter than the poll, so a backed-off recipient can still be reminded before close


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def never(_seconds: float) -> None:
    """The debounce sleep: it never returns, so only an immediate flush edits anything here."""
    await asyncio.Event().wait()


def config_for(**overrides: str) -> Config:
    env = {"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": URL,
           "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES, "POLL_INTERVAL_SECONDS": "3600",
           "DM_BACKOFF_SECONDS": str(BACKOFF_SECONDS)}
    return Config.from_env({**env, **overrides})


@dataclass
class World:
    db: Database
    state: FakeFluxerState
    rest: FluxerREST
    clock: Clock
    config: Config
    post: PollPost
    closer: PollCloser
    backoff: DmBackoff
    jobs: PollJobs
    card_id: int
    kicks: list[int]


def jobs_for(world: World, config: Config) -> PollJobs:
    """A second PollJobs over the same wiring, for a test that needs another config (reminders off)."""
    return PollJobs(db=world.db, rest=world.rest, config=config, closer=world.closer, backoff=world.backoff,
                    clock=world.clock, kick=lambda: world.kicks.append(1))


@pytest.fixture
async def world(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[World]:
    database = Database.open(tmp_path / "poll_jobs.db")
    state = FakeFluxerState(bot_user_id=BOT, next_message_id=9001)
    state.messages[(CHANNEL, POLL_MSG)] = {"embeds": []}
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    config = config_for()
    clock = Clock(float(NOW))
    kicks: list[int] = []
    post: PollPost | None = None
    try:
        async with aiohttp.ClientSession() as client:
            rest = FluxerREST(client, f"{base}/api", TOKEN)
            backoff = DmBackoff(config.dm_backoff_seconds, clock=clock)
            post = PollPost(db=database, rest=rest, config=config, pacer=ChannelPacer(max_calls=1000),
                            identity=BotIdentity(user_id=BOT), backoff=backoff, edit_delay=0, sleep=never)
            locks = CardLocks()
            closer = PollCloser(db=database, locks=locks, poll_post=post, clock=clock)
            card_id = await database.run(_card)
            jobs = PollJobs(db=database, rest=rest, config=config, closer=closer, backoff=backoff, clock=clock,
                            kick=lambda: kicks.append(1))
            yield World(db=database, state=state, rest=rest, clock=clock, config=config, post=post, closer=closer,
                        backoff=backoff, jobs=jobs, card_id=card_id, kicks=kicks)
    finally:
        if post is not None:
            await post.aclose(timeout=0.05)
        database.close()


def _card(conn: Any) -> int:
    card_id = cards.create_card(conn, slug="lobby", title="Lobby", author_id=ADMIN, now=1)
    cards.set_state(conn, card_id, "live", 1)
    return card_id


async def store(world: World, *, starts: list[int], users: tuple[str, ...] = VOTERS, kind: str = "approval",
                parent: int | None = None, message: str = POLL_MSG, card_id: int | None = None) -> int:
    def tx(conn: Any) -> int:
        poll_id = polls.create(conn, card_id=card_id or world.card_id, guild_id=GUILD, channel_id=CHANNEL,
                               message_id=message, starter_id=ADMIN, kind=kind, parent_poll_id=parent, now=NOW,
                               closes_at=CLOSES, ballot_size=6, options=[(s, "offered") for s in starts],
                               recipients=list(users))
        assert poll_id is not None
        return poll_id

    return await world.db.run(tx)


async def option_of(world: World, poll_id: int, starts_at: int) -> int:
    options = await world.db.run(lambda c: polls.options(c, poll_id))
    return next(option.id for option in options if option.starts_at == starts_at)


async def vote(world: World, poll_id: int, user: str, option_ids: list[int], *, ranked: bool = False) -> None:
    assert await world.db.run(lambda c: polls.replace_picks_if_open(c, poll_id, user, option_ids, ranked=ranked))


async def send_ballots(world: World, poll_id: int, users: tuple[str, ...] = VOTERS) -> None:
    for user in users:
        assert await world.post.send_ballot(poll_id, user)


async def stored(world: World, poll_id: int) -> polls.Poll:
    poll = await world.db.run(lambda c: polls.get(c, poll_id))
    assert poll is not None
    return poll


async def ballot_of(world: World, poll_id: int, user: str) -> polls.Ballot:
    ballot = await world.db.run(lambda c: polls.ballot(c, poll_id, user))
    assert ballot is not None
    return ballot


def reactions_on(world: World, message: str) -> set[str]:
    return set(world.state.reactions.get((CHANNEL, message), {}))


def writes(world: World) -> list[Recorded]:
    return [r for r in world.state.requests if r.method == "POST"]


def reminder_text(poll_id: int) -> str:
    link = render.jump_link(text.POLL_LINK_LABEL, URL, GUILD, CHANNEL, POLL_MSG)
    return text.POLL_REMINDER_DM.format(card="Lobby", link=link, n=poll_id, ts=CLOSES)


def reminders_to(world: World, user: str, poll_id: int) -> int:
    channel = world.state.dm_channels.get(user)
    if channel is None:
        return 0
    path = f"/api/v1/channels/{channel}/messages"
    expected = reminder_text(poll_id)
    return sum(1 for r in world.state.requests if r.method == "POST" and r.path == path
               and r.payload.get("content") == expected)


def events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, Any]]:
    return [getattr(r, FIELDS_ATTR, {}) for r in caplog.records if r.getMessage() == name]


# AC6.1: a poll closes on time, with its ranking and its ballots edited --------------------------------------

async def test_nothing_closes_one_second_before_the_close_time(world: World) -> None:
    poll_id = await store(world, starts=[ONE])
    await send_ballots(world, poll_id)

    world.clock.now = CLOSES - 1
    await world.jobs.run()

    assert (await stored(world, poll_id)).state == "open"


async def test_a_due_poll_closes_at_its_close_time_with_the_ranking_and_edited_ballots_and_sends_no_dm(
        world: World) -> None:
    poll_id = await store(world, starts=[ONE, TWO])
    await send_ballots(world, poll_id)
    await vote(world, poll_id, VOTERS[0], [await option_of(world, poll_id, ONE)])
    await vote(world, poll_id, VOTERS[1], [await option_of(world, poll_id, ONE)])
    await vote(world, poll_id, VOTERS[2], [await option_of(world, poll_id, TWO)])
    before = len(writes(world))

    world.clock.now = CLOSES
    await world.jobs.run()

    poll = await stored(world, poll_id)
    assert poll.state == "closed" and poll.outcome == "winner"
    assert poll.winner_option_id == await option_of(world, poll_id, ONE)
    poll_patches = [r for r in world.state.requests if r.method == "PATCH" and r.path == POLL_PATH]
    closed_view = str(poll_patches[-1].payload)
    assert text.POLL_STATE["closed"] in closed_view
    assert text.POLL_WINNER.format(letter="a", ts=ONE) in closed_view
    ballot_patches = [r for r in world.state.requests if r.method == "PATCH" and r.path != POLL_PATH]
    link = render.jump_link(text.POLL_LINK_LABEL, URL, GUILD, CHANNEL, POLL_MSG)
    assert len(ballot_patches) == len(VOTERS)
    closed_line = text.POLL_BALLOT_CLOSED.format(link=link)
    assert all(closed_line in r.payload["content"] for r in ballot_patches)
    assert len(writes(world)) == before           # no DM was opened or sent during the close


async def test_the_kick_is_called_once_when_polls_close(world: World) -> None:
    await store(world, starts=[ONE])
    world.clock.now = CLOSES
    await world.jobs.run()
    assert world.kicks == [1]


# AC6.2: the calendar, and the repeat button on an approval poll ----------------------------------------------

async def test_an_approval_winner_gets_the_calendar_and_the_repeat_button(world: World) -> None:
    poll_id = await store(world, starts=[ONE])
    await vote(world, poll_id, VOTERS[0], [await option_of(world, poll_id, ONE)])

    world.clock.now = CLOSES
    await world.jobs.run()

    assert reactions_on(world, POLL_MSG) == {POLL_SCHEDULE_EMOJI, POLL_RUNOFF_EMOJI}


async def test_a_runoff_winner_gets_the_calendar_only(world: World) -> None:
    parent = await store(world, starts=[ONE], users=VOTERS[:1])
    await world.db.run(lambda c: polls.close_if_open(c, parent, now=NOW, outcome="none", winner_option_id=None))
    world.state.messages[(CHANNEL, RUNOFF_MSG)] = {"embeds": []}
    runoff = await store(world, starts=[ONE, TWO], users=VOTERS[:1], kind="runoff", parent=parent,
                         message=RUNOFF_MSG)
    await vote(world, runoff, VOTERS[0], [await option_of(world, runoff, ONE)], ranked=True)

    world.clock.now = CLOSES
    await world.jobs.run()

    assert (await stored(world, runoff)).outcome == "winner"
    assert reactions_on(world, RUNOFF_MSG) == {POLL_SCHEDULE_EMOJI}


# AC6.3: options too soon, and no votes -----------------------------------------------------------------------

async def test_an_option_too_soon_at_close_is_left_out_so_the_next_usable_option_wins(world: World) -> None:
    poll_id = await store(world, starts=[TOO_SOON, ONE])
    await vote(world, poll_id, VOTERS[0], [await option_of(world, poll_id, TOO_SOON)])
    await vote(world, poll_id, VOTERS[1], [await option_of(world, poll_id, ONE)])

    world.clock.now = CLOSES
    await world.jobs.run()

    poll = await stored(world, poll_id)
    assert poll.outcome == "winner" and poll.winner_option_id == await option_of(world, poll_id, ONE)
    assert reactions_on(world, POLL_MSG) == {POLL_SCHEDULE_EMOJI, POLL_RUNOFF_EMOJI}


async def test_when_every_option_is_too_soon_the_close_has_no_winner_and_seeds_nothing(world: World) -> None:
    poll_id = await store(world, starts=[TOO_SOON])
    await vote(world, poll_id, VOTERS[0], [await option_of(world, poll_id, TOO_SOON)])

    world.clock.now = CLOSES
    await world.jobs.run()

    poll = await stored(world, poll_id)
    assert poll.state == "closed" and poll.outcome == "none"
    assert reactions_on(world, POLL_MSG) == set()


async def test_a_poll_with_no_votes_closes_with_no_winner_and_seeds_nothing(world: World) -> None:
    poll_id = await store(world, starts=[ONE])

    world.clock.now = CLOSES
    await world.jobs.run()

    poll = await stored(world, poll_id)
    assert poll.state == "closed" and poll.outcome == "none"
    assert reactions_on(world, POLL_MSG) == set()


# AC6.8: one halfway reminder to each unanswered recipient --------------------------------------------------

async def test_before_halfway_no_reminder_is_sent(world: World) -> None:
    poll_id = await store(world, starts=[ONE])
    await send_ballots(world, poll_id)

    world.clock.now = HALFWAY - 1
    await world.jobs.run()

    assert all(reminders_to(world, user, poll_id) == 0 for user in VOTERS)


async def test_each_unanswered_recipient_gets_one_reminder_at_halfway_and_a_second_run_sends_none(
        world: World) -> None:
    poll_id = await store(world, starts=[ONE])
    await send_ballots(world, poll_id)
    await vote(world, poll_id, VOTERS[0], [await option_of(world, poll_id, ONE)])

    world.clock.now = HALFWAY
    await world.jobs.run()
    assert reminders_to(world, VOTERS[0], poll_id) == 0       # answered
    assert reminders_to(world, VOTERS[1], poll_id) == 1
    assert reminders_to(world, VOTERS[2], poll_id) == 1

    await world.jobs.run()
    assert reminders_to(world, VOTERS[1], poll_id) == 1
    assert reminders_to(world, VOTERS[2], poll_id) == 1


async def test_a_recipient_whose_ballot_dm_failed_is_reminded_too(world: World) -> None:
    poll_id = await store(world, starts=[ONE])
    await send_ballots(world, poll_id, VOTERS[:2])
    await world.db.run(lambda c: polls.mark_dm_failed(c, poll_id, VOTERS[2]))

    world.clock.now = HALFWAY
    await world.jobs.run()

    assert reminders_to(world, VOTERS[2], poll_id) == 1


async def test_a_recipient_in_backoff_is_skipped_unmarked_and_reminded_once_the_backoff_ends(world: World) -> None:
    poll_id = await store(world, starts=[ONE])
    await send_ballots(world, poll_id)
    world.clock.now = HALFWAY
    world.backoff.failed(VOTERS[1])                # blocked until HALFWAY + BACKOFF_SECONDS, before the close

    await world.jobs.run()
    assert reminders_to(world, VOTERS[1], poll_id) == 0
    assert (await ballot_of(world, poll_id, VOTERS[1])).reminded is False
    assert reminders_to(world, VOTERS[2], poll_id) == 1

    world.clock.now = HALFWAY + BACKOFF_SECONDS
    await world.jobs.run()
    assert reminders_to(world, VOTERS[1], poll_id) == 1
    assert (await ballot_of(world, poll_id, VOTERS[1])).reminded is True


async def test_a_reminder_whose_text_cannot_be_built_is_not_marked_and_other_polls_still_remind(
        world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poll_jobs")
    second_card = await world.db.run(_second_card)
    first = await store(world, starts=[ONE])
    second = await store(world, starts=[ONE], card_id=second_card, message=SECOND_MSG)
    world.state.messages[(CHANNEL, SECOND_MSG)] = {"embeds": []}
    await send_ballots(world, first)
    await send_ballots(world, second)
    real_content = world.jobs._reminder_content

    def content_for(view: polls.PollView, jump: tuple[str, str, str] | None) -> str:
        if view.poll.id == first:
            raise RuntimeError("text failed")
        return real_content(view, jump)

    monkeypatch.setattr(world.jobs, "_reminder_content", content_for)
    world.clock.now = HALFWAY
    await world.jobs.run()

    for user in VOTERS:
        assert (await ballot_of(world, first, user)).reminded is False
        assert (await ballot_of(world, second, user)).reminded is True
    assert events(caplog, "poll_job_failed") == [{"kind": "remind", "poll_id": first, "reason": "RuntimeError"}]


async def test_with_reminders_off_no_reminder_is_sent(world: World) -> None:
    poll_id = await store(world, starts=[ONE])
    await send_ballots(world, poll_id)
    jobs = jobs_for(world, replace(world.config, poll_reminder=False))

    world.clock.now = HALFWAY
    await jobs.run()

    assert all(reminders_to(world, user, poll_id) == 0 for user in VOTERS)


async def test_the_reminder_log_names_the_poll_and_count_and_never_a_user(
        world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel.poll_jobs")
    poll_id = await store(world, starts=[ONE])
    await send_ballots(world, poll_id)

    world.clock.now = HALFWAY
    await world.jobs.run()

    [logged] = events(caplog, "poll_reminded")
    assert logged == {"poll_id": poll_id, "count": len(VOTERS)}
    for record in caplog.records:
        if record.name.startswith("fluxer_panel.poll_jobs"):
            assert not set(getattr(record, FIELDS_ATTR, {}).values()) & set(VOTERS)


# `due`: the poller's wait ends at the next halfway time or close -------------------------------------------

async def test_due_is_the_halfway_time_then_the_close_time_once_the_reminders_are_sent(world: World) -> None:
    poll_id = await store(world, starts=[ONE])
    await send_ballots(world, poll_id)

    world.clock.now = NOW
    await world.jobs.run()
    assert world.jobs.due == HALFWAY

    world.clock.now = HALFWAY
    await world.jobs.run()
    assert world.jobs.due == CLOSES

    world.clock.now = CLOSES
    await world.jobs.run()
    assert world.jobs.due is None


async def test_due_is_the_close_time_when_reminders_are_off(world: World) -> None:
    await store(world, starts=[ONE])
    jobs = jobs_for(world, replace(world.config, poll_reminder=False))

    world.clock.now = NOW
    await jobs.run()

    assert jobs.due == CLOSES


async def test_due_gives_no_wake_up_for_a_reminder_that_cannot_be_sent_yet(world: World) -> None:
    # A blocked recipient is still unreminded after halfway. That time has passed, so `due` must not point at it again:
    # it would make the wait zero on every tick. The close is the next time that counts.
    poll_id = await store(world, starts=[ONE])
    await send_ballots(world, poll_id, VOTERS[:1])
    world.clock.now = HALFWAY
    world.backoff.failed(VOTERS[0])

    await world.jobs.run()

    assert world.jobs.due == CLOSES


# A failing step is logged and does not stop the others --------------------------------------------------------

async def test_a_failing_reminder_step_does_not_stop_the_close_and_is_logged(
        world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poll_jobs")
    poll_id = await store(world, starts=[ONE])

    async def _remind() -> None:
        raise RuntimeError("reminders failed")

    monkeypatch.setattr(world.jobs, "_remind", _remind)
    world.clock.now = CLOSES
    await world.jobs.run()

    assert (await stored(world, poll_id)).state == "closed"
    [failed] = events(caplog, "poll_job_failed")
    assert failed == {"kind": "remind", "reason": "RuntimeError"}


async def test_one_failing_close_does_not_stop_the_other_due_polls(
        world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poll_jobs")
    second_card = await world.db.run(_second_card)
    first = await store(world, starts=[ONE])
    second = await store(world, starts=[ONE], card_id=second_card, message=SECOND_MSG)
    world.state.messages[(CHANNEL, SECOND_MSG)] = {"embeds": []}
    real_close = world.closer.close

    async def close_first_fails(poll_id: int, **kwargs: Any) -> Any:
        if poll_id == first:
            raise RuntimeError("close failed")
        return await real_close(poll_id, **kwargs)

    monkeypatch.setattr(world.closer, "close", close_first_fails)
    world.clock.now = CLOSES
    await world.jobs.run()

    assert (await stored(world, second)).state == "closed"
    assert (await stored(world, first)).state == "open"
    assert events(caplog, "poll_job_failed") == [{"kind": "close", "poll_id": first, "reason": "RuntimeError"}]


def _second_card(conn: Any) -> int:
    card_id = cards.create_card(conn, slug="second", title="Second", author_id=ADMIN, now=1)
    cards.set_state(conn, card_id, "live", 1)
    return card_id
