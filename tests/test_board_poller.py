# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The summary board in the poller, against a real SQLite file and the fake Fluxer REST: the post above the cards, the
edits, the admin DM once per new message, the hand-delete and the failure paths."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.events import MessageDelete
from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_panel import board, cards, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.poller import Poller
from fluxer_panel.sources.base import PlayerCounts, ProbeDetails, ProbeResult
from fluxer_panel.sources.registry import Sources
from tests.fakes import FakeFluxerState, Override, Recorded, make_rest_app, settle, wait_until

TOKEN = "SENTINEL-TOKEN-board-7c1e"
INSTANCE = "https://fluxer.invalid"
GUILD = "1001"
GAMES = "3001"
OTHER_GAMES = "3002"
OTHER_GUILD = "1002"                  # the community of OTHER_GAMES, which is not GUILD
AUTHOR = "1001"
GAMES_MESSAGES = f"/api/v1/channels/{GAMES}/messages"
OTHER_MESSAGES = f"/api/v1/channels/{OTHER_GAMES}/messages"
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


async def board_id(world: World) -> str:
    """The stored board's message id. Cards post before the board, so the board takes no fixed fake id."""
    state = await board_row(world.db)
    assert state.message_id is not None
    return state.message_id


def fail_the_board_post(world: World, monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Makes each board post raise a 500 as the REST client would, and lets the cards through. The fake's overrides
    cannot do this: cards and the board share the POST route. Returns the function that restores the real post."""
    real_send = world.rest.send_message

    async def send(channel_id: str, **kwargs: Any) -> Any:
        if kwargs["embeds"][0].get("title") == text.BOARD_TITLE:
            raise FluxerHTTPError(500, None, "SENTINEL")
        return await real_send(channel_id, **kwargs)

    monkeypatch.setattr(world.rest, "send_message", send)
    return lambda: monkeypatch.setattr(world.rest, "send_message", real_send)


def posted(state: FakeFluxerState, path: str = GAMES_MESSAGES) -> list[Recorded]:
    return [r for r in state.requests if r.method == "POST" and r.path == path]


def board_posts(state: FakeFluxerState) -> list[Recorded]:
    return [r for r in posted(state) if r.payload["embeds"][0].get("title") == text.BOARD_TITLE]


def card_posts(state: FakeFluxerState) -> list[Recorded]:
    return [r for r in posted(state) if r.payload["embeds"][0].get("title") != text.BOARD_TITLE]


def card_titles(state: FakeFluxerState) -> list[str]:
    return [r.payload["embeds"][0]["title"] for r in card_posts(state)]


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

async def test_the_cards_are_posted_before_the_board_on_a_fresh_start(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)

    await world.poller().tick()

    order = [r.payload["embeds"][0].get("title") for r in posted(world.fluxer)]
    assert order == ["Alpha", text.BOARD_TITLE]
    assert_board_writes_safe(world.fluxer)


async def test_the_first_board_post_has_the_jump_links_and_no_edit_follows(world: World, db: Database) -> None:
    card_id = await seed(db, "alpha", title="Alpha", probe=True)

    await world.poller().tick()

    [first] = board_posts(world.fluxer)
    assert cells(first.payload) == [("Alpha", link_to("Ready · 1 / 4", await card_message(db, card_id)))]
    assert patches_to(world.fluxer, await board_id(world)) == []
    # The card takes the first fake id, so the board, posted after it, takes the second.
    assert (await board_row(db)).message_id == "5002"


async def test_every_card_is_linked_on_the_first_board_post(world: World, db: Database) -> None:
    alpha = await seed(db, "alpha", title="Alpha", probe=True)
    beta = await seed(db, "beta", title="Beta", probe=True, sort=1)

    await world.poller().tick()

    [first] = board_posts(world.fluxer)
    assert cells(first.payload) == [("Alpha", link_to("Ready · 1 / 4", await card_message(db, alpha))),
                                    ("Beta", link_to("Ready · 1 / 4", await card_message(db, beta)))]
    assert patches_to(world.fluxer, await board_id(world)) == []


async def test_a_card_with_no_manager_and_no_probe_shows_the_dash(world: World, db: Database) -> None:
    card_id = await seed(db, "alpha", title="Alpha")

    await world.poller().tick()

    [first] = board_posts(world.fluxer)
    assert cells(first.payload) == [("Alpha", link_to(text.BOARD_NO_STATUS, await card_message(db, card_id)))]


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


async def test_nothing_is_posted_to_the_games_channel_when_it_is_not_configured(world: World, db: Database,
                                                                              captured: pytest.LogCaptureFixture) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)

    await world.poller(GAMES_CHANNEL_ID=None).tick()

    assert posted(world.fluxer) == []
    assert [r for r in world.fluxer.requests if r.method == "PATCH"] == []
    assert await board_row(db) == board.BoardState(None, None, None)
    # No board request of any kind: no lookup of the games channel, and no message in it.
    assert [r for r in world.fluxer.requests if GAMES in r.path] == []
    for event in ("board_sync_failed", "games_channel_lookup_failed"):
        assert records(captured, event) == [], event


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
    [edit] = patches_to(world.fluxer, await board_id(world))
    assert cells(edit.payload) == [("Alpha", link_to("Ready · 2 / 4", await card_message(db, card_id)))]


async def test_a_title_change_edits_the_board(world: World, db: Database) -> None:
    card_id = await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.tick()
    world.fluxer.requests.clear()
    await db.run(lambda c: cards.update_fields(c, card_id, 0, title="Omega"))

    await poller.tick()

    [edit] = patches_to(world.fluxer, await board_id(world))
    assert cells(edit.payload)[0][0] == "Omega"


# No admin DM on a board post --------------------------------------------------------------------------

async def test_a_board_post_sends_no_admin_dm_and_the_pin_request_is_gone(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()

    await poller.tick()
    await poller.tick()

    assert len(board_posts(world.fluxer)) == 1
    assert world.fluxer.requests_to(OPEN_DM) == []
    assert dm_sends(world.fluxer) == []
    assert not hasattr(text, "BOARD_PIN_ALERT")


# Hand deletion and the startup check ----------------------------------------------------------------

async def test_a_deleted_board_message_is_forgotten_and_reposted_without_waiting_for_the_interval(
        world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.tick()
    task = asyncio.create_task(poller.run())
    try:
        # The run loop's first tick is the second probe call. Its kick is cleared at the top of the loop, so the
        # delete must come once the loop is waiting, or the repost would come from that tick and not from the kick.
        await wait_until(lambda: len(world.probe.calls) == 2)
        await asyncio.sleep(0.1)
        await poller.on_message_delete(MessageDelete(await board_id(world), GAMES, None))
        await wait_until(lambda: len(board_posts(world.fluxer)) == 2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_a_delete_of_another_message_leaves_the_board_alone(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.tick()

    await poller.on_message_delete(MessageDelete("4000", GAMES, None))

    assert (await board_row(db)).message_id == await board_id(world)
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

async def test_a_changed_games_channel_keeps_the_board_and_the_cards_where_they_were_posted(world: World,
                                                                                          db: Database) -> None:
    alpha = await seed(db, "alpha", title="Alpha", probe=True)
    await world.poller().tick()
    world.fluxer.channels[OTHER_GAMES] = {"id": OTHER_GAMES, "name": "other", "type": 0, "guild_id": GUILD}
    beta = await seed(db, "beta", title="Beta", probe=True, sort=1)
    world.fluxer.requests.clear()

    await world.poller(GAMES_CHANNEL_ID=OTHER_GAMES).tick()

    # No board is posted to the new channel, and the stored board is edited where it is. Only the new card goes there.
    assert [r.payload["embeds"][0].get("title") for r in posted(world.fluxer, OTHER_MESSAGES)] == ["Beta"]
    [edit] = patches_to(world.fluxer, await board_id(world))
    # Alpha's message is in the stored board's channel, so it links. Beta's message is in the new channel, so it does not.
    assert cells(edit.payload) == [("Alpha", link_to("Ready \u00b7 1 / 4", await card_message(db, alpha))),
                                   ("Beta", "Ready \u00b7 1 / 4")]
    assert [r for r in world.fluxer.requests if r.method == "DELETE"] == []
    assert world.fluxer.requests_to(f"/api/v1/channels/{OTHER_GAMES}") == []
    assert (await board_row(db)).channel_id == GAMES
    assert await card_message(db, beta)


async def test_a_board_deleted_by_hand_is_posted_to_the_new_games_channel(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    await world.poller().tick()
    world.fluxer.channels[OTHER_GAMES] = {"id": OTHER_GAMES, "name": "other", "type": 0, "guild_id": GUILD}
    poller = world.poller(GAMES_CHANNEL_ID=OTHER_GAMES)
    await poller.on_message_delete(MessageDelete(await board_id(world), GAMES, None))
    world.fluxer.requests.clear()

    await poller.tick()

    [first] = [r for r in posted(world.fluxer, OTHER_MESSAGES) if r.payload["embeds"][0].get("title") == text.BOARD_TITLE]
    # The card is in the old channel and the board is in the new one, so the status is plain text. The new board's
    # community is looked up for the new channel only.
    assert cells(first.payload) == [("Alpha", "Ready \u00b7 1 / 4")]
    assert len(world.fluxer.requests_to(f"/api/v1/channels/{OTHER_GAMES}")) == 1
    assert world.fluxer.requests_to(f"/api/v1/channels/{GAMES}") == []
    assert (await board_row(db)).channel_id == OTHER_GAMES


async def test_a_board_reposted_in_another_channel_looks_up_that_channels_community(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    await world.poller().tick()
    world.fluxer.channels[OTHER_GAMES] = {"id": OTHER_GAMES, "name": "other", "type": 0, "guild_id": OTHER_GUILD}
    poller = world.poller(GAMES_CHANNEL_ID=OTHER_GAMES)
    await poller.tick()
    # Deleted by hand, so the next tick posts a new board to the new games channel with that channel's community.
    await poller.on_message_delete(MessageDelete(await board_id(world), GAMES, None))
    world.fluxer.requests.clear()

    await poller.tick()

    assert len(world.fluxer.requests_to(f"/api/v1/channels/{OTHER_GAMES}")) == 1
    assert world.fluxer.requests_to(f"/api/v1/channels/{GAMES}") == []
    assert (await board_row(db)).channel_id == OTHER_GAMES


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
    assert patches_to(world.fluxer, await board_id(world)) == []

    world.fluxer.channels[GAMES] = dict(GAMES_CHANNEL)
    await poller.tick()

    [edit] = patches_to(world.fluxer, await board_id(world))
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
                                                                          captured: pytest.LogCaptureFixture,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    restore = fail_the_board_post(world, monkeypatch)
    poller = world.poller()

    await poller.tick()

    assert (await board_row(db)).message_id is None
    # The card posts first and is stored. The board's post after it fails and is logged, with nothing else.
    assert card_titles(world.fluxer) == ["Alpha"]
    assert board_posts(world.fluxer) == []
    [synced] = records(captured, "board_sync_failed")
    assert synced.panel_fields == {"reason": "FluxerHTTPError"}
    assert records(captured, "board_post_failed") == []

    restore()
    await poller.tick()

    assert len(board_posts(world.fluxer)) == 1
    assert (await board_row(db)).message_id is not None


async def test_a_failed_board_edit_is_logged_and_retried_on_the_next_tick(world: World, db: Database,
                                                                          captured: pytest.LogCaptureFixture) -> None:
    card_id = await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.tick()
    world.fluxer.overrides[f"PATCH {GAMES_MESSAGES}/{await board_id(world)}"] = Override(500, b'{"code": "SENTINEL"}')
    await db.run(lambda c: cards.update_fields(c, card_id, 0, title="Omega"))

    await poller.tick()

    [record] = records(captured, "board_sync_failed")
    assert record.panel_fields == {"reason": "FluxerHTTPError"}
    world.fluxer.overrides.clear()

    await poller.tick()

    # The failed edit and the retried one. The first tick posts the board with its links, so it has no edit.
    assert len(patches_to(world.fluxer, await board_id(world))) == 2
    assert (await board_row(db)).content_hash is not None


async def test_a_board_message_deleted_before_its_edit_is_posted_again(world: World, db: Database,
                                                                       captured: pytest.LogCaptureFixture) -> None:
    card_id = await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.tick()
    world.fluxer.deleted.add((GAMES, await board_id(world)))
    await db.run(lambda c: cards.update_fields(c, card_id, 0, title="Omega"))
    world.fluxer.requests.clear()
    old_board = await board_id(world)

    await poller.tick()

    assert len(records(captured, "board_message_recreated")) == 1
    [reposted] = board_posts(world.fluxer)
    assert cells(reposted.payload) == [("Omega", link_to("Ready · 1 / 4", await card_message(db, card_id)))]
    assert (await board_row(db)).message_id != old_board


async def test_a_board_message_deleted_before_its_id_is_stored_is_posted_again(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    # The card takes 5001 and the board 5002. The delete comes before the first tick, so no board is stored yet.
    await poller.on_message_delete(MessageDelete("5002", GAMES, None))

    await poller.tick()

    # The board was posted and then forgotten by its delete, so none is stored after the first tick.
    assert len(board_posts(world.fluxer)) == 1
    assert (await board_row(db)).message_id is None

    await poller.tick()

    assert len(board_posts(world.fluxer)) == 2
    assert (await board_row(db)).message_id == "5003"


async def test_a_card_added_during_the_tick_is_not_listed_until_the_next_tick(world: World, db: Database) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)

    async def publish_beta() -> None:
        if not any(c.slug == "beta" for c in await db.run(lambda c: cards.list_cards(c, "live"))):
            await seed(db, "beta", title="Beta", probe=True, sort=1)

    world.probe.on_call = publish_beta
    await world.poller().tick()

    [first] = board_posts(world.fluxer)
    assert [name for name, _ in cells(first.payload)] == ["Alpha"]
    assert patches_to(world.fluxer, await board_id(world)) == []


async def test_titles_and_statuses_never_reach_the_log(world: World, db: Database,
                                                       captured: pytest.LogCaptureFixture) -> None:
    await seed(db, "alpha", title=SENTINEL_TITLE, probe=True)
    poller = world.poller()
    await poller.tick()
    world.fluxer.overrides[f"PATCH {GAMES_MESSAGES}/{await board_id(world)}"] = Override(500, b'{"code": "SENTINEL"}')
    await db.run(lambda c: cards.set_probe(c, 1, cards.ProbeRef("stub", SENTINEL_HOST, 25565)))
    await poller.tick()
    await poller.on_message_delete(MessageDelete(await board_id(world), GAMES, None))

    assert SENTINEL_TITLE not in captured.text
    assert SENTINEL_HOST not in captured.text
    assert "Ready" not in captured.text
    assert '"SENTINEL"' not in captured.text


# Publishing with a board stored, the concurrency bound and a failed evaluation -------------------------

async def test_with_a_board_stored_a_stalled_card_does_not_hold_back_another_cards_post(world: World,
                                                                                         db: Database) -> None:
    await world.poller().tick()
    await seed(db, "alpha", title="Alpha", probe=True)
    await seed(db, "beta", title="Beta", probe=True, sort=1)
    release = asyncio.Event()
    calls = 0

    async def stall_the_first_probe() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()

    world.probe.on_call = stall_the_first_probe
    task = asyncio.create_task(world.poller().tick())
    try:
        # Alpha's evaluation is the one stalled, so beta is the only card posted while the tick is still running.
        await wait_until(lambda: card_titles(world.fluxer) == ["Beta"])
        assert not task.done()
    finally:
        release.set()
        await task
    assert sorted(card_titles(world.fluxer)) == ["Alpha", "Beta"]


# Both phases of a tick, with a board stored and with none. With no board stored, the cards are posted and then the board,
# so the same bounds hold either way.
@pytest.mark.parametrize("board_stored", [True, False], ids=["board-stored", "no-board"])
async def test_at_most_poll_concurrency_cards_are_evaluated_at_once(world: World, db: Database,
                                                                    board_stored: bool) -> None:
    if board_stored:
        await world.poller().tick()
    for n in range(5):
        await seed(db, f"card-{n}", title=f"Card {n}", probe=True, sort=n)
    release = asyncio.Event()
    in_flight = 0
    peak = 0

    async def hold_each_evaluation() -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await release.wait()
        finally:
            in_flight -= 1

    world.probe.on_call = hold_each_evaluation
    task = asyncio.create_task(world.poller(POLL_CONCURRENCY="2").tick())
    try:
        await wait_until(lambda: in_flight >= 2)
        await settle()
        assert in_flight == 2
    finally:
        release.set()
        await task
    assert peak == 2
    assert len(board_posts(world.fluxer)) == 1
    assert len(card_posts(world.fluxer)) == 5
    if not board_stored:
        assert posted(world.fluxer)[-1].payload["embeds"][0].get("title") == text.BOARD_TITLE


@pytest.mark.parametrize("board_stored", [True, False], ids=["board-stored", "no-board"])
async def test_at_most_poll_concurrency_cards_are_published_at_once(world: World, db: Database,
                                                                    board_stored: bool) -> None:
    if board_stored:
        await world.poller().tick()
    for n in range(5):
        await seed(db, f"card-{n}", title=f"Card {n}", probe=True, sort=n)
    release = asyncio.Event()
    key = f"POST {GAMES_MESSAGES}"
    world.fluxer.gates[key] = release
    if not board_stored:
        # The first card's post is the tick's first POST and goes through; each card's post after it is held.
        world.fluxer.gate_passes[key] = 1
    task = asyncio.create_task(world.poller(POLL_CONCURRENCY="2").tick())
    try:
        await wait_until(lambda: len(world.fluxer.gated) >= 2)
        await settle()
        assert len(world.fluxer.gated) == 2
    finally:
        release.set()
        await task
    assert len(board_posts(world.fluxer)) == 1
    assert len(card_posts(world.fluxer)) == 5
    if not board_stored:
        assert posted(world.fluxer)[-1].payload["embeds"][0].get("title") == text.BOARD_TITLE


async def test_a_card_whose_evaluation_fails_stays_on_the_board_with_the_dash(world: World, db: Database,
                                                                              captured: pytest.LogCaptureFixture
                                                                              ) -> None:
    alpha = await seed(db, "alpha", title="Alpha", probe=True)
    beta = await seed(db, "beta", title="Beta", probe=True, sort=1)
    poller = world.poller()
    await poller.tick()
    world.probe.on_call = _fail_evaluation
    world.fluxer.requests.clear()

    await poller.tick()

    [edit] = patches_to(world.fluxer, await board_id(world))
    assert cells(edit.payload) == [("Alpha", link_to(text.BOARD_NO_STATUS, await card_message(db, alpha))),
                                   ("Beta", link_to(text.BOARD_NO_STATUS, await card_message(db, beta)))]
    # A failed card is not published, so its message is not edited.
    assert [r for r in world.fluxer.requests if r.method == "PATCH" and r.path != f"{GAMES_MESSAGES}/{await board_id(world)}"] == []
    assert {r.panel_fields["reason"] for r in records(captured, "card_poll_failed")} == {"RuntimeError"}


async def test_a_card_whose_first_evaluation_fails_is_listed_as_a_plain_dash_and_not_posted(
        world: World, db: Database, captured: pytest.LogCaptureFixture) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    world.probe.on_call = _fail_evaluation

    await world.poller().tick()

    [first] = board_posts(world.fluxer)
    assert cells(first.payload) == [("Alpha", text.BOARD_NO_STATUS)]
    assert card_posts(world.fluxer) == []
    assert len(records(captured, "card_poll_failed")) == 1


async def test_a_failed_board_read_is_logged_and_the_cards_still_post(world: World, db: Database,
                                                                      captured: pytest.LogCaptureFixture) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    await db.run(lambda c: c.execute("DROP TABLE board"))

    await world.poller().tick()

    assert card_titles(world.fluxer) == ["Alpha"]
    # The stored state is unknown, so no board is posted.
    assert board_posts(world.fluxer) == []
    [read] = records(captured, "board_read_failed")
    assert read.panel_fields == {"reason": "OperationalError"}
    [synced] = records(captured, "board_sync_failed")
    assert synced.panel_fields == {"reason": "OperationalError"}


async def test_a_failed_first_board_read_posts_no_board_in_that_tick_and_the_next_tick_posts_it(
        world: World, db: Database, captured: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    await seed(db, "alpha", title="Alpha", probe=True)
    fail_the_first_board_read(db, monkeypatch)
    poller = world.poller()

    await poller.tick()

    # The second read of the same tick succeeds and finds no board, but the tick's first read failed, so nothing is
    # posted now. The card still publishes.
    assert card_titles(world.fluxer) == ["Alpha"]
    assert board_posts(world.fluxer) == []
    assert (await board_row(db)).message_id is None
    assert [r.panel_fields for r in records(captured, "board_read_failed")] == [{"reason": "OperationalError"}]
    assert len(records(captured, "board_post_deferred")) == 1

    await poller.tick()

    assert len(board_posts(world.fluxer)) == 1
    assert (await board_row(db)).message_id is not None


async def test_a_failed_first_board_read_still_edits_a_stored_board(world: World, db: Database,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    card_id = await seed(db, "alpha", title="Alpha", probe=True)
    poller = world.poller()
    await poller.tick()
    world.fluxer.requests.clear()
    await db.run(lambda c: cards.update_fields(c, card_id, 0, title="Omega"))
    fail_the_first_board_read(db, monkeypatch)

    await poller.tick()

    # A stored board is only edited, never posted again, so its edit goes ahead despite the failed first read.
    assert board_posts(world.fluxer) == []
    [edit] = patches_to(world.fluxer, await board_id(world))
    assert cells(edit.payload)[0][0] == "Omega"


def fail_the_first_board_read(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Makes the first board read of the next calls fail, as a failing read would, and lets every later call reach
    the database. The poller's own code runs unchanged."""
    real_run = db.run
    failed = False

    async def run(fn: Callable[[sqlite3.Connection], Any]) -> Any:
        nonlocal failed
        if fn is board.get and not failed:
            failed = True
            raise sqlite3.OperationalError("the first board read fails")
        return await real_run(fn)

    monkeypatch.setattr(db, "run", run)


async def _fail_evaluation() -> None:
    raise RuntimeError("the probe failed")
