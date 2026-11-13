# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reaction seeding: the rules for which emoji a surface gets, the adds that skip what is already there, the 403 log
once per channel, and the removals of the kill switch. Runs against the fake REST server and a real SQLite file."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, interaction_settings
from fluxer_panel.cards import ChannelLink, FollowRole
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.presses import FOLLOW_EMOJI, PLAY_NOW_EMOJI, POLL_EMOJI, SESSION_EMOJI, WAKE_EMOJI
from fluxer_panel.seeding import Seeder, has_play_now_target, has_session_channel, wanted_emoji
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "SENTINEL-TOKEN-seeding"
GAMES = "3001"
MSG = "5001"
OTHER_MSG = "5002"
BOT = "1900"   # the fake's bot_user_id: a reaction with this user reads as me=True
REACTIONS = f"/api/v1/channels/{GAMES}/messages/{MSG}/reactions"


def make_card(**fields: Any) -> cards.Card:
    base: dict[str, Any] = dict(id=1, slug="mc", title="Minecraft", description=None, how_to_join=None,
                                first_time=None, sort_order=0, show_details=(), state="live", channel_id=None,
                                message_id=None, created_by="1001")
    return cards.Card(**{**base, **fields})


def make_config(**extra: str) -> Config:
    env = {"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": "https://fluxer.invalid", "ADMIN_USER_IDS": "1001", **extra}
    return Config.from_env(env)


LINK = ChannelLink(guild_id="1001", channel_id="2500", channel_name=None, verified=True)
ROLE = FollowRole(guild_id="1001", role_id="7001")


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState()
    state.messages[(GAMES, MSG)] = {"content": "card"}
    state.messages[(GAMES, OTHER_MSG)] = {"content": "card"}
    server = await aiohttp_server(make_rest_app(state))
    return state, str(server.make_url("/")).rstrip("/")


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


CARD_ID = 1   # the card the seeded messages belong to; the seeder asks the switches about it


class RecordingPacer:
    def __init__(self) -> None:
        self.channels: list[str] = []

    async def wait(self, channel_id: str) -> None:
        self.channels.append(channel_id)


def panel_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name.startswith("fluxer_panel")]


def raw_paths(state: FakeFluxerState, method: str) -> list[str]:
    return [r.raw_path for r in state.requests if r.method == method]


# The percent-encoded path segment of each emoji, as the bot must send it (U+25B6 U+FE0F, U+1F514, U+1F64B).
WAKE_SEGMENT = "%E2%96%B6%EF%B8%8F"
FOLLOW_SEGMENT = "%F0%9F%94%94"
PLAY_NOW_SEGMENT = "%F0%9F%99%8B"
SESSION_SEGMENT = "%F0%9F%93%85"   # U+1F4C5, the calendar
POLL_SEGMENT = "%F0%9F%97%B3%EF%B8%8F"   # U+1F5F3 U+FE0F, the poll
SEGMENTS = {WAKE_EMOJI: WAKE_SEGMENT, FOLLOW_EMOJI: FOLLOW_SEGMENT, PLAY_NOW_EMOJI: PLAY_NOW_SEGMENT,
            SESSION_EMOJI: SESSION_SEGMENT, POLL_EMOJI: POLL_SEGMENT}


# wanted_emoji: the three rules

def test_wake_is_always_wanted() -> None:
    assert wanted_emoji(make_card(), play_now_target=False) == [WAKE_EMOJI]


def test_follow_is_wanted_only_with_a_follow_role() -> None:
    assert wanted_emoji(make_card(follow_role=ROLE), play_now_target=False) == [WAKE_EMOJI, FOLLOW_EMOJI]
    assert wanted_emoji(make_card(), play_now_target=False) == [WAKE_EMOJI]


def test_play_now_is_wanted_only_with_a_play_now_target() -> None:
    card = make_card(follow_role=ROLE)

    assert wanted_emoji(card, play_now_target=True) == [WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI]
    assert wanted_emoji(card, play_now_target=False) == [WAKE_EMOJI, FOLLOW_EMOJI]


# has_play_now_target: the three surfaces

UNVERIFIED = ChannelLink(guild_id="1001", channel_id="2500", channel_name=None, verified=False)


def test_a_card_surface_has_a_target_from_its_own_channel_or_the_configured_one() -> None:
    bare = make_config()
    configured = make_config(PLAY_NOW_CHANNEL_ID="2600")

    assert has_play_now_target(make_card(play_now_channel=LINK), bare, surface_kind="card", home=True) is True
    assert has_play_now_target(make_card(), configured, surface_kind="card", home=True) is True
    assert has_play_now_target(make_card(), bare, surface_kind="card", home=True) is False


def test_an_unverified_card_link_is_no_target_on_its_own() -> None:
    bare = make_config()
    configured = make_config(PLAY_NOW_CHANNEL_ID="2600")

    assert has_play_now_target(make_card(play_now_channel=UNVERIFIED), bare, surface_kind="card", home=True) is False
    assert has_play_now_target(make_card(play_now_channel=UNVERIFIED), configured, surface_kind="card",
                               home=True) is True


def test_a_copy_elsewhere_has_a_target_when_the_card_has_one() -> None:
    bare = make_config()
    configured = make_config(PLAY_NOW_CHANNEL_ID="2600")

    assert has_play_now_target(make_card(play_now_channel=LINK), bare, surface_kind="copy", home=False) is True
    assert has_play_now_target(make_card(), configured, surface_kind="copy", home=False) is True
    assert has_play_now_target(make_card(play_now_channel=UNVERIFIED), bare, surface_kind="copy",
                               home=False) is False


def test_a_home_copy_always_has_a_target_because_it_posts_in_its_own_channel() -> None:
    assert has_play_now_target(make_card(), make_config(), surface_kind="copy", home=True) is True
    assert has_play_now_target(make_card(play_now_channel=UNVERIFIED), make_config(), surface_kind="copy",
                               home=True) is True


def test_a_copy_elsewhere_has_no_target_when_the_card_has_none() -> None:
    assert has_play_now_target(make_card(), make_config(), surface_kind="copy", home=False) is False


def test_a_play_now_surface_never_has_a_target() -> None:
    config = make_config(PLAY_NOW_CHANNEL_ID="2600")

    assert has_play_now_target(make_card(play_now_channel=LINK), config, surface_kind="play_now", home=True) is False


# wanted_emoji: the calendar rule

def test_the_calendar_is_wanted_only_with_a_session_target() -> None:
    card = make_card(follow_role=ROLE)

    assert wanted_emoji(card, play_now_target=True, session_target=True) == [
        WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI, SESSION_EMOJI]
    assert wanted_emoji(card, play_now_target=True) == [WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI]
    assert wanted_emoji(make_card(), play_now_target=False, session_target=True) == [WAKE_EMOJI, SESSION_EMOJI]


# wanted_emoji: the poll rule

def test_the_poll_is_wanted_last_and_only_with_a_poll_target() -> None:
    card = make_card(follow_role=ROLE)

    assert wanted_emoji(card, play_now_target=True, session_target=True, poll_target=True) == [
        WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI, SESSION_EMOJI, POLL_EMOJI]
    assert wanted_emoji(card, play_now_target=True, session_target=True) == [
        WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI, SESSION_EMOJI]
    assert wanted_emoji(make_card(), play_now_target=False, poll_target=False) == [WAKE_EMOJI]


# has_session_channel: where a session can be planned

def test_a_card_surface_has_a_session_channel_from_its_verified_own_channel_or_the_configured_one() -> None:
    bare = make_config()
    configured = make_config(PLAY_NOW_CHANNEL_ID="2600")

    assert has_session_channel(make_card(play_now_channel=LINK), bare, surface_kind="card", home=True) is True
    assert has_session_channel(make_card(), configured, surface_kind="card", home=True) is True
    assert has_session_channel(make_card(), bare, surface_kind="card", home=True) is False


def test_an_unverified_card_link_is_no_session_channel_on_its_own() -> None:
    assert has_session_channel(make_card(play_now_channel=UNVERIFIED), make_config(), surface_kind="card",
                               home=True) is False


def test_a_home_copy_has_a_session_channel_from_either_source() -> None:
    configured = make_config(PLAY_NOW_CHANNEL_ID="2600")

    assert has_session_channel(make_card(play_now_channel=LINK), make_config(), surface_kind="copy", home=True) is True
    assert has_session_channel(make_card(), configured, surface_kind="copy", home=True) is True
    assert has_session_channel(make_card(), make_config(), surface_kind="copy", home=True) is False


def test_a_far_copy_and_a_play_now_post_never_have_a_session_channel() -> None:
    configured = make_config(PLAY_NOW_CHANNEL_ID="2600")

    assert has_session_channel(make_card(play_now_channel=LINK), configured, surface_kind="copy", home=False) is False
    assert has_session_channel(make_card(play_now_channel=LINK), configured, surface_kind="play_now",
                               home=True) is False


# seed: adds, skips and failures

async def test_seeding_a_fresh_message_adds_the_wanted_emoji_in_order(fake, session, db) -> None:
    state, base = fake
    seeder = Seeder(rest=FluxerREST(session, f"{base}/api", TOKEN), pacer=RecordingPacer(), db=db)

    await seeder.seed(GAMES, MSG, [WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI], card_id=CARD_ID)

    assert raw_paths(state, "PUT") == [
        f"{REACTIONS}/{SEGMENTS[WAKE_EMOJI]}/@me",
        f"{REACTIONS}/{SEGMENTS[FOLLOW_EMOJI]}/@me",
        f"{REACTIONS}/{SEGMENTS[PLAY_NOW_EMOJI]}/@me",
    ]


async def test_seeding_fetches_the_message_when_no_payload_is_given(fake, session, db) -> None:
    state, base = fake
    seeder = Seeder(rest=FluxerREST(session, f"{base}/api", TOKEN), pacer=RecordingPacer(), db=db)

    await seeder.seed(GAMES, MSG, [WAKE_EMOJI], card_id=CARD_ID)

    assert [r.method for r in state.requests if r.path == f"/api/v1/channels/{GAMES}/messages/{MSG}"] == ["GET"]


async def test_seeding_with_a_payload_makes_no_fetch(fake, session, db) -> None:
    state, base = fake
    seeder = Seeder(rest=FluxerREST(session, f"{base}/api", TOKEN), pacer=RecordingPacer(), db=db)

    await seeder.seed(GAMES, MSG, [WAKE_EMOJI], card_id=CARD_ID, message={"id": MSG, "reactions": []})

    assert raw_paths(state, "GET") == []
    assert raw_paths(state, "PUT") == [f"{REACTIONS}/{SEGMENTS[WAKE_EMOJI]}/@me"]


async def test_seeding_adds_only_what_the_bot_has_not_added(fake, session, db) -> None:
    state, base = fake
    state.reactions[(GAMES, MSG)] = {WAKE_EMOJI: [BOT]}
    seeder = Seeder(rest=FluxerREST(session, f"{base}/api", TOKEN), pacer=RecordingPacer(), db=db)

    await seeder.seed(GAMES, MSG, [WAKE_EMOJI, FOLLOW_EMOJI], card_id=CARD_ID)

    assert raw_paths(state, "PUT") == [f"{REACTIONS}/{SEGMENTS[FOLLOW_EMOJI]}/@me"]


async def test_a_stored_wake_without_the_variation_selector_still_counts_as_present(fake, session, db) -> None:
    state, base = fake
    bare = "\u25b6"
    seeder = Seeder(rest=FluxerREST(session, f"{base}/api", TOKEN), pacer=RecordingPacer(), db=db)
    message = {"id": MSG, "reactions": [{"emoji": {"name": bare, "id": None}, "count": 1, "me": True}]}

    await seeder.seed(GAMES, MSG, [WAKE_EMOJI], card_id=CARD_ID, message=message)

    assert raw_paths(state, "PUT") == []


async def test_a_reaction_the_bot_has_not_made_does_not_count_as_present(fake, session, db) -> None:
    state, base = fake
    seeder = Seeder(rest=FluxerREST(session, f"{base}/api", TOKEN), pacer=RecordingPacer(), db=db)
    message = {"id": MSG, "reactions": [{"emoji": {"name": WAKE_EMOJI, "id": None}, "count": 1, "me": False}]}

    await seeder.seed(GAMES, MSG, [WAKE_EMOJI], card_id=CARD_ID, message=message)

    assert raw_paths(state, "PUT") == [f"{REACTIONS}/{SEGMENTS[WAKE_EMOJI]}/@me"]


async def test_every_add_goes_through_the_pacer_for_its_channel(fake, session, db) -> None:
    state, base = fake
    pacer = RecordingPacer()
    seeder = Seeder(rest=FluxerREST(session, f"{base}/api", TOKEN), pacer=pacer, db=db)

    await seeder.seed(GAMES, MSG, [WAKE_EMOJI, FOLLOW_EMOJI], card_id=CARD_ID)

    assert pacer.channels == [GAMES, GAMES]


async def test_a_403_is_logged_once_per_channel_and_stops_each_message(fake, session, db, caplog) -> None:
    state, base = fake
    caplog.set_level(logging.DEBUG)
    for mid in (MSG, OTHER_MSG):
        state.overrides[f"PUT /api/v1/channels/{GAMES}/messages/{mid}/reactions/{WAKE_EMOJI}/@me"] = Override(
            403, b'{"code":"MISSING_PERMISSIONS","message":"no"}')
    seeder = Seeder(rest=FluxerREST(session, f"{base}/api", TOKEN), pacer=RecordingPacer(), db=db)

    await seeder.seed(GAMES, MSG, [WAKE_EMOJI, FOLLOW_EMOJI], card_id=CARD_ID)
    await seeder.seed(GAMES, OTHER_MSG, [WAKE_EMOJI, FOLLOW_EMOJI], card_id=CARD_ID)

    forbidden = [r for r in panel_records(caplog) if r.getMessage() == "seed_forbidden"]
    assert len(forbidden) == 1
    assert getattr(forbidden[0], FIELDS_ATTR) == {"channel_id": GAMES}
    assert len(raw_paths(state, "PUT")) == 2  # one refused add per message, and no add after a refusal


async def test_another_failure_is_logged_and_stops_that_message(fake, session, db, caplog) -> None:
    state, base = fake
    caplog.set_level(logging.DEBUG)
    state.overrides[f"PUT /api/v1/channels/{GAMES}/messages/{MSG}/reactions/{WAKE_EMOJI}/@me"] = Override(
        500, b'{"code":"X","message":"x"}')
    seeder = Seeder(rest=FluxerREST(session, f"{base}/api", TOKEN), pacer=RecordingPacer(), db=db)

    await seeder.seed(GAMES, MSG, [WAKE_EMOJI, FOLLOW_EMOJI], card_id=CARD_ID)

    [failed] = [r for r in panel_records(caplog) if r.getMessage() == "seed_failed"]
    assert getattr(failed, FIELDS_ATTR)["channel_id"] == GAMES
    assert raw_paths(state, "PUT") == [f"{REACTIONS}/{SEGMENTS[WAKE_EMOJI]}/@me"]


class FlippingPacer(RecordingPacer):
    """Switches seeding off as the Nth reaction call waits for its budget. That wait sits between a seed's check and its
    add, which is where a kill switch can land."""

    def __init__(self, db: Database, *, on_wait: int) -> None:
        super().__init__()
        self._db = db
        self._on_wait = on_wait

    async def wait(self, channel_id: str) -> None:
        await super().wait(channel_id)
        if len(self.channels) == self._on_wait:
            await self._db.run(lambda c: interaction_settings.set_enabled(c, False))


async def test_switching_seeding_off_after_the_check_stops_the_add_it_was_about_to_make(fake, session, db) -> None:
    state, base = fake
    seeder = Seeder(rest=FluxerREST(session, f"{base}/api", TOKEN), pacer=FlippingPacer(db, on_wait=1), db=db)

    assert await seeder.enabled_for(CARD_ID) is True      # the caller's check passes, as the poller's does
    await seeder.seed(GAMES, MSG, [WAKE_EMOJI, FOLLOW_EMOJI], card_id=CARD_ID, message={"id": MSG, "reactions": []})

    assert raw_paths(state, "PUT") == []


async def test_a_switch_turned_off_before_the_second_add_stops_only_the_second_add(fake, session, db) -> None:
    state, base = fake
    seeder = Seeder(rest=FluxerREST(session, f"{base}/api", TOKEN), pacer=FlippingPacer(db, on_wait=2), db=db)

    await seeder.seed(GAMES, MSG, [WAKE_EMOJI, FOLLOW_EMOJI], card_id=CARD_ID, message={"id": MSG, "reactions": []})

    assert raw_paths(state, "PUT") == [f"{REACTIONS}/{SEGMENTS[WAKE_EMOJI]}/@me"]


# unseed: the kill switch's removals

async def test_unseed_sends_one_delete_per_emoji(fake, session, db) -> None:
    state, base = fake
    state.reactions[(GAMES, MSG)] = {WAKE_EMOJI: [BOT], FOLLOW_EMOJI: [BOT], PLAY_NOW_EMOJI: [BOT]}
    seeder = Seeder(rest=FluxerREST(session, f"{base}/api", TOKEN), pacer=RecordingPacer(), db=db)

    await seeder.unseed(GAMES, MSG, [WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI])

    assert raw_paths(state, "DELETE") == [
        f"{REACTIONS}/{SEGMENTS[WAKE_EMOJI]}",
        f"{REACTIONS}/{SEGMENTS[FOLLOW_EMOJI]}",
        f"{REACTIONS}/{SEGMENTS[PLAY_NOW_EMOJI]}",
    ]
    assert state.reactions == {}


async def test_unseed_ignores_a_404_and_goes_on_to_the_next_emoji(fake, session, db, caplog) -> None:
    state, base = fake
    caplog.set_level(logging.DEBUG)
    state.overrides[f"DELETE /api/v1/channels/{GAMES}/messages/{MSG}/reactions/{WAKE_EMOJI}"] = Override(
        404, b'{"code":"UNKNOWN_MESSAGE","message":"x"}')
    state.reactions[(GAMES, MSG)] = {FOLLOW_EMOJI: [BOT]}
    seeder = Seeder(rest=FluxerREST(session, f"{base}/api", TOKEN), pacer=RecordingPacer(), db=db)

    await seeder.unseed(GAMES, MSG, [WAKE_EMOJI, FOLLOW_EMOJI])

    assert len(raw_paths(state, "DELETE")) == 2
    assert state.reactions == {}
    assert [r for r in panel_records(caplog) if r.levelno >= logging.WARNING] == []


# enabled_for: the global switch and the per-card switch

async def test_seeding_is_enabled_by_default_and_follows_both_switches(db: Database) -> None:
    card_id = await db.run(lambda c: cards.create_card(c, slug="mc", title="Mc", author_id="1001", now=1))
    seeder = Seeder(rest=None, pacer=RecordingPacer(), db=db)

    assert await seeder.enabled_for(card_id) is True
    await db.run(lambda c: interaction_settings.set_card_off(c, card_id, True))
    assert await seeder.enabled_for(card_id) is False
    await db.run(lambda c: interaction_settings.set_card_off(c, card_id, False))
    await db.run(lambda c: interaction_settings.set_enabled(c, False))
    assert await seeder.enabled_for(card_id) is False
