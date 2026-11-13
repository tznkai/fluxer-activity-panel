# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PollCloser against a real database and the fake REST server: the ranking in the close transaction, the compare-and-set
close (a second close changes nothing and calls nothing), and the edits and seeding after a close. The debounce never
fires in these tests (its sleep waits forever), so every edit seen after a close was flushed by the close itself."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, polls, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.games import BotIdentity
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poll_close import Closed, PollCloser
from fluxer_panel.poll_post import POLL_RUNOFF_EMOJI, POLL_SCHEDULE_EMOJI, PollPost
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "SENTINEL-TOKEN-poll-close"
BOT = "1900"
ADMIN = "1001"
GUILD = "2001"
GAMES = "3001"
CHANNEL = "3010"
POLL_MSG = "5001"
RUNOFF_MSG = "5002"
NOW = 1_800_000_000
TOO_SOON = NOW + 1800        # under an hour away: never usable
ONE = NOW + 7200
TWO = NOW + 10800
THREE = NOW + 14400
CLOSES = NOW + 86400
VOTERS = ("1101", "1102", "1103", "1104", "1105", "1106")
POLL_PATH = f"/api/v1/channels/{CHANNEL}/messages/{POLL_MSG}"
DM_OPEN = "/api/v1/users/@me/channels"


def config_for() -> Config:
    return Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC",
                            "FLUXER_INSTANCE_URL": "https://fluxer.invalid", "ADMIN_USER_IDS": ADMIN,
                            "GAMES_CHANNEL_ID": GAMES, "POLL_INTERVAL_SECONDS": "3600"})


async def never(_seconds: float) -> None:
    """The debounce sleep: it never returns, so only an immediate flush can edit anything in these tests."""
    await asyncio.Event().wait()


def clock() -> float:
    return float(NOW)


@dataclass
class World:
    db: Database
    state: FakeFluxerState
    post: PollPost
    closer: PollCloser
    locks: CardLocks
    card_id: int


@pytest.fixture
async def world(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[World]:
    database = Database.open(tmp_path / "poll_close.db")
    state = FakeFluxerState(bot_user_id=BOT, next_message_id=9001)
    state.messages[(CHANNEL, POLL_MSG)] = {"embeds": []}
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    config = config_for()
    post: PollPost | None = None
    try:
        async with aiohttp.ClientSession() as client:
            rest = FluxerREST(client, f"{base}/api", TOKEN)
            backoff = DmBackoff(config.dm_backoff_seconds, clock=clock)
            post = PollPost(db=database, rest=rest, config=config, pacer=ChannelPacer(max_calls=1000),
                            identity=BotIdentity(user_id=BOT), backoff=backoff, edit_delay=0, sleep=never)
            locks = CardLocks()
            closer = PollCloser(db=database, locks=locks, poll_post=post, clock=clock)
            card_id = await database.run(lambda c: _card(c))
            yield World(db=database, state=state, post=post, closer=closer, locks=locks, card_id=card_id)
    finally:
        if post is not None:
            await post.aclose(timeout=0.05)
        database.close()


def _card(conn: Any) -> int:
    card_id = cards.create_card(conn, slug="mc", title="Lobby", author_id=ADMIN, now=1)
    cards.set_state(conn, card_id, "live", 1)
    return card_id


async def store(world: World, *, starts: list[int], users: tuple[str, ...] = VOTERS, kind: str = "approval",
                parent: int | None = None, message: str = POLL_MSG) -> int:
    def tx(conn: Any) -> int:
        poll_id = polls.create(conn, card_id=world.card_id, guild_id=GUILD, channel_id=CHANNEL, message_id=message,
                               starter_id=ADMIN, kind=kind, parent_poll_id=parent, now=NOW, closes_at=CLOSES,
                               ballot_size=6, options=[(s, "offered") for s in starts], recipients=list(users))
        assert poll_id is not None
        return poll_id
    return await world.db.run(tx)


async def option_of(world: World, poll_id: int, starts_at: int) -> int:
    options = await world.db.run(lambda c: polls.options(c, poll_id))
    return next(o.id for o in options if o.starts_at == starts_at)


async def vote(world: World, poll_id: int, user: str, option_ids: list[int], *, ranked: bool = False) -> None:
    assert await world.db.run(lambda c: polls.replace_picks_if_open(c, poll_id, user, option_ids, ranked=ranked))


async def send_ballots(world: World, poll_id: int, users: tuple[str, ...] = VOTERS) -> None:
    for user in users:
        assert await world.post.send_ballot(poll_id, user)


async def stored(world: World, poll_id: int) -> polls.Poll:
    poll = await world.db.run(lambda c: polls.get(c, poll_id))
    assert poll is not None
    return poll


def reactions_on(world: World, message: str) -> set[str]:
    return set(world.state.reactions.get((CHANNEL, message), {}))


# The ranking: AC5.5's close path ----------------------------------------------------------------------------

async def test_the_most_voted_usable_option_wins_and_an_option_too_soon_is_left_out_even_when_it_leads(world) -> None:
    poll_id = await store(world, starts=[TOO_SOON, ONE, TWO])
    too_soon, two = await option_of(world, poll_id, TOO_SOON), await option_of(world, poll_id, TWO)
    for user in VOTERS[:3]:
        await vote(world, poll_id, user, [too_soon])
    for user in VOTERS[3:5]:
        await vote(world, poll_id, user, [two])
    await vote(world, poll_id, VOTERS[5], [await option_of(world, poll_id, ONE)])

    closed = await world.closer.close(poll_id)

    assert closed == Closed(poll_id=poll_id, card_id=world.card_id, outcome="winner")
    poll = await stored(world, poll_id)
    assert poll.state == "closed" and poll.outcome == "winner" and poll.winner_option_id == two


async def test_a_tie_on_votes_goes_to_the_soonest_option(world) -> None:
    poll_id = await store(world, starts=[TWO, ONE], users=VOTERS[:2])
    await vote(world, poll_id, VOTERS[0], [await option_of(world, poll_id, TWO)])
    await vote(world, poll_id, VOTERS[1], [await option_of(world, poll_id, ONE)])

    await world.closer.close(poll_id)

    poll = await stored(world, poll_id)
    assert poll.winner_option_id == await option_of(world, poll_id, ONE)


async def test_no_votes_closes_with_no_winner_and_seeds_nothing(world) -> None:
    poll_id = await store(world, starts=[ONE, TWO])

    closed = await world.closer.close(poll_id)

    assert closed is not None and closed.outcome == "none"
    poll = await stored(world, poll_id)
    assert poll.outcome == "none" and poll.winner_option_id is None
    assert reactions_on(world, POLL_MSG) == set()


async def test_only_options_too_soon_are_voted_for_and_the_close_has_no_winner(world) -> None:
    poll_id = await store(world, starts=[TOO_SOON], users=VOTERS[:1])
    await vote(world, poll_id, VOTERS[0], [await option_of(world, poll_id, TOO_SOON)])

    closed = await world.closer.close(poll_id)

    assert closed is not None and closed.outcome == "none"
    assert (await stored(world, poll_id)).winner_option_id is None


async def test_a_runoff_closes_on_its_instant_runoff_winner_not_the_first_choice_tie(world) -> None:
    parent = await store(world, starts=[ONE], users=VOTERS[:1])
    await world.db.run(lambda c: polls.close_if_open(c, parent, now=NOW, outcome="none", winner_option_id=None))
    users = VOTERS[:5]
    runoff = await store(world, starts=[ONE, TWO, THREE], users=users, kind="runoff", parent=parent,
                         message=RUNOFF_MSG)
    a, b, c = [await option_of(world, runoff, s) for s in (ONE, TWO, THREE)]
    # First choices: A 2, B 1, C 2. B is dropped, and its ballot moves to C, which then has 3 of 5.
    await vote(world, runoff, users[0], [a], ranked=True)
    await vote(world, runoff, users[1], [a], ranked=True)
    await vote(world, runoff, users[2], [b, c], ranked=True)
    await vote(world, runoff, users[3], [c], ranked=True)
    await vote(world, runoff, users[4], [c], ranked=True)

    closed = await world.closer.close(runoff)

    assert closed is not None and closed.outcome == "winner"
    assert (await stored(world, runoff)).winner_option_id == c


# The edits and the seeding after a close: AC5.5, AC6.1 ----------------------------------------------------

async def test_a_close_edits_the_poll_message_and_every_ballot_at_once_and_sends_no_new_dm(world) -> None:
    poll_id = await store(world, starts=[ONE, TWO])
    await send_ballots(world, poll_id)
    await vote(world, poll_id, VOTERS[0], [await option_of(world, poll_id, TWO)])
    mark = len(world.state.requests)

    closed = await world.closer.close(poll_id)     # the debounce never fires, so this must flush

    assert closed is not None
    after = world.state.requests[mark:]
    assert [r for r in after if r.method == "PATCH" and r.path == POLL_PATH]
    for user in VOTERS:
        dm = world.state.dm_channels[user]
        assert [r for r in after if r.method == "PATCH" and r.path.startswith(f"/api/v1/channels/{dm}/")]
    assert not [r for r in after if r.method == "POST"], "a close sends no new message and opens no DM channel"
    assert not [r for r in after if r.path == DM_OPEN]
    closed_text = text.POLL_BALLOT_CLOSED.split("{")[0]
    dm = world.state.dm_channels[VOTERS[0]]
    ballot = await world.db.run(lambda c: polls.ballot(c, poll_id, VOTERS[0]))
    assert ballot is not None and ballot.dm_message_id is not None
    assert closed_text in world.state.messages[(dm, ballot.dm_message_id)]["content"]


async def test_an_approval_winner_seeds_the_calendar_and_the_repeat_button(world) -> None:
    poll_id = await store(world, starts=[ONE, TWO], users=VOTERS[:1])
    await vote(world, poll_id, VOTERS[0], [await option_of(world, poll_id, ONE)])

    await world.closer.close(poll_id)

    assert reactions_on(world, POLL_MSG) == {POLL_SCHEDULE_EMOJI, POLL_RUNOFF_EMOJI}


async def test_a_runoff_winner_seeds_only_the_calendar(world) -> None:
    parent = await store(world, starts=[ONE], users=VOTERS[:1])
    await world.db.run(lambda c: polls.close_if_open(c, parent, now=NOW, outcome="none", winner_option_id=None))
    runoff = await store(world, starts=[ONE, TWO], users=VOTERS[:1], kind="runoff", parent=parent,
                         message=RUNOFF_MSG)
    await vote(world, runoff, VOTERS[0], [await option_of(world, runoff, TWO)], ranked=True)

    await world.closer.close(runoff)

    assert reactions_on(world, RUNOFF_MSG) == {POLL_SCHEDULE_EMOJI}


async def test_an_outcome_override_of_none_closes_without_ranking_and_seeds_nothing(world) -> None:
    poll_id = await store(world, starts=[ONE, TWO], users=VOTERS[:1])
    await vote(world, poll_id, VOTERS[0], [await option_of(world, poll_id, TWO)])

    closed = await world.closer.close(poll_id, outcome_override="none")

    assert closed == Closed(poll_id=poll_id, card_id=world.card_id, outcome="none")
    poll = await stored(world, poll_id)
    assert poll.outcome == "none" and poll.winner_option_id is None
    assert reactions_on(world, POLL_MSG) == set()


# AC5.8: the close happens once ------------------------------------------------------------------------------

async def test_a_second_close_returns_none_and_makes_no_rest_call(world) -> None:
    poll_id = await store(world, starts=[ONE, TWO], users=VOTERS[:1])
    assert await world.closer.close(poll_id) is not None
    mark = len(world.state.requests)

    assert await world.closer.close(poll_id) is None

    assert len(world.state.requests) == mark


class RaceLocks:
    """A lock registry whose lock first runs `before`, which is a close that wins the race, then takes the real lock.
    `close()` has already read the poll by then, so only the transaction under the lock can refuse it."""

    def __init__(self, before: Callable[[], Awaitable[None]]) -> None:
        self._inner = CardLocks()
        self._before = before

    def get(self, card_id: int) -> AbstractAsyncContextManager[None]:
        return _gated(self._inner.get(card_id), self._before)


@asynccontextmanager
async def _gated(inner: Any, before: Callable[[], Awaitable[None]]) -> AsyncIterator[None]:
    await before()
    async with inner:
        yield


async def test_a_close_that_loses_the_race_to_another_close_returns_none_and_edits_nothing(world) -> None:
    poll_id = await store(world, starts=[ONE, TWO], users=VOTERS[:1])
    await vote(world, poll_id, VOTERS[0], [await option_of(world, poll_id, ONE)])

    async def another_close_wins() -> None:
        await world.db.run(lambda c: polls.close_if_open(c, poll_id, now=NOW, outcome="none", winner_option_id=None))

    closer = PollCloser(db=world.db, locks=RaceLocks(another_close_wins), poll_post=world.post, clock=clock)
    mark = len(world.state.requests)

    assert await closer.close(poll_id) is None

    assert len(world.state.requests) == mark
    assert (await stored(world, poll_id)).outcome == "none"


async def test_a_missing_poll_returns_none_and_makes_no_rest_call(world) -> None:
    mark = len(world.state.requests)

    assert await world.closer.close(424242) is None

    assert len(world.state.requests) == mark


async def test_close_locked_closes_under_a_lock_its_caller_holds(world) -> None:
    poll_id = await store(world, starts=[ONE, TWO], users=VOTERS[:1])
    await vote(world, poll_id, VOTERS[0], [await option_of(world, poll_id, ONE)])

    async with world.locks.get(world.card_id):
        closed = await world.closer.close_locked(poll_id)

    assert closed is not None and closed.outcome == "winner"
    assert (await stored(world, poll_id)).state == "closed"


# The log rule: ids and the outcome only --------------------------------------------------------------------

async def test_a_close_logs_the_poll_card_and_outcome_and_no_user_or_text(world, caplog) -> None:
    caplog.set_level(logging.INFO)
    poll_id = await store(world, starts=[ONE, TWO], users=VOTERS[:1])
    await vote(world, poll_id, VOTERS[0], [await option_of(world, poll_id, ONE)])

    await world.closer.close(poll_id)

    records = [r for r in caplog.records if r.getMessage() == "poll_closed"]
    assert len(records) == 1
    fields = getattr(records[0], FIELDS_ATTR)
    assert fields == {"poll_id": poll_id, "card_id": world.card_id, "outcome": "winner"}


# AC8.3: a 404 on the poll message closes the poll as removed --------------------------------------------------

async def test_a_404_on_the_poll_message_during_a_vote_refresh_closes_the_poll_as_removed(world) -> None:
    poll_id = await store(world, starts=[ONE, TWO])
    await send_ballots(world, poll_id)
    world.post.set_on_gone(world.closer.on_message_gone)     # as __main__ wires it
    world.state.overrides[f"PATCH {POLL_PATH}"] = Override(404, b'{"code": "X", "message": "gone"}')
    await vote(world, poll_id, VOTERS[0], [await option_of(world, poll_id, TWO)])
    mark = len(world.state.requests)

    world.post.changed(poll_id)                               # a vote's refresh, flushed now
    await asyncio.wait_for(world.post.flush(poll_id), 5)

    poll = await stored(world, poll_id)
    assert poll.state == "closed" and poll.outcome == "removed" and poll.winner_option_id is None
    after = world.state.requests[mark:]
    assert [r.method for r in after if r.path == POLL_PATH] == ["PATCH"], "the refused edit is the last to the poll"
    assert reactions_on(world, POLL_MSG) == set()
    removed_text = text.POLL_BALLOT_REMOVED
    for user in VOTERS:
        ballot = await world.db.run(lambda c, u=user: polls.ballot(c, poll_id, u))
        assert ballot is not None and ballot.dm_message_id is not None
        dm = world.state.dm_channels[user]
        assert world.state.messages[(dm, ballot.dm_message_id)]["content"] == removed_text


async def test_a_404_on_a_poll_that_is_already_closed_changes_nothing(world) -> None:
    poll_id = await store(world, starts=[ONE, TWO], users=VOTERS[:1])
    assert await world.closer.close(poll_id) is not None      # no votes: closed with no winner
    mark = len(world.state.requests)

    await world.closer.on_message_gone(poll_id)

    assert len(world.state.requests) == mark
    assert (await stored(world, poll_id)).outcome == "none"
