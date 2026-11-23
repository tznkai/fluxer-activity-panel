# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Play-now presses end to end: `Presses.on_reaction` feeding a real `Interactions`, against a real SQLite file and the
fake REST app. The play-now posts, the presser's DMs and the seeded reactions are what the tests read. Emoji are written
as escapes (presses.PLAY_NOW_EMOJI and friends); the tests never contain a literal emoji."""

from __future__ import annotations

import asyncio
import itertools
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.rest import FluxerHTTPError, FluxerREST, ReactionUsersPage
from fluxer_client.roles import RoleCache
from fluxer_panel import cards, play_now, render, scope, subscriptions, text
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import ChannelLink, FollowRole
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import GamesChannelRefused, Interactions, _scope_role_in
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import PLAY_NOW_EMOJI, WAKE_EMOJI, Presses
from fluxer_panel.resolver import CardStatus, StatusKind
from fluxer_panel.seeding import Seeder
from tests.fakes import FakeFluxerState, Override, Recorded, make_rest_app

TOKEN = "SENTINEL-TOKEN-interactions-play-now"
BOT_ID = "1900"
ADMIN = "1001"
PRESSER = "6001"
PRESSER_2 = "6002"
WAKER = "6003"
SUB_ROLE = "7002"       # the follow role of the far copy's local scope, in OTHER_GUILD
GUILD = "2001"           # the games community
OTHER_GUILD = "2002"
GAMES = "3001"           # the games channel, in GUILD
COPY = "3002"            # a copy's channel, in GUILD
FAR_COPY = "3003"        # a copy's channel, in OTHER_GUILD
PLAY_CH = "3004"         # a card's play-now channel, in GUILD
FAR_PLAY = "3005"        # a play-now channel in OTHER_GUILD
FAR_PLAY_2 = "3007"      # a second play-now channel in OTHER_GUILD, for a local copy's own target
CONFIG_PLAY = "3006"     # PLAY_NOW_CHANNEL_ID, in GUILD
MISSING = "3999"         # a configured play-now channel the fake does not know
BOT_ROLE = "5000"
ALPHA_ROLE = "7001"      # the card's follow role: permissions 0, position 5, below the bot
CARD_MSG = "5101"
SECOND_MSG = "5102"
COPY_MSG = "5103"
FAR_MSG = "5104"
CARD_ID = 1
SECOND_ID = 2
CARD_TITLE = "Mine] (craft"   # brackets and a parenthesis: they must be escaped in the content
SECOND_TITLE = "Ark"
JOIN = "Join at the gate"
JUMP_URL = "https://fluxer.invalid"
API = f"{JUMP_URL}/api"
EVERYONE = {"id": GUILD, "name": "@everyone", "permissions": "0", "position": 0, "mentionable": False}
BOT_ROLE_DATA = {"id": BOT_ROLE, "name": "bot", "permissions": "8", "position": 10, "mentionable": False}
ALPHA_DATA = {"id": ALPHA_ROLE, "name": "card_subscription_mc", "permissions": "0", "position": 5,
              "mentionable": True}
FORBIDDEN = b'{"code":"MISSING_PERMISSIONS","message":"no"}'
BOUND = 30   # seconds: a hard stop for a wait that should never hang


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RecordingWake:
    def __init__(self) -> None:
        self.presses: list[tuple[int, str]] = []

    async def press(self, card_id: int, user_id: str) -> None:
        self.presses.append((card_id, user_id))


def seed(conn) -> None:
    card = cards.create_card(conn, slug="mc", title=CARD_TITLE, author_id=ADMIN, now=1)
    assert card == CARD_ID
    cards.set_state(conn, card, "live", 1)
    cards.set_message(conn, card, GAMES, CARD_MSG, "a" * 64)
    cards.set_follow_role(conn, card, GUILD, ALPHA_ROLE)
    cards.update_fields(conn, card, 1, how_to_join=JOIN)
    second = cards.create_card(conn, slug="ark", title=SECOND_TITLE, author_id=ADMIN, now=1)
    assert second == SECOND_ID
    cards.set_state(conn, second, "live", 1)
    cards.set_message(conn, second, GAMES, SECOND_MSG, "a" * 64)
    subscriptions.add(conn, card, GUILD, COPY)
    subscriptions.set_message(conn, card, COPY, COPY_MSG, "a" * 64)
    subscriptions.add(conn, card, OTHER_GUILD, FAR_COPY)
    subscriptions.set_message(conn, card, FAR_COPY, FAR_MSG, "a" * 64)


@dataclass
class Harness:
    state: FakeFluxerState
    db: Database
    presses: Presses
    interactions: Interactions
    clock: Clock
    config: Config
    wake: RecordingWake
    locks: CardLocks


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState(bot_user_id=BOT_ID)
    state.channels.update({
        GAMES: {"id": GAMES, "guild_id": GUILD, "type": 0},
        COPY: {"id": COPY, "guild_id": GUILD, "type": 0},
        FAR_COPY: {"id": FAR_COPY, "guild_id": OTHER_GUILD, "type": 0},
        PLAY_CH: {"id": PLAY_CH, "guild_id": GUILD, "type": 0},
        FAR_PLAY: {"id": FAR_PLAY, "guild_id": OTHER_GUILD, "type": 0},
        FAR_PLAY_2: {"id": FAR_PLAY_2, "guild_id": OTHER_GUILD, "type": 0},
        CONFIG_PLAY: {"id": CONFIG_PLAY, "guild_id": GUILD, "type": 0},
    })
    state.roles = {GUILD: [dict(EVERYONE), dict(BOT_ROLE_DATA), dict(ALPHA_DATA)]}
    state.members = {(GUILD, BOT_ID): {"roles": [BOT_ROLE]}}
    server = await aiohttp_server(make_rest_app(state))
    return state, f"{str(server.make_url('/')).rstrip('/')}/api"


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def harness(tmp_path: Path, session, fake) -> AsyncIterator[Callable[..., Any]]:
    state, api = fake
    opened: list[Database] = []

    async def build(*, instance_url: bool = True, status_of: Callable[[int], CardStatus | None] | None = None,
                    pacer: ChannelPacer | None = None, locks: CardLocks | None = None, **env: str) -> Harness:
        rest = FluxerREST(session, api, TOKEN)
        database = Database.open(tmp_path / f"panel{len(opened)}.db")
        opened.append(database)
        await database.run(seed)
        base = {"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES}
        base["FLUXER_INSTANCE_URL" if instance_url else "FLUXER_API_URL"] = JUMP_URL if instance_url else API
        config = Config.from_env({**base, **env})
        games = GamesGuild(rest, config)
        if pacer is None:
            pacer = ChannelPacer(max_calls=1000)
        if locks is None:
            locks = CardLocks()
        identity = BotIdentity(user_id=BOT_ID)
        clock = Clock()
        wake = RecordingWake()
        interactions = Interactions(db=database, rest=rest, config=config, locks=locks, games_guild=games,
                                    seeder=Seeder(rest=rest, pacer=pacer, db=database), pacer=pacer,
                                    identity=identity, roles=RoleCache(rest), clock=clock, wake=wake,
                                    status_of=status_of or (lambda _id: None))
        presses = Presses(db=database, rest=rest, config=config, handler=interactions, games_guild=games,
                          pacer=pacer, identity=identity, clock=clock)
        return Harness(state=state, db=database, presses=presses, interactions=interactions, clock=clock,
                       config=config, wake=wake, locks=locks)

    yield build
    for database in opened:
        database.close()


def press_event(emoji: str, *, user: str = PRESSER, channel: str = GAMES, message: str = CARD_MSG,
                guild: str | None = GUILD) -> events.ReactionAdd:
    return events.ReactionAdd(user_id=user, channel_id=channel, message_id=message, guild_id=guild,
                              emoji=events.Emoji(name=emoji, id=None), member_role_ids=frozenset(), user_is_bot=False)


def play_press(**kwargs: Any) -> events.ReactionAdd:
    return press_event(PLAY_NOW_EMOJI, **kwargs)


async def set_card_play_channel(h: Harness, card_id: int, guild: str, channel: str) -> None:
    await h.db.run(lambda c: cards.set_play_now_channel(c, card_id, ChannelLink(guild, channel, None, True)))


def dm_channel_ids(state: FakeFluxerState) -> set[str]:
    return set(state.dm_channels.values())


def channel_posts(state: FakeFluxerState) -> list[Recorded]:
    """Every message POST that went to a channel, not to a DM channel."""
    dms = {f"/api/v1/channels/{channel}/messages" for channel in dm_channel_ids(state)}
    return [r for r in state.requests if r.method == "POST" and r.path.endswith("/messages") and r.path not in dms]


def dm_channel_texts(state: FakeFluxerState, user: str) -> list[str]:
    channel = state.dm_channels.get(user)
    if channel is None:
        return []
    return [r.payload["content"] for r in state.requests
            if r.method == "POST" and r.path == f"/api/v1/channels/{channel}/messages"]


def panel_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name.startswith("fluxer_panel")]


def field_value(embed: dict[str, Any], name: str) -> str:
    [value] = [f["value"] for f in embed["fields"] if f["name"] == name]
    return value


def play_now_message_ids(state: FakeFluxerState, channel: str) -> list[str]:
    return [mid for (cid, mid) in state.messages if cid == channel]


def seeded(state: FakeFluxerState, channel: str, message: str) -> set[str]:
    return set(state.reactions.get((channel, message), {}))


# Posting ----------------------------------------------------------------------------------------------

async def test_a_card_with_its_own_play_now_channel_posts_there_with_the_join_details(harness) -> None:
    h = await harness()
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)
    await h.presses.on_reaction(play_press())
    [post] = channel_posts(h.state)
    assert post.path == f"/api/v1/channels/{PLAY_CH}/messages"
    assert post.payload["content"].startswith(text.PLAY_NOW_CONTENT.format(card=render.escape(CARD_TITLE)))
    [embed] = post.payload["embeds"]
    assert field_value(embed, text.FIELD_JOIN) == JOIN


async def test_without_a_card_channel_it_posts_in_the_configured_channel(harness) -> None:
    h = await harness(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    await h.presses.on_reaction(press_event(PLAY_NOW_EMOJI, message=SECOND_MSG))
    [post] = channel_posts(h.state)
    assert post.path == f"/api/v1/channels/{CONFIG_PLAY}/messages"
    assert post.payload["content"] == text.PLAY_NOW_CONTENT.format(card=render.escape(SECOND_TITLE))


async def test_a_press_on_a_home_copy_posts_in_the_copy_channel(harness) -> None:
    h = await harness()
    await h.presses.on_reaction(play_press(channel=COPY, message=COPY_MSG))
    [post] = channel_posts(h.state)
    assert post.path == f"/api/v1/channels/{COPY}/messages"


async def test_a_press_on_a_home_copy_posts_in_both_the_copy_and_the_play_now_channel(harness) -> None:
    h = await harness(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    await h.presses.on_reaction(play_press(channel=COPY, message=COPY_MSG))
    posts = {r.path: r for r in channel_posts(h.state)}
    assert set(posts) == {f"/api/v1/channels/{COPY}/messages", f"/api/v1/channels/{CONFIG_PLAY}/messages"}
    [copy_mid] = play_now_message_ids(h.state, COPY)
    [lfg_mid] = play_now_message_ids(h.state, CONFIG_PLAY)
    assert await h.db.run(_play_now_rows) == 2
    assert seeded(h.state, COPY, copy_mid) == {PLAY_NOW_EMOJI}
    assert seeded(h.state, CONFIG_PLAY, lfg_mid) == {PLAY_NOW_EMOJI}
    for post in posts.values():
        assert post.payload["allowed_mentions"] == {"parse": [], "roles": [ALPHA_ROLE], "replied_user": False}


async def test_a_home_copy_press_posts_to_the_cards_own_play_now_channel_as_well(harness) -> None:
    h = await harness(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)
    await h.presses.on_reaction(play_press(channel=COPY, message=COPY_MSG))
    assert {r.path for r in channel_posts(h.state)} == {f"/api/v1/channels/{COPY}/messages",
                                                        f"/api/v1/channels/{PLAY_CH}/messages"}
    assert play_now_message_ids(h.state, CONFIG_PLAY) == []


async def test_a_home_copy_press_with_no_play_now_target_posts_once_in_the_copy_channel(harness) -> None:
    h = await harness()
    await h.presses.on_reaction(play_press(channel=COPY, message=COPY_MSG))
    assert [r.path for r in channel_posts(h.state)] == [f"/api/v1/channels/{COPY}/messages"]
    assert await h.db.run(_play_now_rows) == 1


async def test_a_home_copy_press_whose_copy_channel_is_the_play_now_channel_posts_once(harness) -> None:
    h = await harness()
    await set_card_play_channel(h, CARD_ID, GUILD, COPY)
    await h.presses.on_reaction(play_press(channel=COPY, message=COPY_MSG))
    assert [r.path for r in channel_posts(h.state)] == [f"/api/v1/channels/{COPY}/messages"]
    assert await h.db.run(_play_now_rows) == 1


async def test_a_home_copy_press_with_the_configured_channel_as_copy_channel_posts_once(harness) -> None:
    h = await harness(PLAY_NOW_CHANNEL_ID=COPY)
    await h.presses.on_reaction(play_press(channel=COPY, message=COPY_MSG))
    assert [r.path for r in channel_posts(h.state)] == [f"/api/v1/channels/{COPY}/messages"]


async def test_a_press_on_the_card_posts_once_in_its_target_even_with_a_copy_and_a_configured_channel(harness) -> None:
    h = await harness(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)
    await h.presses.on_reaction(play_press())
    assert [r.path for r in channel_posts(h.state)] == [f"/api/v1/channels/{PLAY_CH}/messages"]


async def test_a_failed_lfg_post_keeps_the_copy_post_and_logs_the_status_only(harness, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    h.state.overrides[f"POST /api/v1/channels/{CONFIG_PLAY}/messages"] = Override(403, FORBIDDEN)
    await h.presses.on_reaction(play_press(channel=COPY, message=COPY_MSG))
    assert [r.path for r in channel_posts(h.state)] == [f"/api/v1/channels/{COPY}/messages",
                                                        f"/api/v1/channels/{CONFIG_PLAY}/messages"]
    [copy_mid] = play_now_message_ids(h.state, COPY)
    assert seeded(h.state, COPY, copy_mid) == {PLAY_NOW_EMOJI}
    assert await h.db.run(_play_now_rows) == 1
    assert dm_channel_texts(h.state, PRESSER) == []
    [failed] = [r for r in panel_records(caplog) if r.getMessage() == "play_now_failed"]
    assert getattr(failed, FIELDS_ATTR) == {"card_id": CARD_ID, "channel_id": CONFIG_PLAY, "status": 403}


async def test_a_failed_copy_post_keeps_the_lfg_post_and_logs_the_status_only(harness, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    h.state.overrides[f"POST /api/v1/channels/{COPY}/messages"] = Override(403, FORBIDDEN)
    await h.presses.on_reaction(play_press(channel=COPY, message=COPY_MSG))
    assert [r.path for r in channel_posts(h.state)] == [f"/api/v1/channels/{COPY}/messages",
                                                        f"/api/v1/channels/{CONFIG_PLAY}/messages"]
    [lfg_mid] = play_now_message_ids(h.state, CONFIG_PLAY)
    assert seeded(h.state, CONFIG_PLAY, lfg_mid) == {PLAY_NOW_EMOJI}
    assert await h.db.run(_play_now_rows) == 1
    assert dm_channel_texts(h.state, PRESSER) == []
    [failed] = [r for r in panel_records(caplog) if r.getMessage() == "play_now_failed"]
    assert getattr(failed, FIELDS_ATTR) == {"card_id": CARD_ID, "channel_id": COPY, "status": 403}


async def test_when_both_home_posts_fail_the_press_is_unavailable_by_dm(harness) -> None:
    h = await harness(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    h.state.overrides[f"POST /api/v1/channels/{COPY}/messages"] = Override(403, FORBIDDEN)
    h.state.overrides[f"POST /api/v1/channels/{CONFIG_PLAY}/messages"] = Override(403, FORBIDDEN)
    await h.presses.on_reaction(play_press(channel=COPY, message=COPY_MSG))
    assert dm_channel_texts(h.state, PRESSER) == [text.PLAY_NOW_UNAVAILABLE.format(card=render.escape(CARD_TITLE))]
    assert await h.db.run(_play_now_rows) == 0


async def test_a_store_failure_on_one_of_two_copy_posts_keeps_the_other_and_tells_no_one(harness, monkeypatch,
                                                                                         caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    real_add = play_now.add
    calls: list[int] = []

    def fail_first_store(conn: Any, **kwargs: Any) -> int:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real_add(conn, **kwargs)

    monkeypatch.setattr(play_now, "add", fail_first_store)
    await h.presses.on_reaction(play_press(channel=COPY, message=COPY_MSG))

    assert [r.path for r in channel_posts(h.state)] == [f"/api/v1/channels/{COPY}/messages",
                                                        f"/api/v1/channels/{CONFIG_PLAY}/messages"]
    [copy_mid] = play_now_message_ids(h.state, COPY)
    [lfg_mid] = play_now_message_ids(h.state, CONFIG_PLAY)
    assert (COPY, copy_mid) in h.state.deleted
    assert (CONFIG_PLAY, lfg_mid) not in h.state.deleted
    assert seeded(h.state, COPY, copy_mid) == set()
    assert seeded(h.state, CONFIG_PLAY, lfg_mid) == {PLAY_NOW_EMOJI}
    assert await h.db.run(_play_now_rows) == 1
    assert dm_channel_texts(h.state, PRESSER) == []
    assert "play_now_store_failed" in event_names(caplog)
    [failed] = [r for r in panel_records(caplog) if r.getMessage() == "play_now_failed"]
    assert getattr(failed, FIELDS_ATTR) == {"card_id": CARD_ID, "channel_id": COPY, "reason": "OperationalError"}


async def test_the_follow_role_is_the_only_mention_and_allowed_mentions_names_it(harness) -> None:
    h = await harness()
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)
    await h.presses.on_reaction(play_press())
    [post] = channel_posts(h.state)
    assert post.payload["allowed_mentions"] == {"parse": [], "roles": [ALPHA_ROLE], "replied_user": False}
    assert post.payload["content"].endswith(f" <@&{ALPHA_ROLE}>")
    assert post.payload["content"].count("<@") == 1


async def test_with_no_follow_role_nothing_is_mentioned(harness) -> None:
    h = await harness(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    await h.presses.on_reaction(press_event(PLAY_NOW_EMOJI, message=SECOND_MSG))
    [post] = channel_posts(h.state)
    assert post.payload["allowed_mentions"] == {"parse": []}
    assert "<@" not in post.payload["content"]


async def test_the_follow_role_is_not_mentioned_in_another_community(harness) -> None:
    h = await harness()
    await set_card_play_channel(h, CARD_ID, OTHER_GUILD, FAR_PLAY)
    await h.presses.on_reaction(play_press())
    [post] = channel_posts(h.state)
    assert post.path == f"/api/v1/channels/{FAR_PLAY}/messages"
    assert post.payload["allowed_mentions"] == {"parse": []}
    assert "<@" not in post.payload["content"]


# Refusals ---------------------------------------------------------------------------------------------

async def test_with_no_channel_set_the_press_is_refused_by_dm_and_nothing_is_posted(harness) -> None:
    h = await harness()
    await h.presses.on_reaction(press_event(PLAY_NOW_EMOJI, message=SECOND_MSG))
    assert channel_posts(h.state) == []
    assert dm_channel_texts(h.state, PRESSER) == [text.PLAY_NOW_NO_CHANNEL.format(card=render.escape(SECOND_TITLE))]


async def test_a_copy_outside_the_home_community_is_refused_by_dm_and_nothing_is_posted(harness) -> None:
    h = await harness()
    await h.presses.on_reaction(play_press(channel=FAR_COPY, message=FAR_MSG, guild=OTHER_GUILD))
    assert channel_posts(h.state) == []
    assert dm_channel_texts(h.state, PRESSER) == [text.PLAY_NOW_ELSEWHERE]


async def test_an_unreachable_configured_channel_is_unavailable_by_dm(harness, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness(PLAY_NOW_CHANNEL_ID=MISSING)
    await h.presses.on_reaction(press_event(PLAY_NOW_EMOJI, message=SECOND_MSG))
    assert channel_posts(h.state) == []
    assert dm_channel_texts(h.state, PRESSER) == [text.PLAY_NOW_UNAVAILABLE.format(card=render.escape(SECOND_TITLE))]
    [failed] = [r for r in panel_records(caplog) if r.getMessage() == "play_now_failed"]
    assert getattr(failed, FIELDS_ATTR)["card_id"] == SECOND_ID
    assert getattr(failed, FIELDS_ATTR)["channel_id"] == MISSING


async def test_a_refused_post_is_unavailable_by_dm(harness) -> None:
    h = await harness(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    h.state.overrides[f"POST /api/v1/channels/{CONFIG_PLAY}/messages"] = Override(403, FORBIDDEN)
    await h.presses.on_reaction(press_event(PLAY_NOW_EMOJI, message=SECOND_MSG))
    assert dm_channel_texts(h.state, PRESSER) == [text.PLAY_NOW_UNAVAILABLE.format(card=render.escape(SECOND_TITLE))]
    assert await h.db.run(_play_now_rows) == 0


async def test_the_games_channel_is_refused_before_any_rest_call(harness) -> None:
    h = await harness()
    with pytest.raises(GamesChannelRefused):
        await h.interactions.send_play_now(GAMES, {"content": "x", "embeds": []}, None)
    assert channel_posts(h.state) == []


async def test_a_play_now_channel_set_to_the_games_channel_posts_nothing_there(harness, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    await set_card_play_channel(h, CARD_ID, GUILD, GAMES)
    await h.presses.on_reaction(play_press())
    assert channel_posts(h.state) == []
    assert "play_now_refused" in [record.getMessage() for record in panel_records(caplog)]


# Cooldown ---------------------------------------------------------------------------------------------

async def test_a_press_within_the_cooldown_posts_nothing_and_links_the_first_message(harness) -> None:
    h = await harness()
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)
    h.clock.now = 100
    await h.presses.on_reaction(play_press(user=PRESSER))
    [first] = play_now_message_ids(h.state, PLAY_CH)
    h.clock.now = 200
    await h.presses.on_reaction(play_press(user=PRESSER_2))
    assert play_now_message_ids(h.state, PLAY_CH) == [first]
    assert len(channel_posts(h.state)) == 1
    link = render.jump_link(text.JUMP_LABEL_MESSAGE, JUMP_URL, GUILD, PLAY_CH, first)
    assert dm_channel_texts(h.state, PRESSER_2) == [
        text.PLAY_NOW_COOLDOWN.format(card=render.escape(CARD_TITLE), link=link)]


async def test_a_press_at_the_end_of_the_cooldown_posts_again(harness) -> None:
    h = await harness()
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)
    h.clock.now = 100
    await h.presses.on_reaction(play_press(user=PRESSER))
    h.clock.now = 100 + h.config.play_now_cooldown_seconds
    await h.presses.on_reaction(play_press(user=PRESSER_2))
    assert len(channel_posts(h.state)) == 2


async def test_both_messages_of_one_copy_press_count_as_one_press_for_the_cooldown(harness) -> None:
    h = await harness(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    h.clock.now = 100
    await h.presses.on_reaction(play_press(channel=COPY, message=COPY_MSG, user=PRESSER))
    [lfg_mid] = play_now_message_ids(h.state, CONFIG_PLAY)
    h.clock.now = 200
    await h.presses.on_reaction(play_press(user=PRESSER_2))
    assert len(channel_posts(h.state)) == 2
    link = render.jump_link(text.JUMP_LABEL_MESSAGE, JUMP_URL, GUILD, CONFIG_PLAY, lfg_mid)
    assert dm_channel_texts(h.state, PRESSER_2) == [
        text.PLAY_NOW_COOLDOWN.format(card=render.escape(CARD_TITLE), link=link)]


async def test_a_copy_press_inside_the_cooldown_of_a_card_press_posts_nothing(harness) -> None:
    h = await harness(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    h.clock.now = 100
    await h.presses.on_reaction(play_press(user=PRESSER))
    h.clock.now = 200
    await h.presses.on_reaction(play_press(channel=COPY, message=COPY_MSG, user=PRESSER_2))
    assert len(channel_posts(h.state)) == 1
    assert play_now_message_ids(h.state, COPY) == []


async def test_without_an_instance_url_the_cooldown_text_has_no_link(harness) -> None:
    h = await harness(instance_url=False)
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)
    await h.presses.on_reaction(play_press(user=PRESSER))
    await h.presses.on_reaction(play_press(user=PRESSER_2))
    assert len(channel_posts(h.state)) == 1
    assert dm_channel_texts(h.state, PRESSER_2) == [
        text.PLAY_NOW_COOLDOWN_NO_LINK.format(card=render.escape(CARD_TITLE))]


async def test_concurrent_home_copy_presses_post_twice_once_and_the_loser_is_cooled_down(harness) -> None:
    h = await harness(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    h.clock.now = 100
    await asyncio.gather(
        h.presses.on_reaction(play_press(channel=COPY, message=COPY_MSG, user=PRESSER)),
        h.presses.on_reaction(play_press(channel=COPY, message=COPY_MSG, user=PRESSER_2)),
    )
    assert len(channel_posts(h.state)) == 2
    assert await h.db.run(_play_now_rows) == 2
    [lfg_mid] = play_now_message_ids(h.state, CONFIG_PLAY)
    link = render.jump_link(text.JUMP_LABEL_MESSAGE, JUMP_URL, GUILD, CONFIG_PLAY, lfg_mid)
    cooled = text.PLAY_NOW_COOLDOWN.format(card=render.escape(CARD_TITLE), link=link)
    # Whichever presser reached the card lock second is the one told about the cooldown; the other gets no DM.
    dms = {user: dm_channel_texts(h.state, user) for user in (PRESSER, PRESSER_2)}
    assert sorted(len(texts) for texts in dms.values()) == [0, 1]
    assert [cooled] in dms.values()


# Seeding and wake -------------------------------------------------------------------------------------

async def test_the_posted_message_is_seeded_with_me_too_only_when_not_asleep(harness) -> None:
    h = await harness()
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)
    await h.presses.on_reaction(play_press())
    [mid] = play_now_message_ids(h.state, PLAY_CH)
    assert seeded(h.state, PLAY_CH, mid) == {PLAY_NOW_EMOJI}


async def test_the_posted_message_also_gets_wake_when_the_card_is_asleep(harness) -> None:
    asleep = {CARD_ID: CardStatus(StatusKind.ASLEEP)}
    h = await harness(status_of=lambda card_id: asleep.get(card_id))
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)
    await h.presses.on_reaction(play_press())
    [mid] = play_now_message_ids(h.state, PLAY_CH)
    assert seeded(h.state, PLAY_CH, mid) == {PLAY_NOW_EMOJI, WAKE_EMOJI}


async def test_a_wake_press_on_the_play_now_message_wakes_the_card(harness) -> None:
    h = await harness()
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)
    await h.presses.on_reaction(play_press())
    [mid] = play_now_message_ids(h.state, PLAY_CH)
    await h.presses.on_reaction(press_event(WAKE_EMOJI, user=WAKER, channel=PLAY_CH, message=mid))
    assert h.wake.presses == [(CARD_ID, WAKER)]


def _play_now_rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM play_now").fetchone()[0]


# Me-too counting and expiry ---------------------------------------------------------------------------

REACTOR_A = "6101"
REACTOR_B = "6102"
PLAY_PATH = f"/api/v1/channels/{PLAY_CH}/messages"


async def post_play_now(h: Harness, *, user: str = PRESSER) -> str:
    """Posts one play-now message from a press on the card, in PLAY_CH, and returns its message id."""
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)
    await h.presses.on_reaction(play_press(user=user))
    [mid] = play_now_message_ids(h.state, PLAY_CH)
    return mid


def set_reactors(state: FakeFluxerState, message: str, users: list[str]) -> None:
    """Puts these play-now reactors on the play-now message, after the bot's own."""
    state.reactions[(PLAY_CH, message)][PLAY_NOW_EMOJI] = [BOT_ID, *users]


def edits_to(state: FakeFluxerState, message: str) -> list[Recorded]:
    return [r for r in state.requests if r.method == "PATCH" and r.path == f"{PLAY_PATH}/{message}"]


def deletes_all_reactions(state: FakeFluxerState, message: str) -> list[Recorded]:
    return [r for r in state.requests if r.method == "DELETE" and r.path == f"{PLAY_PATH}/{message}/reactions"]


def me_too(user: str, message: str) -> events.ReactionAdd:
    return press_event(PLAY_NOW_EMOJI, user=user, channel=PLAY_CH, message=message, guild=GUILD)


def me_too_removed(user: str, message: str) -> events.ReactionRemove:
    return events.ReactionRemove(user_id=user, channel_id=PLAY_CH, message_id=message, guild_id=GUILD,
                                 emoji=events.Emoji(name=PLAY_NOW_EMOJI, id=None), member_role_ids=frozenset(),
                                 user_is_bot=False)


def status_field(recorded: Recorded) -> str:
    return field_value(recorded.payload["embeds"][0], text.FIELD_STATUS)


class ProbePacer(ChannelPacer):
    """Records whether the card's lock is held each time a reaction call waits for its budget."""

    def __init__(self, card_locks: CardLocks, card_id: int) -> None:
        super().__init__(max_calls=1000)
        self._card_locks = card_locks   # not `_locks`: ChannelPacer keeps its own per-channel locks under that name
        self._card_id = card_id
        self.held: list[bool] = []

    async def wait(self, channel_id: str) -> None:
        self.held.append(self._card_locks.get(self._card_id).locked())
        await super().wait(channel_id)


async def test_a_me_too_add_counts_the_distinct_non_bot_reactors_plus_the_presser(harness) -> None:
    h = await harness()
    mid = await post_play_now(h)
    set_reactors(h.state, mid, [REACTOR_A, REACTOR_B])
    await h.presses.on_reaction(me_too(REACTOR_B, mid))
    [edit] = edits_to(h.state, mid)
    assert status_field(edit) == text.PLAY_NOW_PLAYING.format(n=3)
    assert edit.payload["allowed_mentions"] == {"parse": []}


async def test_a_me_too_remove_lowers_the_count(harness) -> None:
    h = await harness()
    mid = await post_play_now(h)
    set_reactors(h.state, mid, [REACTOR_A, REACTOR_B])
    await h.presses.on_reaction(me_too(REACTOR_B, mid))
    set_reactors(h.state, mid, [REACTOR_A])
    await h.presses.on_reaction(me_too_removed(REACTOR_B, mid))
    assert status_field(edits_to(h.state, mid)[-1]) == text.PLAY_NOW_PLAYING.format(n=2)


async def test_a_me_too_never_removes_a_reaction_and_no_user_reaction_is_deleted(harness) -> None:
    h = await harness()
    mid = await post_play_now(h)
    set_reactors(h.state, mid, [REACTOR_A, REACTOR_B])
    before = len(h.state.requests)     # the presser's own tap on the card was removed during setup, not by this event
    await h.presses.on_reaction(me_too(REACTOR_B, mid))
    assert [r for r in h.state.requests[before:] if r.method == "DELETE"] == []


async def test_a_bot_reactor_is_not_counted(harness) -> None:
    h = await harness()
    mid = await post_play_now(h)
    set_reactors(h.state, mid, [REACTOR_A])     # the bot's own reaction is also on the message
    await h.presses.on_reaction(me_too(REACTOR_A, mid))
    assert status_field(edits_to(h.state, mid)[-1]) == text.PLAY_NOW_PLAYING.format(n=2)


async def test_the_count_pages_past_one_hundred_reactors_and_counts_the_presser_once(harness) -> None:
    h = await harness()
    mid = await post_play_now(h)
    crowd = [str(7000 + i) for i in range(149)] + [PRESSER]     # 150 reactors, the presser among them
    set_reactors(h.state, mid, crowd)
    await h.presses.on_reaction(me_too(crowd[0], mid))
    assert status_field(edits_to(h.state, mid)[-1]) == text.PLAY_NOW_PLAYING.format(n=150)


async def test_the_reactor_reads_and_the_edit_are_not_held_under_the_card_lock(harness, ) -> None:
    locks = CardLocks()
    pacer = ProbePacer(locks, CARD_ID)
    h = await harness(pacer=pacer, locks=locks)
    mid = await post_play_now(h)
    pacer.held.clear()
    set_reactors(h.state, mid, [REACTOR_A])
    await h.presses.on_reaction(me_too(REACTOR_A, mid))
    assert pacer.held and not any(pacer.held)


async def test_an_older_me_too_recount_that_finishes_last_does_not_overwrite_a_newer_count(harness, monkeypatch) -> None:
    """Two Me Too recounts overlap. The older one reads its reactors first and finishes last, so its edit must be
    skipped: the newer count stands."""
    h = await harness()
    mid = await post_play_now(h)
    real_count = h.interactions._count_playing
    reading = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def interleaved(pn: play_now.PlayNow) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            count = await real_count(pn)     # the older recount reads the reactors as they are: 2
            reading.set()
            await release.wait()             # and finishes only after the newer recount has been applied
            return count
        return await real_count(pn)

    monkeypatch.setattr(h.interactions, "_count_playing", interleaved)
    set_reactors(h.state, mid, [REACTOR_A])
    older = asyncio.create_task(h.presses.on_reaction(me_too(REACTOR_A, mid)))
    await asyncio.wait_for(reading.wait(), BOUND)
    set_reactors(h.state, mid, [REACTOR_A, REACTOR_B])
    await h.presses.on_reaction(me_too(REACTOR_B, mid))      # the newer recount: 3
    release.set()
    await asyncio.wait_for(older, BOUND)

    assert [status_field(edit) for edit in edits_to(h.state, mid)] == [text.PLAY_NOW_PLAYING.format(n=3)]


async def test_an_expired_message_leaves_no_recount_ticket_behind(harness) -> None:
    h = await harness()
    h.clock.now = 100
    mid = await post_play_now(h)           # expires at 100 + 3600
    set_reactors(h.state, mid, [REACTOR_A])
    await h.presses.on_reaction(me_too(REACTOR_A, mid))
    assert mid in h.interactions._recount_applied
    h.clock.now = 3700
    await h.interactions.expire_play_now()
    assert mid not in h.interactions._recount_applied


async def test_a_play_now_message_ends_at_its_expiry_with_one_edit_and_one_bulk_removal(harness) -> None:
    h = await harness()
    h.clock.now = 100
    mid = await post_play_now(h)           # expires at 100 + PLAY_NOW_EXPIRY_SECONDS (3600)
    h.clock.now = 3699
    await h.interactions.expire_play_now()
    assert edits_to(h.state, mid) == [] and deletes_all_reactions(h.state, mid) == []

    h.clock.now = 3700
    await h.interactions.expire_play_now()
    [ended] = edits_to(h.state, mid)
    assert ended.payload["content"] == text.PLAY_NOW_ENDED_CONTENT.format(card=render.escape(CARD_TITLE))
    assert ended.payload["allowed_mentions"] == {"parse": []}
    assert len(deletes_all_reactions(h.state, mid)) == 1
    assert (PLAY_CH, mid) not in h.state.reactions


async def test_a_later_me_too_on_an_ended_message_makes_no_request(harness) -> None:
    h = await harness()
    h.clock.now = 100
    mid = await post_play_now(h)
    h.clock.now = 3700
    await h.interactions.expire_play_now()
    before = len(h.state.requests)
    await h.presses.on_reaction(me_too(REACTOR_A, mid))
    await h.presses.on_reaction(me_too_removed(REACTOR_A, mid))
    assert len(h.state.requests) == before


async def test_the_ended_message_keeps_the_last_count(harness) -> None:
    h = await harness()
    h.clock.now = 100
    mid = await post_play_now(h)
    set_reactors(h.state, mid, [REACTOR_A, REACTOR_B])
    await h.presses.on_reaction(me_too(REACTOR_B, mid))
    h.clock.now = 3700
    await h.interactions.expire_play_now()
    assert status_field(edits_to(h.state, mid)[-1]) == text.PLAY_NOW_PLAYING.format(n=3)


async def test_a_failed_end_edit_is_logged_and_the_reactions_are_still_removed(harness, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    h.clock.now = 100
    mid = await post_play_now(h)
    h.state.overrides[f"PATCH {PLAY_PATH}/{mid}"] = Override(404, b'{"code":"UNKNOWN_MESSAGE","message":"gone"}')
    h.clock.now = 3700
    await h.interactions.expire_play_now()
    assert len(deletes_all_reactions(h.state, mid)) == 1
    assert "play_now_end_failed" in [record.getMessage() for record in panel_records(caplog)]


async def test_the_bulk_removal_is_not_held_under_the_card_lock(harness) -> None:
    locks = CardLocks()
    pacer = ProbePacer(locks, CARD_ID)
    h = await harness(pacer=pacer, locks=locks)
    h.clock.now = 100
    await post_play_now(h)
    pacer.held.clear()
    h.clock.now = 3700
    await h.interactions.expire_play_now()
    assert pacer.held and not any(pacer.held)


# Reactor counting: paging, restarts and the failure paths ----------------------------------------------

def event_names(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in panel_records(caplog)]


def page_source(rest: FluxerREST, respond: Callable[[str | None], ReactionUsersPage],
                monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Replaces the reactor-page call with `respond(after)`. Returns the list of `after` cursors it was asked for."""
    asked: list[str | None] = []

    async def page(channel_id: str, message_id: str, emoji: str, *, limit: int = 100,
                   after: str | None = None) -> ReactionUsersPage:
        asked.append(after)
        return respond(after)

    monkeypatch.setattr(rest, "get_reaction_users_page", page)
    return asked


def restarted(h: Harness) -> Interactions:
    """A new Interactions over the same database and client: no in-memory state carried over from before."""
    old = h.interactions
    return Interactions(db=old._db, rest=old._rest, config=old._config, locks=old._locks, games_guild=old._games_guild,
                        seeder=old._seeder, pacer=old._pacer, identity=old._identity, roles=old._roles,
                        clock=h.clock, wake=old._wake)


def _live_rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM play_now WHERE state = 'live'").fetchone()[0]


def ended_status_fields(recorded: Recorded) -> list[str]:
    return [f["name"] for f in recorded.payload["embeds"][0]["fields"]]


def crowd(start: int, count: int) -> list[dict[str, Any]]:
    return [{"id": str(start + i), "bot": False} for i in range(count)]


async def test_after_a_restart_the_ended_message_counts_its_reactors(harness) -> None:
    h = await harness()
    h.clock.now = 100
    mid = await post_play_now(h)
    set_reactors(h.state, mid, [REACTOR_A, REACTOR_B])
    h.clock.now = 3700
    await restarted(h).expire_play_now()
    [ended] = edits_to(h.state, mid)
    assert status_field(ended) == text.PLAY_NOW_PLAYING.format(n=3)
    assert len(deletes_all_reactions(h.state, mid)) == 1


async def test_the_count_at_expiry_ignores_the_bot_and_counts_the_presser_once(harness) -> None:
    h = await harness()
    h.clock.now = 100
    mid = await post_play_now(h)
    set_reactors(h.state, mid, [REACTOR_A, PRESSER])
    h.clock.now = 3700
    await h.interactions.expire_play_now()
    assert status_field(edits_to(h.state, mid)[-1]) == text.PLAY_NOW_PLAYING.format(n=2)


async def test_a_reactor_list_that_ignores_after_terminates_with_one_hundred_and_one(harness, monkeypatch) -> None:
    h = await harness()
    h.clock.now = 100
    mid = await post_play_now(h)
    full = crowd(7000, 100)
    asked = page_source(h.interactions._rest, lambda after: ReactionUsersPage(full, True, full[-1]["id"]), monkeypatch)
    h.clock.now = 3700
    await h.interactions.expire_play_now()
    assert asked == [None, "7099"]
    assert status_field(edits_to(h.state, mid)[-1]) == text.PLAY_NOW_PLAYING.format(n=101)


async def test_a_short_page_with_has_more_continues_to_the_next_page(harness, monkeypatch) -> None:
    h = await harness()
    h.clock.now = 100
    mid = await post_play_now(h)
    first = ReactionUsersPage(crowd(7001, 3), True, "7003")
    second = ReactionUsersPage(crowd(7004, 2), False, None)
    asked = page_source(h.interactions._rest, lambda after: first if after is None else second, monkeypatch)
    h.clock.now = 3700
    await h.interactions.expire_play_now()
    assert asked == [None, "7003"]
    assert status_field(edits_to(h.state, mid)[-1]) == text.PLAY_NOW_PLAYING.format(n=6)


async def test_a_cursor_that_does_not_advance_stops_the_count_and_is_logged(harness, monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    h.clock.now = 100
    mid = await post_play_now(h)
    responses = {None: ReactionUsersPage(crowd(7001, 2), True, "7002"),
                 "7002": ReactionUsersPage(crowd(7003, 1), True, "7002")}
    asked = page_source(h.interactions._rest, lambda after: responses[after], monkeypatch)
    h.clock.now = 3700
    await h.interactions.expire_play_now()
    assert asked == [None, "7002"]
    assert status_field(edits_to(h.state, mid)[-1]) == text.PLAY_NOW_PLAYING.format(n=4)
    assert "play_now_count_stalled" in event_names(caplog)


async def test_a_page_with_no_new_user_stops_the_count_even_when_the_cursor_moves(harness, monkeypatch,
                                                                                  caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    h.clock.now = 100
    mid = await post_play_now(h)
    responses = {None: ReactionUsersPage(crowd(7001, 2), True, "7002"),
                 "7002": ReactionUsersPage(crowd(7001, 2), True, "7099")}
    asked = page_source(h.interactions._rest, lambda after: responses[after], monkeypatch)
    h.clock.now = 3700
    await h.interactions.expire_play_now()
    assert asked == [None, "7002"]
    assert status_field(edits_to(h.state, mid)[-1]) == text.PLAY_NOW_PLAYING.format(n=3)
    assert "play_now_count_stalled" in event_names(caplog)


async def test_the_page_cap_stops_the_count_and_logs_the_card_id_only(harness, monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    h.clock.now = 100
    mid = await post_play_now(h)
    counter = itertools.count(7000)

    def endless(after: str | None) -> ReactionUsersPage:
        user_id = str(next(counter))
        return ReactionUsersPage([{"id": user_id, "bot": False}], True, user_id)

    asked = page_source(h.interactions._rest, endless, monkeypatch)
    h.clock.now = 3700
    await h.interactions.expire_play_now()
    assert len(asked) == 20
    assert status_field(edits_to(h.state, mid)[-1]) == text.PLAY_NOW_PLAYING.format(n=21)
    [truncated] = [r for r in panel_records(caplog) if r.getMessage() == "play_now_count_truncated"]
    assert set(getattr(truncated, FIELDS_ATTR)) == {"card_id"}


async def test_a_failed_recount_at_expiry_omits_the_count_and_still_ends_the_message(harness, monkeypatch,
                                                                                      caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    h.clock.now = 100
    mid = await post_play_now(h)

    async def unreachable(*args: Any, **kwargs: Any) -> ReactionUsersPage:
        raise aiohttp.ClientError("dropped")

    monkeypatch.setattr(h.interactions._rest, "get_reaction_users_page", unreachable)
    h.clock.now = 3700
    await h.interactions.expire_play_now()
    [ended] = edits_to(h.state, mid)
    assert text.FIELD_STATUS not in ended_status_fields(ended)
    assert ended.payload["content"] == text.PLAY_NOW_ENDED_CONTENT.format(card=render.escape(CARD_TITLE))
    assert len(deletes_all_reactions(h.state, mid)) == 1
    assert "play_now_count_failed" in event_names(caplog)


async def test_a_failed_recount_at_expiry_logs_the_http_status_and_no_reason(harness, monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    h.clock.now = 100
    mid = await post_play_now(h)

    async def refused(*args: Any, **kwargs: Any) -> ReactionUsersPage:
        raise FluxerHTTPError(500, None, "server error")

    monkeypatch.setattr(h.interactions._rest, "get_reaction_users_page", refused)
    h.clock.now = 3700
    await h.interactions.expire_play_now()
    [ended] = edits_to(h.state, mid)
    assert text.FIELD_STATUS not in ended_status_fields(ended)
    [failed] = [r for r in panel_records(caplog) if r.getMessage() == "play_now_count_failed"]
    assert getattr(failed, FIELDS_ATTR)["status"] == 500
    assert "reason" not in getattr(failed, FIELDS_ATTR)


async def test_a_failed_me_too_recount_logs_the_http_status_and_edits_nothing(harness, monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    mid = await post_play_now(h)
    set_reactors(h.state, mid, [REACTOR_A])

    async def refused(*args: Any, **kwargs: Any) -> ReactionUsersPage:
        raise FluxerHTTPError(503, None, "unavailable")

    monkeypatch.setattr(h.interactions._rest, "get_reaction_users_page", refused)
    await h.presses.on_reaction(me_too(REACTOR_A, mid))
    assert edits_to(h.state, mid) == []
    [failed] = [r for r in panel_records(caplog) if r.getMessage() == "play_now_count_failed"]
    assert getattr(failed, FIELDS_ATTR)["status"] == 503
    assert "reason" not in getattr(failed, FIELDS_ATTR)


async def test_a_transport_error_on_the_ended_edit_still_removes_reactions_and_ends_the_next_message(
        harness, monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    h.clock.now = 100
    first = await post_play_now(h)                                  # expires at 3700
    h.clock.now = 100 + h.config.play_now_cooldown_seconds          # the cooldown has passed: a second post
    await h.presses.on_reaction(play_press(user=PRESSER_2))
    second = next(m for m in play_now_message_ids(h.state, PLAY_CH) if m != first)   # expires at 5500
    real_edit = h.interactions._rest.edit_message

    async def dropped_for_first(channel_id: str, message_id: str, **kwargs: Any) -> Any:
        if message_id == first:
            raise aiohttp.ClientError("dropped")
        return await real_edit(channel_id, message_id, **kwargs)

    monkeypatch.setattr(h.interactions._rest, "edit_message", dropped_for_first)
    h.clock.now = 5500
    await h.interactions.expire_play_now()
    assert len(deletes_all_reactions(h.state, first)) == 1
    assert len(deletes_all_reactions(h.state, second)) == 1
    [ended] = edits_to(h.state, second)
    assert ended.payload["content"] == text.PLAY_NOW_ENDED_CONTENT.format(card=render.escape(CARD_TITLE))
    assert await h.db.run(_live_rows) == 0
    assert "play_now_end_failed" in event_names(caplog)


async def test_a_transport_error_on_the_me_too_edit_is_logged_and_not_raised(harness, monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    mid = await post_play_now(h)
    set_reactors(h.state, mid, [REACTOR_A])

    async def dropped(*args: Any, **kwargs: Any) -> Any:
        raise aiohttp.ClientError("dropped")

    monkeypatch.setattr(h.interactions._rest, "edit_message", dropped)
    await h.presses.on_reaction(me_too(REACTOR_A, mid))
    assert edits_to(h.state, mid) == []
    assert "play_now_edit_failed" in event_names(caplog)


async def test_an_unexpected_failure_in_the_me_too_handler_is_logged_and_not_raised(harness, monkeypatch,
                                                                                    caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    mid = await post_play_now(h)

    async def broken(card_id: int, surface: Any, user_id: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(h.interactions, "me_too_changed", broken)
    await h.presses.on_reaction(me_too(REACTOR_A, mid))
    assert "play_now_edit_failed" not in event_names(caplog)
    [failed] = [r for r in panel_records(caplog) if r.getMessage() == "play_now_me_too_failed"]
    assert getattr(failed, FIELDS_ATTR)["reason"] == "RuntimeError"


async def test_a_store_failure_after_the_send_deletes_the_message_and_tells_the_presser(harness, monkeypatch,
                                                                                         caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)

    def refused(*args: Any, **kwargs: Any) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(play_now, "add", refused)
    await h.presses.on_reaction(play_press())
    [mid] = play_now_message_ids(h.state, PLAY_CH)
    assert len(deletes_all_reactions(h.state, mid)) == 0
    assert [r for r in h.state.requests if r.method == "DELETE" and r.path == f"{PLAY_PATH}/{mid}"]
    assert (PLAY_CH, mid) in h.state.deleted
    assert seeded(h.state, PLAY_CH, mid) == set()
    assert dm_channel_texts(h.state, PRESSER) == [text.PRESS_FAILED]
    assert await h.db.run(_play_now_rows) == 0
    assert "play_now_store_failed" in event_names(caplog)


async def test_a_failed_discard_after_a_store_failure_is_logged_and_the_presser_still_told(
        harness, monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)

    def refused(*args: Any, **kwargs: Any) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    async def dropped(*args: Any, **kwargs: Any) -> None:
        raise aiohttp.ClientError("dropped")

    monkeypatch.setattr(play_now, "add", refused)
    monkeypatch.setattr(h.interactions._rest, "delete_message", dropped)
    await h.presses.on_reaction(play_press())
    assert dm_channel_texts(h.state, PRESSER) == [text.PRESS_FAILED]
    assert "play_now_discard_failed" in event_names(caplog)


# Subscription scopes ----------------------------------------------------------------------------------

FAR_PLAY_PATH = f"/api/v1/channels/{FAR_PLAY}/messages"


async def make_local(h: Harness, *, channel: str | None = FAR_PLAY, role: str | None = SUB_ROLE) -> int:
    """Makes the far copy a local scope that posts to `channel` (None: its own channel), with `role` as its follow
    role. Returns the subscription id."""
    def tx(conn: Any) -> int:
        sub = subscriptions.get(conn, CARD_ID, FAR_COPY)
        assert sub is not None
        assert subscriptions.set_mode_if(conn, sub.id, "redirect", "local")
        subscriptions.set_local_channel(conn, sub.id, channel)
        if role is not None:
            cards.set_subscription_role(conn, sub.id, role)
        return sub.id

    return await h.db.run(tx)


def _play_now_scopes(conn) -> list[int | None]:
    return [row[0] for row in conn.execute("SELECT scope_sub_id FROM play_now ORDER BY id").fetchall()]


def far_press(user: str = PRESSER) -> events.ReactionAdd:
    return play_press(user=user, channel=FAR_COPY, message=FAR_MSG, guild=OTHER_GUILD)


async def test_a_local_copy_press_posts_once_to_its_far_channel_with_the_scope_role(harness) -> None:
    h = await harness()
    sub = await make_local(h)
    await h.presses.on_reaction(far_press())
    [post] = channel_posts(h.state)
    assert post.path == FAR_PLAY_PATH
    assert post.payload["allowed_mentions"] == {"parse": [], "roles": [SUB_ROLE], "replied_user": False}
    assert post.payload["content"].endswith(f" <@&{SUB_ROLE}>")
    assert post.payload["content"].count("<@") == 1
    assert await h.db.run(_play_now_scopes) == [sub]
    assert dm_channel_texts(h.state, PRESSER) == []


async def test_a_local_copy_press_with_a_local_channel_posts_only_there(harness) -> None:
    h = await harness(PLAY_NOW_CHANNEL_ID=CONFIG_PLAY)
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)
    await make_local(h, channel=FAR_PLAY_2)
    await h.presses.on_reaction(far_press())
    assert [post.path for post in channel_posts(h.state)] == [f"/api/v1/channels/{FAR_PLAY_2}/messages"]


async def test_a_local_copy_without_a_local_channel_posts_in_its_own_channel(harness) -> None:
    h = await harness()
    await make_local(h, channel=None)
    await h.presses.on_reaction(far_press())
    [post] = channel_posts(h.state)
    assert post.path == f"/api/v1/channels/{FAR_COPY}/messages"


async def test_a_local_scope_with_no_role_mentions_nobody(harness) -> None:
    h = await harness()
    await make_local(h, role=None)
    await h.presses.on_reaction(far_press())
    [post] = channel_posts(h.state)
    assert post.payload["allowed_mentions"] == {"parse": []}
    assert "<@" not in post.payload["content"]


async def test_a_local_press_inside_a_home_cooldown_still_posts(harness) -> None:
    h = await harness()
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)
    await make_local(h)
    await h.presses.on_reaction(play_press(user=PRESSER))
    await h.presses.on_reaction(far_press(user=PRESSER_2))
    assert [post.path for post in channel_posts(h.state)] == [PLAY_PATH, FAR_PLAY_PATH]
    assert dm_channel_texts(h.state, PRESSER_2) == []


async def test_a_home_press_inside_a_local_cooldown_still_posts(harness) -> None:
    h = await harness()
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)
    await make_local(h)
    await h.presses.on_reaction(far_press(user=PRESSER))
    await h.presses.on_reaction(play_press(user=PRESSER_2))
    assert [post.path for post in channel_posts(h.state)] == [FAR_PLAY_PATH, PLAY_PATH]


async def test_a_second_local_press_inside_the_local_cooldown_is_refused_by_dm(harness) -> None:
    h = await harness()
    await make_local(h)
    await h.presses.on_reaction(far_press(user=PRESSER))
    [first] = play_now_message_ids(h.state, FAR_PLAY)
    await h.presses.on_reaction(far_press(user=PRESSER_2))
    assert [post.path for post in channel_posts(h.state)] == [FAR_PLAY_PATH]
    link = render.jump_link(text.JUMP_LABEL_MESSAGE, JUMP_URL, OTHER_GUILD, FAR_PLAY, first)
    assert dm_channel_texts(h.state, PRESSER_2) == [
        text.PLAY_NOW_COOLDOWN.format(card=render.escape(CARD_TITLE), link=link)]


async def test_a_me_too_recount_on_a_local_post_names_only_the_scope_role(harness) -> None:
    h = await harness()
    await make_local(h)
    await h.presses.on_reaction(far_press())
    [mid] = play_now_message_ids(h.state, FAR_PLAY)
    h.state.reactions[(FAR_PLAY, mid)][PLAY_NOW_EMOJI] = [BOT_ID, REACTOR_A]
    await h.presses.on_reaction(press_event(PLAY_NOW_EMOJI, user=REACTOR_A, channel=FAR_PLAY, message=mid,
                                            guild=OTHER_GUILD))
    [edit] = [r for r in h.state.requests if r.method == "PATCH" and r.path == f"{FAR_PLAY_PATH}/{mid}"]
    assert edit.payload["allowed_mentions"] == {"parse": []}
    assert edit.payload["content"].endswith(f" <@&{SUB_ROLE}>")
    assert ALPHA_ROLE not in str(edit.payload)
    assert status_field(edit) == text.PLAY_NOW_PLAYING.format(n=2)


def test_a_scope_role_is_mentioned_only_in_its_own_community() -> None:
    local = scope.Scope(card_id=CARD_ID, sub=None, guild_id=GUILD, role=FollowRole(guild_id=GUILD, role_id=SUB_ROLE))
    assert _scope_role_in(local, GUILD) == SUB_ROLE
    assert _scope_role_in(local, OTHER_GUILD) is None
    no_role = scope.Scope(card_id=CARD_ID, sub=None, guild_id=GUILD, role=None)
    assert _scope_role_in(no_role, GUILD) is None
