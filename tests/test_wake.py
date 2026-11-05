# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wake presses against a real SQLite file and a fake Fluxer. A press on an asleep card with a Pterodactyl manager opens a
request; any other press is a signal that reaches only the admins' DM. While a request or a signal is current, the
admins' DMs are edited rather than re-sent."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, notify, render, wake_requests
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import ManagerRef
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.resolver import CardStatus, StatusKind
from fluxer_panel.sources import pterodactyl
from fluxer_panel.sources.registry import Sources
from fluxer_panel.wake import WakeRequests
from tests.fakes import FakeFluxerState, Override, Recorded, make_rest_app

TOKEN = "SENTINEL-TOKEN-wake"
ALICE = "1101"
BOB = "1102"
CAROL = "1103"
ADMIN_A = "1201"
ADMIN_B = "1202"
TITLE = "Minecraft"
CHANNEL = "2001"
MESSAGE_ID = "5900"
WINDOW = 7200
REQUESTER_DM = "Sending your request to the admins for Minecraft."


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Harness:
    db: Database
    state: FakeFluxerState
    rest: FluxerREST
    config: Config
    clock: Clock
    statuses: dict[int, CardStatus | None] = field(default_factory=dict)
    kicks: list[int] = field(default_factory=list)
    sources: Sources = field(default_factory=lambda: Sources(managers={pterodactyl.KIND: object()}, probes={}))

    def wake(self) -> WakeRequests:
        """A fresh WakeRequests over the same database: calling it again is a restart."""
        return WakeRequests(db=self.db, rest=self.rest, config=self.config, sources=self.sources,
                            locks=CardLocks(), status_of=self.statuses.get, clock=self.clock,
                            kick=lambda: self.kicks.append(1))


def make_config() -> Config:
    env = {"FLUXER_TOKEN": TOKEN, "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
           "ADMIN_USER_IDS": f"{ADMIN_B},{ADMIN_A}"}
    return Config.from_env(env)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState()
    server = await aiohttp_server(make_rest_app(state))
    return state, str(server.make_url("/")).rstrip("/")


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def harness(db: Database, fake, session: aiohttp.ClientSession) -> Harness:
    state, base = fake
    rest = FluxerREST(session, f"{base}/api", TOKEN)
    return Harness(db=db, state=state, rest=rest, config=make_config(), clock=Clock())


async def live_card(db: Database, slug: str = "mc", *, manager: bool = True, state: str = "live",
                    title: str = TITLE) -> int:
    def create(c) -> int:
        card_id = cards.create_card(c, slug=slug, title=title, author_id="1001", now=1)
        cards.set_state(c, card_id, state, 1)
        cards.set_message(c, card_id, CHANNEL, MESSAGE_ID, None)
        if manager:
            cards.set_manager(c, card_id, ManagerRef(pterodactyl.KIND, "abcd1234"))
        return card_id

    return await db.run(create)


def sent_to(state: FakeFluxerState, user_id: str) -> list[Recorded]:
    """The messages POSTed to a user's DM channel (the requester's, or an admin's)."""
    channel = state.dm_channels.get(user_id)
    if channel is None:
        return []
    return [r for r in state.requests if r.method == "POST" and r.path == f"/api/v1/channels/{channel}/messages"]


def patches(state: FakeFluxerState) -> list[Recorded]:
    return [r for r in state.requests if r.method == "PATCH"]


async def open_counts(db: Database) -> dict[int, int]:
    return await db.run(lambda c: wake_requests.open_counts(c))


async def test_a_press_on_an_asleep_card_opens_one_request_and_dms_the_admins(db: Database, harness: Harness) -> None:
    card = await live_card(db)
    harness.statuses[card] = CardStatus(StatusKind.ASLEEP)

    await harness.wake().press(card, ALICE)

    assert await open_counts(db) == {card: 1}
    [requester] = sent_to(harness.state, ALICE)
    assert requester.payload == {"content": REQUESTER_DM, "allowed_mentions": {"parse": []}}
    for admin in (ADMIN_A, ADMIN_B):
        [dm] = sent_to(harness.state, admin)
        assert TITLE in dm.payload["content"]
        assert f"<@{ALICE}>" in dm.payload["content"]
        assert dm.payload["allowed_mentions"] == {"parse": []}
    assert harness.kicks == [1]


async def test_a_repeat_press_inserts_no_row_and_edits_the_admin_dms(db: Database, harness: Harness) -> None:
    card = await live_card(db)
    harness.statuses[card] = CardStatus(StatusKind.ASLEEP)
    wake = harness.wake()
    await wake.press(card, ALICE)
    harness.clock.now += 30

    await wake.press(card, ALICE)

    assert await open_counts(db) == {card: 1}
    assert len(sent_to(harness.state, ALICE)) == 2   # the requester still gets their DM
    for admin in (ADMIN_A, ADMIN_B):
        assert len(sent_to(harness.state, admin)) == 1   # no new admin DM
    assert len(patches(harness.state)) == 2
    assert all(f"<@{ALICE}>" in p.payload["content"] for p in patches(harness.state))


async def test_a_second_requester_edits_each_admin_dm_with_both_mentions(db: Database, harness: Harness) -> None:
    card = await live_card(db)
    harness.statuses[card] = CardStatus(StatusKind.ASLEEP)
    wake = harness.wake()
    await wake.press(card, ALICE)
    harness.clock.now += 10

    await wake.press(card, BOB)

    assert await open_counts(db) == {card: 2}
    for admin in (ADMIN_A, ADMIN_B):
        assert len(sent_to(harness.state, admin)) == 1
    edits = patches(harness.state)
    assert len(edits) == 2
    for edit in edits:
        assert f"<@{ALICE}>" in edit.payload["content"]
        assert f"<@{BOB}>" in edit.payload["content"]
        assert edit.payload["allowed_mentions"] == {"parse": []}


SIGNAL_CASES = [
    pytest.param(CardStatus(StatusKind.READY), True, "Ready", "It's already running.", id="ready"),
    pytest.param(CardStatus(StatusKind.WAKING), True, "Waking up", "It's already starting.", id="waking"),
    pytest.param(CardStatus(StatusKind.GOING_TO_SLEEP), True, "Going to sleep",
                 "It's shutting down right now.", id="going-to-sleep"),
    pytest.param(CardStatus(StatusKind.NOT_RESPONDING), True, "Not responding",
                 "It's running but not answering.", id="not-responding"),
    pytest.param(CardStatus(StatusKind.UNAVAILABLE), True, "Unavailable", "It's unavailable right now.",
                 id="unavailable"),
    pytest.param(CardStatus(StatusKind.UNKNOWN), True, "Status unknown", "Its status is unknown.", id="unknown"),
    pytest.param(None, True, "no status", "It has no status yet.", id="no-status"),
    pytest.param(CardStatus(StatusKind.ASLEEP), False, "Asleep",
                 "The bot can't see its server, so the admins will check.", id="asleep-no-manager"),
    # A probe-only card reports its real status: the no-manager note is for a card the bot cannot see into.
    pytest.param(CardStatus(StatusKind.READY), False, "Ready", "It's already running.", id="ready-no-manager"),
    pytest.param(CardStatus(StatusKind.NOT_RESPONDING), False, "Not responding",
                 "It's running but not answering.", id="not-responding-no-manager"),
]


@pytest.mark.parametrize(("status", "has_manager", "shown", "note"), SIGNAL_CASES)
async def test_a_signal_opens_no_row_and_tells_the_requester_and_the_admins(
        db: Database, harness: Harness, status: CardStatus | None, has_manager: bool, shown: str, note: str) -> None:
    card = await live_card(db, manager=has_manager)
    harness.statuses[card] = status

    await harness.wake().press(card, ALICE)

    assert await open_counts(db) == {}
    [requester] = sent_to(harness.state, ALICE)
    assert requester.payload["content"] == f"{REQUESTER_DM} {note}"
    for admin in (ADMIN_A, ADMIN_B):
        [dm] = sent_to(harness.state, admin)
        assert f"Status: {shown}." in dm.payload["content"]
        assert dm.payload["allowed_mentions"] == {"parse": []}
    assert harness.kicks == []


async def test_a_title_is_escaped_in_the_requester_and_admin_dms(db: Database, harness: Harness) -> None:
    raw = "*Mine* <@1>"
    card = await live_card(db, title=raw)
    harness.statuses[card] = CardStatus(StatusKind.ASLEEP)

    await harness.wake().press(card, ALICE)

    escaped = render.escape(raw)
    [requester] = sent_to(harness.state, ALICE)
    assert requester.payload["content"] == f"Sending your request to the admins for {escaped}."
    for admin in (ADMIN_A, ADMIN_B):
        [dm] = sent_to(harness.state, admin)
        assert escaped in dm.payload["content"]
        assert raw not in dm.payload["content"]


async def test_a_title_is_escaped_in_the_close_dms(db: Database, harness: Harness) -> None:
    raw = "*Mine* <@1>"
    card = await live_card(db, title=raw)
    harness.statuses[card] = CardStatus(StatusKind.ASLEEP)
    wake = harness.wake()
    await wake.press(card, ALICE)
    harness.statuses[card] = CardStatus(StatusKind.READY)

    await wake.close_due()

    escaped = render.escape(raw)
    assert contents_to(harness.state, ALICE)[-1] == f"{escaped} is ready."
    closes = patches(harness.state)
    assert len(closes) == 2
    for edit in closes:
        assert escaped in edit.payload["content"]
        assert raw not in edit.payload["content"]


async def test_repeat_signals_inside_the_window_edit_the_admin_dm(db: Database, harness: Harness) -> None:
    card = await live_card(db)
    harness.statuses[card] = CardStatus(StatusKind.READY)
    wake = harness.wake()
    await wake.press(card, ALICE)
    harness.clock.now += 60

    await wake.press(card, BOB)

    for admin in (ADMIN_A, ADMIN_B):
        assert len(sent_to(harness.state, admin)) == 1
    edits = patches(harness.state)
    assert len(edits) == 2
    assert all(f"<@{ALICE}>" in e.payload["content"] and f"<@{BOB}>" in e.payload["content"] for e in edits)


async def test_a_signal_after_the_window_starts_a_new_admin_dm(db: Database, harness: Harness) -> None:
    card = await live_card(db)
    harness.statuses[card] = CardStatus(StatusKind.READY)
    wake = harness.wake()
    await wake.press(card, ALICE)
    harness.clock.now += WINDOW

    await wake.press(card, BOB)

    for admin in (ADMIN_A, ADMIN_B):
        assert len(sent_to(harness.state, admin)) == 2
    assert patches(harness.state) == []
    newest = sent_to(harness.state, ADMIN_A)[-1]
    assert f"<@{BOB}>" in newest.payload["content"]
    assert f"<@{ALICE}>" not in newest.payload["content"]


async def test_after_a_restart_a_signal_starts_a_new_admin_dm(db: Database, harness: Harness) -> None:
    card = await live_card(db)
    harness.statuses[card] = CardStatus(StatusKind.READY)
    await harness.wake().press(card, ALICE)

    await harness.wake().press(card, BOB)   # a new WakeRequests: the signal list is gone

    for admin in (ADMIN_A, ADMIN_B):
        assert len(sent_to(harness.state, admin)) == 2
    assert patches(harness.state) == []


async def test_an_admin_dm_that_was_deleted_gets_a_fresh_one(db: Database, harness: Harness) -> None:
    card = await live_card(db)
    harness.statuses[card] = CardStatus(StatusKind.READY)
    wake = harness.wake()
    await wake.press(card, ALICE)
    channel = harness.state.dm_channels[ADMIN_A]
    [message] = [mid for (cid, mid) in harness.state.messages if cid == channel]
    harness.state.overrides[f"PATCH /api/v1/channels/{channel}/messages/{message}"] = Override(
        404, b'{"code":"UNKNOWN_MESSAGE","message":"Unknown Message"}')

    await wake.press(card, BOB)

    assert len(sent_to(harness.state, ADMIN_A)) == 2
    assert len(sent_to(harness.state, ADMIN_B)) == 1


async def test_a_failed_requester_dm_does_not_undo_the_press(db: Database, harness: Harness) -> None:
    harness.state.dm_channels[ALICE] = "9100"
    harness.state.overrides["POST /api/v1/channels/9100/messages"] = Override(
        403, b'{"code":"MISSING_PERMISSIONS","message":"no"}')
    card = await live_card(db)
    harness.statuses[card] = CardStatus(StatusKind.ASLEEP)

    await harness.wake().press(card, ALICE)

    assert await open_counts(db) == {card: 1}
    for admin in (ADMIN_A, ADMIN_B):
        assert len(sent_to(harness.state, admin)) == 1


async def test_a_card_that_is_not_live_gets_no_press_handling(db: Database, harness: Harness) -> None:
    card = await live_card(db, state="draft")
    harness.statuses[card] = CardStatus(StatusKind.ASLEEP)

    await harness.wake().press(card, ALICE)

    assert await open_counts(db) == {}
    assert harness.state.requests == []


async def test_a_press_logs_the_card_and_outcome_but_no_user_id(db: Database, harness: Harness,
                                                              caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    card = await live_card(db)
    harness.statuses[card] = CardStatus(StatusKind.ASLEEP)

    await harness.wake().press(card, ALICE)

    records = [r for r in caplog.records if r.name.startswith("fluxer_panel")]
    [press] = [r for r in records if r.getMessage() == "wake_press"]
    assert getattr(press, FIELDS_ATTR) == {"card_id": card, "outcome": "opened"}
    assert ALICE not in " ".join(r.getMessage() + repr(getattr(r, FIELDS_ATTR, {})) for r in records)


async def test_list_open_gives_slug_count_and_age_per_card(db: Database, harness: Harness) -> None:
    mc = await live_card(db, "mc")
    ark = await live_card(db, "ark")
    harness.statuses[mc] = CardStatus(StatusKind.ASLEEP)
    harness.statuses[ark] = CardStatus(StatusKind.ASLEEP)
    wake = harness.wake()
    await wake.press(mc, ALICE)
    await wake.press(mc, BOB)
    await wake.press(ark, CAROL)

    harness.clock.now += 125

    assert await wake.list_open() == [("ark", 1, 125), ("mc", 2, 125)]


async def test_list_open_is_empty_with_no_open_request(db: Database, harness: Harness) -> None:
    assert await harness.wake().list_open() == []


READY_DM = "Minecraft is ready."
GAVE_UP_DM = "Nobody could start Minecraft right now."
READY_ADMIN = "Minecraft (mc) is ready. The request from 2 people is closed."
EXPIRED_ADMIN = "Nobody started Minecraft (mc). The request from 2 people has expired."


def contents_to(state: FakeFluxerState, user_id: str) -> list[str]:
    return [r.payload["content"] for r in sent_to(state, user_id)]


def admin_edits(state: FakeFluxerState, content: str) -> list[Recorded]:
    return [p for p in patches(state) if p.payload["content"] == content]


async def open_two_requests(db: Database, harness: Harness, status: StatusKind) -> tuple[int, WakeRequests]:
    card = await live_card(db)
    harness.statuses[card] = CardStatus(status)
    wake = harness.wake()
    await wake.press(card, ALICE)
    await wake.press(card, BOB)
    return card, wake


async def test_close_returns_the_count_once_and_edits_the_admin_dms_to_the_cleared_text(
        db: Database, harness: Harness) -> None:
    card, wake = await open_two_requests(db, harness, StatusKind.ASLEEP)

    assert await wake.close(card, "cleared") == 2
    assert await wake.close(card, "cleared") == 0

    assert await open_counts(db) == {}
    cleared = "The request for Minecraft (mc) from 2 people was cleared."
    assert len(admin_edits(harness.state, cleared)) == 2
    assert await db.run(lambda c: wake_requests.get_dms(c, card)) == []


async def test_a_ready_card_closes_its_requests_and_tells_each_requester_once(db: Database, harness: Harness) -> None:
    card, wake = await open_two_requests(db, harness, StatusKind.ASLEEP)
    harness.statuses[card] = CardStatus(StatusKind.READY)   # the card wakes after the requests were opened

    await wake.close_due()
    await wake.close_due()

    rows = await db.run(lambda c: c.execute("SELECT requester_id, status FROM requests ORDER BY requester_id").fetchall())
    assert [tuple(row) for row in rows] == [(ALICE, "ready"), (BOB, "ready")]
    for user in (ALICE, BOB):
        assert contents_to(harness.state, user).count(READY_DM) == 1
    assert len(admin_edits(harness.state, READY_ADMIN)) == 2
    assert await db.run(lambda c: wake_requests.get_dms(c, card)) == []
    assert await open_counts(db) == {}


CLOSE_COUNT_CASES = [
    pytest.param(1, "ready", "Minecraft (mc) is ready. The request from 1 person is closed.", id="ready-one"),
    pytest.param(2, "ready", "Minecraft (mc) is ready. The request from 2 people is closed.", id="ready-two"),
    pytest.param(1, "expired", "Nobody started Minecraft (mc). The request from 1 person has expired.", id="expired-one"),
    pytest.param(2, "expired", "Nobody started Minecraft (mc). The request from 2 people has expired.", id="expired-two"),
]


@pytest.mark.parametrize(("count", "outcome", "expected"), CLOSE_COUNT_CASES)
async def test_the_close_text_says_one_person_or_people(db: Database, harness: Harness, count: int, outcome: str,
                                                        expected: str) -> None:
    card = await live_card(db)
    harness.statuses[card] = CardStatus(StatusKind.ASLEEP)
    wake = harness.wake()
    await wake.press(card, ALICE)
    if count == 2:
        await wake.press(card, BOB)
    if outcome == "ready":
        harness.statuses[card] = CardStatus(StatusKind.READY)
    else:
        harness.clock.now += WINDOW + 1

    await wake.close_due()

    assert len(admin_edits(harness.state, expected)) == 2


async def test_a_request_expires_after_the_window_and_tells_each_requester_once(db: Database, harness: Harness) -> None:
    card, wake = await open_two_requests(db, harness, StatusKind.ASLEEP)

    harness.clock.now = 1000 + WINDOW   # the oldest row is exactly at the window: not yet expired
    await wake.close_due()
    assert await open_counts(db) == {card: 2}

    harness.clock.now = 1000 + WINDOW + 1
    await wake.close_due()
    await wake.close_due()

    assert await open_counts(db) == {}
    for user in (ALICE, BOB):
        assert contents_to(harness.state, user).count(GAVE_UP_DM) == 1
    assert len(admin_edits(harness.state, EXPIRED_ADMIN)) == 2


async def test_a_restart_sends_no_second_close_dm(db: Database, harness: Harness) -> None:
    _, wake = await open_two_requests(db, harness, StatusKind.ASLEEP)
    harness.clock.now = 1000 + WINDOW + 1
    await wake.close_due()

    await harness.wake().close_due()

    for user in (ALICE, BOB):
        assert contents_to(harness.state, user).count(GAVE_UP_DM) == 1


async def test_a_crash_after_the_close_sends_nothing_on_the_next_run(db: Database, harness: Harness,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    _, wake = await open_two_requests(db, harness, StatusKind.ASLEEP)
    harness.clock.now = 1000 + WINDOW + 1
    real_dm_user = notify.dm_user

    async def crash(*args: object, **kwargs: object) -> None:
        raise RuntimeError("crash after the close")

    monkeypatch.setattr(notify, "dm_user", crash)
    with pytest.raises(RuntimeError):
        await wake.close_due()
    monkeypatch.setattr(notify, "dm_user", real_dm_user)
    before = {user: len(sent_to(harness.state, user)) for user in (ALICE, BOB)}

    await harness.wake().close_due()

    assert {user: len(sent_to(harness.state, user)) for user in (ALICE, BOB)} == before
    assert await open_counts(db) == {}
