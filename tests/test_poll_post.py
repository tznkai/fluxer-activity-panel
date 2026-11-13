# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PollPost against the fake REST and a real database: the games-channel guard (AC9.1), empty mentions (AC9.2), the
debounced edits (AC4.4), ballot DMs, the closed-poll reactions and the log rule (AC9.3)."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, polls
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.games import BotIdentity
from fluxer_panel.interactions import GamesChannelRefused
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poll_post import POLL_RUNOFF_EMOJI, POLL_SCHEDULE_EMOJI, PollPost, _Dirty
from fluxer_panel.presses import POLL_EMOJI
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "poll-post-token"
ADMIN = "1001"
GUILD = "2001"
GAMES = "3001"
CHANNEL = "3010"
POLL_MSG = "5001"
U1 = "1002"
U2 = "1003"
BOT = "1900"
START_A = 1_800_003_600
START_B = 1_800_007_200
CLOSES = 1_800_086_400
POLL_PATH = f"/api/v1/channels/{CHANNEL}/messages/{POLL_MSG}"
POLL_KEY = f"PATCH {POLL_PATH}"
DM_PATH = "/api/v1/users/@me/channels"


def config_for() -> Config:
    return Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
                            "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES, "POLL_INTERVAL_SECONDS": "3600"})


@pytest.fixture
async def world(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[SimpleNamespace]:
    database = Database.open(tmp_path / "poll_post.db")
    state = FakeFluxerState(bot_user_id=BOT, next_message_id=9001)
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.sleep(0)

    try:
        async with aiohttp.ClientSession() as client:
            rest = FluxerREST(client, f"{base}/api", TOKEN)
            backoff = DmBackoff(3600)
            post = PollPost(db=database, rest=rest, config=config_for(), pacer=ChannelPacer(max_calls=1000),
                            identity=BotIdentity(user_id=BOT), backoff=backoff, edit_delay=0, sleep=record_sleep)
            yield SimpleNamespace(db=database, state=state, rest=rest, post=post, backoff=backoff, sleeps=sleeps)
    finally:
        database.close()


def _seed(conn, *, channel: str = CHANNEL, slug: str = "mc", message: str = POLL_MSG,
          users: tuple[str, ...] = (U1, U2)) -> tuple[int, int]:
    """A live card with one open approval poll over two offered times. Returns (card id, poll id)."""
    card = cards.create_card(conn, slug=slug, title="Lobby", author_id=ADMIN, now=1)
    cards.set_state(conn, card, "live", 1)
    poll_id = polls.create(conn, card_id=card, guild_id=GUILD, channel_id=channel, message_id=message,
                           starter_id=ADMIN, kind="approval", parent_poll_id=None, now=1, closes_at=CLOSES,
                           ballot_size=6, options=[(START_A, "offered"), (START_B, "offered")],
                           recipients=list(users))
    assert poll_id is not None
    return card, poll_id


async def seed(world: SimpleNamespace, **kwargs: Any) -> int:
    _, poll_id = await world.db.run(lambda c: _seed(c, **kwargs))
    return poll_id


async def option_ids(world: SimpleNamespace, poll_id: int) -> list[int]:
    return [option.id for option in await world.db.run(lambda c: polls.options(c, poll_id))]


async def vote(world: SimpleNamespace, poll_id: int, user_id: str, option_ids_: list[int]) -> None:
    assert await world.db.run(lambda c: polls.replace_picks_if_open(c, poll_id, user_id, option_ids_, ranked=False))


async def settle(world: SimpleNamespace) -> None:
    """Waits for every pending edit. A timeout fails the test, instead of letting a runaway flush hang the run."""
    await asyncio.wait_for(world.post.drain(), timeout=5)


def requests(world: SimpleNamespace, method: str, path: str | None = None) -> list[Any]:
    return [r for r in world.state.requests if r.method == method and (path is None or r.path == path)]


def patches_to_dm(world: SimpleNamespace, user_id: str) -> list[Any]:
    """The PATCHes to one user's ballot message. The fake gives each user's DM channel in `dm_channels`."""
    channel = world.state.dm_channels.get(user_id)
    return [r for r in world.state.requests if r.method == "PATCH" and channel is not None
            and r.path.startswith(f"/api/v1/channels/{channel}/")]


async def ballot_dm(world: SimpleNamespace, poll_id: int, user_id: str) -> tuple[str, str]:
    ballot = await world.db.run(lambda c: polls.ballot(c, poll_id, user_id))
    assert ballot is not None and ballot.dm_channel_id is not None and ballot.dm_message_id is not None
    return ballot.dm_channel_id, ballot.dm_message_id


def poll_payload(text: str = "Lobby") -> dict[str, Any]:
    return {"embeds": [{"title": text, "fields": []}], "allowed_mentions": {"parse": []}}


# delete_unstored: the best-effort delete of a provisional message whose row was not stored ----------------

async def test_delete_unstored_deletes_the_posted_message(world: SimpleNamespace) -> None:
    world.state.messages[(CHANNEL, POLL_MSG)] = poll_payload()
    await world.post.delete_unstored(CHANNEL, POLL_MSG)
    assert requests(world, "DELETE", POLL_PATH) != []
    assert (CHANNEL, POLL_MSG) in world.state.deleted


async def test_delete_unstored_refuses_the_games_channel_before_any_request_and_does_not_raise(
        world: SimpleNamespace, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    await world.post.delete_unstored(GAMES, POLL_MSG)
    assert world.state.requests == []
    assert [r.getMessage() for r in caplog.records].count("poll_discard_failed") == 1


async def test_delete_unstored_logs_a_failed_delete_and_never_raises(world: SimpleNamespace,
                                                                     caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    world.state.messages[(CHANNEL, POLL_MSG)] = poll_payload()
    world.state.overrides[f"DELETE {POLL_PATH}"] = Override(500, b'{"code":"X","message":"x"}')
    await world.post.delete_unstored(CHANNEL, POLL_MSG)
    [record] = [r for r in caplog.records if r.getMessage() == "poll_discard_failed"]
    assert getattr(record, FIELDS_ATTR) == {"channel_id": CHANNEL, "reason": "http_500"}


# AC9.1: the games channel is refused before any REST call --------------------------------------------

async def test_send_poll_refuses_the_games_channel_before_any_request(world: SimpleNamespace) -> None:
    with pytest.raises(GamesChannelRefused):
        await world.post.send_poll(GAMES, poll_payload())
    assert world.state.requests == []


async def test_a_poll_stored_in_the_games_channel_gets_no_flush_or_reaction_request(world: SimpleNamespace) -> None:
    poll_id = await seed(world, channel=GAMES)
    await world.db.run(lambda c: polls.close_if_open(c, poll_id, now=5, outcome="winner",
                                                     winner_option_id=(polls.options(c, poll_id)[0].id)))
    world.post.changed(poll_id)
    await settle(world)
    await world.post.seed_closed(poll_id)
    await world.post.unseed_closed(poll_id)
    assert world.state.requests == []


async def test_remove_reaction_refuses_a_poll_in_the_games_channel_before_any_request(world: SimpleNamespace) -> None:
    poll_id = await seed(world, channel=GAMES)
    await world.post.remove_reaction(poll_id, POLL_SCHEDULE_EMOJI, U1)
    assert world.state.requests == []


async def test_remove_reaction_removes_only_the_named_users_reaction(world: SimpleNamespace) -> None:
    poll_id = await seed(world)
    await world.post.remove_reaction(poll_id, POLL_SCHEDULE_EMOJI, U1)
    [delete] = requests(world, "DELETE")
    assert delete.path.endswith(f"/{U1}")
    assert delete.path.rsplit("/", 2)[-2] == POLL_SCHEDULE_EMOJI


# AC9.2: every poll message and ballot carries empty mentions ------------------------------------------

async def test_every_poll_send_and_edit_sends_empty_allowed_mentions(world: SimpleNamespace) -> None:
    await world.post.send_poll(CHANNEL, poll_payload())
    poll_id = await seed(world)
    await world.post.send_ballot(poll_id, U1)
    await world.post.send_ballot(poll_id, U2)
    await vote(world, poll_id, U1, [(await option_ids(world, poll_id))[0]])
    world.post.changed(poll_id)
    await settle(world)
    writes = [r for r in world.state.requests if r.method in ("POST", "PATCH") and "/messages" in r.path]
    assert len(writes) >= 4, "too few poll writes were recorded to check"
    for record in writes:
        assert record.payload is not None and record.payload.get("allowed_mentions") == {"parse": []}, record.path


# AC4.4: debounced edits ------------------------------------------------------------------------------

async def test_three_changes_before_the_task_runs_give_one_patch_per_message(world: SimpleNamespace) -> None:
    poll_id = await seed(world)
    await world.post.send_ballot(poll_id, U1)
    await world.post.send_ballot(poll_id, U2)
    options = await option_ids(world, poll_id)
    await vote(world, poll_id, U1, [options[0]])
    world.post.changed(poll_id)
    world.post.changed(poll_id)
    world.post.changed(poll_id)
    await settle(world)
    assert len(requests(world, "PATCH", POLL_PATH)) == 1
    for user in (U1, U2):
        assert len(patches_to_dm(world, user)) == 1
    assert world.sleeps == [0], "the burst waits one debounce window"


async def test_a_change_with_no_content_difference_gives_no_patch(world: SimpleNamespace) -> None:
    poll_id = await seed(world)
    options = await option_ids(world, poll_id)
    await vote(world, poll_id, U1, [options[0]])
    world.post.changed(poll_id)
    await settle(world)
    before = len(world.state.requests)
    world.post.changed(poll_id)
    await settle(world)
    assert len(world.state.requests) == before


async def test_changed_with_ballots_edits_only_that_ballot_and_the_poll_message(world: SimpleNamespace) -> None:
    poll_id = await seed(world)
    await world.post.send_ballot(poll_id, U1)
    await world.post.send_ballot(poll_id, U2)
    options = await option_ids(world, poll_id)
    await vote(world, poll_id, U1, [options[0]])
    await vote(world, poll_id, U2, [options[1]])
    world.post.changed(poll_id, ballots=[U1])
    await settle(world)
    assert len(requests(world, "PATCH", POLL_PATH)) == 1
    assert len(patches_to_dm(world, U1)) == 1
    assert patches_to_dm(world, U2) == []


# Ballots ---------------------------------------------------------------------------------------------

async def test_send_ballot_stores_the_dm_ids(world: SimpleNamespace) -> None:
    poll_id = await seed(world)
    assert await world.post.send_ballot(poll_id, U1) is True
    channel, message = await ballot_dm(world, poll_id, U1)
    assert channel.isdigit() and message.isdigit()
    sent = requests(world, "POST", f"/api/v1/channels/{channel}/messages")
    assert len(sent) == 1 and sent[0].payload["allowed_mentions"] == {"parse": []}


async def test_a_dm_failure_marks_the_ballot_failed(world: SimpleNamespace) -> None:
    poll_id = await seed(world)
    world.state.overrides[f"POST {DM_PATH}"] = Override(403, b'{"code": "CANNOT", "message": "no"}')
    assert await world.post.send_ballot(poll_id, U1) is False
    ballot = await world.db.run(lambda c: polls.ballot(c, poll_id, U1))
    assert ballot is not None and ballot.dm_failed is True and ballot.dm_channel_id is None


async def test_a_user_in_backoff_gets_no_request_and_is_marked_failed(world: SimpleNamespace) -> None:
    poll_id = await seed(world)
    world.backoff.failed(U1)
    assert await world.post.send_ballot(poll_id, U1) is False
    assert requests(world, "POST", DM_PATH) == []
    ballot = await world.db.run(lambda c: polls.ballot(c, poll_id, U1))
    assert ballot is not None and ballot.dm_failed is True


# 404 and the hook ------------------------------------------------------------------------------------

async def test_a_404_on_the_poll_message_calls_the_hook_once_and_the_hook_can_flush(world: SimpleNamespace) -> None:
    poll_id = await seed(world)
    world.state.overrides[f"PATCH {POLL_PATH}"] = Override(404, b'{"code": "UNKNOWN_MESSAGE", "message": "gone"}')
    calls: list[int] = []

    async def hook(gone_poll_id: int) -> None:
        calls.append(gone_poll_id)
        del world.state.overrides[f"PATCH {POLL_PATH}"]   # the second flush can then edit the message
        world.post.changed(gone_poll_id)                  # a close marks the poll dirty, then flushes it
        await world.post.flush(gone_poll_id)              # would deadlock if the lock were still held

    world.post.set_on_gone(hook)
    world.post.changed(poll_id)
    await asyncio.wait_for(world.post.drain(), timeout=5)
    assert calls == [poll_id]
    assert len(requests(world, "PATCH", POLL_PATH)) == 2


async def test_a_404_on_one_ballot_does_not_stop_the_others(world: SimpleNamespace) -> None:
    poll_id = await seed(world)
    await world.post.send_ballot(poll_id, U1)
    await world.post.send_ballot(poll_id, U2)
    channel, message = await ballot_dm(world, poll_id, U1)
    world.state.overrides[f"PATCH /api/v1/channels/{channel}/messages/{message}"] = Override(
        404, b'{"code": "UNKNOWN_MESSAGE", "message": "gone"}')
    options = await option_ids(world, poll_id)
    await vote(world, poll_id, U1, [options[0]])
    world.post.changed(poll_id)
    await settle(world)
    assert len(patches_to_dm(world, U1)) == 1
    assert len(patches_to_dm(world, U2)) == 1
    assert len(requests(world, "PATCH", POLL_PATH)) == 1


async def test_a_removed_poll_gets_ballot_edits_but_no_poll_message_patch(world: SimpleNamespace) -> None:
    poll_id = await seed(world)
    await world.post.send_ballot(poll_id, U1)
    await world.db.run(lambda c: polls.close_if_open(c, poll_id, now=5, outcome="removed", winner_option_id=None))
    world.post.changed(poll_id)
    await settle(world)
    assert requests(world, "PATCH", POLL_PATH) == []
    edits = patches_to_dm(world, U1)
    assert len(edits) == 1 and edits[0].payload["content"] == "This poll was removed."


# The closed poll's reactions -----------------------------------------------------------------------

async def _closed_with_winner(world: SimpleNamespace, *, kind: str = "approval") -> int:
    if kind == "approval":
        poll_id = await seed(world)
    else:
        poll_id = await world.db.run(lambda c: _runoff(c))
    options = await option_ids(world, poll_id)
    await world.db.run(lambda c: polls.close_if_open(c, poll_id, now=5, outcome="winner",
                                                     winner_option_id=options[0]))
    return poll_id


def _runoff(conn) -> int:
    """A closed parent approval poll and an open runoff on the same card, with one ranked ballot each."""
    card, parent = _seed(conn, message="5000")
    polls.close_if_open(conn, parent, now=2, outcome="none", winner_option_id=None)
    runoff = polls.create(conn, card_id=card, guild_id=GUILD, channel_id=CHANNEL, message_id=POLL_MSG,
                          starter_id=ADMIN, kind="runoff", parent_poll_id=parent, now=3, closes_at=CLOSES,
                          ballot_size=6, options=[(START_A, "offered"), (START_B, "offered")],
                          recipients=[U1, U2])
    assert runoff is not None
    ids = [option.id for option in polls.options(conn, runoff)]
    polls.replace_picks_if_open(conn, runoff, U1, ids, ranked=True)
    polls.replace_picks_if_open(conn, runoff, U2, list(reversed(ids)), ranked=True)
    return runoff


async def test_seed_closed_adds_the_calendar_and_the_runoff_for_an_approval_winner(world: SimpleNamespace) -> None:
    poll_id = await _closed_with_winner(world)
    await world.post.seed_closed(poll_id)
    emojis = [r.path.rsplit("/", 2)[-2] for r in requests(world, "PUT")]
    assert emojis == [POLL_SCHEDULE_EMOJI, POLL_RUNOFF_EMOJI]


async def test_seed_closed_adds_only_the_calendar_for_a_runoff(world: SimpleNamespace) -> None:
    poll_id = await _closed_with_winner(world, kind="runoff")
    await world.post.seed_closed(poll_id)
    emojis = [r.path.rsplit("/", 2)[-2] for r in requests(world, "PUT")]
    assert emojis == [POLL_SCHEDULE_EMOJI]


async def test_a_closed_poll_message_never_gets_the_poll_emoji(world: SimpleNamespace) -> None:
    """The poll message carries the calendar and the runoff repeat only; the poll emoji is a press on the card and its
    copies, never on a poll message."""
    poll_id = await _closed_with_winner(world)
    await world.post.seed_closed(poll_id)
    emojis = [r.path.rsplit("/", 2)[-2] for r in requests(world, "PUT")]
    assert POLL_EMOJI not in emojis
    assert emojis == [POLL_SCHEDULE_EMOJI, POLL_RUNOFF_EMOJI]


async def test_seed_closed_adds_nothing_without_a_winner(world: SimpleNamespace) -> None:
    poll_id = await seed(world)
    await world.db.run(lambda c: polls.close_if_open(c, poll_id, now=5, outcome="none", winner_option_id=None))
    await world.post.seed_closed(poll_id)
    assert requests(world, "PUT") == []


async def test_seed_closed_waits_the_pacer_before_each_add(world: SimpleNamespace) -> None:
    waits: list[str] = []

    class SpyPacer(ChannelPacer):
        async def wait(self, channel_id: str) -> None:
            waits.append(channel_id)
            await super().wait(channel_id)

    poll_id = await _closed_with_winner(world)
    post = PollPost(db=world.db, rest=world.rest, config=config_for(), pacer=SpyPacer(max_calls=1000),
                    identity=BotIdentity(user_id=BOT), edit_delay=0, sleep=lambda _s: asyncio.sleep(0))
    await post.seed_closed(poll_id)
    assert waits == [CHANNEL, CHANNEL]
    assert len(requests(world, "PUT")) == 2


async def test_unseed_closed_removes_only_the_bots_own_reactions(world: SimpleNamespace) -> None:
    poll_id = await _closed_with_winner(world)
    post = PollPost(db=world.db, rest=world.rest, config=config_for(), pacer=ChannelPacer(max_calls=1000),
                    edit_delay=0, sleep=lambda _s: asyncio.sleep(0), identity=BotIdentity(user_id=BOT))
    await post.unseed_closed(poll_id)
    deletes = requests(world, "DELETE")
    assert [r.path.rsplit("/", 2)[-2] for r in deletes] == [POLL_SCHEDULE_EMOJI, POLL_RUNOFF_EMOJI]
    assert all(r.path.endswith(f"/{BOT}") for r in deletes)


# AC9.3: logs carry no user id, name or typed text ----------------------------------------------------

async def test_logs_carry_no_user_id_name_or_text_through_a_failing_ballot_edit(world: SimpleNamespace,
                                                                                caplog: pytest.LogCaptureFixture) -> None:
    poll_id = await seed(world)
    await world.post.send_ballot(poll_id, U1)
    channel, message = await ballot_dm(world, poll_id, U1)
    world.state.overrides[f"PATCH /api/v1/channels/{channel}/messages/{message}"] = Override(
        500, b'{"code": "INTERNAL", "message": "boom"}')
    options = await option_ids(world, poll_id)
    await vote(world, poll_id, U1, [options[0]])
    caplog.set_level(logging.DEBUG)
    world.post.changed(poll_id)
    await settle(world)
    failed = [r for r in caplog.records if r.getMessage() == "poll_ballot_edit_failed"]
    assert failed, "the failing ballot edit was not logged"
    for record in caplog.records:
        fields = getattr(record, FIELDS_ATTR, {})
        assert "user_id" not in fields
        assert U1 not in [str(value) for value in fields.values()]
        assert U2 not in [str(value) for value in fields.values()]
        assert "Lobby" not in record.getMessage() and "Lobby" not in str(fields)
        assert "Time poll" not in record.getMessage()


# Shutdown and cleanup --------------------------------------------------------------------------------

async def test_aclose_lets_a_pending_flush_finish_within_its_timeout(world: SimpleNamespace) -> None:
    poll_id = await seed(world)
    world.state.delays[POLL_KEY] = 0.05
    world.post.changed(poll_id)
    await world.post.aclose(timeout=5)
    assert len(requests(world, "PATCH", POLL_PATH)) == 1


async def test_aclose_cancels_a_flush_blocked_past_its_timeout(world: SimpleNamespace) -> None:
    poll_id = await seed(world)
    gate = asyncio.Event()
    world.state.gates[POLL_KEY] = gate
    world.post.changed(poll_id)
    for _ in range(200):
        if POLL_KEY in world.state.gated:
            break
        await asyncio.sleep(0.01)
    assert POLL_KEY in world.state.gated
    task = world.post._tasks[poll_id]
    await world.post.aclose(timeout=0.05)
    assert task.cancelled(), "the flush blocked past the timeout was not cancelled"
    assert world.post._tasks == {}
    gate.set()   # releases the server side of the cancelled request so the fixture can close


async def test_a_closed_polls_cache_is_dropped_and_a_change_during_its_flush_is_not_lost(world: SimpleNamespace) -> None:
    poll_id = await _closed_with_winner(world)
    card_id = (await world.db.run(lambda c: polls.get(c, poll_id))).card_id
    gate = asyncio.Event()
    world.state.gates[POLL_KEY] = gate
    world.post.changed(poll_id)
    for _ in range(200):
        if POLL_KEY in world.state.gated:
            break
        await asyncio.sleep(0.01)
    # a change made while the first edit is in flight: the title, which the poll message shows
    await world.db.run(lambda c: cards.update_fields(c, card_id, 2, title="Renamed"))
    world.post.changed(poll_id)
    gate.set()
    await settle(world)
    edits = requests(world, "PATCH", POLL_PATH)
    assert len(edits) == 2
    assert "Renamed" in str(edits[-1].payload)
    assert world.post._last.get(poll_id) is None
    assert poll_id not in world.post._dirty and poll_id not in world.post._locks
    assert world.post._tasks == {}


async def test_a_closed_runoff_message_shows_its_rounds(world: SimpleNamespace) -> None:
    poll_id = await _closed_with_winner(world, kind="runoff")
    world.post.changed(poll_id)
    await settle(world)
    [edit] = requests(world, "PATCH", POLL_PATH)
    assert "Round 1:" in str(edit.payload)


# Wiring: the bot identity is required -----------------------------------------------------------------

async def test_identity_is_required_so_wiring_that_omits_it_fails_loudly(world: SimpleNamespace) -> None:
    with pytest.raises(TypeError):
        PollPost(db=world.db, rest=world.rest, config=config_for(), pacer=ChannelPacer(max_calls=1000))


async def test_unseed_without_a_bot_id_sends_no_delete_and_logs_the_skip(world: SimpleNamespace,
                                                                         caplog: pytest.LogCaptureFixture) -> None:
    poll_id = await _closed_with_winner(world)
    post = PollPost(db=world.db, rest=world.rest, config=config_for(), pacer=ChannelPacer(max_calls=1000),
                    identity=BotIdentity(), edit_delay=0, sleep=lambda _s: asyncio.sleep(0))
    caplog.set_level(logging.DEBUG)
    await post.unseed_closed(poll_id)
    assert requests(world, "DELETE") == []
    skipped = [r for r in caplog.records if r.getMessage() == "poll_unseed_skipped"]
    assert len(skipped) == 1
    assert getattr(skipped[0], FIELDS_ATTR) == {"poll_id": poll_id, "reason": "no_bot_id"}


# Background flush failures ----------------------------------------------------------------------------

async def test_a_flush_that_raises_is_logged_ends_its_task_and_keeps_its_marks(world: SimpleNamespace,
                                                                               monkeypatch: pytest.MonkeyPatch,
                                                                               caplog: pytest.LogCaptureFixture) -> None:
    poll_id = await seed(world)
    await world.post.send_ballot(poll_id, U1)
    options = await option_ids(world, poll_id)
    await vote(world, poll_id, U1, [options[0]])
    real_run = world.db.run
    failed_once: list[bool] = []

    async def fail_first_read(fn: Any) -> Any:
        if not failed_once:
            failed_once.append(True)
            raise sqlite3.OperationalError("disk I/O error")
        return await real_run(fn)

    monkeypatch.setattr(world.db, "run", fail_first_read)
    caplog.set_level(logging.DEBUG)
    world.post.changed(poll_id, ballots=[U1])
    task = world.post._tasks[poll_id]
    done, _pending = await asyncio.wait({task}, timeout=5)
    assert done == {task}
    assert not task.cancelled() and task.exception() is None, "the background task must end without raising"
    assert world.post._tasks == {}

    failed = [r for r in caplog.records if r.getMessage() == "poll_flush_failed"]
    assert len(failed) == 1
    assert getattr(failed[0], FIELDS_ATTR) == {"poll_id": poll_id, "reason": "OperationalError"}
    assert requests(world, "PATCH", POLL_PATH) == []

    world.post.changed(poll_id, ballots=[])   # a later change, with no ballot of its own
    await settle(world)
    assert len(requests(world, "PATCH", POLL_PATH)) == 1, "the restored poll-message mark was not flushed"
    assert len(patches_to_dm(world, U1)) == 1, "the restored ballot mark was not flushed"


async def test_a_failed_flush_of_every_ballot_restores_the_everyone_mark(world: SimpleNamespace,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """A flush that raises after changed(poll_id) with no ballots (every ballot) hands its everyone mark back, so the
    next change that names no ballot still edits every ballot's DM."""
    poll_id = await seed(world)
    await world.post.send_ballot(poll_id, U1)
    options = await option_ids(world, poll_id)
    await vote(world, poll_id, U1, [options[0]])
    real_run = world.db.run
    failed_once: list[bool] = []

    async def fail_first_read(fn: Any) -> Any:
        if not failed_once:
            failed_once.append(True)
            raise sqlite3.OperationalError("disk I/O error")
        return await real_run(fn)

    monkeypatch.setattr(world.db, "run", fail_first_read)
    world.post.changed(poll_id)                     # ballots=None: every ballot is marked
    await asyncio.wait_for(world.post.drain(), timeout=5)
    assert failed_once == [True]
    assert requests(world, "PATCH", POLL_PATH) == []
    assert patches_to_dm(world, U1) == []

    world.post.changed(poll_id, ballots=[])         # a later change naming no ballot of its own
    await settle(world)
    assert len(requests(world, "PATCH", POLL_PATH)) == 1
    assert len(patches_to_dm(world, U1)) == 1, "the restored everyone mark was not flushed"


async def test_a_raising_hook_is_logged_and_the_flush_completes(world: SimpleNamespace,
                                                                caplog: pytest.LogCaptureFixture) -> None:
    poll_id = await seed(world)
    await world.post.send_ballot(poll_id, U1)
    options = await option_ids(world, poll_id)
    await vote(world, poll_id, U1, [options[0]])
    world.state.overrides[POLL_KEY] = Override(404, b'{"code": "UNKNOWN_MESSAGE", "message": "gone"}')
    calls: list[int] = []

    async def broken_hook(gone_poll_id: int) -> None:
        calls.append(gone_poll_id)
        raise RuntimeError("hook broke")

    world.post.set_on_gone(broken_hook)
    caplog.set_level(logging.DEBUG)
    world.post.changed(poll_id)
    await settle(world)
    assert calls == [poll_id]
    logged = [r for r in caplog.records if r.getMessage() == "poll_gone_hook_failed"]
    assert len(logged) == 1
    assert getattr(logged[0], FIELDS_ATTR) == {"poll_id": poll_id, "reason": "RuntimeError"}
    assert len(patches_to_dm(world, U1)) == 1, "the flush went on to the ballots after the hook failed"


# Edit and seed failures -------------------------------------------------------------------------------

async def test_a_500_on_the_poll_message_is_logged_and_the_next_change_edits_it(world: SimpleNamespace,
                                                                                caplog: pytest.LogCaptureFixture) -> None:
    poll_id = await seed(world)
    world.state.overrides[POLL_KEY] = Override(500, b'{"code": "INTERNAL", "message": "boom"}')
    caplog.set_level(logging.DEBUG)
    world.post.changed(poll_id)
    await settle(world)
    failed = [r for r in caplog.records if r.getMessage() == "poll_edit_failed"]
    assert len(failed) == 1
    assert getattr(failed[0], FIELDS_ATTR) == {"poll_id": poll_id, "reason": "http_500"}
    assert (CHANNEL, POLL_MSG) not in world.post._last.get(poll_id, {})

    del world.state.overrides[POLL_KEY]
    world.post.changed(poll_id)
    await settle(world)
    assert len(requests(world, "PATCH", POLL_PATH)) == 2
    assert (CHANNEL, POLL_MSG) in world.post._last[poll_id], "the next change did not edit the message"


async def test_a_failing_seed_is_logged_and_later_seeds_stop(world: SimpleNamespace,
                                                             caplog: pytest.LogCaptureFixture) -> None:
    poll_id = await _closed_with_winner(world)
    world.state.overrides[f"PUT /api/v1/channels/{CHANNEL}/messages/{POLL_MSG}/reactions/"
                          f"{POLL_SCHEDULE_EMOJI}/@me"] = Override(500, b'{"code": "INTERNAL", "message": "boom"}')
    caplog.set_level(logging.DEBUG)
    await world.post.seed_closed(poll_id)
    assert [r.path.rsplit("/", 2)[-2] for r in requests(world, "PUT")] == [POLL_SCHEDULE_EMOJI]
    failed = [r for r in caplog.records if r.getMessage() == "poll_seed_failed"]
    assert len(failed) == 1
    assert getattr(failed[0], FIELDS_ATTR) == {"poll_id": poll_id, "reason": "http_500"}


@pytest.mark.parametrize("status", [404, 500])
async def test_an_unseed_failure_is_logged_only_when_it_is_not_a_404_and_the_next_emoji_is_still_removed(
        world: SimpleNamespace, caplog: pytest.LogCaptureFixture, status: int) -> None:
    poll_id = await _closed_with_winner(world)
    world.state.overrides[f"DELETE /api/v1/channels/{CHANNEL}/messages/{POLL_MSG}/reactions/"
                          f"{POLL_SCHEDULE_EMOJI}/{BOT}"] = Override(status, b'{"code": "X", "message": "x"}')
    caplog.set_level(logging.DEBUG)
    await world.post.unseed_closed(poll_id)
    assert [r.path.rsplit("/", 2)[-2] for r in requests(world, "DELETE")] == [POLL_SCHEDULE_EMOJI, POLL_RUNOFF_EMOJI]
    failed = [r for r in caplog.records if r.getMessage() == "poll_unseed_failed"]
    if status == 404:
        assert failed == []
    else:
        assert len(failed) == 1
        assert getattr(failed[0], FIELDS_ATTR) == {"poll_id": poll_id, "reason": f"http_{status}"}


# One flush per poll at a time ------------------------------------------------------------------------

def _mark(post: PollPost, poll_id: int) -> None:
    """Marks the poll message dirty without starting the poll's background task."""
    post._dirty.setdefault(poll_id, _Dirty()).message = True


async def _until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    async def spin() -> None:
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(spin(), timeout)


async def test_flushes_of_one_poll_share_one_lock_and_their_edits_never_overlap(
        world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """A flush holds the poll's PATCH while a second flush queues on the lock. When the first releases it with the
    queue still waiting, the lock must stay: dropping it would let a third flush run beside the queued one."""
    poll_id = await seed(world)
    post = world.post
    original = world.rest.edit_message
    held = asyncio.Event()
    release = asyncio.Event()
    inside = 0
    peak = 0
    holds = 0

    async def spy(channel_id: str, message_id: str, **kwargs: Any) -> Any:
        nonlocal inside, peak, holds
        inside += 1
        peak = max(peak, inside)
        try:
            result = await original(channel_id, message_id, **kwargs)
            if holds == 0:
                holds += 1
                held.set()
                await release.wait()     # the first flush keeps its lock here, after the edit
            return result
        finally:
            inside -= 1

    monkeypatch.setattr(world.rest, "edit_message", spy)
    _mark(post, poll_id)
    first = asyncio.create_task(post.flush(poll_id))
    await asyncio.wait_for(held.wait(), timeout=5)
    lock = post._locks[poll_id]

    queued = asyncio.create_task(post.flush(poll_id))       # no marks: it waits for the lock, then returns
    await _until(lambda: post._users.get(poll_id) == 2)

    release.set()
    await asyncio.sleep(0)                                   # the first flush finishes; the queued one has not run
    assert post._locks.get(poll_id) is lock, "the lock was dropped while a flush was still queued on it"

    _mark(post, poll_id)
    third = asyncio.create_task(post.flush(poll_id))
    await asyncio.wait_for(asyncio.gather(first, queued, third), timeout=5)
    assert peak == 1, "two flushes of one poll held their PATCH at the same time"
