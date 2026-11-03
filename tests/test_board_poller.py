# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The summary board in the poller, against a real SQLite file and the fake Fluxer REST: the post above the cards, the
edits, the admin DM once per new message, the hand-delete and the failure paths."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.events import MessageDelete
from fluxer_client.rest import FluxerREST
from fluxer_panel import board, cards, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.poller import Poller
from fluxer_panel.sources.base import PlayerCounts, ProbeDetails, ProbeResult
from fluxer_panel.sources.registry import Sources
from tests.fakes import FakeFluxerState, Override, Recorded, make_rest_app

TOKEN = "SENTINEL-TOKEN-board-7c1e"
INSTANCE = "https://fluxer.invalid"
GUILD = "1001"
GAMES = "3001"
OTHER_GAMES = "3002"
AUTHOR = "1001"
GAMES_MESSAGES = f"/api/v1/channels/{GAMES}/messages"
OTHER_MESSAGES = f"/api/v1/channels/{OTHER_GAMES}/messages"
BOARD = "5001"                        # the first message the fake creates, which the first board post takes
OPEN_DM = "/api/v1/users/@me/channels"
DM_CHANNEL_ONE = "/api/v1/channels/9000/messages"
DM_CHANNEL_TWO = "/api/v1/channels/9001/messages"
GAMES_CHANNEL = {"id": GAMES, "name": "games", "type": 0, "guild_id": GUILD}
SENTINEL_TITLE = "SENTINEL-TITLE-4d2a"
SENTINEL_HOST = "sentinel-host.invalid"
RUNNING = ProbeResult(PlayerCounts(1, 4), ProbeDetails())


def link_to(label: str, message: str, *, guild: str = GUILD, channel: str = GAMES) -> str:
    return f"[{label}]({INSTANCE}/channels/{guild}/{channel}/{message})"


def env(**overrides: str) -> dict[str, str]:
    values = {
        "FLUXER_TOKEN": TOKEN,
        "FLUXER_INSTANCE_URL": INSTANCE,
        "ADMIN_USER_IDS": "1001,1002",
        "GAMES_CHANNEL_ID": GAMES,
        "POLL_INTERVAL_SECONDS": "3600",
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class StubProbe:
    """A probe plugin double behind the Probe contract. It answers with RUNNING, and it can run `on_call` first, so a
    test can change the store while a tick is evaluating."""

    reply: ProbeResult = RUNNING
    on_call: Callable[[], Any] | None = None
    calls: list[str] = field(default_factory=list)

    async def probe(self, host: str, port: int, timeout: float) -> ProbeResult:
        self.calls.append(host)
        if self.on_call is not None:
            await self.on_call()
        return self.reply


@dataclass
class World:
    db: Database
    rest: FluxerREST
    fluxer: FakeFluxerState
    probe: StubProbe
    clock: FakeClock

    def poller(self, **overrides: str | None) -> Poller:
        config = Config.from_env(env(**overrides))
        sources = Sources(managers={}, probes={"stub": self.probe})
        return Poller(db=self.db, rest=self.rest, sources=sources, config=config, locks=CardLocks(), clock=self.clock)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def world(db: Database, session: aiohttp.ClientSession, aiohttp_server: Any) -> AsyncIterator[World]:
    fluxer = FakeFluxerState()
    fluxer.channels[GAMES] = dict(GAMES_CHANNEL)
    server = await aiohttp_server(make_rest_app(fluxer))
    rest = FluxerREST(session, f"{str(server.make_url('/')).rstrip('/')}/api", TOKEN)
    yield World(db=db, rest=rest, fluxer=fluxer, probe=StubProbe(), clock=FakeClock())


@pytest.fixture
def captured(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


async def seed(db: Database, slug: str, *, title: str = "Card", sort: int = 0, probe: bool = False,
               state: str = "live") -> int:
    def tx(conn: Any) -> int:
        card_id = cards.create_card(conn, slug=slug, title=title, author_id=AUTHOR, now=0)
        if sort:
            cards.update_fields(conn, card_id, 0, sort_order=sort)
        if probe:
            cards.set_probe(conn, card_id, cards.ProbeRef("stub", "127.0.0.1", 25565))
        cards.set_state(conn, card_id, state, 0)
        return card_id

    return await db.run(tx)


async def board_row(db: Database) -> board.BoardState:
    return await db.run(board.get)


async def card_message(db: Database, card_id: int) -> str:
    """The card's stored message id. Direct messages take fake message ids too, so tests read the id back."""
    card = await db.run(lambda c: cards.get_by_id(c, card_id))
    assert card is not None and card.message_id is not None
    return card.message_id


def posted(state: FakeFluxerState, path: str = GAMES_MESSAGES) -> list[Recorded]:
    return [r for r in state.requests if r.method == "POST" and r.path == path]


def board_posts(state: FakeFluxerState) -> list[Recorded]:
    return [r for r in posted(state) if r.payload["embeds"][0].get("title") == text.BOARD_TITLE]


def card_posts(state: FakeFluxerState) -> list[Recorded]:
    return [r for r in posted(state) if r.payload["embeds"][0].get("title") != text.BOARD_TITLE]


def patches_to(state: FakeFluxerState, message: str) -> list[Recorded]:
    return [r for r in state.requests if r.method == "PATCH" and r.path == f"{GAMES_MESSAGES}/{message}"]


def dm_sends(state: FakeFluxerState) -> list[Recorded]:
    return [r for r in state.requests if r.method == "POST" and r.path in (DM_CHANNEL_ONE, DM_CHANNEL_TWO)]


def cells(payload: dict[str, Any]) -> list[tuple[str, str]]:
    return [(f["name"], f["value"]) for embed in payload["embeds"] for f in embed.get("fields", [])]


def assert_board_writes_safe(state: FakeFluxerState) -> None:
    writes = [r for r in state.requests if r.method in ("POST", "PATCH") and "/messages" in r.path]
    assert writes
    for request in writes:
        assert request.payload["allowed_mentions"] == {"parse": []}


def records(captured: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [r for r in captured.records if r.getMessage() == event]


# The position above the cards and the first edit ---------------------------------------------------

async def test_the_board_is_posted_before_the_cards_it_lists_on_a_fresh_start(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)

    await world.poller().tick()

    order = [r.payload["embeds"][0].get("title") for r in posted(world.fluxer)]
    assert order == [text.BOARD_TITLE, "Alpha"]
    assert_board_writes_safe(world.fluxer)


async def test_the_first_board_post_has_plain_status_and_the_next_edit_adds_the_jump_link(world: World,
                                                                                           db: Database) -> None:
    card_id = await seed(db, "alpha", title="Alpha", probe=True)

    await world.poller().tick()

    [first] = board_posts(world.fluxer)
    assert cells(first.payload) == [("Alpha", "Ready · 1 / 4")]
    [edit] = patches_to(world.fluxer, BOARD)
    assert cells(edit.payload) == [("Alpha", link_to("Ready · 1 / 4", await card_message(db, card_id)))]
    assert (await board_row(db)).message_id == BOARD


async def test_a_card_with_no_message_yet_is_not_linked_on_the_first_post_only(world: World, db: Database) -> None:
    alpha = await seed(db, "alpha", title="Alpha", probe=True)
    beta = await seed(db, "beta", title="Beta", probe=True, sort=1)

    await world.poller().tick()

    [first] = board_posts(world.fluxer)
    assert cells(first.payload) == [("Alpha", "Ready · 1 / 4"), ("Beta", "Ready · 1 / 4")]
    [edit] = patches_to(world.fluxer, BOARD)
    assert cells(edit.payload) == [("Alpha", link_to("Ready · 1 / 4", await card_message(db, alpha))),
                                   ("Beta", link_to("Ready · 1 / 4", await card_message(db, beta)))]


async def test_a_card_with_no_manager_and_no_probe_shows_the_dash(world: World, db: Database) -> None:
    card_id = await seed(db, "alpha", title="Alpha")

    await world.poller().tick()

    [edit] = patches_to(world.fluxer, BOARD)
    assert cells(edit.payload) == [("Alpha", link_to(text.BOARD_NO_STATUS, await card_message(db, card_id)))]


async def test_cards_are_listed_in_sort_order_then_id(world: World, db: Database) -> None:
    await seed(db, "late", title="Late", probe=True, sort=5)
    await seed(db, "early", title="Early", probe=True, sort=1)

    await world.poller().tick()

    [first] = board_posts(world.fluxer)
    assert [name for name, _ in cells(first.payload)] == ["Early", "Late"]


async def test_zero_live_cards_still_post_the_board_with_the_empty_description(world: World, db: Database) -> None:
    await world.poller().tick()

    [only] = board_posts(world.fluxer)
    assert only.payload["embeds"][0]["description"] == text.BOARD_EMPTY
    assert len(world.fluxer.requests_to(GAMES_MESSAGES)) == 1


async def test_nothing_is_posted_to_the_games_channel_when_it_is_not_configured(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)

    await world.poller(GAMES_CHANNEL_ID=None).tick()

    assert posted(world.fluxer) == []
    assert patches_to(world.fluxer, BOARD) == []
    assert await board_row(db) == board.BoardState(None, None, None)


# Steady state -----------------------------------------------------------------------------------

async def test_an_unchanged_board_is_not_edited_or_reposted(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.tick()
    world.fluxer.requests.clear()

    await poller.tick()

    assert [r for r in world.fluxer.requests if r.method in ("POST", "PATCH") and "/messages" in r.path] == []


async def test_a_status_change_edits_the_board_once_and_does_not_repost_it(world: World, db: Database) -> None:
    card_id = await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.tick()
    world.fluxer.requests.clear()
    world.probe.reply = ProbeResult(PlayerCounts(2, 4), ProbeDetails())

    await poller.tick()

    assert board_posts(world.fluxer) == []
    [edit] = patches_to(world.fluxer, BOARD)
    assert cells(edit.payload) == [("Alpha", link_to("Ready · 2 / 4", await card_message(db, card_id)))]


async def test_a_title_change_edits_the_board(world: World, db: Database) -> None:
    card_id = await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.tick()
    world.fluxer.requests.clear()
    await db.run(lambda c: cards.update_fields(c, card_id, 0, title="Omega"))

    await poller.tick()

    [edit] = patches_to(world.fluxer, BOARD)
    assert cells(edit.payload)[0][0] == "Omega"


# The admin DM, once per new message ---------------------------------------------------------------

async def test_each_admin_gets_the_pin_request_once_for_the_new_board_message(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()

    await poller.tick()
    await poller.tick()

    sends = dm_sends(world.fluxer)
    assert [r.payload["content"] for r in sends] == [text.BOARD_PIN_ALERT, text.BOARD_PIN_ALERT]
    assert all(r.payload["allowed_mentions"] == {"parse": []} for r in sends)


async def test_a_reposted_board_sends_the_pin_request_again_for_the_new_message(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.tick()
    await poller.on_message_delete(MessageDelete(BOARD, GAMES, None))

    await poller.tick()

    assert len(board_posts(world.fluxer)) == 2
    assert len(dm_sends(world.fluxer)) == 4


async def test_a_failed_admin_dm_is_logged_and_the_board_is_still_stored(world: World, db: Database,
                                                                         captured: pytest.LogCaptureFixture) -> None:
    world.fluxer.overrides[f"POST {OPEN_DM}"] = Override(500, b'{"code": "SENTINEL"}')

    await world.poller().tick()

    assert (await board_row(db)).message_id == BOARD
    failed = records(captured, "board_alert_failed")
    assert [r.panel_fields for r in failed] == [{"user_id": "1001", "reason": "FluxerHTTPError"},
                                                 {"user_id": "1002", "reason": "FluxerHTTPError"}]


# Hand deletion and the startup check ----------------------------------------------------------------

async def test_a_deleted_board_message_is_forgotten_and_reposted_without_waiting_for_the_interval(
        world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.tick()
    task = asyncio.create_task(poller.run())
    try:
        await _wait_until(lambda: len(board_posts(world.fluxer)) == 1)
        await poller.on_message_delete(MessageDelete(BOARD, GAMES, None))
        await _wait_until(lambda: len(board_posts(world.fluxer)) == 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_a_delete_of_another_message_leaves_the_board_alone(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.tick()

    await poller.on_message_delete(MessageDelete("4000", GAMES, None))

    assert (await board_row(db)).message_id == BOARD
    assert len(board_posts(world.fluxer)) == 1


async def test_a_board_message_that_is_gone_at_startup_is_reposted(world: World, db: Database) -> None:
    await db.run(lambda c: board.set_message(c, GAMES, "7777", "a" * 64))
    poller = world.poller()

    await poller.reconcile()
    assert await board_row(db) == board.BoardState(None, None, None)
    await poller.tick()

    assert len(board_posts(world.fluxer)) == 1


async def test_a_board_message_that_still_exists_at_startup_is_kept(world: World, db: Database) -> None:
    await db.run(lambda c: board.set_message(c, GAMES, "7777", "a" * 64))
    world.fluxer.messages[(GAMES, "7777")] = {"embeds": []}

    await world.poller().reconcile()

    assert (await board_row(db)).message_id == "7777"


async def test_a_failed_board_lookup_at_startup_keeps_the_stored_message(world: World, db: Database,
                                                                         captured: pytest.LogCaptureFixture) -> None:
    await db.run(lambda c: board.set_message(c, GAMES, "7777", "a" * 64))
    world.fluxer.overrides[f"GET {GAMES_MESSAGES}/7777"] = Override(500, b'{"code": "SENTINEL"}')

    await world.poller().reconcile()

    assert (await board_row(db)).message_id == "7777"
    [record] = records(captured, "board_reconcile_failed")
    assert record.panel_fields == {"reason": "FluxerHTTPError"}


# The games channel and its community -----------------------------------------------------------------

async def test_a_new_games_channel_gets_a_new_board_and_the_old_one_is_left_in_place(world: World,
                                                                                    db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    await world.poller().tick()
    world.fluxer.channels[OTHER_GAMES] = {"id": OTHER_GAMES, "guild_id": GUILD}
    world.fluxer.requests.clear()

    await world.poller(GAMES_CHANNEL_ID=OTHER_GAMES).tick()

    assert len(posted(world.fluxer, OTHER_MESSAGES)) == 1
    assert [r for r in world.fluxer.requests if r.method == "DELETE"] == []
    assert patches_to(world.fluxer, BOARD) == []
    row = await board_row(db)
    assert row.channel_id == OTHER_GAMES


async def test_a_board_whose_community_is_unknown_shows_plain_text_until_the_lookup_works(
        world: World, db: Database, captured: pytest.LogCaptureFixture) -> None:
    del world.fluxer.channels[GAMES]
    await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()

    await poller.tick()

    [first] = board_posts(world.fluxer)
    assert cells(first.payload) == [("Alpha", "Ready · 1 / 4")]
    assert {r.panel_fields["reason"] for r in records(captured, "games_channel_lookup_failed")} == {
        "FluxerHTTPError"}
    assert patches_to(world.fluxer, BOARD) == []

    world.fluxer.channels[GAMES] = dict(GAMES_CHANNEL)
    await poller.tick()

    [edit] = patches_to(world.fluxer, BOARD)
    assert cells(edit.payload) == [("Alpha", link_to("Ready · 1 / 4", await card_message(db, 1)))]


async def test_a_community_id_that_is_not_a_snowflake_gives_plain_text(world: World, db: Database,
                                                                       captured: pytest.LogCaptureFixture) -> None:
    world.fluxer.channels[GAMES] = {"id": GAMES, "guild_id": "10a"}
    await seed(db, "alpha", title="Alpha", probe=True)

    await world.poller().tick()

    [first] = board_posts(world.fluxer)
    assert cells(first.payload) == [("Alpha", "Ready · 1 / 4")]
    assert {r.panel_fields["reason"] for r in records(captured, "games_channel_lookup_failed")} == {"no_community"}


async def test_the_community_is_looked_up_once_after_it_is_known(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.tick()
    await poller.tick()
    await poller.tick()

    assert len(world.fluxer.requests_to(f"/api/v1/channels/{GAMES}")) == 1


# Failures ---------------------------------------------------------------------------------------------

async def test_a_failed_board_post_is_logged_and_retried_on_the_next_tick(world: World, db: Database,
                                                                          captured: pytest.LogCaptureFixture) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    world.fluxer.overrides[f"POST {GAMES_MESSAGES}"] = Override(500, b'{"code": "SENTINEL"}')
    poller = world.poller()

    await poller.tick()

    assert (await board_row(db)).message_id is None
    # The post before the cards fails. The card still posts, and the sync after the cards tries the board again and fails.
    [failed] = records(captured, "board_post_failed")
    assert failed.panel_fields == {"channel_id": GAMES, "reason": "FluxerHTTPError"}
    [synced] = records(captured, "board_sync_failed")
    assert synced.panel_fields == {"reason": "FluxerHTTPError"}
    assert len(posted(world.fluxer)) == 3  # the board, the card, and the board again

    world.fluxer.overrides.clear()
    await poller.tick()

    assert (await board_row(db)).message_id is not None


async def test_a_failed_board_edit_is_logged_and_retried_on_the_next_tick(world: World, db: Database,
                                                                          captured: pytest.LogCaptureFixture) -> None:
    card_id = await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.tick()
    world.fluxer.overrides[f"PATCH {GAMES_MESSAGES}/{BOARD}"] = Override(500, b'{"code": "SENTINEL"}')
    await db.run(lambda c: cards.update_fields(c, card_id, 0, title="Omega"))

    await poller.tick()

    [record] = records(captured, "board_sync_failed")
    assert record.panel_fields == {"reason": "FluxerHTTPError"}
    world.fluxer.overrides.clear()

    await poller.tick()

    # The linking edit on the first tick, the failed edit, and the retried edit.
    assert len(patches_to(world.fluxer, BOARD)) == 3
    assert (await board_row(db)).content_hash is not None


async def test_a_board_message_deleted_before_its_edit_is_posted_again(world: World, db: Database,
                                                                       captured: pytest.LogCaptureFixture) -> None:
    card_id = await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.tick()
    world.fluxer.deleted.add((GAMES, BOARD))
    await db.run(lambda c: cards.update_fields(c, card_id, 0, title="Omega"))
    world.fluxer.requests.clear()

    await poller.tick()

    assert len(records(captured, "board_message_recreated")) == 1
    [reposted] = board_posts(world.fluxer)
    assert cells(reposted.payload) == [("Omega", link_to("Ready · 1 / 4", await card_message(db, card_id)))]
    assert (await board_row(db)).message_id != BOARD


async def test_a_board_message_deleted_before_its_id_is_stored_is_posted_again(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.on_message_delete(MessageDelete(BOARD, GAMES, None))

    await poller.tick()

    assert len(board_posts(world.fluxer)) == 2
    assert len(dm_sends(world.fluxer)) == 2
    assert (await board_row(db)).message_id == "5003"


async def test_a_card_added_during_the_tick_is_not_listed_until_the_next_tick(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)

    async def publish_beta() -> None:
        if not any(c.slug == "beta" for c in await db.run(lambda c: cards.list_cards(c, "live"))):
            await seed(db, "beta", title="Beta", probe=True, sort=1)

    world.probe.on_call = publish_beta
    await world.poller().tick()

    [edit] = patches_to(world.fluxer, BOARD)
    assert [name for name, _ in cells(edit.payload)] == ["Alpha"]


async def test_titles_and_statuses_never_reach_the_log(world: World, db: Database,
                                                       captured: pytest.LogCaptureFixture) -> None:
    await seed(db, "alpha", title=SENTINEL_TITLE, probe=True)
    world.fluxer.overrides[f"PATCH {GAMES_MESSAGES}/{BOARD}"] = Override(500, b'{"code": "SENTINEL"}')
    poller = world.poller()
    await poller.tick()
    await db.run(lambda c: cards.set_probe(c, 1, cards.ProbeRef("stub", SENTINEL_HOST, 25565)))
    await poller.tick()
    await poller.on_message_delete(MessageDelete(BOARD, GAMES, None))

    assert SENTINEL_TITLE not in captured.text
    assert SENTINEL_HOST not in captured.text
    assert "Ready" not in captured.text
    assert '"SENTINEL"' not in captured.text


async def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the fake server")
        await asyncio.sleep(0.01)
