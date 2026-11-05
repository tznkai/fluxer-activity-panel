# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`!card interactions on|off [slug]` through the admin path: the kill switch removes the bot's reactions from every card
and copy, `on` seeds them again, and a tap while the switch is off reaches no handler and makes no request. Real SQLite,
the fake REST server and a recording press handler. The emoji are written as escapes; the tests never contain a literal
emoji."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.rest import encode_emoji
from fluxer_panel import cards, interaction_settings, subscriptions, text
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import FOLLOW_EMOJI, PLAY_NOW_EMOJI, WAKE_EMOJI, Press, Presses, Surface
from fluxer_panel.seeding import Seeder
from tests.fakes import FakeFluxerState, make_rest_app
from tests.test_card_commands import World, build

GUILD = "1001"
OTHER_GUILD = "1009"
GAMES = "2100"
COPY_CHANNEL = "2002"
OTHER_CHANNEL = "2301"            # a text channel in OTHER_GUILD, a community the games channel is not in
CARD_MSG = "5101"
COPY_MSG = "5102"
OTHER_COPY_MSG = "5302"
AB_MSG = "5201"                   # a second live card's message in the games channel
PRESSER = "3001"
BOT_ID = "9001"
ROLE = "7001"
HASH = "c" * 64
EMOJI = (WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI)


def reaction_path(channel: str, message: str, emoji: str) -> str:
    return f"/api/v1/channels/{channel}/messages/{message}/reactions/{encode_emoji(emoji)}"


def sent(world: World, method: str) -> list[str]:
    """The raw paths of the requests with this method, in the order they were sent."""
    return [r.raw_path for r in world.state.requests if r.method == method]


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState()
    server = await aiohttp_server(make_rest_app(state))
    return state, str(server.make_url("/")).rstrip("/")


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
async def world(db, fake, session) -> World:
    """Games channel set, and the reaction wiring that `__main__` gives the command context."""
    state, base = fake
    state.guilds[GUILD] = {"id": GUILD}
    state.guilds[OTHER_GUILD] = {"id": OTHER_GUILD}
    state.channels[GAMES] = {"id": GAMES, "type": 0, "guild_id": GUILD, "name": "games"}
    state.channels[COPY_CHANNEL] = {"id": COPY_CHANNEL, "type": 0, "guild_id": GUILD, "name": "lounge"}
    state.channels[OTHER_CHANNEL] = {"id": OTHER_CHANNEL, "type": 0, "guild_id": OTHER_GUILD, "name": "elsewhere"}
    built = await build(db, state, base, session, GAMES_CHANNEL_ID=GAMES)
    built.ctx.seeder = Seeder(rest=built.rest, pacer=ChannelPacer(), db=db)
    built.ctx.games_guild = GamesGuild(built.rest, built.config)
    built.ctx.identity = BotIdentity(user_id=BOT_ID)
    return built


async def live_card(world: World, slug: str, message: str) -> cards.Card:
    """A live card whose message the bot has posted in the games channel."""
    await world.run(f"!card new {slug} Minecraft Survival")
    await world.run(f"!card publish {slug}")
    card = await world.card(slug)
    assert card is not None
    await world.db.run(lambda c: cards.set_message(c, card.id, GAMES, message, None))
    world.state.messages[(GAMES, message)] = {"embeds": []}
    return card


async def add_copy(world: World, card: cards.Card, channel: str, guild: str, message: str) -> None:
    """A copy the bot has posted in `channel`, which belongs to `guild`."""
    await world.run(f"!card subscribe {card.slug} {guild} {channel}")
    world.state.messages[(channel, message)] = {"embeds": []}
    await world.db.run(lambda c: subscriptions.set_message(c, card.id, channel, message, HASH))


class RecordingHandler:
    def __init__(self) -> None:
        self.presses: list[Press] = []

    async def handle(self, press: Press) -> None:
        self.presses.append(press)

    async def me_too_changed(self, card_id: int, surface: Surface, user_id: str) -> None:
        return None


def presses_for(world: World) -> tuple[Presses, RecordingHandler]:
    handler = RecordingHandler()
    presses = Presses(db=world.db, rest=world.rest, config=world.config, handler=handler,
                      games_guild=world.ctx.games_guild, pacer=ChannelPacer(), identity=BotIdentity(user_id=BOT_ID),
                      clock=time.time)
    return presses, handler


def tap(channel: str, message: str, name: str = WAKE_EMOJI, guild: str | None = GUILD) -> events.ReactionAdd:
    return events.ReactionAdd(user_id=PRESSER, channel_id=channel, message_id=message, guild_id=guild,
                              emoji=events.Emoji(name=name, id=None), member_role_ids=None)


# off ---------------------------------------------------------------------------------------------------

async def test_off_removes_the_bots_reactions_from_the_card_and_each_copy(world: World) -> None:
    card = await live_card(world, "mc", CARD_MSG)
    await add_copy(world, card, COPY_CHANNEL, GUILD, COPY_MSG)
    world.clear_requests()
    await world.run("!card interactions off")
    expected = [reaction_path(GAMES, CARD_MSG, emoji) for emoji in EMOJI]
    expected += [reaction_path(COPY_CHANNEL, COPY_MSG, emoji) for emoji in EMOJI]
    assert sent(world, "DELETE") == expected
    assert world.last_reply() == text.INTERACTIONS_OFF
    assert await world.db.run(interaction_settings.enabled) is False


async def test_a_tap_after_off_reaches_no_handler_and_makes_no_request(world: World) -> None:
    await live_card(world, "mc", CARD_MSG)
    await world.run("!card interactions off")
    world.clear_requests()
    presses, handler = presses_for(world)
    await presses.on_reaction(tap(GAMES, CARD_MSG))
    assert handler.presses == []
    assert world.state.requests == []


# on ----------------------------------------------------------------------------------------------------

async def test_on_adds_the_wake_and_play_now_reactions_to_the_card_and_its_home_copy(world: World) -> None:
    card = await live_card(world, "mc", CARD_MSG)
    await add_copy(world, card, COPY_CHANNEL, GUILD, COPY_MSG)
    await world.run("!card interactions off")
    world.clear_requests()
    await world.run("!card interactions on")
    # A home copy posts in its own channel, so it always takes the play-now emoji. The card has no play-now channel.
    assert sent(world, "PUT") == [reaction_path(GAMES, CARD_MSG, WAKE_EMOJI) + "/@me",
                                  reaction_path(COPY_CHANNEL, COPY_MSG, WAKE_EMOJI) + "/@me",
                                  reaction_path(COPY_CHANNEL, COPY_MSG, PLAY_NOW_EMOJI) + "/@me"]
    assert world.last_reply() == text.INTERACTIONS_ON
    assert await world.db.run(interaction_settings.enabled) is True


async def test_on_gives_the_play_now_emoji_to_home_copies_only(world: World) -> None:
    card = await live_card(world, "mc", CARD_MSG)
    await add_copy(world, card, COPY_CHANNEL, GUILD, COPY_MSG)
    await add_copy(world, card, OTHER_CHANNEL, OTHER_GUILD, OTHER_COPY_MSG)
    await world.run("!card interactions off")
    world.clear_requests()
    await world.run("!card interactions on")
    assert sent(world, "PUT") == [reaction_path(GAMES, CARD_MSG, WAKE_EMOJI) + "/@me",
                                  reaction_path(COPY_CHANNEL, COPY_MSG, WAKE_EMOJI) + "/@me",
                                  reaction_path(COPY_CHANNEL, COPY_MSG, PLAY_NOW_EMOJI) + "/@me",
                                  reaction_path(OTHER_CHANNEL, OTHER_COPY_MSG, WAKE_EMOJI) + "/@me"]


async def test_on_adds_the_follow_reaction_to_home_surfaces_with_a_follow_role(world: World) -> None:
    card = await live_card(world, "mc", CARD_MSG)
    await add_copy(world, card, COPY_CHANNEL, GUILD, COPY_MSG)
    await world.db.run(lambda c: cards.set_follow_role(c, card.id, GUILD, ROLE))
    await world.run("!card interactions off")
    world.clear_requests()
    await world.run("!card interactions on")
    expected = [reaction_path(GAMES, CARD_MSG, WAKE_EMOJI), reaction_path(GAMES, CARD_MSG, FOLLOW_EMOJI),
                reaction_path(COPY_CHANNEL, COPY_MSG, WAKE_EMOJI), reaction_path(COPY_CHANNEL, COPY_MSG, FOLLOW_EMOJI),
                reaction_path(COPY_CHANNEL, COPY_MSG, PLAY_NOW_EMOJI)]
    assert sent(world, "PUT") == [path + "/@me" for path in expected]


async def test_on_leaves_the_follow_reaction_off_a_copy_outside_the_games_community(world: World) -> None:
    card = await live_card(world, "mc", CARD_MSG)
    await add_copy(world, card, OTHER_CHANNEL, OTHER_GUILD, OTHER_COPY_MSG)
    await world.db.run(lambda c: cards.set_follow_role(c, card.id, GUILD, ROLE))
    await world.run("!card interactions off")
    world.clear_requests()
    await world.run("!card interactions on")
    assert sent(world, "PUT") == [reaction_path(GAMES, CARD_MSG, WAKE_EMOJI) + "/@me",
                                  reaction_path(GAMES, CARD_MSG, FOLLOW_EMOJI) + "/@me",
                                  reaction_path(OTHER_CHANNEL, OTHER_COPY_MSG, WAKE_EMOJI) + "/@me"]


async def test_on_after_off_restores_the_presses(world: World) -> None:
    card = await live_card(world, "mc", CARD_MSG)
    presses, handler = presses_for(world)
    await world.run("!card interactions off")
    await presses.on_reaction(tap(GAMES, CARD_MSG))
    assert handler.presses == []
    await world.run("!card interactions on")
    await presses.on_reaction(tap(GAMES, CARD_MSG))
    assert [press.card_id for press in handler.presses] == [card.id]


# one card -----------------------------------------------------------------------------------------------

async def test_off_for_one_card_removes_only_its_reactions_and_its_copies(world: World) -> None:
    card = await live_card(world, "mc", CARD_MSG)
    await add_copy(world, card, COPY_CHANNEL, GUILD, COPY_MSG)
    await live_card(world, "ab", AB_MSG)
    world.clear_requests()
    await world.run("!card interactions off mc")
    expected = [reaction_path(GAMES, CARD_MSG, emoji) for emoji in EMOJI]
    expected += [reaction_path(COPY_CHANNEL, COPY_MSG, emoji) for emoji in EMOJI]
    assert sent(world, "DELETE") == expected
    assert world.last_reply() == text.INTERACTIONS_CARD_OFF.format(slug="mc")
    assert await world.db.run(interaction_settings.enabled) is True


async def test_a_tap_on_a_switched_off_card_is_ignored_and_another_card_still_responds(world: World) -> None:
    await live_card(world, "mc", CARD_MSG)
    await live_card(world, "ab", AB_MSG)
    await world.run("!card interactions off mc")
    presses, handler = presses_for(world)
    await presses.on_reaction(tap(GAMES, CARD_MSG))
    await presses.on_reaction(tap(GAMES, AB_MSG, guild=GUILD))
    assert [press.card_id for press in handler.presses] == [(await world.card("ab")).id]


async def test_on_for_one_card_restores_only_its_reactions(world: World) -> None:
    card = await live_card(world, "mc", CARD_MSG)
    await add_copy(world, card, COPY_CHANNEL, GUILD, COPY_MSG)
    await live_card(world, "ab", AB_MSG)
    await world.run("!card interactions off mc")
    world.clear_requests()
    await world.run("!card interactions on mc")
    assert sent(world, "PUT") == [reaction_path(GAMES, CARD_MSG, WAKE_EMOJI) + "/@me",
                                  reaction_path(COPY_CHANNEL, COPY_MSG, WAKE_EMOJI) + "/@me",
                                  reaction_path(COPY_CHANNEL, COPY_MSG, PLAY_NOW_EMOJI) + "/@me"]
    assert world.last_reply() == text.INTERACTIONS_CARD_ON.format(slug="mc")


async def test_global_on_does_not_restore_a_card_that_is_switched_off(world: World) -> None:
    await live_card(world, "mc", CARD_MSG)
    await live_card(world, "ab", AB_MSG)
    await world.run("!card interactions off mc")
    await world.run("!card interactions off")
    world.clear_requests()
    await world.run("!card interactions on")
    assert sent(world, "PUT") == [reaction_path(GAMES, AB_MSG, WAKE_EMOJI) + "/@me"]


async def test_card_on_while_the_switch_is_off_seeds_nothing_until_the_switch_is_on(world: World) -> None:
    await live_card(world, "mc", CARD_MSG)
    await world.run("!card interactions off")
    world.clear_requests()
    await world.run("!card interactions on mc")
    assert sent(world, "PUT") == []
    await world.run("!card interactions on")
    assert sent(world, "PUT") == [reaction_path(GAMES, CARD_MSG, WAKE_EMOJI) + "/@me"]


# errors and access ---------------------------------------------------------------------------------------

async def test_an_unknown_slug_gets_no_such_card_and_changes_nothing(world: World) -> None:
    await live_card(world, "mc", CARD_MSG)
    world.clear_requests()
    await world.run("!card interactions off nope")
    assert world.last_reply() == text.NO_SUCH_CARD
    assert sent(world, "DELETE") == []
    assert await world.db.run(interaction_settings.enabled) is True


@pytest.mark.parametrize("command", [
    "!card interactions",
    "!card interactions maybe",
    "!card interactions on mc extra",
])
async def test_a_bad_interactions_command_gets_the_usage_and_changes_nothing(world: World, command: str) -> None:
    await live_card(world, "mc", CARD_MSG)
    world.clear_requests()
    await world.run(command)
    assert world.last_reply() == text.INTERACTIONS_USAGE
    assert sent(world, "DELETE") == [] and sent(world, "PUT") == []


@pytest.mark.parametrize("command", ["!card interactions off", "!card interactions off mc"])
async def test_missing_reaction_wiring_changes_no_state(world: World, command: str) -> None:
    """The wiring is checked before any write, so a command that cannot reach the reactions changes no setting."""
    card = await live_card(world, "mc", CARD_MSG)
    world.ctx.seeder = None
    world.clear_requests()
    with pytest.raises(RuntimeError):
        await world.run(command)
    assert await world.db.run(interaction_settings.enabled) is True
    assert await world.db.run(lambda c: interaction_settings.card_off(c, card.id)) is False
    assert sent(world, "DELETE") == []


async def test_a_non_admin_gets_no_reply_and_no_request(world: World, caplog: pytest.LogCaptureFixture) -> None:
    await live_card(world, "mc", CARD_MSG)
    world.clear_requests()
    caplog.set_level(logging.INFO)
    await world.run("!card interactions off", author=PRESSER)
    assert world.state.requests == []
    assert any(r.getMessage() == "admin_denied" for r in caplog.records)
    assert await world.db.run(interaction_settings.enabled) is True


# locks ---------------------------------------------------------------------------------------------------

async def test_no_card_lock_is_held_while_reactions_are_removed_or_added(world: World, monkeypatch) -> None:
    card = await live_card(world, "mc", CARD_MSG)
    await add_copy(world, card, COPY_CHANNEL, GUILD, COPY_MSG)
    seeder = world.ctx.seeder
    assert seeder is not None
    held: list[bool] = []
    real_unseed, real_seed = seeder.unseed, seeder.seed

    async def unseed(channel_id: str, message_id: str, emojis: Any) -> None:
        held.append(world.ctx.locks.get(card.id).locked())
        await real_unseed(channel_id, message_id, emojis)

    async def seed(channel_id: str, message_id: str, wanted: Any, message: Any = None) -> None:
        held.append(world.ctx.locks.get(card.id).locked())
        await real_seed(channel_id, message_id, wanted, message)

    monkeypatch.setattr(seeder, "unseed", unseed)
    monkeypatch.setattr(seeder, "seed", seed)
    await world.run("!card interactions off")
    await world.run("!card interactions on")
    assert len(held) == 4
    assert not any(held)
