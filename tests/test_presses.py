# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reaction adapter: filtering, surface resolution, the kill switch, rate limits, degraded channels, removals and
press logs. Runs against a real SQLite file, the fake REST server and a recording PressHandler. Emoji are written as
escapes; the tests never contain a literal emoji."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.rest import FluxerREST, encode_emoji
from fluxer_panel import board, cards, interaction_settings, play_now, polls, subscriptions, text
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import (
    FOLLOW_EMOJI, PLAY_NOW_EMOJI, POLL_EMOJI, RUNOFF_EMOJI, SESSION_EMOJI, WAKE_EMOJI, Action, Press, Presses, Surface)
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "SENTINEL-TOKEN-presses"
BOT_ID = "1900"           # the fake's bot_user_id
ADMIN = "1001"
PRESSER = "6001"
PRESSER_2 = "6002"
PRESSER_3 = "6003"
PRESSER_4 = "6004"
PRESSER_5 = "6005"
ROLE = "7001"
GUILD = "2001"           # the games community
OTHER_GUILD = "2002"
GAMES = "3001"           # the games channel, in GUILD
COPY = "3002"            # a copy's channel, in GUILD
FAR_COPY = "3003"        # a copy's channel, in OTHER_GUILD
PAUSED = "3004"          # a paused copy's channel, in GUILD
PLAY_NOW_CHANNEL = "3005"
CARD_MSG = "5001"
SECOND_MSG = "5002"
ARCHIVED_MSG = "5003"
BOARD_MSG = "5004"
COPY_MSG = "5005"
FAR_MSG = "5006"
PAUSED_MSG = "5007"
PLAY_NOW_MSG = "5008"
POLL_CHANNEL = "3006"    # a poll's channel, in GUILD
POLL_MSG = "5009"        # a poll's message
UNKNOWN_MSG = "5999"
CARD_ID = 1
SECOND_ID = 2
ARCHIVED_ID = 3
THUMBS_UP = "\U0001F44D"
CUSTOM_NAME = "play"
CUSTOM_ID = "7777"
DAY = 86400
HASH = "a" * 64   # a rendered hash: the store takes 64 lowercase hex digits
FORBIDDEN = json.dumps({"code": "MISSING_PERMISSIONS", "message": "Missing Permissions"}).encode()
NOT_FOUND = json.dumps({"code": "UNKNOWN_MESSAGE", "message": "Unknown Message"}).encode()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RecordingHandler:
    """A PressHandler that records each call. `deletes_at_call` is how many reaction removals the fake had received by
    then, so a test can check that the removal comes after the handler."""

    def __init__(self, state: FakeFluxerState) -> None:
        self._state = state
        self.presses: list[Press] = []
        self.me_too: list[tuple[int, Surface, str]] = []
        self.rsvps: list[tuple[int, str, str, bool]] = []   # (session id, user id, choice, added)
        self.poll_reactions: list[tuple[int, str, str, str, str]] = []   # (poll id, user, emoji, channel, message)
        self.deletes_at_call: list[int] = []
        self.fail = False
        self.rsvp_fail = False

    async def handle(self, press: Press) -> None:
        self.deletes_at_call.append(sum(1 for r in self._state.requests if r.method == "DELETE"))
        self.presses.append(press)
        if self.fail:
            raise RuntimeError("the handler broke")

    async def me_too_changed(self, card_id: int, surface: Surface, user_id: str) -> None:
        self.me_too.append((card_id, surface, user_id))

    async def poll_reaction(self, poll_id: int, user_id: str, emoji: str, channel_id: str, message_id: str) -> None:
        self.poll_reactions.append((poll_id, user_id, emoji, channel_id, message_id))

    async def rsvp_changed(self, session_id: int, user_id: str, choice: str, added: bool) -> None:
        self.rsvps.append((session_id, user_id, choice, added))
        if self.rsvp_fail:
            raise RuntimeError("the rsvp handler broke")


class BarrierPacer:
    """A pacer that holds every reaction call until `parties` of them are waiting, so that concurrent presses are in
    flight together before either removal goes out."""

    def __init__(self, parties: int) -> None:
        self._barrier = asyncio.Barrier(parties)

    async def wait(self, channel_id: str) -> None:
        await self._barrier.wait()


@dataclass
class Harness:
    state: FakeFluxerState
    db: Database
    clock: Clock
    handler: RecordingHandler
    presses: Presses
    config: Config
    identity: BotIdentity = field(default_factory=lambda: BotIdentity(user_id=BOT_ID))


def seed(conn) -> None:
    card = cards.create_card(conn, slug="mc", title="Minecraft", author_id=ADMIN, now=1)
    assert card == CARD_ID
    cards.set_state(conn, card, "live", 1)
    cards.set_message(conn, card, GAMES, CARD_MSG, HASH)
    second = cards.create_card(conn, slug="ark", title="Ark", author_id=ADMIN, now=1)
    assert second == SECOND_ID
    cards.set_state(conn, second, "live", 1)
    cards.set_message(conn, second, GAMES, SECOND_MSG, HASH)
    archived = cards.create_card(conn, slug="old", title="Old", author_id=ADMIN, now=1)
    assert archived == ARCHIVED_ID
    cards.set_state(conn, archived, "archived", 1)
    cards.set_message(conn, archived, GAMES, ARCHIVED_MSG, HASH)
    subscriptions.add(conn, card, GUILD, COPY)
    subscriptions.set_message(conn, card, COPY, COPY_MSG, HASH)
    subscriptions.add(conn, card, OTHER_GUILD, FAR_COPY)
    subscriptions.set_message(conn, card, FAR_COPY, FAR_MSG, HASH)
    subscriptions.add(conn, card, GUILD, PAUSED)
    subscriptions.set_message(conn, card, PAUSED, PAUSED_MSG, HASH)
    subscriptions.pause(conn, card, PAUSED)
    play_now.add(conn, card_id=card, guild_id=GUILD, channel_id=PLAY_NOW_CHANNEL, message_id=PLAY_NOW_MSG,
                 presser_id=PRESSER, offers_wake=True, now=1, expires_at=10**9)
    board.set_message(conn, GAMES, BOARD_MSG, HASH)


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState(bot_user_id=BOT_ID)
    state.channels.update({
        GAMES: {"id": GAMES, "guild_id": GUILD, "type": 0},
        COPY: {"id": COPY, "guild_id": GUILD, "type": 0},
        FAR_COPY: {"id": FAR_COPY, "guild_id": OTHER_GUILD, "type": 0},
    })
    server = await aiohttp_server(make_rest_app(state))
    return state, str(server.make_url("/")).rstrip("/")


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def harness(tmp_path: Path, session, fake) -> AsyncIterator[Callable[..., Any]]:
    """Builds a harness with a fresh database. Keyword arguments are config environment variables."""
    state, base = fake
    opened: list[Database] = []

    async def build(*, pacer: ChannelPacer | BarrierPacer | None = None, **env: str) -> Harness:
        rest = FluxerREST(session, f"{base}/api", TOKEN)
        database = Database.open(tmp_path / f"panel{len(opened)}.db")
        opened.append(database)
        await database.run(seed)
        config = Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
                                  "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES, **env})
        clock = Clock()
        handler = RecordingHandler(state)
        identity = BotIdentity(user_id=BOT_ID)
        presses = Presses(db=database, rest=rest, config=config, handler=handler,
                          games_guild=GamesGuild(rest, config),
                          pacer=pacer if pacer is not None else ChannelPacer(max_calls=1000),
                          identity=identity, clock=clock)
        return Harness(state=state, db=database, clock=clock, handler=handler, presses=presses, config=config,
                       identity=identity)

    yield build
    for database in opened:
        database.close()


def emoji_event(name: str, custom_id: str | None = None) -> events.Emoji:
    return events.Emoji(name=name, id=custom_id)


def reaction_add(user: str = PRESSER, channel: str = GAMES, message: str = CARD_MSG, *, guild: str | None = GUILD,
                 name: str = WAKE_EMOJI, custom_id: str | None = None, bot: bool = False) -> events.ReactionAdd:
    return events.ReactionAdd(user_id=user, channel_id=channel, message_id=message, guild_id=guild,
                              emoji=emoji_event(name, custom_id), member_role_ids=frozenset({ROLE}),
                              user_is_bot=bot)


def reaction_remove(user: str = PRESSER, channel: str = GAMES, message: str = CARD_MSG, *,
                    guild: str | None = GUILD, name: str = WAKE_EMOJI) -> events.ReactionRemove:
    return events.ReactionRemove(user_id=user, channel_id=channel, message_id=message, guild_id=guild,
                                 emoji=emoji_event(name), member_role_ids=frozenset({ROLE}))


def store_poll(conn) -> int:
    """An open approval poll in POLL_CHANNEL, on POLL_MSG, for the seeded card."""
    poll_id = polls.create(conn, card_id=CARD_ID, guild_id=GUILD, channel_id=POLL_CHANNEL, message_id=POLL_MSG,
                           starter_id=ADMIN, kind="approval", parent_poll_id=None, now=1, closes_at=10**9,
                           ballot_size=2, options=[(10**9, "offered")], recipients=[PRESSER])
    assert poll_id is not None
    return poll_id


def deletes(state: FakeFluxerState) -> list[str]:
    return [r.raw_path for r in state.requests if r.method == "DELETE"]


def removal_raw_path(channel: str, message: str, name: str, user: str) -> str:
    return f"/api/v1/channels/{channel}/messages/{message}/reactions/{encode_emoji(name)}/{user}"


def removal_key(channel: str, message: str, name: str, user: str) -> str:
    """The Override key for a removal: the request path as the fake sees it, decoded."""
    return f"DELETE /api/v1/channels/{channel}/messages/{message}/reactions/{name}/{user}"


def forbid_removals(state: FakeFluxerState, channel: str, message: str, name: str, user: str) -> None:
    state.overrides[removal_key(channel, message, name, user)] = Override(status=403, body=FORBIDDEN)


def not_found_removals(state: FakeFluxerState, channel: str, message: str, name: str, user: str) -> None:
    state.overrides[removal_key(channel, message, name, user)] = Override(status=404, body=NOT_FOUND)


def dm_channel_of(state: FakeFluxerState, user: str) -> str | None:
    return state.dm_channels.get(user)


def open_dms_for(state: FakeFluxerState, user: str) -> list[dict[str, Any]]:
    return [r.payload for r in state.requests if r.method == "POST" and r.path == "/api/v1/users/@me/channels"
            and isinstance(r.payload, dict) and r.payload.get("recipient_id") == user]


def dm_texts(state: FakeFluxerState, user: str) -> list[str]:
    channel = dm_channel_of(state, user)
    if channel is None:
        return []
    return [r.payload["content"] for r in state.requests
            if r.method == "POST" and r.path == f"/api/v1/channels/{channel}/messages"]


def panel_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name.startswith("fluxer_panel")]


# Press shapes ------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "action"), [
    (WAKE_EMOJI, Action.WAKE),
    ("\u25b6", Action.WAKE),              # the play emoji without U+FE0F matches too
    (FOLLOW_EMOJI, Action.FOLLOW),
    (PLAY_NOW_EMOJI, Action.PLAY_NOW),
    (SESSION_EMOJI, Action.SESSION),
    ("\U0001F4C5", Action.SESSION),       # the calendar, as Fluxer names it
    (POLL_EMOJI, Action.POLL),
    ("\U0001F5F3", Action.POLL),          # the poll without U+FE0F matches too
])
async def test_the_three_emoji_on_a_card_are_presses_for_it(harness, name: str, action: Action) -> None:
    h = await harness()
    await h.presses.on_reaction(reaction_add(name=name))
    [press] = h.handler.presses
    assert press == Press(card_id=CARD_ID, action=action, user_id=PRESSER, member_role_ids=frozenset({ROLE}),
                          surface=Surface("card", GAMES, CARD_MSG, GUILD, True))


@pytest.mark.parametrize(("channel", "message", "guild", "home"), [
    (COPY, COPY_MSG, GUILD, True),
    (FAR_COPY, FAR_MSG, OTHER_GUILD, False),
])
async def test_an_active_copy_is_a_press_for_its_card(harness, channel: str, message: str, guild: str,
                                                      home: bool) -> None:
    h = await harness()
    await h.presses.on_reaction(reaction_add(channel=channel, message=message, guild=guild))
    [press] = h.handler.presses
    assert press.card_id == CARD_ID
    sub = await h.db.run(lambda c: subscriptions.get(c, CARD_ID, channel))
    assert sub is not None
    assert press.surface == Surface("copy", channel, message, guild, home, sub.id, "redirect")


async def test_a_press_on_a_far_local_copy_carries_its_subscription_and_mode(harness) -> None:
    h = await harness()
    sub = await h.db.run(lambda c: subscriptions.get(c, CARD_ID, FAR_COPY))
    assert sub is not None
    await h.db.run(lambda c: subscriptions.set_mode_if(c, sub.id, "redirect", "local"))
    await h.presses.on_reaction(reaction_add(channel=FAR_COPY, message=FAR_MSG, guild=OTHER_GUILD))
    [press] = h.handler.presses
    assert press.surface == Surface("copy", FAR_COPY, FAR_MSG, OTHER_GUILD, False, sub.id, "local")


async def test_a_play_now_post_takes_wake_as_a_press(harness) -> None:
    h = await harness()
    await h.presses.on_reaction(reaction_add(channel=PLAY_NOW_CHANNEL, message=PLAY_NOW_MSG))
    [press] = h.handler.presses
    assert press.action is Action.WAKE
    assert press.surface == Surface("play_now", PLAY_NOW_CHANNEL, PLAY_NOW_MSG, GUILD, True)
    assert deletes(h.state) == [removal_raw_path(PLAY_NOW_CHANNEL, PLAY_NOW_MSG, WAKE_EMOJI, PRESSER)]


async def test_me_too_on_a_play_now_post_goes_to_the_handler_for_adds_and_removes(harness) -> None:
    h = await harness()
    await h.presses.on_reaction(reaction_add(user=PRESSER_2, channel=PLAY_NOW_CHANNEL, message=PLAY_NOW_MSG,
                                             name=PLAY_NOW_EMOJI))
    await h.presses.on_reaction(reaction_remove(user=PRESSER_2, channel=PLAY_NOW_CHANNEL, message=PLAY_NOW_MSG,
                                                name=PLAY_NOW_EMOJI))
    assert h.handler.presses == []
    assert [(card, surface.kind, user) for card, surface, user in h.handler.me_too] == [
        (CARD_ID, "play_now", PRESSER_2), (CARD_ID, "play_now", PRESSER_2)]
    assert deletes(h.state) == []


# Poll messages -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", [SESSION_EMOJI, SESSION_EMOJI + "\ufe0f", RUNOFF_EMOJI, RUNOFF_EMOJI + "\ufe0f"])
async def test_schedule_and_runoff_on_a_poll_message_reach_the_handler_and_are_not_presses(
        harness, caplog, name: str) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    poll_id = await h.db.run(store_poll)
    await h.presses.on_reaction(reaction_add(channel=POLL_CHANNEL, message=POLL_MSG, name=name))
    assert h.handler.poll_reactions == [(poll_id, PRESSER, name, POLL_CHANNEL, POLL_MSG)]
    assert h.handler.presses == []
    assert h.handler.me_too == []
    assert [r for r in panel_records(caplog) if r.getMessage() == "press"] == []


async def test_a_poll_message_action_is_not_rate_limited_or_killed(harness) -> None:
    h = await harness()
    poll_id = await h.db.run(store_poll)
    await h.db.run(lambda c: interaction_settings.set_enabled(c, False))
    for _ in range(2):   # the same user twice in one window: both reach the handler
        await h.presses.on_reaction(reaction_add(channel=POLL_CHANNEL, message=POLL_MSG, name=SESSION_EMOJI))
    assert h.handler.poll_reactions == [(poll_id, PRESSER, SESSION_EMOJI, POLL_CHANNEL, POLL_MSG)] * 2


async def test_a_removal_on_a_poll_message_reaches_nothing(harness) -> None:
    h = await harness()
    await h.db.run(store_poll)
    await h.presses.on_reaction(reaction_remove(channel=POLL_CHANNEL, message=POLL_MSG, name=SESSION_EMOJI))
    await h.presses.on_reaction(reaction_remove(channel=POLL_CHANNEL, message=POLL_MSG, name=RUNOFF_EMOJI))
    assert h.handler.poll_reactions == []
    assert deletes(h.state) == []


async def test_other_or_custom_emoji_on_a_poll_message_is_not_a_poll_action(harness) -> None:
    h = await harness()
    await h.db.run(store_poll)
    await h.presses.on_reaction(reaction_add(channel=POLL_CHANNEL, message=POLL_MSG, name=SESSION_EMOJI,
                                             custom_id=CUSTOM_ID))
    await h.presses.on_reaction(reaction_add(channel=POLL_CHANNEL, message=POLL_MSG, name=THUMBS_UP))
    assert h.handler.poll_reactions == []


async def test_the_runoff_emoji_on_a_card_does_nothing(harness) -> None:
    h = await harness()
    await h.presses.on_reaction(reaction_add(name=RUNOFF_EMOJI))
    assert h.handler.presses == []
    assert h.handler.poll_reactions == []


# Removal -----------------------------------------------------------------------------------------------

async def test_a_handled_press_removes_the_reaction_after_the_handler(harness) -> None:
    h = await harness()
    await h.presses.on_reaction(reaction_add())
    assert h.handler.deletes_at_call == [0]
    assert deletes(h.state) == [removal_raw_path(GAMES, CARD_MSG, WAKE_EMOJI, PRESSER)]


async def test_a_failing_handler_logs_dms_the_presser_and_still_removes(harness, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    h.handler.fail = True
    await h.presses.on_reaction(reaction_add())
    assert deletes(h.state) == [removal_raw_path(GAMES, CARD_MSG, WAKE_EMOJI, PRESSER)]
    assert dm_texts(h.state, PRESSER) == [text.PRESS_FAILED]
    [failed] = [r for r in panel_records(caplog) if r.getMessage() == "press_failed"]
    fields = failed.panel_fields
    assert fields["card_id"] == CARD_ID and fields["action"] == "wake" and fields["kind"] == "RuntimeError"
    assert "user_id" not in fields


# Ignored reactions -------------------------------------------------------------------------------------

@pytest.mark.parametrize("make_event", [
    lambda: reaction_add(user=BOT_ID),                                   # the bot's own reaction
    lambda: reaction_add(bot=True),                                      # another bot
    lambda: reaction_add(name=THUMBS_UP),                                # not one of the three
    lambda: reaction_add(name=CUSTOM_NAME, custom_id=CUSTOM_ID),         # a custom emoji with the same name
    lambda: reaction_add(message=BOARD_MSG),                             # the board's message
    lambda: reaction_add(message=UNKNOWN_MSG),                           # a message that is no card surface
    lambda: reaction_add(message=ARCHIVED_MSG),                          # an archived card's message
    lambda: reaction_add(channel=COPY, message=CARD_MSG),                # a card's message in another channel
    lambda: reaction_add(guild=None),                                    # a DM
])
async def test_ignored_reactions_reach_no_handler_and_no_request(harness, make_event) -> None:
    h = await harness()
    await h.presses.on_reaction(make_event())
    assert h.handler.presses == []
    assert h.handler.me_too == []
    assert h.state.requests == []


async def test_a_removal_is_never_a_press(harness) -> None:
    h = await harness()
    await h.presses.on_reaction(reaction_remove())
    assert h.handler.presses == []
    assert h.state.requests == []


async def test_a_switched_off_card_or_global_switch_ignores_presses(harness) -> None:
    h = await harness()
    await h.db.run(lambda c: interaction_settings.set_enabled(c, False))
    await h.presses.on_reaction(reaction_add())
    assert h.handler.presses == []
    assert h.state.requests == []
    await h.db.run(lambda c: interaction_settings.set_enabled(c, True))
    await h.db.run(lambda c: interaction_settings.set_card_off(c, CARD_ID, True))
    await h.presses.on_reaction(reaction_add())
    assert h.handler.presses == []
    assert h.state.requests == []
    await h.db.run(lambda c: interaction_settings.set_card_off(c, CARD_ID, False))
    await h.presses.on_reaction(reaction_add())
    assert len(h.handler.presses) == 1


async def test_a_paused_copy_is_ignored(harness) -> None:
    h = await harness()
    await h.presses.on_reaction(reaction_add(channel=PAUSED, message=PAUSED_MSG))
    assert h.handler.presses == []
    assert h.state.requests == []


# Rate limits -------------------------------------------------------------------------------------------

async def test_the_same_press_within_the_user_window_is_absorbed_but_still_removed(harness, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    h.clock.now = 0
    await h.presses.on_reaction(reaction_add())
    h.clock.now = 1
    await h.presses.on_reaction(reaction_add())
    assert len(h.handler.presses) == 1
    assert len(deletes(h.state)) == 2
    [absorbed] = [r for r in panel_records(caplog) if r.getMessage() == "press_absorbed"]
    assert absorbed.panel_fields["user_id"] == PRESSER
    assert absorbed.panel_fields["reason"] == "user_window"


async def test_a_calendar_press_is_handled_before_its_removal_and_the_removal_is_sent(harness) -> None:
    h = await harness()
    await h.presses.on_reaction(reaction_add(name=SESSION_EMOJI))
    assert [(p.card_id, p.action, p.user_id) for p in h.handler.presses] == [(CARD_ID, Action.SESSION, PRESSER)]
    assert h.handler.deletes_at_call == [0]   # the handler ran before the one removal
    assert deletes(h.state) == [removal_raw_path(GAMES, CARD_MSG, SESSION_EMOJI, PRESSER)]


async def test_a_different_action_or_card_is_not_absorbed(harness) -> None:
    h = await harness()
    h.clock.now = 0
    await h.presses.on_reaction(reaction_add(name=WAKE_EMOJI))
    h.clock.now = 1
    await h.presses.on_reaction(reaction_add(name=FOLLOW_EMOJI))
    h.clock.now = 2
    await h.presses.on_reaction(reaction_add(message=SECOND_MSG))
    assert [(p.card_id, p.action) for p in h.handler.presses] == [
        (CARD_ID, Action.WAKE), (CARD_ID, Action.FOLLOW), (SECOND_ID, Action.WAKE)]


async def test_the_same_press_is_handled_again_after_the_user_window(harness) -> None:
    h = await harness()
    h.clock.now = 0
    await h.presses.on_reaction(reaction_add())
    h.clock.now = 11
    await h.presses.on_reaction(reaction_add())
    assert len(h.handler.presses) == 2


async def test_presses_beyond_the_card_burst_are_absorbed_silently(harness, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness(PRESS_CARD_BURST="3")
    for user in (PRESSER, PRESSER_2, PRESSER_3, PRESSER_4):
        await h.presses.on_reaction(reaction_add(user=user))
    assert [p.user_id for p in h.handler.presses] == [PRESSER, PRESSER_2, PRESSER_3]
    absorbed = [r for r in panel_records(caplog) if r.getMessage() == "press_absorbed"]
    assert [r.panel_fields["reason"] for r in absorbed] == ["card_burst"]
    assert dm_texts(h.state, PRESSER_4) == []
    h.clock.now = 10
    await h.presses.on_reaction(reaction_add(user=PRESSER_5))
    assert h.handler.presses[-1].user_id == PRESSER_5


async def test_the_rate_limit_maps_do_not_grow_with_expired_presses(harness) -> None:
    # The maps are private on purpose: this checks that they stay bounded, which no press outcome shows.
    h = await harness()
    for i in range(50):
        h.clock.now = i * 100   # every earlier press is far outside both windows
        await h.presses.on_reaction(reaction_add(user=str(6100 + i)))
    assert len(h.handler.presses) == 50
    assert len(h.presses._user_last) <= 1
    assert all(len(times) <= 1 for times in h.presses._card_times.values())


async def test_a_card_with_no_presses_left_in_its_window_leaves_the_card_map(harness) -> None:
    h = await harness()
    h.clock.now = 0
    await h.presses.on_reaction(reaction_add())                                     # the first card
    h.clock.now = 1000
    await h.presses.on_reaction(reaction_add(user=PRESSER_2, message=SECOND_MSG))   # the second card
    assert CARD_ID not in h.presses._card_times
    assert SECOND_ID in h.presses._card_times


# Degraded channels -------------------------------------------------------------------------------------

async def test_a_403_on_removal_degrades_the_channel_and_dms_each_admin_once(harness, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    forbid_removals(h.state, GAMES, CARD_MSG, WAKE_EMOJI, PRESSER)
    await h.presses.on_reaction(reaction_add())
    assert await h.db.run(lambda c: interaction_settings.is_degraded(c, GAMES))
    assert len(open_dms_for(h.state, ADMIN)) == 1
    assert dm_texts(h.state, ADMIN) == [text.REACTION_DEGRADED_ALERT.format(channel_id=GAMES)]
    assert len(deletes(h.state)) == 1
    assert any(r.getMessage() == "reaction_degraded" for r in panel_records(caplog))

    # the second press is handled, but nothing is removed and no DM goes out
    await h.presses.on_reaction(reaction_add(user=PRESSER_2, message=SECOND_MSG))
    assert len(h.handler.presses) == 2
    assert len(deletes(h.state)) == 1
    assert len(open_dms_for(h.state, ADMIN)) == 1


async def test_a_removal_in_a_degraded_channel_is_not_a_press(harness) -> None:
    h = await harness()
    await h.db.run(lambda c: interaction_settings.mark_degraded(c, GAMES, 0))
    await h.presses.on_reaction(reaction_remove())
    assert h.handler.presses == []
    assert h.state.requests == []


async def test_a_degraded_channel_retries_once_a_day_and_clears_on_success(harness) -> None:
    h = await harness()
    await h.db.run(lambda c: interaction_settings.mark_degraded(c, GAMES, 0))
    h.clock.now = 5
    await h.presses.on_reaction(reaction_add())
    assert deletes(h.state) == []
    h.clock.now = DAY + 1
    await h.presses.on_reaction(reaction_add(user=PRESSER_2))
    assert len(deletes(h.state)) == 1
    assert not await h.db.run(lambda c: interaction_settings.is_degraded(c, GAMES))


async def test_a_failed_retry_stays_degraded_with_no_dm_and_no_second_try_that_day(harness) -> None:
    h = await harness()
    await h.db.run(lambda c: interaction_settings.mark_degraded(c, GAMES, 0))
    forbid_removals(h.state, GAMES, CARD_MSG, WAKE_EMOJI, PRESSER)
    h.clock.now = DAY + 1
    await h.presses.on_reaction(reaction_add())
    assert len(deletes(h.state)) == 1
    assert await h.db.run(lambda c: interaction_settings.is_degraded(c, GAMES))
    assert open_dms_for(h.state, ADMIN) == []
    h.clock.now = DAY + 2
    await h.presses.on_reaction(reaction_add(user=PRESSER_2))
    assert len(deletes(h.state)) == 1
    assert open_dms_for(h.state, ADMIN) == []


async def test_two_presses_in_flight_that_both_get_403_dm_each_admin_once_and_log_once(harness, caplog) -> None:
    """The gateway runs dispatches concurrently, so two presses in one channel can both be refused before either marks
    the channel degraded. The barrier holds both removals until both are in flight."""
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness(pacer=BarrierPacer(parties=2))
    forbid_removals(h.state, GAMES, CARD_MSG, WAKE_EMOJI, PRESSER)
    forbid_removals(h.state, GAMES, SECOND_MSG, WAKE_EMOJI, PRESSER_2)
    await asyncio.gather(h.presses.on_reaction(reaction_add()),
                         h.presses.on_reaction(reaction_add(user=PRESSER_2, message=SECOND_MSG)))
    assert len(deletes(h.state)) == 2
    assert await h.db.run(lambda c: interaction_settings.is_degraded(c, GAMES))
    assert len(open_dms_for(h.state, ADMIN)) == 1
    assert dm_texts(h.state, ADMIN) == [text.REACTION_DEGRADED_ALERT.format(channel_id=GAMES)]
    assert len([r for r in panel_records(caplog) if r.getMessage() == "reaction_degraded"]) == 1


async def test_a_404_on_a_retry_is_inconclusive_and_does_not_spend_the_day(harness, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    await h.db.run(lambda c: interaction_settings.mark_degraded(c, GAMES, 0))
    not_found_removals(h.state, GAMES, CARD_MSG, WAKE_EMOJI, PRESSER)
    not_found_removals(h.state, GAMES, SECOND_MSG, WAKE_EMOJI, PRESSER_2)
    h.clock.now = DAY + 1
    await h.presses.on_reaction(reaction_add())
    assert len(deletes(h.state)) == 1
    assert await h.db.run(lambda c: interaction_settings.is_degraded(c, GAMES))
    assert open_dms_for(h.state, ADMIN) == []
    [inconclusive] = [r for r in panel_records(caplog) if r.getMessage() == "reaction_retry_inconclusive"]
    assert inconclusive.panel_fields == {"channel_id": GAMES}

    # The 404 gave the retry back, so another press the same day is still allowed to try.
    h.clock.now = DAY + 2
    await h.presses.on_reaction(reaction_add(user=PRESSER_2, message=SECOND_MSG))
    assert len(deletes(h.state)) == 2
    assert await h.db.run(lambda c: interaction_settings.is_degraded(c, GAMES))
    assert open_dms_for(h.state, ADMIN) == []


# Logs --------------------------------------------------------------------------------------------------

async def test_press_logs_carry_the_action_but_no_user_id_unless_absorbed(harness, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    await h.presses.on_reaction(reaction_add())
    [press] = [r for r in panel_records(caplog) if r.getMessage() == "press"]
    assert press.panel_fields == {"event_type": "MESSAGE_REACTION_ADD", "card_id": CARD_ID, "action": "wake",
                                  "surface": "card"}
    await h.presses.on_reaction(reaction_add())
    [absorbed] = [r for r in panel_records(caplog) if r.getMessage() == "press_absorbed"]
    assert absorbed.panel_fields["user_id"] == PRESSER


async def test_no_press_log_carries_emoji_text_or_content(harness, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    h.handler.fail = True
    await h.presses.on_reaction(reaction_add(name=WAKE_EMOJI))
    await h.presses.on_reaction(reaction_add(name=FOLLOW_EMOJI))
    await h.presses.on_reaction(reaction_add(name=PLAY_NOW_EMOJI))
    await h.presses.on_reaction(reaction_add(name=PLAY_NOW_EMOJI))
    banned = {"\u25b6", "\U0001F514", "\U0001F64B", "\ufe0f"}
    records = panel_records(caplog)
    assert records
    for record in records:
        texts = [record.getMessage(), *(str(v) for v in getattr(record, "panel_fields", {}).values())]
        for value in texts:
            assert not any(char in value for char in banned), record.getMessage()
