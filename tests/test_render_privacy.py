# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""AC6.1: no card, copy, board or play-now rendering carries a requester's, follower's or presser's id or name.

The sentinel ids and the sentinel name are written into the database and the fake servers, so a rendering that read
any of them would show it. `RENDERERS` lists every public payload renderer of `render`, and a test checks that list
against the module, so a new renderer cannot skip the sentinel check.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_panel import board, cards, play_now, render, wake_requests
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import ChannelLink
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.poller import Poller
from fluxer_panel.resolver import CardStatus, StatusKind
from fluxer_panel.rsvp import Counts
from fluxer_panel.sessions import Session
from fluxer_panel.sources.base import PlayerCounts
from fluxer_panel.sources.registry import Sources
from tests.fakes import FakeFluxerState, make_rest_app

REQUESTER = "424242"        # a person with an open wake request
FOLLOWER = "535353"         # a person holding the card's follow role
PRESSER = "646464"          # the person who raised a play-now post
# a display name, in the fake's member records. No markdown control character, so `render.escape` leaves it unchanged
# and the raw search in `leaks` can find it.
NAME = "SENTINEL NAME 9d2c"
SENTINELS = (REQUESTER, FOLLOWER, PRESSER, NAME)

RENDERERS = ("board_message", "card_message", "outside_text", "play_now_message", "render_embed", "session_message")
RENDER_PREFIXES = ("render_", "board_", "card_", "play_now_", "session_")

TOKEN = "render-privacy-token"
ADMIN = "1001"
GUILD = "2001"
GAMES = "3001"
PLAY_CH = "3005"
ROLE = "7001"
BOARD_MSG = "8999999"          # kept above every fake id, so the stored board stays the channel's newest message
PLAY_NOW_MSG = "5008"
URL = "https://fluxer.invalid"
TITLE = "Minecraft"
NEXT = "Next start in 5 minutes"


def _module_renderers() -> set[str]:
    """Public functions defined in `render` whose names are payload renderers."""
    found = {name for name, fn in inspect.getmembers(render, inspect.isfunction)
             if fn.__module__ == render.__name__ and not name.startswith("_")}
    return {name for name in found if name.startswith(RENDER_PREFIXES) or name == "outside_text"}


def leaks(payload: object) -> list[str]:
    dumped = json.dumps(payload)
    return [sentinel for sentinel in SENTINELS if sentinel in dumped]


def seed(conn) -> int:
    card = cards.create_card(conn, slug="mc", title=TITLE, author_id=ADMIN, now=1)
    cards.set_state(conn, card, "live", 1)
    cards.set_follow_role(conn, card, GUILD, ROLE)
    cards.set_play_now_channel(conn, card, ChannelLink(GUILD, PLAY_CH, None, True))
    assert wake_requests.add_open(conn, card, REQUESTER, 1) is True
    play_now.add(conn, card_id=card, guild_id=GUILD, channel_id=PLAY_CH, message_id=PLAY_NOW_MSG,
                 presser_id=PRESSER, offers_wake=True, now=1, expires_at=10**9)
    board.set_message(conn, GAMES, BOARD_MSG, "0" * 64)
    return card


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "render.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as client:
        yield client


def member(user_id: str, roles: list[str]) -> dict[str, Any]:
    # The name sits in the fake's member record, so a lookup that read it would surface it.
    return {"user": {"id": user_id, "username": NAME}, "nick": NAME, "roles": roles}


def config_for() -> Config:
    return Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": URL, "ADMIN_USER_IDS": ADMIN,
                            "GAMES_CHANNEL_ID": GAMES, "POLL_INTERVAL_SECONDS": "3600"})


def test_the_renderer_list_matches_the_render_module() -> None:
    assert set(RENDERERS) == _module_renderers()


async def test_every_rendering_with_the_sentinel_data_is_free_of_ids_and_names(db: Database) -> None:
    card_id = await db.run(seed)
    # the sentinels are really stored, so an empty result below means no leak rather than no data
    open_counts = await db.run(wake_requests.open_counts)
    assert open_counts == {card_id: 1}
    stored_presser = await db.run(lambda c: play_now.latest_for_card(c, card_id))
    assert stored_presser is not None and stored_presser.presser_id == PRESSER
    card = await db.run(lambda c: cards.get_by_id(c, card_id))
    assert card is not None
    asleep = CardStatus(StatusKind.ASLEEP)
    ready = CardStatus(StatusKind.READY, counts=PlayerCounts(1, 4))
    # The card carries its next session, in the near form and in the far form, so the sentinel check covers both.
    session_line = render.NextSession(starts_at=1_800_000_000, going=2, jump=(GUILD, GAMES, "5010"), has_location=True)
    embed = render.render_embed(card, asleep, NEXT, instance_url=URL, requested=open_counts[card_id],
                                next_session=session_line)
    rendered = {
        "render_embed": embed,
        "card_message": render.card_message(embed),
        "board_message": render.board_message([(card, asleep), (card, ready), (card, None)],
                                              games_channel_id=GAMES, games_guild_id=GUILD, instance_url=URL,
                                              requested=open_counts),
        "outside_text": render.outside_text(card, asleep, NEXT, next_session=session_line),
        "play_now_message": render.play_now_message(card, role_id=ROLE, playing=3, ended=False, instance_url=URL),
        "session_message": render.session_message(
            card, Session(id=42, card_id=card.id, guild_id=GUILD, channel_id=GAMES, message_id="5010",
                          starts_at=1_800_000_000, ends_at=1_800_003_600, proposer_id=REQUESTER,
                          location_text=None, seats=None, recurrence=None, series_id=42, next_posted=False,
                          state="upcoming", closed_at=None, moved=False, headsup_sent=False, created_at=1),
            Counts(going=1, maybe=0, waiting=0), effective_location=None, effective_seats=None, instance_url=URL),
    }
    # the ended and the unknown-count forms of the play-now message are renderings too
    ended = render.play_now_message(card, role_id=ROLE, playing=None, ended=True, instance_url=URL)
    assert set(rendered) == set(RENDERERS)
    for name, payload in [*rendered.items(), ("play_now_message", ended)]:
        assert leaks(payload) == [], f"{name} carries a sentinel"
    # the rendering carries the card, so the check is looking at real output
    assert TITLE in json.dumps(rendered["card_message"])


async def test_every_message_the_poller_sends_to_a_channel_is_free_of_ids_and_names(
        db: Database, session: aiohttp.ClientSession, aiohttp_server: Any) -> None:
    state = FakeFluxerState(bot_user_id="1900", next_message_id=9001)
    state.members = {(GUILD, REQUESTER): member(REQUESTER, [ROLE]), (GUILD, FOLLOWER): member(FOLLOWER, [ROLE])}
    state.messages[(GAMES, BOARD_MSG)] = {"embeds": []}
    state.channels[GAMES] = {"id": GAMES, "guild_id": GUILD, "type": 0}
    server = await aiohttp_server(make_rest_app(state))
    rest = FluxerREST(session, f"{str(server.make_url('/')).rstrip('/')}/api", TOKEN)
    card_id = await db.run(seed)
    poller = Poller(db=db, rest=rest, sources=Sources(managers={}, probes={}), config=config_for(),
                    locks=CardLocks(), clock=lambda: 1000.0)
    await poller.tick()
    # a second tick sends an edit: the title changes, so the card and the board are both patched
    await db.run(lambda c: cards.update_fields(c, card_id, 0, title="Minecraft Night"))
    await poller.tick()

    dm_channels = set(state.dm_channels.values())   # the admins' DM channels, which are not channels
    channel_writes = [r for r in state.requests
                      if r.method in ("POST", "PATCH") and r.path.startswith("/api/v1/channels/")
                      and r.path.split("/")[4] not in dm_channels]
    posts = [r for r in channel_writes if r.method == "POST"]
    patches = [r for r in channel_writes if r.method == "PATCH"]
    assert posts and patches, "the poller sent no card or board write to check"
    assert "Minecraft" in json.dumps(posts[0].payload)
    for record in channel_writes:
        assert leaks(record.payload) == [], f"{record.method} {record.path} carries a sentinel"
