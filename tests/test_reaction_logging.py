# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AC6.2 and AC6.3 across the feature: the press logs, the reaction and DM log lines, and the mentions in admin DMs.

One scenario drives each kind of reaction through `Bot.on_dispatch` against the fake Fluxer server and a real SQLite
file: a wake press (a signal, since no manager is configured), a follow press, a play-now press, an absorbed press, a
play-now me-too, ignored reactions, and a follow press whose DM the fake refuses with a 403. A non-admin DM carries a
message content sentinel. The logs are captured with the bot's own JSON stream handler.

Emoji are written as escapes, so the test file holds no literal emoji.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import cards, play_now
from fluxer_panel.bot import Bot
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.logs import setup_logging
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import FOLLOW_EMOJI, PLAY_NOW_EMOJI, WAKE_EMOJI, Presses
from fluxer_panel.seeding import Seeder
from fluxer_panel.sources.registry import Sources
from fluxer_panel.wake import WakeRequests
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "SENTINEL-TOKEN-logging"
CONTENT = "SENTINEL-CONTENT-logging"
NAME = "SENTINEL-NAME-logging"
ADMIN = "1001"
BOT_ID = "1900"
PRESSER = "6101"           # wakes, then is absorbed, then raises a play-now post
PRESSER_2 = "6102"         # follows, then adds a me-too
PRESSER_3 = "6103"         # follows, and the fake refuses the DM that tells them so
GUILD = "2001"
GAMES = "3001"
PLAY_CH = "3005"           # PLAY_NOW_CHANNEL_ID, in GUILD
BOT_ROLE = "5000"
ALPHA_ROLE = "7001"        # the card's follow role
CARD_MSG = "5001"
CARD_ID = 1
DM_FAILING = "9500"        # PRESSER_3's DM channel, set before the run so the 403 override can name it
URL = "https://fluxer.invalid"
THUMBS_UP = "\U0001F44D"
# The three press emoji and the sentinels, as escapes: the test file itself never holds a literal emoji.
WAKE_CHAR = "\u25b6"
WAKE_FE0F = "\ufe0f"   # the variation selector the wake emoji carries as the bot sends and Fluxer stores it
BELL = "\U0001F514"
HAND = "\U0001F64B"
FORBIDDEN = b'{"code":"MISSING_PERMISSIONS","message":"no"}'
EVERYONE = {"id": GUILD, "name": "@everyone", "permissions": "0", "position": 0, "mentionable": False}
BOT_ROLE_DATA = {"id": BOT_ROLE, "name": "bot", "permissions": "8", "position": 10, "mentionable": False}
ALPHA_DATA = {"id": ALPHA_ROLE, "name": "card_subscription_mc", "permissions": "0", "position": 5,
              "mentionable": True}
ACTION_EVENTS = {"reaction", "press", "user_dm_failed"}
ACTION_PREFIXES = ("press_", "wake_", "follow_", "play_now_")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def member(user_id: str) -> dict[str, Any]:
    # The name sits in the fake's member record. Nothing in the bot logs a member record, so it must not surface.
    return {"user": {"id": user_id, "username": NAME}, "nick": NAME, "roles": []}


def seed(conn) -> None:
    card = cards.create_card(conn, slug="mc", title="Minecraft", author_id=ADMIN, now=1)
    assert card == CARD_ID
    cards.set_state(conn, card, "live", 1)
    cards.set_message(conn, card, GAMES, CARD_MSG, "a" * 64)
    cards.set_follow_role(conn, card, GUILD, ALPHA_ROLE)


@dataclass
class Run:
    state: FakeFluxerState
    db: Database
    stream: io.StringIO
    lines: list[dict[str, Any]] = field(default_factory=list)
    raw: str = ""

    def dm_channel_ids(self) -> set[str]:
        return set(self.state.dm_channels.values())


def lines_of(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


@pytest.fixture
def root_logging():
    """setup_logging replaces the root handlers and level; restore both so pytest's capture keeps working."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)
    logging.captureWarnings(False)


@pytest.fixture
def captured(root_logging) -> io.StringIO:
    stream = io.StringIO()
    setup_logging("DEBUG", [TOKEN], stream)
    return stream


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def run(tmp_path: Path, aiohttp_server: Any, session: aiohttp.ClientSession,
              captured: io.StringIO) -> AsyncIterator[Run]:
    state = FakeFluxerState(bot_user_id=BOT_ID, next_message_id=9001)
    state.channels.update({
        GAMES: {"id": GAMES, "guild_id": GUILD, "type": 0},
        PLAY_CH: {"id": PLAY_CH, "guild_id": GUILD, "type": 0},
    })
    state.roles = {GUILD: [dict(EVERYONE), dict(BOT_ROLE_DATA), dict(ALPHA_DATA)]}
    state.members = {(GUILD, BOT_ID): {"roles": [BOT_ROLE]}, (GUILD, PRESSER_2): member(PRESSER_2),
                     (GUILD, PRESSER_3): member(PRESSER_3)}
    state.dm_channels[PRESSER_3] = DM_FAILING
    state.overrides[f"POST /api/v1/channels/{DM_FAILING}/messages"] = Override(403, FORBIDDEN)
    server = await aiohttp_server(make_rest_app(state))
    rest = FluxerREST(session, f"{str(server.make_url('/')).rstrip('/')}/api", TOKEN)
    database = Database.open(tmp_path / "logging.db")
    try:
        await database.run(seed)
        config = Config.from_env({"FLUXER_TOKEN": TOKEN, "FLUXER_INSTANCE_URL": URL, "ADMIN_USER_IDS": ADMIN,
                                  "GAMES_CHANNEL_ID": GAMES, "PLAY_NOW_CHANNEL_ID": PLAY_CH})
        identity = BotIdentity()
        clock = Clock()
        locks = CardLocks()
        pacer = ChannelPacer(max_calls=1000)
        games = GamesGuild(rest, config)
        sources = Sources(managers={}, probes={})
        wake = WakeRequests(db=database, rest=rest, config=config, sources=sources, locks=locks,
                            status_of=lambda _id: None, clock=clock)
        interactions = Interactions(db=database, rest=rest, config=config, locks=locks, games_guild=games,
                                    seeder=Seeder(rest=rest, pacer=pacer, db=database), pacer=pacer,
                                    identity=identity, roles=RoleCache(rest), clock=clock, wake=wake,
                                    status_of=lambda _id: None)
        presses = Presses(db=database, rest=rest, config=config, handler=interactions, games_guild=games,
                          pacer=pacer, identity=identity, clock=clock)
        bot = Bot(rest=rest, config=config, roles=RoleCache(rest), presses=presses, identity=identity)

        await bot.on_dispatch("READY", {"session_id": "s1", "user": {"id": BOT_ID}})

        async def react(emoji: str, user: str, *, message: str = CARD_MSG, channel: str = GAMES,
                        event: str = "MESSAGE_REACTION_ADD") -> None:
            await bot.on_dispatch(event, {"user_id": user, "channel_id": channel, "message_id": message,
                                          "guild_id": GUILD, "emoji": {"name": emoji, "id": None}})

        # a wake press: no manager, so it is a signal, which DMs the admins with the presser as <@id>
        await react(WAKE_EMOJI, PRESSER)
        # a follow press that adds the role and DMs the presser
        await react(FOLLOW_EMOJI, PRESSER_2)
        # a play-now press: the post goes to the configured play-now channel
        await react(PLAY_NOW_EMOJI, PRESSER)
        # the same presser's second wake press inside the window: absorbed, with the user id logged
        await react(WAKE_EMOJI, PRESSER)
        # a me-too on the play-now post the press created
        post = await database.run(lambda c: play_now.latest_for_card(c, CARD_ID))
        assert post is not None
        await react(PLAY_NOW_EMOJI, PRESSER_2, message=post.message_id, channel=PLAY_CH)
        # ignored: a reaction that is not one of the three, and a removal of the follow emoji (never a press)
        await react(THUMBS_UP, PRESSER)
        await react(FOLLOW_EMOJI, PRESSER_2, event="MESSAGE_REACTION_REMOVE")
        # a follow press whose DM the fake refuses: user_dm_failed must log the type only
        await react(FOLLOW_EMOJI, PRESSER_3)
        # a DM from a non-admin carrying the content sentinel: refused, and nothing of it reaches the logs
        await bot.on_dispatch("MESSAGE_CREATE", {"id": "8001", "channel_id": "9100",
                                                 "author": {"id": PRESSER, "bot": False}, "content": CONTENT})
        # a removal the fake refuses degrades the channel, and the admins hear it through dm_admins
        state.overrides[f"DELETE /api/v1/channels/{GAMES}/messages/{CARD_MSG}/reactions/{FOLLOW_EMOJI}/{PRESSER}"] = (
            Override(403, FORBIDDEN))
        await react(FOLLOW_EMOJI, PRESSER)

        yield Run(state=state, db=database, stream=captured, lines=lines_of(captured),
                  raw=captured.getvalue())
    finally:
        database.close()


def events_named(run: Run, name: str) -> list[dict[str, Any]]:
    return [line for line in run.lines if line["event"] == name]


def is_action_event(name: str) -> bool:
    return name in ACTION_EVENTS or name.startswith(ACTION_PREFIXES)


async def test_every_press_logs_its_event_type_card_and_action_and_no_user_id(run: Run) -> None:
    presses = events_named(run, "press")
    assert len(presses) == 5   # wake, follow, play-now, and two follows: one whose DM was refused, one whose removal was
    for line in presses:
        assert {"event_type", "card_id", "action"} <= set(line)
        assert "user_id" not in line
    assert {line["action"] for line in presses} == {"wake", "follow", "play_now"}


async def test_only_an_absorbed_press_logs_a_user_id(run: Run) -> None:
    absorbed = events_named(run, "press_absorbed")
    assert len(absorbed) == 1
    assert absorbed[0]["user_id"] == PRESSER
    assert absorbed[0]["reason"] == "user_window"
    for line in run.lines:
        name = line["event"]
        if name == "press_absorbed" or not is_action_event(name):
            continue
        assert "user_id" not in line, f"{name} carries a user id"


async def test_the_logged_events_cover_each_kind_of_reaction(run: Run) -> None:
    seen = {line["event"] for line in run.lines}
    for name in ("reaction", "press", "press_absorbed", "wake_press", "follow_toggled", "play_now_posted",
                 "play_now_count", "user_dm_failed", "reaction_degraded"):
        assert name in seen, f"the scenario did not reach {name}"


async def test_a_failed_dm_logs_the_type_only(run: Run) -> None:
    failed = events_named(run, "user_dm_failed")
    assert len(failed) == 1
    assert failed[0]["reason"] == "FluxerHTTPError"   # the exception type name only, never its message
    assert "user_id" not in failed[0]
    assert set(failed[0]) <= {"event", "level", "logger", "ts", "reason"}


def escaped_forms(text: str) -> list[str]:
    """How a character appears in the JSON stream: the logger writes ensure_ascii escapes, so a literal check misses it."""
    return [json.dumps(ch)[1:-1] for ch in text]


async def test_no_line_has_an_emoji_the_content_or_a_name(run: Run) -> None:
    assert run.lines
    emoji_chars = (WAKE_CHAR, BELL, HAND, WAKE_FE0F, THUMBS_UP)
    # Decoded: every parsed entry, re-serialised with real characters, must hold none of the emoji characters.
    for entry in run.lines:
        decoded = json.dumps(entry, ensure_ascii=False)
        for ch in emoji_chars:
            assert ch not in decoded, f"{entry.get('event')} carries an emoji character"
        assert CONTENT not in decoded
        assert NAME not in decoded
    # Raw: the stream holds the escaped forms (\\u25b6, the surrogate pair for U+1F514), so those are checked too.
    for ch in emoji_chars:
        for escaped in escaped_forms(ch):
            assert escaped not in run.raw, f"the stream carries an escaped emoji character {escaped}"
    assert CONTENT not in run.raw
    assert NAME not in run.raw
    assert TOKEN not in run.raw


async def test_every_dm_write_sends_no_mention_parsing_and_names_people_only_as_mentions(run: Run) -> None:
    dm_channels = run.dm_channel_ids()
    writes = [record for record in run.state.requests
              if record.method in ("POST", "PATCH") and record.path.startswith("/api/v1/channels/")
              and record.path.split("/")[4] in dm_channels]
    assert writes, "the scenario sent no DM"
    # every DM write parses no mentions, whatever its content: this covers the admin DMs and the presser DMs alike
    for record in writes:
        assert (record.payload or {}).get("allowed_mentions") == {"parse": []}, f"{record.method} {record.path}"
    # and the admin DM that names the presser does so as a <@id> mention (AC6.2)
    mentioned = [record for record in writes if "<@" in (record.payload or {}).get("content", "")]
    assert mentioned, "no DM named a person as a mention, so the check would be empty"
    assert any(f"<@{PRESSER}>" in record.payload["content"] for record in mentioned)
