# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RSVP reactions on a session message: `Presses.on_reaction` routes them to the handler's `rsvp_changed`, outside the
press path. They bypass the kill switch, the rate limits and the removal (AC4.9), and every other reaction reaches no
handler method (AC4.10). Runs against a real SQLite file and the fake REST app. Emoji are written as escapes or taken
from rsvp and presses; the tests never contain a literal emoji."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, interaction_settings, rsvp, sessions
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import WAKE_EMOJI, Presses
from tests.fakes import FakeFluxerState, make_rest_app
from tests.test_presses import (BOT_ID, CARD_MSG, GAMES, GUILD, PRESSER, PRESSER_2, RecordingHandler, reaction_add,
                                reaction_remove)

TOKEN = "SENTINEL-TOKEN-presses-session"
ADMIN = "1001"
PROPOSER = "6010"
SESSION_CH = "3200"          # a session's channel, in GUILD: never the games channel
SESSION_MSG = "5100"
CUSTOM_ID = "7777"
PARTY = "\U0001F389"         # a party popper: not an RSVP emoji
HASH = "a" * 64
STARTS = 1_800_000_000
CARD_ID = 1


@dataclass
class World:
    state: FakeFluxerState
    db: Database
    handler: RecordingHandler
    presses: Presses
    config: Config
    session_id: int
    card_id: int


def seed(conn) -> tuple[int, int]:
    card = cards.create_card(conn, slug="mc", title="Minecraft", author_id=ADMIN, now=1)
    assert card == CARD_ID
    cards.set_state(conn, card, "live", 1)
    cards.set_message(conn, card, GAMES, CARD_MSG, HASH)
    session_id = sessions.add(conn, card_id=card, guild_id=GUILD, channel_id=SESSION_CH, message_id=SESSION_MSG,
                              starts_at=STARTS, ends_at=STARTS + 3600, proposer_id=PROPOSER, now=1)
    return card, session_id


@pytest.fixture
async def fake(aiohttp_server: Any) -> tuple[FakeFluxerState, str]:
    state = FakeFluxerState(bot_user_id=BOT_ID)
    state.channels.update({
        GAMES: {"id": GAMES, "guild_id": GUILD, "type": 0},
        SESSION_CH: {"id": SESSION_CH, "guild_id": GUILD, "type": 0},
    })
    server = await aiohttp_server(make_rest_app(state))
    return state, f"{str(server.make_url('/')).rstrip('/')}/api"


@pytest.fixture
async def world(tmp_path: Path, fake: tuple[FakeFluxerState, str]) -> AsyncIterator[World]:
    state, api = fake
    database = Database.open(tmp_path / "presses_session.db")
    try:
        card_id, session_id = await database.run(seed)
        async with aiohttp.ClientSession() as client:
            rest = FluxerREST(client, api, TOKEN)
            config = Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC",
                                      "FLUXER_INSTANCE_URL": "https://fluxer.invalid", "ADMIN_USER_IDS": ADMIN,
                                      "GAMES_CHANNEL_ID": GAMES})
            handler = RecordingHandler(state)
            presses = Presses(db=database, rest=rest, config=config, handler=handler,
                              games_guild=GamesGuild(rest, config), pacer=ChannelPacer(max_calls=1000),
                              identity=BotIdentity(user_id=BOT_ID), clock=lambda: 0.0)
            yield World(state=state, db=database, handler=handler, presses=presses, config=config,
                        session_id=session_id, card_id=card_id)
    finally:
        database.close()


def session_add(name: str, user: str = PRESSER) -> events.ReactionAdd:
    return reaction_add(user=user, channel=SESSION_CH, message=SESSION_MSG, name=name)


def session_remove(name: str, user: str = PRESSER) -> events.ReactionRemove:
    return reaction_remove(user=user, channel=SESSION_CH, message=SESSION_MSG, name=name)


def deletes(state: FakeFluxerState) -> list[str]:
    return [r.raw_path for r in state.requests if r.method == "DELETE"]


# AC4.8 input: each RSVP emoji reaches the handler with its choice and direction.

async def test_a_going_reaction_added_on_a_session_message_reaches_rsvp_changed(world: World) -> None:
    await world.presses.on_reaction(session_add(rsvp.GOING_EMOJI))
    assert world.handler.rsvps == [(world.session_id, PRESSER, "going", True)]
    assert world.handler.presses == []
    assert world.handler.me_too == []


async def test_a_maybe_reaction_removed_from_a_session_message_reaches_rsvp_changed(world: World) -> None:
    await world.presses.on_reaction(session_remove(rsvp.MAYBE_EMOJI))
    assert world.handler.rsvps == [(world.session_id, PRESSER, "maybe", False)]


async def test_a_no_reaction_with_a_variation_selector_is_the_no_choice(world: World) -> None:
    await world.presses.on_reaction(session_add(rsvp.NO_EMOJI + "\ufe0f"))
    assert world.handler.rsvps == [(world.session_id, PRESSER, "no", True)]


# AC4.9: RSVPs are never rate limited, never removed by the bot, and the kill switch does not stop them.

async def test_rapid_rsvp_adds_and_removes_are_all_handled_and_never_deleted(world: World) -> None:
    for _ in range(20):
        await world.presses.on_reaction(session_add(rsvp.GOING_EMOJI))
        await world.presses.on_reaction(session_remove(rsvp.GOING_EMOJI))
    assert len(world.handler.rsvps) == 40
    assert deletes(world.state) == []


async def test_rsvps_reach_the_handler_with_the_global_switch_off(world: World) -> None:
    await world.db.run(lambda c: interaction_settings.set_enabled(c, False))
    await world.presses.on_reaction(session_add(rsvp.GOING_EMOJI))
    assert world.handler.rsvps == [(world.session_id, PRESSER, "going", True)]
    assert deletes(world.state) == []


async def test_rsvps_reach_the_handler_with_the_card_switched_off(world: World) -> None:
    await world.db.run(lambda c: interaction_settings.set_card_off(c, world.card_id, True))
    await world.presses.on_reaction(session_add(rsvp.MAYBE_EMOJI, user=PRESSER_2))
    assert world.handler.rsvps == [(world.session_id, PRESSER_2, "maybe", True)]
    assert deletes(world.state) == []


# AC4.10: every other reaction changes nothing. The bot's own, another bot's, a custom emoji named like an RSVP, other
# emoji, a card message's RSVP emoji, and a play emoji on a session message reach no handler method.

NOT_RSVPS = [
    pytest.param(reaction_add(user=BOT_ID, channel=SESSION_CH, message=SESSION_MSG, name=rsvp.GOING_EMOJI),
                 id="the bot's own reaction"),
    pytest.param(reaction_add(channel=SESSION_CH, message=SESSION_MSG, name=rsvp.GOING_EMOJI, bot=True),
                 id="another bot's reaction"),
    pytest.param(reaction_add(channel=SESSION_CH, message=SESSION_MSG, name=rsvp.GOING_EMOJI, custom_id=CUSTOM_ID),
                 id="a custom emoji named like an rsvp"),
    pytest.param(reaction_add(channel=SESSION_CH, message=SESSION_MSG, name=PARTY), id="other emoji"),
    pytest.param(reaction_add(channel=GAMES, message=CARD_MSG, name=rsvp.GOING_EMOJI), id="rsvp on a card message"),
    pytest.param(reaction_add(channel=SESSION_CH, message=SESSION_MSG, name=WAKE_EMOJI), id="play emoji on a session"),
]


@pytest.mark.parametrize("event", NOT_RSVPS)
async def test_a_reaction_that_is_not_an_rsvp_reaches_no_handler_method(world: World, event: events.ReactionAdd) -> None:
    await world.presses.on_reaction(event)
    assert world.handler.rsvps == []
    assert world.handler.presses == []
    assert world.handler.me_too == []


# A failing handler is logged with the session id and the exception type, and never raised into the gateway.

async def test_a_failing_rsvp_handler_is_logged_and_not_raised(world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="fluxer_panel.presses")
    world.handler.rsvp_fail = True
    await world.presses.on_reaction(session_add(rsvp.GOING_EMOJI))
    [failed] = [r for r in caplog.records if r.getMessage() == "rsvp_failed"]
    assert getattr(failed, FIELDS_ATTR) == {"session_id": world.session_id, "reason": "RuntimeError"}
