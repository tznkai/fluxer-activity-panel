# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The wiring of a wake press in `Interactions`: a press on the card, on a copy or on a play-now post reaches the wake
requests, and a handler built without wake requests does nothing. The request itself is covered by tests/test_wake.py."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import cards, text, wake_requests
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import ManagerRef
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import Action, Press, Surface, SurfaceKind
from fluxer_panel.resolver import CardStatus, StatusKind
from fluxer_panel.seeding import Seeder
from fluxer_panel.sources import pterodactyl
from fluxer_panel.sources.registry import Sources
from fluxer_panel.wake import WakeRequests
from tests.fakes import FakeFluxerState, Recorded, make_rest_app

TOKEN = "SENTINEL-TOKEN-interactions-wake"
BOT_ID = "1900"
PRESSER = "6001"
GUILD = "2001"
GAMES = "3001"
CARD_ID = 7
API = "https://fluxer.invalid/api"
TITLE = "Minecraft"
CARD_CHANNEL = "3002"
CARD_MESSAGE = "5001"
ADMIN_A = "1201"
ADMIN_B = "1202"
FAR_GUILD = "2002"
FAR_CHANNEL = "3003"
SUB_ID = 41


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
    config = Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
                              "ADMIN_USER_IDS": "1001", "GAMES_CHANNEL_ID": GAMES})
    rest = FluxerREST(session, API, TOKEN)
    pacer = ChannelPacer()
    return Interactions(db=db, rest=rest, config=config, locks=CardLocks(), games_guild=GamesGuild(rest, config),
                        seeder=Seeder(rest=rest, pacer=pacer, db=db), pacer=pacer,
                        identity=BotIdentity(user_id=BOT_ID), roles=RoleCache(rest), wake=wake)


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


async def test_a_wake_press_on_a_play_now_post_outside_the_games_community_still_wakes_the_card(
        db: Database, session: aiohttp.ClientSession) -> None:
    wake = RecordingWake()
    interactions = make_interactions(db, session, wake)
    far_post = Surface(kind="play_now", channel_id=FAR_CHANNEL, message_id="5003", guild_id=FAR_GUILD,
                       home_community=False)

    await interactions.handle(Press(card_id=CARD_ID, action=Action.WAKE, user_id=PRESSER, member_role_ids=None,
                                    surface=far_post))

    assert wake.presses == [(CARD_ID, PRESSER)]


async def test_a_wake_press_without_wake_requests_does_nothing(db: Database, session: aiohttp.ClientSession) -> None:
    interactions = make_interactions(db, session, None)

    await interactions.handle(wake_press("card"))   # no request is made: the session points at no server


def copy_surface(mode: str) -> Surface:
    """A copy in another community, in a local or both subscription. Its home_community is False: it is not the games
    community's copy."""
    return Surface(kind="copy", channel_id=FAR_CHANNEL, message_id="5002", guild_id=FAR_GUILD, home_community=False,
                   sub_id=SUB_ID, mode=mode)


WAKE_SURFACES: dict[str, Surface] = {
    "card": Surface(kind="card", channel_id=GAMES, message_id=CARD_MESSAGE, guild_id=GUILD, home_community=True),
    "local copy": copy_surface("local"),
    "both copy": copy_surface("both"),
}


@pytest.mark.parametrize("name", sorted(WAKE_SURFACES))
async def test_a_wake_press_on_a_local_or_both_copy_is_the_same_press_as_on_the_card(
        db: Database, session: aiohttp.ClientSession, name: str) -> None:
    wake = RecordingWake()
    interactions = make_interactions(db, session, wake)
    surface = WAKE_SURFACES[name]

    await interactions.handle(Press(card_id=CARD_ID, action=Action.WAKE, user_id=PRESSER, member_role_ids=None,
                                    surface=surface))

    assert wake.presses == [(CARD_ID, PRESSER)]


def dm_posts(state: FakeFluxerState, user_id: str) -> list[Recorded]:
    channel = state.dm_channels.get(user_id)
    if channel is None:
        return []
    return [r for r in state.requests if r.method == "POST" and r.path == f"/api/v1/channels/{channel}/messages"]


@pytest.fixture
async def fake(aiohttp_server) -> AsyncIterator[tuple[FakeFluxerState, str]]:
    state = FakeFluxerState()
    server = await aiohttp_server(make_rest_app(state))
    yield state, str(server.make_url("/")).rstrip("/")


@pytest.mark.parametrize("name", sorted(WAKE_SURFACES))
async def test_a_wake_press_on_a_local_or_both_copy_opens_the_same_request_as_on_the_card(
        db: Database, session: aiohttp.ClientSession, fake: tuple[FakeFluxerState, str], name: str) -> None:
    state, base = fake
    rest = FluxerREST(session, f"{base}/api", TOKEN)
    config = Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC",
                              "FLUXER_INSTANCE_URL": "https://fluxer.invalid", "GAMES_CHANNEL_ID": GAMES,
                              "ADMIN_USER_IDS": f"{ADMIN_B},{ADMIN_A}"})
    card_id = await db.run(_live_asleep_card)
    pacer = ChannelPacer()
    wakes = WakeRequests(db=db, rest=rest, config=config,
                         sources=Sources(managers={pterodactyl.KIND: object()}, probes={}), locks=CardLocks(),
                         status_of=lambda _: CardStatus(StatusKind.ASLEEP), clock=lambda: 1000.0)
    interactions = Interactions(db=db, rest=rest, config=config, locks=CardLocks(),
                                games_guild=GamesGuild(rest, config), seeder=Seeder(rest=rest, pacer=pacer, db=db),
                                pacer=pacer, identity=BotIdentity(user_id=BOT_ID), roles=RoleCache(rest), wake=wakes)

    await interactions.handle(Press(card_id=card_id, action=Action.WAKE, user_id=PRESSER, member_role_ids=None,
                                    surface=WAKE_SURFACES[name]))

    assert await db.run(lambda c: wake_requests.open_counts(c)) == {card_id: 1}
    [requester] = dm_posts(state, PRESSER)
    assert requester.payload["content"] == text.WAKE_SENT.format(card=TITLE)
    for admin in (ADMIN_A, ADMIN_B):
        [dm] = dm_posts(state, admin)
        assert TITLE in dm.payload["content"]
        assert f"<@{PRESSER}>" in dm.payload["content"]


def _live_asleep_card(conn: sqlite3.Connection) -> int:
    card_id = cards.create_card(conn, slug="mc", title=TITLE, author_id="1001", now=1)
    cards.set_state(conn, card_id, "live", 1)
    cards.set_message(conn, card_id, CARD_CHANNEL, CARD_MESSAGE, None)
    cards.set_manager(conn, card_id, ManagerRef(pterodactyl.KIND, "abcd1234"))
    return card_id
