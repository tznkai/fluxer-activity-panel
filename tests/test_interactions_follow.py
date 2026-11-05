# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Follow presses end to end: `Presses.on_reaction` feeding a real `Interactions`, against a real SQLite file and the
fake REST app. The presser's DMs, the admins' DMs and the role calls are what the tests read. Emoji are written as
escapes; the tests never contain a literal emoji."""

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
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import FOLLOW_EMOJI, Action, Press, Presses, Surface
from fluxer_panel.seeding import Seeder
from tests.fakes import FakeFluxerState, Override, Recorded, make_rest_app

TOKEN = "SENTINEL-TOKEN-interactions-follow"
BOT_ID = "1900"
ADMIN = "1001"
PRESSER = "6001"
PRESSER_2 = "6002"
GUILD = "2001"           # the games community
OTHER_GUILD = "2002"
GAMES = "3001"           # the games channel, in GUILD
FAR_COPY = "3003"        # a copy's channel, in OTHER_GUILD
COPY = "3002"            # a copy's channel, in GUILD
BOT_ROLE = "5000"
ALPHA_ROLE = "7001"      # the card's follow role: permissions 0, position 5, below the bot
EVERYONE = {"id": GUILD, "name": "@everyone", "permissions": "0", "position": 0, "mentionable": False}
BOT_ROLE_DATA = {"id": BOT_ROLE, "name": "bot", "permissions": "8", "position": 10, "mentionable": False}
ALPHA_DATA = {"id": ALPHA_ROLE, "name": "card_subscription_mc", "permissions": "0", "position": 5,
              "mentionable": True}
CARD_MSG = "5001"
SECOND_MSG = "5002"
COPY_MSG = "5005"
FAR_MSG = "5006"
CARD_ID = 1
SECOND_ID = 2
CARD_TITLE = "Mine] (craft"   # brackets and a parenthesis: they must not break the jump link
SECOND_TITLE = "Ark"
ROLE_PATH = f"/api/v1/guilds/{GUILD}/members/{PRESSER}/roles/{ALPHA_ROLE}"
ROLE_PATH_2 = f"/api/v1/guilds/{GUILD}/members/{PRESSER_2}/roles/{ALPHA_ROLE}"
ROLES_PATH = f"/api/v1/guilds/{GUILD}/roles"
JUMP_URL = "https://fluxer.invalid"
FORBIDDEN = b'{"code":"MISSING_PERMISSIONS","message":"no"}'


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def seed(conn) -> None:
    card = cards.create_card(conn, slug="mc", title=CARD_TITLE, author_id=ADMIN, now=1)
    assert card == CARD_ID
    cards.set_state(conn, card, "live", 1)
    cards.set_message(conn, card, GAMES, CARD_MSG, "a" * 64)
    cards.set_follow_role(conn, card, GUILD, ALPHA_ROLE)
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
    identity: BotIdentity
    clock: Clock
    config: Config


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState(bot_user_id=BOT_ID)
    state.channels.update({
        GAMES: {"id": GAMES, "guild_id": GUILD, "type": 0},
        COPY: {"id": COPY, "guild_id": GUILD, "type": 0},
        FAR_COPY: {"id": FAR_COPY, "guild_id": OTHER_GUILD, "type": 0},
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

    async def build(**env: str) -> Harness:
        rest = FluxerREST(session, api, TOKEN)
        database = Database.open(tmp_path / f"panel{len(opened)}.db")
        opened.append(database)
        await database.run(seed)
        config = Config.from_env({"FLUXER_TOKEN": TOKEN, "FLUXER_INSTANCE_URL": JUMP_URL,
                                  "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES, **env})
        games = GamesGuild(rest, config)
        pacer = ChannelPacer(max_calls=1000)
        identity = BotIdentity(user_id=BOT_ID)
        interactions = Interactions(db=database, rest=rest, config=config, locks=CardLocks(), games_guild=games,
                                    seeder=Seeder(rest=rest, pacer=pacer, db=database), pacer=pacer,
                                    identity=identity, roles=RoleCache(rest))
        clock = Clock()
        presses = Presses(db=database, rest=rest, config=config, handler=interactions, games_guild=games,
                          pacer=pacer, identity=identity, clock=clock)
        return Harness(state=state, db=database, presses=presses, interactions=interactions, identity=identity,
                       clock=clock, config=config)

    yield build
    for database in opened:
        database.close()


def follow_press(user: str = PRESSER, *, channel: str = GAMES, message: str = CARD_MSG, guild: str | None = GUILD,
                 member_roles: frozenset[str] = frozenset()) -> events.ReactionAdd:
    return events.ReactionAdd(user_id=user, channel_id=channel, message_id=message, guild_id=guild,
                              emoji=events.Emoji(name=FOLLOW_EMOJI, id=None), member_role_ids=member_roles,
                              user_is_bot=False)


def forbid_adds(state: FakeFluxerState) -> None:
    """Both pressers' role adds get 403."""
    for path in (ROLE_PATH, ROLE_PATH_2):
        state.overrides[f"PUT {path}"] = Override(403, FORBIDDEN)


def role_requests(state: FakeFluxerState) -> list[Recorded]:
    return [r for r in state.requests if "/roles" in r.path]


def role_writes(state: FakeFluxerState) -> list[tuple[str, str]]:
    return [(r.method, r.path) for r in role_requests(state) if r.method != "GET"]


def dm_channel_texts(state: FakeFluxerState, user: str) -> list[str]:
    channel = state.dm_channels.get(user)
    if channel is None:
        return []
    return [r.payload["content"] for r in state.requests
            if r.method == "POST" and r.path == f"/api/v1/channels/{channel}/messages"]


def panel_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name.startswith("fluxer_panel")]


def jump_to_games_card() -> str:
    return render.jump_link(text.JUMP_LABEL_CARD, JUMP_URL, GUILD, GAMES, CARD_MSG)


# Add and remove ---------------------------------------------------------------------------------------

async def test_press_without_the_role_adds_it_and_says_following(harness) -> None:
    h = await harness()
    await h.presses.on_reaction(follow_press())
    assert role_writes(h.state) == [("PUT", ROLE_PATH)]
    assert dm_channel_texts(h.state, PRESSER) == [text.FOLLOW_ADDED.format(card=render.escape(CARD_TITLE))]


async def test_press_with_the_role_removes_it_and_says_stopped(harness) -> None:
    h = await harness()
    await h.presses.on_reaction(follow_press(member_roles=frozenset({ALPHA_ROLE})))
    assert role_writes(h.state) == [("DELETE", ROLE_PATH)]
    assert dm_channel_texts(h.state, PRESSER) == [text.FOLLOW_REMOVED.format(card=render.escape(CARD_TITLE))]


async def test_the_role_call_carries_the_audit_reason_and_no_user_id_is_logged(harness, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel")
    h = await harness()
    await h.presses.on_reaction(follow_press())
    [put] = [r for r in h.state.requests if r.method == "PUT" and r.path == ROLE_PATH]
    assert put.headers["X-Audit-Log-Reason"] == "card follow role"
    for record in panel_records(caplog):
        assert PRESSER not in record.getMessage()
        assert "user_id" not in getattr(record, "panel_fields", {})


# Refusals and admin alerts ----------------------------------------------------------------------------

async def test_card_without_a_follow_role_is_unavailable_with_no_role_call_and_no_admin_alert(harness) -> None:
    # A card cleared with `!card clear <slug> follow_role` is deliberate, so the presser hears "unavailable" only.
    h = await harness()
    await h.presses.on_reaction(follow_press(message=SECOND_MSG))
    assert role_requests(h.state) == []
    assert dm_channel_texts(h.state, PRESSER) == [
        text.FOLLOW_UNAVAILABLE.format(card=render.escape(SECOND_TITLE))]
    assert dm_channel_texts(h.state, ADMIN) == []


async def test_a_role_snapshot_that_is_not_allowlisted_alerts_the_admins_once(harness, monkeypatch) -> None:
    # The card still has its role when read, but the allowlist no longer holds it (a DB change between the reads).
    h = await harness()
    monkeypatch.setattr(cards, "follow_role_ids", lambda conn: frozenset())
    await h.presses.on_reaction(follow_press())
    await h.presses.on_reaction(follow_press(user=PRESSER_2))
    assert role_requests(h.state) == []
    assert dm_channel_texts(h.state, PRESSER) == [text.FOLLOW_UNAVAILABLE.format(card=render.escape(CARD_TITLE))]
    assert dm_channel_texts(h.state, PRESSER_2) == [text.FOLLOW_UNAVAILABLE.format(card=render.escape(CARD_TITLE))]
    assert dm_channel_texts(h.state, ADMIN) == [
        text.FOLLOW_ADMIN_ALERT.format(slug="mc", reason=text.FOLLOW_REASON["not_allowlisted"])]


async def test_hierarchy_refusal_is_unavailable_and_alerts_the_admins_once(harness) -> None:
    h = await harness()
    h.state.roles[GUILD] = [dict(EVERYONE), dict(BOT_ROLE_DATA), {**ALPHA_DATA, "position": 20}]
    await h.presses.on_reaction(follow_press())
    await h.presses.on_reaction(follow_press(user=PRESSER_2))
    assert role_writes(h.state) == []
    assert dm_channel_texts(h.state, PRESSER) == [text.FOLLOW_UNAVAILABLE.format(card=render.escape(CARD_TITLE))]
    assert dm_channel_texts(h.state, PRESSER_2) == [text.FOLLOW_UNAVAILABLE.format(card=render.escape(CARD_TITLE))]
    assert dm_channel_texts(h.state, ADMIN) == [
        text.FOLLOW_ADMIN_ALERT.format(slug="mc", reason=text.FOLLOW_REASON["hierarchy"])]


async def test_a_403_on_the_role_call_is_unavailable_and_admins_hear_once_per_card(harness) -> None:
    h = await harness()
    forbid_adds(h.state)
    await h.presses.on_reaction(follow_press())
    assert dm_channel_texts(h.state, PRESSER) == [text.FOLLOW_UNAVAILABLE.format(card=render.escape(CARD_TITLE))]
    assert dm_channel_texts(h.state, ADMIN) == [
        text.FOLLOW_ADMIN_ALERT.format(slug="mc", reason=text.FOLLOW_REASON["forbidden"])]

    await h.presses.on_reaction(follow_press(user=PRESSER_2))
    assert dm_channel_texts(h.state, PRESSER_2) == [text.FOLLOW_UNAVAILABLE.format(card=render.escape(CARD_TITLE))]
    assert len(dm_channel_texts(h.state, ADMIN)) == 1


async def test_the_admin_alert_is_once_per_card_and_per_reason(harness) -> None:
    h = await harness()
    h.state.roles[GUILD] = [dict(EVERYONE), dict(BOT_ROLE_DATA), {**ALPHA_DATA, "position": 20}]
    await h.presses.on_reaction(follow_press())
    h.state.roles[GUILD] = [dict(EVERYONE), dict(BOT_ROLE_DATA), dict(ALPHA_DATA)]
    forbid_adds(h.state)
    await h.presses.on_reaction(follow_press(user=PRESSER_2))
    assert dm_channel_texts(h.state, ADMIN) == [
        text.FOLLOW_ADMIN_ALERT.format(slug="mc", reason=text.FOLLOW_REASON["hierarchy"]),
        text.FOLLOW_ADMIN_ALERT.format(slug="mc", reason=text.FOLLOW_REASON["forbidden"]),
    ]


async def test_no_bot_id_is_unavailable_with_no_admin_alert_and_no_role_call(harness) -> None:
    h = await harness()
    h.identity.user_id = None
    await h.presses.on_reaction(follow_press())
    assert role_requests(h.state) == []
    assert dm_channel_texts(h.state, PRESSER) == [text.FOLLOW_UNAVAILABLE.format(card=render.escape(CARD_TITLE))]
    assert dm_channel_texts(h.state, ADMIN) == []


async def test_a_card_that_is_no_longer_live_gets_no_reply_and_no_role_call(harness) -> None:
    h = await harness()
    await h.db.run(lambda c: cards.set_state(c, CARD_ID, "archived", 2))
    await h.interactions.follow(Press(card_id=CARD_ID, action=Action.FOLLOW, user_id=PRESSER, member_role_ids=None,
                                      surface=Surface("card", GAMES, CARD_MSG, GUILD, True)))
    assert role_requests(h.state) == []
    assert dm_channel_texts(h.state, PRESSER) == []


async def test_a_press_with_no_games_community_known_is_unavailable_with_no_role_call(harness) -> None:
    h = await harness()
    h.state.channels.pop(GAMES)   # the games lookup now fails, and nothing is cached for it
    await h.interactions.follow(Press(card_id=CARD_ID, action=Action.FOLLOW, user_id=PRESSER,
                                      member_role_ids=frozenset(), surface=Surface("card", GAMES, CARD_MSG, GUILD,
                                                                                    True)))
    assert role_requests(h.state) == []
    assert dm_channel_texts(h.state, PRESSER) == [text.FOLLOW_UNAVAILABLE.format(card=render.escape(CARD_TITLE))]


# A copy outside the home community --------------------------------------------------------------------

async def test_a_copy_elsewhere_gets_a_jump_link_and_no_role_call(harness) -> None:
    h = await harness()
    await h.presses.on_reaction(follow_press(channel=FAR_COPY, message=FAR_MSG, guild=OTHER_GUILD))
    assert role_requests(h.state) == []
    assert dm_channel_texts(h.state, PRESSER) == [
        text.FOLLOW_ELSEWHERE.format(card=render.escape(CARD_TITLE), link=jump_to_games_card())]


async def test_the_jump_link_survives_brackets_in_the_card_title(harness) -> None:
    h = await harness()
    await h.presses.on_reaction(follow_press(channel=FAR_COPY, message=FAR_MSG, guild=OTHER_GUILD))
    [dm] = dm_channel_texts(h.state, PRESSER)
    assert render.JUMP_LINK_RE.findall(dm) == [(text.JUMP_LABEL_CARD, GUILD, GAMES, CARD_MSG)]


async def test_a_copy_elsewhere_with_no_card_message_gets_the_plain_text(harness) -> None:
    h = await harness()
    await h.db.run(lambda c: cards.clear_message_if(c, GAMES, CARD_MSG))
    await h.presses.on_reaction(follow_press(channel=FAR_COPY, message=FAR_MSG, guild=OTHER_GUILD))
    assert role_requests(h.state) == []
    assert dm_channel_texts(h.state, PRESSER) == [
        text.FOLLOW_ELSEWHERE_NO_LINK.format(card=render.escape(CARD_TITLE))]
    assert "](" not in dm_channel_texts(h.state, PRESSER)[0]
