# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The wiring of a wake press in `Interactions`: a press on the card, on a copy or on a play-now post reaches the wake
requests, and a handler built without wake requests does nothing. The request itself is covered by tests/test_wake.py."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import Action, Press, Surface, SurfaceKind
from fluxer_panel.seeding import Seeder

TOKEN = "SENTINEL-TOKEN-interactions-wake"
BOT_ID = "1900"
PRESSER = "6001"
GUILD = "2001"
GAMES = "3001"
CARD_ID = 7
API = "https://fluxer.invalid/api"


class RecordingWake:
    def __init__(self) -> None:
        self.presses: list[tuple[int, str]] = []

    async def press(self, card_id: int, user_id: str) -> None:
        self.presses.append((card_id, user_id))


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


def make_interactions(db: Database, session: aiohttp.ClientSession, wake: RecordingWake | None) -> Interactions:
    config = Config.from_env({"FLUXER_TOKEN": TOKEN, "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
                              "ADMIN_USER_IDS": "1001", "GAMES_CHANNEL_ID": GAMES})
    rest = FluxerREST(session, API, TOKEN)
    return Interactions(db=db, rest=rest, config=config, locks=CardLocks(), games_guild=GamesGuild(rest, config),
                        seeder=Seeder(rest=rest, pacer=ChannelPacer(), db=db), identity=BotIdentity(user_id=BOT_ID),
                        roles=RoleCache(rest), wake=wake)


def wake_press(kind: SurfaceKind) -> Press:
    surface = Surface(kind=kind, channel_id=GAMES, message_id="5001", guild_id=GUILD, home_community=True)
    return Press(card_id=CARD_ID, action=Action.WAKE, user_id=PRESSER, member_role_ids=None, surface=surface)


@pytest.mark.parametrize("kind", ["card", "copy", "play_now"])
async def test_a_wake_press_on_any_surface_wakes_the_card(db: Database, session: aiohttp.ClientSession,
                                                          kind: SurfaceKind) -> None:
    wake = RecordingWake()
    interactions = make_interactions(db, session, wake)

    await interactions.handle(wake_press(kind))

    assert wake.presses == [(CARD_ID, PRESSER)]


async def test_a_wake_press_without_wake_requests_does_nothing(db: Database, session: aiohttp.ClientSession) -> None:
    interactions = make_interactions(db, session, None)

    await interactions.handle(wake_press("card"))   # no request is made: the session points at no server
