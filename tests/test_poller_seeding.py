# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The poller seeds the bot's reactions on each posted card and copy, and on reconcile. Seeding runs outside the card
lock, under the channel pacer, and a 403 is logged once per channel without failing the publish. Runs against a real
SQLite file, the fake Fluxer REST server and the card commands."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerHTTPError
from fluxer_client.roles import RoleCache
from fluxer_panel import board, cards, interaction_settings, subscriptions
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poller import Poller
from fluxer_panel.presses import FOLLOW_EMOJI, PLAY_NOW_EMOJI, POLL_EMOJI, SESSION_EMOJI, WAKE_EMOJI
from fluxer_panel.seeding import Seeder
from fluxer_panel.sources.registry import Sources
from tests.fakes import FakeFluxerState, Override, make_rest_app
from tests.test_card_commands import World, build

GUILD = "1001"                  # the games community
OTHER_GUILD = "1002"            # a community the bot posts a copy into
GAMES = "2100"                  # the games channel, in GUILD
HOME_COPY = "2003"              # a copy's channel in GUILD
OTHER_COPY = "2004"             # a copy's channel in OTHER_GUILD
BOT = "1900"                    # the fake's bot_user_id: a reaction by it reads as me=True
ROLE_BOT = "5000"
NEW_ROLE = "7001"               # the id the fake gives the next role it creates
AUTHOR = "1001"
ROLES = f"/api/v1/guilds/{GUILD}/roles"
PLAY_NOW_CHANNEL = "2400"


class Clock:
    """The poller's and the pacer's clock: `now` moves only when a test or the fake sleep moves it."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class TimedPacer(ChannelPacer):
    """A real pacer on a fake clock: its sleeps advance the clock, and each call is recorded with the time it went out."""

    def __init__(self, clock: Clock) -> None:
        self._clock_of = clock
        self.calls: list[tuple[str, float]] = []
        super().__init__(clock=clock, sleep=self._sleep)

    async def _sleep(self, seconds: float) -> None:
        self._clock_of.now += seconds

    async def wait(self, channel_id: str) -> None:
        await super().wait(channel_id)
        self.calls.append((channel_id, self._clock_of.now))


class WatchingSeeder(Seeder):
    """Records, at each seed call, whether the seeded card's own lock is held.

    Only the card's own lock counts: cards publish concurrently, so another card's publish may hold its lock at that
    moment, which is correct and was the cause of a 1-in-25 flake when this checked every card's lock."""

    def __init__(self, *, locks: Any, card_ids: list[int], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._locks = locks
        self._card_ids = card_ids
        self.held: list[bool] = []

    async def seed(self, channel_id: str, message_id: str, wanted: Any, message: Any = None, *, card_id: int) -> None:
        assert card_id in self._card_ids
        self.held.append(self._locks.get(card_id).locked())
        await super().seed(channel_id, message_id, wanted, message, card_id=card_id)


def base_roles() -> list[dict[str, Any]]:
    everyone = {"id": GUILD, "name": "@everyone", "permissions": "0", "position": 0, "mentionable": False}
    bot = {"id": ROLE_BOT, "name": "bot", "permissions": "8", "position": 10, "mentionable": False}
    return [everyone, bot]


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


async def make_world(db: Database, session: aiohttp.ClientSession, aiohttp_server: Any, **env: str) -> World:
    """A world with the games community, the bot's role and the follow role machinery wired, as the panel wires it."""
    state = FakeFluxerState()
    state.guilds[GUILD] = {"id": GUILD}
    state.channels[GAMES] = {"id": GAMES, "type": 0, "guild_id": GUILD, "name": "games"}
    state.roles = {GUILD: base_roles()}
    state.members = {(GUILD, BOT): {"roles": [ROLE_BOT]}}
    state.next_role_id = int(NEW_ROLE)
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    world = await build(db, state, base, session, GAMES_CHANNEL_ID=GAMES, **env)
    world.ctx.roles = RoleCache(world.rest)
    world.ctx.identity = BotIdentity(user_id=BOT)
    world.ctx.games_guild = GamesGuild(world.rest, world.config)
    world.ctx.seeder = Seeder(rest=world.rest, pacer=ChannelPacer(), db=db)
    return world


@pytest.fixture
async def world(db: Database, session: aiohttp.ClientSession, aiohttp_server: Any) -> World:
    return await make_world(db, session, aiohttp_server, PLAY_NOW_CHANNEL_ID=PLAY_NOW_CHANNEL)


def make_poller(world: World, clock: Clock, seeder: Seeder | None = None) -> Poller:
    return Poller(db=world.db, rest=world.rest, sources=Sources(managers={}, probes={}), config=world.config,
                  locks=world.ctx.locks, clock=clock, games_guild=world.ctx.games_guild,
                  seeder=seeder if seeder is not None else world.ctx.seeder)


async def live_card(db: Database, slug: str, *, follow: bool = True) -> int:
    """A live card, with or without a stored follow role, written straight to the store."""
    def tx(conn: Any) -> int:
        card_id = cards.create_card(conn, slug=slug, title=slug.upper(), author_id=AUTHOR, now=0)
        cards.set_state(conn, card_id, "live", 0)
        if follow:
            cards.set_follow_role(conn, card_id, GUILD, str(7100 + card_id))   # one role per card, as publish makes
        return card_id

    return await db.run(tx)


async def card_message(db: Database, card_id: int) -> str:
    card = await db.run(lambda c: cards.get_by_id(c, card_id))
    assert card is not None and card.message_id is not None
    return card.message_id


def puts(state: FakeFluxerState, channel_id: str, message_id: str) -> list[str]:
    """The emoji the bot reacted with on one message, in the order the PUT calls were made."""
    prefix = f"/api/v1/channels/{channel_id}/messages/{message_id}/reactions/"
    return [r.path[len(prefix):].rsplit("/", 1)[0] for r in state.requests
            if r.method == "PUT" and r.path.startswith(prefix)]


def reacted(state: FakeFluxerState, channel_id: str, message_id: str) -> set[str]:
    """The emoji the bot has on one message now."""
    return {name for name, users in state.reactions.get((channel_id, message_id), {}).items() if BOT in users}


# AC5.6: what each newly posted surface gets -----------------------------------------------------------------


async def test_a_card_published_with_the_command_gets_wake_bell_and_play_now(world: World) -> None:
    await world.run("!card new mc Minecraft")
    await world.run("!card publish mc")
    poller = make_poller(world, Clock())

    await poller.tick()

    card = await world.card("mc")
    assert card is not None and card.message_id is not None
    assert puts(world.state, GAMES, card.message_id) == [WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI, SESSION_EMOJI, POLL_EMOJI]


async def test_without_a_follow_role_a_new_card_gets_no_bell(world: World, db: Database) -> None:
    card_id = await live_card(db, "mc", follow=False)
    await make_poller(world, Clock()).tick()

    assert puts(world.state, GAMES, await card_message(db, card_id)) == [WAKE_EMOJI, PLAY_NOW_EMOJI, SESSION_EMOJI, POLL_EMOJI]


async def test_without_a_play_now_target_a_new_card_gets_the_wake_emoji_only(db: Database, session: Any,
                                                                             aiohttp_server: Any) -> None:
    world = await make_world(db, session, aiohttp_server)
    card_id = await live_card(db, "mc", follow=False)
    await make_poller(world, Clock()).tick()

    assert puts(world.state, GAMES, await card_message(db, card_id)) == [WAKE_EMOJI]


async def test_a_home_copy_gets_wake_bell_and_play_now(world: World, db: Database) -> None:
    card_id = await live_card(db, "mc")
    await db.run(lambda c: subscriptions.add(c, card_id, GUILD, HOME_COPY))
    await make_poller(world, Clock()).tick()

    copy = await db.run(lambda c: subscriptions.list_for_card(c, card_id))
    assert copy[0].message_id is not None
    assert puts(world.state, HOME_COPY, copy[0].message_id) == [WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI, SESSION_EMOJI, POLL_EMOJI]


async def test_a_copy_in_another_community_gets_wake_bell_and_play_now(world: World, db: Database) -> None:
    card_id = await live_card(db, "mc")
    await db.run(lambda c: subscriptions.add(c, card_id, OTHER_GUILD, OTHER_COPY))
    await make_poller(world, Clock()).tick()

    copy = await db.run(lambda c: subscriptions.list_for_card(c, card_id))
    assert copy[0].message_id is not None
    assert puts(world.state, OTHER_COPY, copy[0].message_id) == [WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI]


# AC5.6: reconcile adds only what is missing ----------------------------------------------------------------


async def test_reconcile_adds_only_the_missing_emoji(world: World, db: Database) -> None:
    card_id = await live_card(db, "mc")
    message_id = "5001"
    world.state.messages[(GAMES, message_id)] = {"content": "card"}
    world.state.reactions[(GAMES, message_id)] = {WAKE_EMOJI: [BOT]}
    await db.run(lambda c: cards.set_message(c, card_id, GAMES, message_id, None))

    await make_poller(world, Clock()).reconcile()

    assert puts(world.state, GAMES, message_id) == [FOLLOW_EMOJI, PLAY_NOW_EMOJI, SESSION_EMOJI, POLL_EMOJI]
    assert reacted(world.state, GAMES, message_id) == {WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI, SESSION_EMOJI, POLL_EMOJI}


# The kill switch -------------------------------------------------------------------------------------------


async def test_the_kill_switch_seeds_nothing_on_post_or_reconcile(world: World, db: Database) -> None:
    card_id = await live_card(db, "mc")
    await db.run(lambda c: interaction_settings.set_enabled(c, False))
    poller = make_poller(world, Clock())

    await poller.tick()
    await poller.reconcile()

    assert [r for r in world.state.requests if r.method == "PUT"] == []
    assert await card_message(db, card_id)


# AC5.7: pacing and the lock -------------------------------------------------------------------------------


async def test_seeding_never_runs_under_a_card_lock(world: World, db: Database) -> None:
    card_ids = [await live_card(db, f"c{n}") for n in range(2)]
    seeder = WatchingSeeder(locks=world.ctx.locks, card_ids=card_ids, rest=world.rest, pacer=ChannelPacer(), db=db)

    await make_poller(world, Clock(), seeder=seeder).tick()

    assert len(seeder.held) == 2        # one seed call per posted card
    assert seeder.held == [False, False]


async def test_seeding_keeps_each_channel_within_twenty_reaction_calls_per_ten_seconds(world: World,
                                                                                       db: Database) -> None:
    for n in range(7):
        await live_card(db, f"c{n}")
    clock = Clock()
    pacer = TimedPacer(clock)
    seeder = Seeder(rest=world.rest, pacer=pacer, db=db)

    await make_poller(world, clock, seeder=seeder).tick()

    times = [t for channel, t in pacer.calls if channel == GAMES]
    assert len(times) == 35                       # seven cards, five emoji each (the calendar and the poll too)
    for t in times:
        assert sum(1 for x in times if t - 10 < x <= t) <= 20
    assert times[20] >= times[0] + 10


# AC5.8: a 403 is logged once per channel and the publish still succeeds --------------------------------


async def test_a_403_on_seeding_is_logged_once_per_channel_and_both_cards_post(world: World, db: Database,
                                                                               monkeypatch: pytest.MonkeyPatch,
                                                                               caplog: pytest.LogCaptureFixture) -> None:
    card_ids = [await live_card(db, "c0"), await live_card(db, "c1")]
    real_add = world.rest.add_reaction

    async def refuse_in_games(channel_id: str, message_id: str, emoji: str) -> Any:
        if channel_id == GAMES:
            raise FluxerHTTPError(403, None, "SENTINEL")
        return await real_add(channel_id, message_id, emoji)

    monkeypatch.setattr(world.rest, "add_reaction", refuse_in_games)
    caplog.set_level(logging.WARNING)

    await make_poller(world, Clock()).tick()

    forbidden = [r for r in caplog.records if r.getMessage() == "seed_forbidden"]
    assert len(forbidden) == 1
    for card_id in card_ids:
        assert await card_message(db, card_id)


# A failed publish or a failed surface does not drop the reactions of the surfaces that were posted -----------------


def gets_to(state: FakeFluxerState, channel_id: str, message_id: str) -> int:
    path = f"/api/v1/channels/{channel_id}/messages/{message_id}"
    return len([r for r in state.requests if r.method == "GET" and r.path == path])


async def test_the_card_posted_before_a_failed_copy_pass_is_still_seeded(world: World, db: Database,
                                                                         monkeypatch: pytest.MonkeyPatch,
                                                                         caplog: pytest.LogCaptureFixture) -> None:
    card_id = await live_card(db, "mc")
    caplog.set_level(logging.WARNING)

    async def fail(self: Poller, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("SENTINEL")

    monkeypatch.setattr(Poller, "_publish_subscriptions", fail)
    await make_poller(world, Clock()).tick()

    assert puts(world.state, GAMES, await card_message(db, card_id)) == [WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI, SESSION_EMOJI, POLL_EMOJI]
    assert [r.getMessage() for r in caplog.records].count("card_poll_failed") == 1


async def test_a_seeding_error_on_one_surface_stops_neither_the_other_surfaces_nor_the_board(
        world: World, db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    card_ids = [await live_card(db, "c0"), await live_card(db, "c1")]
    await db.run(lambda c: subscriptions.add(c, card_ids[0], GUILD, HOME_COPY))
    await make_poller(world, Clock()).tick()          # posts both cards, the copy and the board
    caplog.set_level(logging.WARNING)
    card_messages = [await card_message(db, card_id) for card_id in card_ids]
    copy = await db.run(lambda c: subscriptions.list_for_card(c, card_ids[0]))
    assert copy[0].message_id is not None
    stored_board = await db.run(board.get)
    assert stored_board.message_id is not None
    world.state.requests.clear()

    async def fail(card_id: int) -> bool:
        raise RuntimeError("SENTINEL")

    monkeypatch.setattr(world.ctx.seeder, "enabled_for", fail)
    await make_poller(world, Clock()).reconcile()

    assert gets_to(world.state, GAMES, card_messages[0]) == 1
    assert gets_to(world.state, GAMES, card_messages[1]) == 1
    assert gets_to(world.state, HOME_COPY, copy[0].message_id) == 1
    assert gets_to(world.state, GAMES, stored_board.message_id) == 1
    failed = [r for r in caplog.records if r.getMessage() == "seed_failed"]
    assert len(failed) == 3
    assert sorted(getattr(r, FIELDS_ATTR)["card_id"] for r in failed) == sorted([card_ids[0], card_ids[0], card_ids[1]])
    assert all(getattr(r, FIELDS_ATTR)["reason"] == "RuntimeError" for r in failed)


async def test_a_home_copy_reposted_after_a_404_edit_keeps_its_community_and_gets_all_three_emoji(
        world: World, db: Database) -> None:
    card_id = await live_card(db, "mc")
    await db.run(lambda c: subscriptions.add(c, card_id, GUILD, HOME_COPY))
    poller = make_poller(world, Clock())
    await poller.tick()
    old = (await db.run(lambda c: subscriptions.list_for_card(c, card_id)))[0].message_id
    assert old is not None
    await db.run(lambda c: cards.update_fields(c, card_id, 0, title="Beta"))   # the copy's rendering changes
    world.state.overrides[f"PATCH /api/v1/channels/{HOME_COPY}/messages/{old}"] = Override(
        404, b'{"code": "UNKNOWN_MESSAGE", "message": "x"}')

    await poller.tick()

    copy = (await db.run(lambda c: subscriptions.list_for_card(c, card_id)))[0]
    assert copy.message_id is not None and copy.message_id != old
    assert puts(world.state, HOME_COPY, copy.message_id) == [WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI, SESSION_EMOJI, POLL_EMOJI]


# AC5.6: reconcile seeds a copy the way it seeds a card ---------------------------------------------------------


async def stored_copy(world: World, db: Database, channel_id: str, guild_id: str, message_id: str,
                      reacted_with: list[str]) -> int:
    card_id = await live_card(db, "mc")
    await db.run(lambda c: subscriptions.add(c, card_id, guild_id, channel_id))
    world.state.messages[(channel_id, message_id)] = {"content": "copy"}
    if reacted_with:
        world.state.reactions[(channel_id, message_id)] = {emoji: [BOT] for emoji in reacted_with}
    await db.run(lambda c: subscriptions.set_message(c, card_id, channel_id, message_id, "0" * 64))
    return card_id


async def test_reconcile_of_a_home_copy_that_has_the_wake_emoji_adds_only_bell_and_play_now(
        world: World, db: Database) -> None:
    message_id = "5002"
    await stored_copy(world, db, HOME_COPY, GUILD, message_id, [WAKE_EMOJI])

    await make_poller(world, Clock()).reconcile()

    assert puts(world.state, HOME_COPY, message_id) == [FOLLOW_EMOJI, PLAY_NOW_EMOJI, SESSION_EMOJI, POLL_EMOJI]
    assert reacted(world.state, HOME_COPY, message_id) == {WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI, SESSION_EMOJI, POLL_EMOJI}


async def test_reconcile_of_a_copy_in_another_community_that_has_the_wake_emoji_adds_only_bell_and_play_now(
        world: World, db: Database) -> None:
    message_id = "5003"
    await stored_copy(world, db, OTHER_COPY, OTHER_GUILD, message_id, [WAKE_EMOJI])

    await make_poller(world, Clock()).reconcile()

    assert puts(world.state, OTHER_COPY, message_id) == [FOLLOW_EMOJI, PLAY_NOW_EMOJI]


async def test_reconcile_of_a_copy_in_another_community_adds_all_three_when_missing(
        world: World, db: Database) -> None:
    message_id = "5004"
    await stored_copy(world, db, OTHER_COPY, OTHER_GUILD, message_id, [])

    await make_poller(world, Clock()).reconcile()

    assert puts(world.state, OTHER_COPY, message_id) == [WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI]
    assert reacted(world.state, OTHER_COPY, message_id) == {WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI}


# A cancelled publish drops the seed jobs it queued, so a later publish on the same Poller seeds nothing stale ---------


async def test_a_cancelled_publish_drops_its_seed_jobs_and_a_later_publish_seeds_nothing_stale(
        world: World, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    card_id = await live_card(db, "mc")
    poller = make_poller(world, Clock())
    card = await db.run(lambda c: cards.get_by_id(c, card_id))
    assert card is not None
    evaluated = await poller._evaluate(card)
    assert evaluated.embed is not None

    async def cancel(self: Poller, *args: Any, **kwargs: Any) -> None:
        raise asyncio.CancelledError()

    assert evaluated.far_embed is not None
    monkeypatch.setattr(Poller, "_publish_subscriptions", cancel)
    with pytest.raises(asyncio.CancelledError):
        await poller._publish(card_id, evaluated.embed, evaluated.far_embed)

    # `_post` ran and queued the card's seed job before the cancel, so the cancel branch must drop it.
    assert card_id not in poller._to_seed
    assert puts(world.state, GAMES, await card_message(db, card_id)) == []

    monkeypatch.undo()
    await poller._publish(card_id, evaluated.embed, evaluated.far_embed)

    assert puts(world.state, GAMES, await card_message(db, card_id)) == []


# Subscription scopes: a local copy wants its own bell and hand ---------------------------------------------------

SUB_ROLE = "7200"               # a subscription's follow role, in OTHER_GUILD


async def scoped_copy(world: World, db: Database, message_id: str, reacted_with: list[str], *, role: bool) -> int:
    """A local copy in OTHER_GUILD with a stored message, and optionally its own follow role."""
    card_id = await stored_copy(world, db, OTHER_COPY, OTHER_GUILD, message_id, reacted_with)

    def tx(conn: Any) -> None:
        [sub] = subscriptions.list_for_card(conn, card_id)
        subscriptions.set_mode_if(conn, sub.id, "redirect", "local")
        if role:
            cards.set_subscription_role(conn, sub.id, SUB_ROLE)

    await db.run(tx)
    return card_id


async def test_a_local_copy_with_a_stored_role_is_seeded_with_bell_and_hand(world: World, db: Database) -> None:
    card_id = await live_card(db, "mc")
    await db.run(lambda c: subscriptions.add(c, card_id, OTHER_GUILD, OTHER_COPY))

    def tx(conn: Any) -> None:
        [sub] = subscriptions.list_for_card(conn, card_id)
        subscriptions.set_mode_if(conn, sub.id, "redirect", "local")
        cards.set_subscription_role(conn, sub.id, SUB_ROLE)

    await db.run(tx)
    await make_poller(world, Clock()).tick()

    copy = await db.run(lambda c: subscriptions.list_for_card(c, card_id))
    assert copy[0].message_id is not None
    assert puts(world.state, OTHER_COPY, copy[0].message_id) == [WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI]


async def test_a_local_copy_without_a_stored_role_is_seeded_without_bell(world: World, db: Database) -> None:
    card_id = await live_card(db, "mc")
    await db.run(lambda c: subscriptions.add(c, card_id, OTHER_GUILD, OTHER_COPY))

    def tx(conn: Any) -> None:
        [sub] = subscriptions.list_for_card(conn, card_id)
        subscriptions.set_mode_if(conn, sub.id, "redirect", "local")

    await db.run(tx)
    await make_poller(world, Clock()).tick()

    copy = await db.run(lambda c: subscriptions.list_for_card(c, card_id))
    assert copy[0].message_id is not None
    assert puts(world.state, OTHER_COPY, copy[0].message_id) == [WAKE_EMOJI, PLAY_NOW_EMOJI]


async def test_after_the_role_row_is_removed_a_reconcile_adds_no_bell(world: World, db: Database) -> None:
    message_id = "5005"
    card_id = await scoped_copy(world, db, message_id, [WAKE_EMOJI], role=True)

    def clear(conn: Any) -> None:
        [sub] = subscriptions.list_for_card(conn, card_id)
        cards.clear_subscription_role(conn, sub.id)

    await db.run(clear)

    await make_poller(world, Clock()).reconcile()

    assert puts(world.state, OTHER_COPY, message_id) == [PLAY_NOW_EMOJI]
