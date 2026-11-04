# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`!card subscribe` and `!card unsubscribe` through the admin path, and what subscriptions add to `show`, `archive` and
`help`. The fake Fluxer REST answers the community and channel checks; the database is real SQLite."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import aiohttp
import pytest

from fluxer_panel import board, cards, subscriptions, text
from fluxer_panel.card_commands import SUBCOMMANDS
from fluxer_panel.db import Database
from fluxer_panel.poller import Poller
from tests.fakes import FakeFluxerState, Override, Recorded, make_rest_app
from tests.test_card_commands import World, build

GUILD = "1001"
OTHER_GUILD = "1009"
GAMES_CHANNEL = "2100"
CHANNELS = tuple(str(2000 + n) for n in range(1, 7))   # 2001 to 2006: text channels in GUILD
HASH = "c" * 64
COPY = "5101"


def channel_path(channel_id: str) -> str:
    return f"/api/v1/channels/{channel_id}"


def copy_path(channel_id: str, message_id: str) -> str:
    return f"/api/v1/channels/{channel_id}/messages/{message_id}"


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
    state, base = fake
    state.guilds[GUILD] = {"id": GUILD}
    for channel in CHANNELS:
        state.channels[channel] = {"id": channel, "type": 0, "guild_id": GUILD, "name": "lounge"}
    return await build(db, state, base, session)


@pytest.fixture
async def games_world(db, fake, session) -> World:
    state, base = fake
    state.guilds[GUILD] = {"id": GUILD}
    for channel in CHANNELS:
        state.channels[channel] = {"id": channel, "type": 0, "guild_id": GUILD, "name": "lounge"}
    state.channels[GAMES_CHANNEL] = {"id": GAMES_CHANNEL, "type": 0, "guild_id": GUILD, "name": "games"}
    return await build(db, state, base, session, GAMES_CHANNEL_ID=GAMES_CHANNEL)


async def make_card(world: World, slug: str = "mc") -> cards.Card:
    await world.run(f"!card new {slug} Minecraft Survival")
    card = await world.card(slug)
    assert card is not None
    return card


async def subscribe_by_ids(world: World, slug: str, channel: str) -> None:
    await world.run(f"!card subscribe {slug} {GUILD} {channel}")


async def stored(world: World, card_id: int, channel: str) -> subscriptions.Subscription | None:
    return await world.db.run(lambda c: subscriptions.get(c, card_id, channel))


async def post_copy(world: World, card: cards.Card, channel: str, message_id: str = COPY) -> None:
    """A copy the bot has posted: the fake holds the message and the store holds its id and hash."""
    world.state.messages[(channel, message_id)] = {"embeds": []}
    await world.db.run(lambda c: subscriptions.set_message(c, card.id, channel, message_id, HASH))


def deletes(world: World) -> list[str]:
    return [r.path for r in world.state.requests if r.method == "DELETE"]


# subscribe -------------------------------------------------------------------------------------------

async def test_subscribe_by_two_ids_stores_an_active_subscription_and_kicks_the_poller(world: World) -> None:
    card = await make_card(world)
    await subscribe_by_ids(world, "mc", CHANNELS[0])
    assert await stored(world, card.id, CHANNELS[0]) == subscriptions.Subscription(
        card_id=card.id, guild_id=GUILD, channel_id=CHANNELS[0], message_id=None, content_hash=None, paused=False)
    assert world.last_reply() == text.SUBSCRIBED
    assert world.kicks


async def test_subscribe_accepts_a_pasted_channel_link(world: World) -> None:
    card = await make_card(world)
    await world.run(f"!card subscribe mc https://fluxer.invalid/channels/{GUILD}/{CHANNELS[1]}")
    assert await stored(world, card.id, CHANNELS[1]) is not None
    assert world.last_reply() == text.SUBSCRIBED


async def test_subscribe_refuses_the_games_channel_without_a_check(world: World, games_world: World) -> None:
    card = await make_card(games_world)
    await games_world.run(f"!card subscribe mc {GUILD} {GAMES_CHANNEL}")
    assert await stored(games_world, card.id, GAMES_CHANNEL) is None
    assert games_world.last_reply() == text.SUBSCRIPTION_IS_GAMES
    assert games_world.state.requests_to(channel_path(GAMES_CHANNEL)) == []


async def test_subscribe_refuses_a_community_the_bot_is_not_in_when_the_community_is_forbidden(world: World) -> None:
    card = await make_card(world)
    world.state.overrides[f"GET /api/v1/guilds/{OTHER_GUILD}"] = Override(403, b'{"code": "X", "message": "no"}')
    await world.run(f"!card subscribe mc {OTHER_GUILD} {CHANNELS[0]}")
    assert world.last_reply() == text.SUBSCRIPTION_NOT_IN_COMMUNITY
    assert await stored(world, card.id, CHANNELS[0]) is None


async def test_subscribe_refuses_a_community_that_is_not_found(world: World) -> None:
    card = await make_card(world)
    await world.run(f"!card subscribe mc {OTHER_GUILD} {CHANNELS[0]}")
    assert world.last_reply() == text.SUBSCRIPTION_NOT_IN_COMMUNITY
    assert await stored(world, card.id, CHANNELS[0]) is None


async def test_subscribe_asks_to_retry_when_the_community_check_fails_with_a_server_error(world: World) -> None:
    card = await make_card(world)
    world.state.overrides[f"GET /api/v1/guilds/{GUILD}"] = Override(500, b'{"code": "X", "message": "boom"}')
    await subscribe_by_ids(world, "mc", CHANNELS[0])
    assert world.last_reply() == text.CHANNEL_RETRY
    assert await stored(world, card.id, CHANNELS[0]) is None


async def test_subscribe_asks_to_retry_when_the_channel_check_fails_with_a_server_error(world: World) -> None:
    card = await make_card(world)
    world.state.overrides[f"GET {channel_path(CHANNELS[0])}"] = Override(500, b'{"code": "X", "message": "boom"}')
    await subscribe_by_ids(world, "mc", CHANNELS[0])
    assert world.last_reply() == text.CHANNEL_RETRY
    assert await stored(world, card.id, CHANNELS[0]) is None


async def test_subscribe_refuses_a_channel_that_is_missing(world: World) -> None:
    card = await make_card(world)
    await subscribe_by_ids(world, "mc", "2999")
    assert world.last_reply() == text.CHANNEL_MISSING
    assert await stored(world, card.id, "2999") is None


async def test_subscribe_refuses_a_voice_channel(world: World) -> None:
    card = await make_card(world)
    world.state.channels["2050"] = {"id": "2050", "type": 2, "guild_id": GUILD, "name": "voice"}
    await subscribe_by_ids(world, "mc", "2050")
    assert world.last_reply() == text.CHANNEL_WRONG_TYPE.format(kind=text.CHANNEL_KIND_TEXT)
    assert await stored(world, card.id, "2050") is None


async def test_subscribe_refuses_a_channel_in_another_community(world: World) -> None:
    card = await make_card(world)
    world.state.channels["2060"] = {"id": "2060", "type": 0, "guild_id": OTHER_GUILD, "name": "lounge"}
    await subscribe_by_ids(world, "mc", "2060")
    assert world.last_reply() == text.CHANNEL_OTHER_COMMUNITY
    assert await stored(world, card.id, "2060") is None


async def test_subscribe_refuses_a_target_that_is_neither_two_ids_nor_a_link_without_echoing_it(world: World) -> None:
    await make_card(world)
    await world.run("!card subscribe mc SENTINEL-CHAN")
    assert world.last_reply() == text.SUBSCRIPTION_USAGE
    assert "SENTINEL" not in world.last_reply()


async def test_subscribe_to_an_unknown_card_replies_no_such_card(world: World) -> None:
    await world.run(f"!card subscribe nope {GUILD} {CHANNELS[0]}")
    assert world.last_reply() == text.NO_SUCH_CARD
    assert await world.db.run(lambda c: cards.get_by_slug(c, "nope")) is None


async def test_subscribe_with_a_missing_target_replies_the_usage(world: World) -> None:
    await make_card(world)
    await world.run("!card subscribe mc")
    assert world.last_reply() == text.SUBSCRIPTION_USAGE


async def test_subscribe_by_a_non_admin_changes_nothing(world: World) -> None:
    card = await make_card(world)
    world.clear_requests()
    await world.run(f"!card subscribe mc {GUILD} {CHANNELS[0]}", author="2002")
    assert world.state.requests == []
    assert await stored(world, card.id, CHANNELS[0]) is None


async def test_subscribing_an_active_channel_again_says_already_subscribed(world: World) -> None:
    card = await make_card(world)
    await subscribe_by_ids(world, "mc", CHANNELS[0])
    await subscribe_by_ids(world, "mc", CHANNELS[0])
    assert world.last_reply() == text.SUBSCRIPTION_ALREADY
    assert len(await world.db.run(lambda c: subscriptions.list_for_card(c, card.id))) == 1


async def test_subscribing_a_paused_channel_resumes_it_and_keeps_its_message(world: World) -> None:
    card = await make_card(world)
    await subscribe_by_ids(world, "mc", CHANNELS[0])
    await post_copy(world, card, CHANNELS[0])
    await world.db.run(lambda c: subscriptions.pause(c, card.id, CHANNELS[0]))
    world.kicks.clear()
    await subscribe_by_ids(world, "mc", CHANNELS[0])
    assert world.last_reply() == text.SUBSCRIPTION_RESUMED
    sub = await stored(world, card.id, CHANNELS[0])
    assert sub is not None and (sub.paused, sub.message_id, sub.content_hash) == (False, COPY, HASH)
    assert world.kicks


async def test_resuming_a_paused_channel_whose_check_now_fails_leaves_it_paused(world: World) -> None:
    card = await make_card(world)
    await subscribe_by_ids(world, "mc", CHANNELS[0])
    await world.db.run(lambda c: subscriptions.pause(c, card.id, CHANNELS[0]))
    world.state.channels.pop(CHANNELS[0])
    await subscribe_by_ids(world, "mc", CHANNELS[0])
    assert world.last_reply() == text.CHANNEL_MISSING
    sub = await stored(world, card.id, CHANNELS[0])
    assert sub is not None and sub.paused is True


async def test_a_sixth_channel_is_refused_with_the_limit(world: World) -> None:
    card = await make_card(world)
    for channel in CHANNELS[:5]:
        await subscribe_by_ids(world, "mc", channel)
    world.clear_requests()
    await subscribe_by_ids(world, "mc", CHANNELS[5])
    assert world.last_reply() == text.SUBSCRIPTION_LIMIT.format(limit=subscriptions.MAX_SUBSCRIPTIONS)
    assert await stored(world, card.id, CHANNELS[5]) is None
    # The cap is read before the Fluxer checks, so a full card costs no request.
    assert world.state.requests_to(channel_path(CHANNELS[5])) == []


# unsubscribe -----------------------------------------------------------------------------------------

async def test_unsubscribe_deletes_the_copy_and_removes_the_row(world: World) -> None:
    card = await make_card(world)
    await subscribe_by_ids(world, "mc", CHANNELS[0])
    await post_copy(world, card, CHANNELS[0])
    await world.run(f"!card unsubscribe mc {GUILD} {CHANNELS[0]}")
    assert deletes(world) == [copy_path(CHANNELS[0], COPY)]
    assert await stored(world, card.id, CHANNELS[0]) is None
    assert world.last_reply() == text.UNSUBSCRIBED


async def test_unsubscribe_by_a_pasted_link_finds_the_same_channel(world: World) -> None:
    card = await make_card(world)
    await subscribe_by_ids(world, "mc", CHANNELS[0])
    await world.run(f"!card unsubscribe mc https://fluxer.invalid/channels/{GUILD}/{CHANNELS[0]}")
    assert await stored(world, card.id, CHANNELS[0]) is None


async def test_unsubscribe_treats_an_already_gone_copy_as_removed(world: World) -> None:
    card = await make_card(world)
    await subscribe_by_ids(world, "mc", CHANNELS[0])
    await post_copy(world, card, CHANNELS[0])
    world.state.deleted.add((CHANNELS[0], COPY))
    await world.run(f"!card unsubscribe mc {GUILD} {CHANNELS[0]}")
    assert await stored(world, card.id, CHANNELS[0]) is None
    assert world.last_reply() == text.UNSUBSCRIBED


async def test_unsubscribe_removes_the_row_and_says_so_when_the_copy_cannot_be_deleted(world: World) -> None:
    card = await make_card(world)
    await subscribe_by_ids(world, "mc", CHANNELS[0])
    await post_copy(world, card, CHANNELS[0])
    world.state.overrides[f"DELETE {copy_path(CHANNELS[0], COPY)}"] = Override(
        403, b'{"code": "X", "message": "no"}')
    await world.run(f"!card unsubscribe mc {GUILD} {CHANNELS[0]}")
    assert await stored(world, card.id, CHANNELS[0]) is None
    assert world.last_reply() == text.UNSUBSCRIBED_COPY_LEFT.format(channel_id=CHANNELS[0])


async def test_unsubscribe_of_a_channel_not_subscribed_says_so(world: World) -> None:
    await make_card(world)
    await world.run(f"!card unsubscribe mc {GUILD} {CHANNELS[3]}")
    assert world.last_reply() == text.SUBSCRIPTION_NOT_FOUND


async def test_unsubscribe_with_a_bad_target_replies_the_usage(world: World) -> None:
    await make_card(world)
    await world.run("!card unsubscribe mc SENTINEL-CHAN")
    assert world.last_reply() == text.UNSUBSCRIBE_USAGE


async def test_unsubscribe_of_an_unknown_card_replies_no_such_card(world: World) -> None:
    await world.run(f"!card unsubscribe nope {GUILD} {CHANNELS[0]}")
    assert world.last_reply() == text.NO_SUCH_CARD


# archive ---------------------------------------------------------------------------------------------

GAMES_ID = "2200"
MAIN_MESSAGE = "5100"


def copy_id(channel: str) -> str:
    """The message id of the copy in `channel`, distinct per channel."""
    return f"{COPY}{channel}"


async def publish_with_copies(world: World, card: cards.Card, channels: tuple[str, ...], games: str = GAMES_ID) -> None:
    """A live card with its main message in `games` and a copy in each channel, as the poller leaves them."""
    world.state.messages[(games, MAIN_MESSAGE)] = {"embeds": []}

    def live(conn):
        cards.set_state(conn, card.id, "live", 1)
        cards.set_message(conn, card.id, games, MAIN_MESSAGE, HASH)

    await world.db.run(live)
    for channel in channels:
        await subscribe_by_ids(world, card.slug, channel)
        await post_copy(world, card, channel, message_id=copy_id(channel))


def delete_status(world: World, channel: str, message_id: str, status: int) -> None:
    """Makes the fake answer a delete of this message with `status`, without changing its state."""
    world.state.overrides[f"DELETE {copy_path(channel, message_id)}"] = Override(
        status, b'{"code": "X", "message": "no"}')


async def test_archive_deletes_every_copy_before_the_card_message_and_keeps_the_rows(world: World) -> None:
    card = await make_card(world)
    await publish_with_copies(world, card, (CHANNELS[0], CHANNELS[1]))
    await world.run("!card archive mc")
    assert deletes(world) == [copy_path(CHANNELS[0], copy_id(CHANNELS[0])),
                              copy_path(CHANNELS[1], copy_id(CHANNELS[1])),
                              copy_path(GAMES_ID, MAIN_MESSAGE)]
    archived = await world.card("mc")
    assert archived is not None and archived.state == "archived" and archived.message_id is None
    for channel in (CHANNELS[0], CHANNELS[1]):
        sub = await stored(world, card.id, channel)
        assert sub is not None and (sub.message_id, sub.content_hash) == (None, None)
    assert world.last_reply() == text.CARD_ARCHIVED


async def test_archive_stopped_by_a_copy_that_fails_with_a_server_error_keeps_the_card_live_with_its_message(
        world: World) -> None:
    card = await make_card(world)
    await publish_with_copies(world, card, (CHANNELS[0],))
    delete_status(world, CHANNELS[0], copy_id(CHANNELS[0]), 500)
    await world.run("!card archive mc")
    assert world.last_reply() == text.ARCHIVE_RETRY
    after = await world.card("mc")
    assert after is not None and after.state == "live" and after.message_id == MAIN_MESSAGE
    # The card's own message was never tried, and it still exists in Fluxer.
    assert deletes(world) == [copy_path(CHANNELS[0], copy_id(CHANNELS[0]))]
    assert (GAMES_ID, MAIN_MESSAGE) in world.state.messages and (GAMES_ID, MAIN_MESSAGE) not in world.state.deleted
    sub = await stored(world, card.id, CHANNELS[0])
    assert sub is not None and sub.message_id == copy_id(CHANNELS[0])


async def test_archive_stopped_by_a_copy_whose_delete_drops_the_connection_keeps_the_card_live(world: World) -> None:
    card = await make_card(world)
    await publish_with_copies(world, card, (CHANNELS[0],))
    world.state.drops.add(f"DELETE {copy_path(CHANNELS[0], copy_id(CHANNELS[0]))}")
    await world.run("!card archive mc")
    assert world.last_reply() == text.ARCHIVE_RETRY
    after = await world.card("mc")
    assert after is not None and after.state == "live" and after.message_id == MAIN_MESSAGE
    assert (GAMES_ID, MAIN_MESSAGE) in world.state.messages and (GAMES_ID, MAIN_MESSAGE) not in world.state.deleted


async def test_archive_leaves_a_copy_the_bot_is_refused_and_names_its_channel(world: World) -> None:
    card = await make_card(world)
    await publish_with_copies(world, card, (CHANNELS[0],))
    delete_status(world, CHANNELS[0], copy_id(CHANNELS[0]), 403)
    await world.run("!card archive mc")
    assert world.last_reply() == text.CARD_ARCHIVED + "\n" + text.ARCHIVED_COPY_LEFT.format(channel_id=CHANNELS[0])
    archived = await world.card("mc")
    assert archived is not None and archived.state == "archived" and archived.message_id is None
    assert (GAMES_ID, MAIN_MESSAGE) in world.state.deleted
    assert (CHANNELS[0], copy_id(CHANNELS[0])) in world.state.messages
    assert (CHANNELS[0], copy_id(CHANNELS[0])) not in world.state.deleted
    sub = await stored(world, card.id, CHANNELS[0])
    assert sub is not None and (sub.message_id, sub.content_hash) == (None, None)


async def test_archive_leaves_the_card_message_the_bot_is_refused_and_names_its_channel(world: World) -> None:
    card = await make_card(world)
    await publish_with_copies(world, card, (CHANNELS[0],))
    delete_status(world, GAMES_ID, MAIN_MESSAGE, 403)
    await world.run("!card archive mc")
    assert world.last_reply() == text.CARD_ARCHIVED + "\n" + text.ARCHIVED_COPY_LEFT.format(channel_id=GAMES_ID)
    archived = await world.card("mc")
    assert archived is not None and archived.state == "archived" and archived.message_id is None
    assert (GAMES_ID, MAIN_MESSAGE) in world.state.messages and (GAMES_ID, MAIN_MESSAGE) not in world.state.deleted


async def test_archive_counts_a_copy_already_gone_as_removed(world: World) -> None:
    card = await make_card(world)
    await publish_with_copies(world, card, (CHANNELS[0],))
    world.state.deleted.add((CHANNELS[0], copy_id(CHANNELS[0])))
    await world.run("!card archive mc")
    assert world.last_reply() == text.CARD_ARCHIVED


BOARD_MESSAGE = "4000"


async def poller_with_board(world: World) -> Poller:
    """A poller on this world's store, Fluxer and card locks. Its games channel already holds the summary board, so a
    tick publishes only cards and copies."""
    world.state.messages[(GAMES_CHANNEL, BOARD_MESSAGE)] = {"embeds": []}
    await world.db.run(lambda c: board.set_message(c, GAMES_CHANNEL, BOARD_MESSAGE, HASH))
    return Poller(db=world.db, rest=world.rest, sources=world.ctx.sources, config=world.config,
                  locks=world.ctx.locks, clock=lambda: 0.0)


def posts_to(state: FakeFluxerState, channel: str) -> list[Recorded]:
    return [r for r in state.requests if r.method == "POST" and r.path == f"/api/v1/channels/{channel}/messages"]


async def test_a_stopped_archive_leaves_the_card_live_and_the_poller_posts_the_copies_it_deleted(
        games_world: World) -> None:
    card = await make_card(games_world)
    await publish_with_copies(games_world, card, (CHANNELS[0], CHANNELS[1]), games=GAMES_CHANNEL)
    delete_status(games_world, CHANNELS[1], copy_id(CHANNELS[1]), 500)
    games_world.kicks.clear()

    await games_world.run("!card archive mc")

    assert games_world.last_reply() == text.ARCHIVE_RETRY
    assert games_world.kicks    # the forgotten copy is posted again at once, not at the next interval
    assert (CHANNELS[0], copy_id(CHANNELS[0])) in games_world.state.deleted
    forgotten = await stored(games_world, card.id, CHANNELS[0])
    assert forgotten is not None and forgotten.message_id is None
    kept = await stored(games_world, card.id, CHANNELS[1])
    assert kept is not None and kept.message_id == copy_id(CHANNELS[1])
    assert (GAMES_CHANNEL, MAIN_MESSAGE) in games_world.state.messages
    assert (GAMES_CHANNEL, MAIN_MESSAGE) not in games_world.state.deleted

    poller = await poller_with_board(games_world)
    await poller.tick()

    assert len(posts_to(games_world.state, CHANNELS[0])) == 1
    reposted = await stored(games_world, card.id, CHANNELS[0])
    assert reposted is not None and reposted.message_id not in (None, copy_id(CHANNELS[0]))
    assert posts_to(games_world.state, CHANNELS[1]) == []     # the copy the stopped archive kept is not posted again


# Commands during a tick: the card's lock makes a command wait for the tick that holds it ---------------

async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the fake servers")
        await asyncio.sleep(0.01)


async def settle() -> None:
    """Gives the running tasks time to reach their next wait, so a count taken after it does not change."""
    for _ in range(20):
        await asyncio.sleep(0.01)


async def tick_held_at_a_copy_post(world: World, poller: Poller, channel: str) -> tuple[asyncio.Event, asyncio.Task]:
    """Starts a tick and holds its POST of the copy in `channel` in flight. The tick holds the card's lock meanwhile."""
    key = f"POST /api/v1/channels/{channel}/messages"
    gate = asyncio.Event()
    world.state.gates[key] = gate
    tick = asyncio.create_task(poller.tick())
    await wait_until(lambda: key in world.state.gated)
    return gate, tick


async def test_an_unsubscribe_waits_for_a_copy_post_in_flight_and_then_deletes_that_copy(games_world: World) -> None:
    card = await make_card(games_world)
    await subscribe_by_ids(games_world, "mc", CHANNELS[0])
    await games_world.run("!card publish mc")
    poller = await poller_with_board(games_world)
    gate, tick = await tick_held_at_a_copy_post(games_world, poller, CHANNELS[0])

    unsubscribe = asyncio.create_task(games_world.run(f"!card unsubscribe mc {GUILD} {CHANNELS[0]}"))
    await settle()
    assert not unsubscribe.done()

    gate.set()
    await tick
    await unsubscribe

    [made] = [message_id for channel, message_id in games_world.state.messages if channel == CHANNELS[0]]
    assert copy_path(CHANNELS[0], made) in deletes(games_world)
    assert await stored(games_world, card.id, CHANNELS[0]) is None
    assert games_world.last_reply() == text.UNSUBSCRIBED


async def test_a_resume_waits_for_a_copy_post_in_flight_and_then_resumes_the_paused_copy(games_world: World) -> None:
    card = await make_card(games_world)
    await games_world.run("!card publish mc")
    await subscribe_by_ids(games_world, "mc", CHANNELS[0])
    await post_copy(games_world, card, CHANNELS[0])
    await games_world.db.run(lambda c: subscriptions.pause(c, card.id, CHANNELS[0]))
    await subscribe_by_ids(games_world, "mc", CHANNELS[1])
    poller = await poller_with_board(games_world)
    gate, tick = await tick_held_at_a_copy_post(games_world, poller, CHANNELS[1])

    resume = asyncio.create_task(games_world.run(f"!card subscribe mc {GUILD} {CHANNELS[0]}"))
    await settle()
    assert not resume.done()

    gate.set()
    await tick
    await resume

    assert games_world.last_reply() == text.SUBSCRIPTION_RESUMED
    sub = await stored(games_world, card.id, CHANNELS[0])
    assert sub is not None and (sub.paused, sub.message_id) == (False, COPY)


# show and help ---------------------------------------------------------------------------------------

async def test_show_lists_the_subscribed_channels_with_paused_marked(world: World) -> None:
    card = await make_card(world)
    await subscribe_by_ids(world, "mc", CHANNELS[0])
    await subscribe_by_ids(world, "mc", CHANNELS[1])
    await world.db.run(lambda c: subscriptions.pause(c, card.id, CHANNELS[1]))
    await world.run("!card show mc")
    listing = world.payloads()[-1]
    assert listing["content"] == (text.SUBSCRIPTIONS_HEADING + "\n"
                                  f"<#{CHANNELS[0]}>\n<#{CHANNELS[1]}>{text.SUBSCRIPTION_PAUSED_SUFFIX}")
    assert listing["allowed_mentions"] == {"parse": []}


async def test_show_sends_no_list_when_the_card_has_no_subscriptions(world: World) -> None:
    await make_card(world)
    world.clear_requests()
    await world.run("!card show mc")
    assert len(world.payloads()) == 2   # the embed and the outside text, as before subscriptions existed


def test_every_subcommand_is_named_in_the_help() -> None:
    for name in SUBCOMMANDS:
        assert f"!card {name}" in text.HELP_COMMANDS, name


def test_the_help_lists_subscribe_and_unsubscribe_with_their_arguments() -> None:
    assert "!card subscribe <slug> <channel>:" in text.HELP_COMMANDS
    assert "!card unsubscribe <slug> <channel>:" in text.HELP_COMMANDS


async def test_subscribe_and_unsubscribe_are_logged_under_their_own_command_names(
        world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="fluxer_panel.card_commands")
    await make_card(world)
    await subscribe_by_ids(world, "mc", CHANNELS[0])
    await world.run(f"!card unsubscribe mc {GUILD} {CHANNELS[0]}")
    commands = [record.panel_fields["command"] for record in caplog.records if record.getMessage() == "admin_command"]
    assert commands == ["card_new", "card_subscribe", "card_unsubscribe"]
