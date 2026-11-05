# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Play-now presses end to end: `Presses.on_reaction` feeding a real `Interactions`, against a real SQLite file and the
fake REST app. The play-now posts, the presser's DMs and the seeded reactions are what the tests read. Emoji are written
as escapes (presses.PLAY_NOW_EMOJI and friends); the tests never contain a literal emoji."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import cards, render, subscriptions, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import ChannelLink
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import GamesChannelRefused, Interactions
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
GUILD = "2001"           # the games community
OTHER_GUILD = "2002"
GAMES = "3001"           # the games channel, in GUILD
COPY = "3002"            # a copy's channel, in GUILD
FAR_COPY = "3003"        # a copy's channel, in OTHER_GUILD
PLAY_CH = "3004"         # a card's play-now channel, in GUILD
FAR_PLAY = "3005"        # a play-now channel in OTHER_GUILD
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


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState(bot_user_id=BOT_ID)
    state.channels.update({
        GAMES: {"id": GAMES, "guild_id": GUILD, "type": 0},
        COPY: {"id": COPY, "guild_id": GUILD, "type": 0},
        FAR_COPY: {"id": FAR_COPY, "guild_id": OTHER_GUILD, "type": 0},
        PLAY_CH: {"id": PLAY_CH, "guild_id": GUILD, "type": 0},
        FAR_PLAY: {"id": FAR_PLAY, "guild_id": OTHER_GUILD, "type": 0},
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
                    **env: str) -> Harness:
        rest = FluxerREST(session, api, TOKEN)
        database = Database.open(tmp_path / f"panel{len(opened)}.db")
        opened.append(database)
        await database.run(seed)
        base = {"FLUXER_TOKEN": TOKEN, "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES}
        base["FLUXER_INSTANCE_URL" if instance_url else "FLUXER_API_URL"] = JUMP_URL if instance_url else API
        config = Config.from_env({**base, **env})
        games = GamesGuild(rest, config)
        pacer = ChannelPacer(max_calls=1000)
        identity = BotIdentity(user_id=BOT_ID)
        clock = Clock()
        wake = RecordingWake()
        interactions = Interactions(db=database, rest=rest, config=config, locks=CardLocks(), games_guild=games,
                                    seeder=Seeder(rest=rest, pacer=pacer, db=database), identity=identity,
                                    roles=RoleCache(rest), clock=clock, wake=wake,
                                    status_of=status_of or (lambda _id: None))
        presses = Presses(db=database, rest=rest, config=config, handler=interactions, games_guild=games,
                          pacer=pacer, identity=identity, clock=clock)
        return Harness(state=state, db=database, presses=presses, interactions=interactions, clock=clock,
                       config=config, wake=wake)

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


async def test_an_unreachable_configured_channel_is_unavailable_by_dm(harness) -> None:
    h = await harness(PLAY_NOW_CHANNEL_ID=MISSING)
    await h.presses.on_reaction(press_event(PLAY_NOW_EMOJI, message=SECOND_MSG))
    assert channel_posts(h.state) == []
    assert dm_channel_texts(h.state, PRESSER) == [text.PLAY_NOW_UNAVAILABLE.format(card=render.escape(SECOND_TITLE))]


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


async def test_without_an_instance_url_the_cooldown_text_has_no_link(harness) -> None:
    h = await harness(instance_url=False)
    await set_card_play_channel(h, CARD_ID, GUILD, PLAY_CH)
    await h.presses.on_reaction(play_press(user=PRESSER))
    await h.presses.on_reaction(play_press(user=PRESSER_2))
    assert len(channel_posts(h.state)) == 1
    assert dm_channel_texts(h.state, PRESSER_2) == [
        text.PLAY_NOW_COOLDOWN_NO_LINK.format(card=render.escape(CARD_TITLE))]


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
