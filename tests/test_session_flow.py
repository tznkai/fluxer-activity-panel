# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The calendar DM flow (AC2) and the DM backoff for it (AC7.1). A real SessionFlow, SessionPost and Interactions run
against the fake REST server and a SQLite file. The clock is a stub that moves only when a test moves it, and the
pacer never sleeps. The keycaps are written as escapes."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.roles import RoleCache
from fluxer_client.rest import FluxerREST, encode_emoji
from fluxer_panel import cards, interaction_settings, play_now, rsvp, sessions, subscriptions, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import ChannelLink
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.dm_router import NonAdminLimiter
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.logs import FIELDS_ATTR
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import SESSION_EMOJI, Action, Press, Presses, Surface
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_flow import FLOW_TIMEOUT_SECONDS, SessionFlow
from fluxer_panel.session_post import SessionPost
from tests.fakes import FakeFluxerState, Override, make_rest_app, wait_until

TOKEN = "SENTINEL-TOKEN-session-flow"
BOT_ID = "1900"
ADMIN = "1001"
PROPOSER = "6001"
VOTER = "6002"
GUILD = "2001"            # the games community
OTHER_GUILD = "2002"
GAMES = "3100"            # the games channel, in GUILD
PLAY_NOW = "3200"         # PLAY_NOW_CHANNEL_ID, in GUILD
OWN = "3300"              # a card's own verified play-now channel, in GUILD
FAR = "3400"              # a copy's channel, in OTHER_GUILD
CARD_MSG = "5101"
COPY_MSG = "5201"
COPY_CH = "3500"         # a copy's channel, in GUILD
FAR_MSG = "5202"         # a far copy's message, in FAR
PLAY_MSG = "5301"        # a play-now post, in PLAY_NOW
HASH = "a" * 64
NOW = datetime(2026, 11, 11, 8, 0, tzinfo=timezone.utc).timestamp()   # a Wednesday, 08:00 UTC
FORBIDDEN = b'{"code": "MISSING_PERMISSIONS", "message": "Missing Permissions"}'
SERVER_ERROR = b'{"code": "INTERNAL", "message": "x"}'
PLAY_NOW_POSTS = f"/api/v1/channels/{PLAY_NOW}/messages"
OWN_POSTS = f"/api/v1/channels/{OWN}/messages"


def keycap(n: int) -> str:
    """The keycap for slot n as Fluxer names it: the digit, U+FE0F, U+20E3."""
    return f"{n}\ufe0f\u20e3"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@dataclass
class World:
    db: Database
    state: FakeFluxerState
    rest: FluxerREST
    clock: Clock
    config: Config
    backoff: DmBackoff
    post: SessionPost
    flow: SessionFlow
    interactions: Interactions
    card_id: int
    kicks: list[int]


def config_for(*, play_now: bool) -> Config:
    env = {"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
           "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES}
    if play_now:
        env["PLAY_NOW_CHANNEL_ID"] = PLAY_NOW
    return Config.from_env(env)


def seed_card(conn: Any, *, own_channel: bool) -> int:
    card_id = cards.create_card(conn, slug="mc", title="Minecraft", author_id=ADMIN, now=1)
    cards.set_state(conn, card_id, "live", 1)
    cards.set_message(conn, card_id, GAMES, CARD_MSG, HASH)
    if own_channel:
        cards.set_play_now_channel(conn, card_id, ChannelLink(GUILD, OWN, "lounge", True))
    return card_id


@pytest.fixture
async def build(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[Callable[..., Awaitable[World]]]:
    opened: list[Database] = []
    async with aiohttp.ClientSession() as client:

        async def make(*, play_now: bool = True, own_channel: bool = False) -> World:
            state = FakeFluxerState(bot_user_id=BOT_ID)
            state.channels.update({
                GAMES: {"id": GAMES, "guild_id": GUILD, "type": 0},
                PLAY_NOW: {"id": PLAY_NOW, "guild_id": GUILD, "type": 0},
                OWN: {"id": OWN, "guild_id": GUILD, "type": 0},
                FAR: {"id": FAR, "guild_id": OTHER_GUILD, "type": 0},
            })
            server = await aiohttp_server(make_rest_app(state))
            base = str(server.make_url("/")).rstrip("/")
            rest = FluxerREST(client, f"{base}/api", TOKEN)
            database = Database.open(tmp_path / f"flow{len(opened)}.db")
            opened.append(database)
            config = config_for(play_now=play_now)
            clock = Clock()
            pacer = ChannelPacer(max_calls=1000)
            identity = BotIdentity(user_id=BOT_ID)
            games_guild = GamesGuild(rest, config)
            locks = CardLocks()
            backoff = DmBackoff(config.dm_backoff_seconds, clock=clock)
            limiter = NonAdminLimiter(config, clock=clock)
            post = SessionPost(db=database, rest=rest, config=config, pacer=pacer, backoff=backoff)
            kicks: list[int] = []
            flow = SessionFlow(db=database, rest=rest, config=config, locks=locks, session_post=post,
                               games_guild=games_guild, pacer=pacer, identity=identity, backoff=backoff,
                               limiter=limiter, clock=clock, kick=lambda: kicks.append(1))
            interactions = Interactions(db=database, rest=rest, config=config, locks=locks, games_guild=games_guild,
                                        seeder=Seeder(rest=rest, pacer=pacer, db=database), pacer=pacer,
                                        identity=identity, roles=RoleCache(rest), clock=clock,
                                        kick=lambda: kicks.append(1), session_post=post, backoff=backoff,
                                        session_flow=flow)
            card_id = await database.run(lambda c: seed_card(c, own_channel=own_channel))
            return World(db=database, state=state, rest=rest, clock=clock, config=config, backoff=backoff,
                         post=post, flow=flow, interactions=interactions, card_id=card_id, kicks=kicks)

        yield make
    for database in opened:
        database.close()


def press(card_id: int, user: str = PROPOSER, *, home: bool = True) -> Press:
    surface = (Surface(kind="card", channel_id=GAMES, message_id=CARD_MSG, guild_id=GUILD, home_community=True)
               if home else
               Surface(kind="copy", channel_id=FAR, message_id=COPY_MSG, guild_id=OTHER_GUILD,
                       home_community=False))
    return Press(card_id=card_id, action=Action.SESSION, user_id=user, member_role_ids=None, surface=surface)


def dm_reaction(user: str, channel: str, message: str, name: str) -> events.ReactionAdd:
    return events.ReactionAdd(user_id=user, channel_id=channel, message_id=message, guild_id=None,
                              emoji=events.Emoji(name=name, id=None), member_role_ids=None)


def typed(user: str, channel: str, content: str) -> events.MessageCreate:
    return events.MessageCreate(message_id="7000", channel_id=channel, guild_id=None, author_id=user,
                                author_is_bot=False, member_role_ids=None, content=content)


def dm_channel_of(world: World, user: str = PROPOSER) -> str:
    channel = world.state.dm_channels.get(user)
    assert channel is not None, "the user has no DM channel yet"
    return channel


def dm_ids(world: World, needle: str, user: str = PROPOSER) -> list[str]:
    """The ids of the DM messages to this user whose text contains `needle`, oldest first."""
    channel = dm_channel_of(world, user)
    return [mid for (cid, mid), payload in world.state.messages.items()
            if cid == channel and needle in str(payload.get("content", ""))]


def dm_texts(world: World, user: str = PROPOSER) -> list[str]:
    channel = world.state.dm_channels.get(user)
    if channel is None:
        return []
    return [str(payload.get("content", "")) for (cid, _), payload in world.state.messages.items() if cid == channel]


def slots_of(prompt: str) -> list[int]:
    return [int(t) for t in re.findall(r"<t:(\d+):F>", prompt)]


def reactions_put_on(world: World, channel: str, message: str) -> list[str]:
    """The emoji names of the bot's PUT reactions on one message, decoded, in the order they were sent."""
    prefix = f"/api/v1/channels/{channel}/messages/{message}/reactions/"
    return [r.path[len(prefix):].rsplit("/", 1)[0] for r in world.state.requests
            if r.method == "PUT" and r.path.startswith(prefix)]


def requests(world: World, method: str, path: str | None = None) -> list[Any]:
    return [r for r in world.state.requests if r.method == method and (path is None or r.path == path)]


def posted_ids(world: World) -> list[str]:
    """The message ids the fake created in the play-now channel."""
    return [mid for (cid, mid) in world.state.messages if cid == PLAY_NOW]


async def open_prompt(world: World, user: str = PROPOSER) -> tuple[str, list[int]]:
    """Presses the calendar as the user and returns the prompt's message id and its six slot times."""
    await world.flow.start(press(world.card_id, user))
    prompt = dm_ids(world, "Pick a time", user)[-1]
    return prompt, slots_of(dm_texts(world, user)[-1])


async def confirm_slot(world: World, n: int, *, user: str = PROPOSER) -> str:
    """Presses keycap n on the user's prompt, opening a draft first when there is none. Returns the new confirmation's
    message id."""
    if not world.flow.has_draft(user):
        await open_prompt(world, user)
    prompt = dm_ids(world, "Pick a time", user)[-1]
    before = len(dm_ids(world, "to post it", user))
    await world.flow.on_dm_reaction(dm_reaction(user, dm_channel_of(world, user), prompt, keycap(n)))
    confirms = dm_ids(world, "to post it", user)
    assert len(confirms) == before + 1, "keycap should have produced one new confirmation"
    return confirms[-1]


async def add_sessions(world: World, count: int, *, proposer: Callable[[int], str], first_day: int = 2,
                       card_id: int | None = None) -> None:
    """Adds `count` upcoming sessions on the card, far apart in time, each by proposer(n)."""
    target = card_id if card_id is not None else world.card_id

    def tx(c: Any) -> None:
        for n in range(count):
            start = int(NOW) + (first_day + n) * 86400
            sessions.add(c, card_id=target, guild_id=GUILD, channel_id=PLAY_NOW, message_id=str(6100 + n),
                         starts_at=start, ends_at=start + 7200, proposer_id=proposer(n), now=int(NOW))

    await world.db.run(tx)


# AC2.1: the prompt -----------------------------------------------------------------------------------------

async def test_a_calendar_press_sends_a_prompt_with_six_slots_in_order(build: Any) -> None:
    world = await build()

    await world.flow.start(press(world.card_id))

    [prompt] = dm_texts(world)
    slots = slots_of(prompt)
    assert len(slots) == 6
    assert slots == sorted(slots) and len(set(slots)) == 6
    assert "Pick a time for Minecraft" in prompt


async def test_the_prompt_gets_the_six_keycaps_in_order(build: Any) -> None:
    world = await build()
    prompt, _ = await open_prompt(world)

    names = reactions_put_on(world, dm_channel_of(world), prompt)

    assert names == [keycap(n) for n in range(1, 7)]
    assert all("\ufe0f" in name for name in names)


async def test_a_prompt_that_cannot_be_sent_starts_no_draft_and_starts_the_backoff(build: Any) -> None:
    world = await build()
    world.state.overrides["POST /api/v1/users/@me/channels"] = Override(status=403, body=FORBIDDEN)

    await world.flow.start(press(world.card_id))

    assert world.flow.has_draft(PROPOSER) is False
    assert world.backoff.blocked(PROPOSER) is True
    assert requests(world, "PUT") == []


async def test_next_expiry_is_the_draft_start_plus_the_timeout_and_none_without_one(build: Any) -> None:
    world = await build()
    assert world.flow.next_expiry() is None

    await world.flow.start(press(world.card_id))

    assert world.flow.next_expiry() == NOW + FLOW_TIMEOUT_SECONDS


# AC2.2: a keycap or a typed time gives a confirmation, and nothing is posted before ✅ ---------------------

async def test_a_keycap_gives_a_confirmation_with_absolute_and_relative_times_and_seeds_a_check(build: Any) -> None:
    world = await build()
    prompt, slots = await open_prompt(world)

    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, dm_channel_of(world), prompt, keycap(1)))

    confirm = dm_texts(world)[-1]
    assert f"<t:{slots[0]}:F>" in confirm and f"<t:{slots[0]}:R>" in confirm
    confirm_id = dm_ids(world, "to post it")[-1]
    assert reactions_put_on(world, dm_channel_of(world), confirm_id) == [rsvp.GOING_EMOJI]
    assert requests(world, "POST", PLAY_NOW_POSTS) == []


async def test_a_typed_time_gives_the_same_confirmation(build: Any) -> None:
    world = await build()
    await open_prompt(world)
    tomorrow = int(datetime(2026, 11, 12, 19, 30, tzinfo=timezone.utc).timestamp())

    await world.flow.on_reply(typed(PROPOSER, dm_channel_of(world), "tomorrow 19:30"))

    assert f"<t:{tomorrow}:F>" in dm_texts(world)[-1]
    assert requests(world, "POST", PLAY_NOW_POSTS) == []


# AC2.3: ✅ posts the session in the card's play-now channel, stores it and tells the proposer ------------

async def test_a_check_posts_the_session_stores_it_seeds_the_rsvps_and_links_the_proposer(build: Any) -> None:
    world = await build()
    confirm = await confirm_slot(world, 1)

    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, dm_channel_of(world), confirm, rsvp.GOING_EMOJI))

    [posted] = posted_ids(world)
    [send] = requests(world, "POST", PLAY_NOW_POSTS)
    assert send.payload["allowed_mentions"] == {"parse": []}
    assert reactions_put_on(world, PLAY_NOW, posted) == list(rsvp.RSVP_EMOJI)
    session = await world.db.run(lambda c: sessions.get_by_message(c, posted))
    assert session is not None
    assert session.proposer_id == PROPOSER and session.card_id == world.card_id
    going = await world.db.run(lambda c: sessions.rsvps_of(c, session.id))
    assert going[PROPOSER][0] == "going"
    [patch] = requests(world, "PATCH", f"{PLAY_NOW_POSTS}/{posted}")
    assert patch.payload["embeds"][0]["footer"]["text"] == text.SESSION_FOOTER.format(n=session.id)
    assert f"/channels/{GUILD}/{PLAY_NOW}/{posted}" in dm_texts(world)[-1]
    assert world.kicks == [1]


async def test_a_check_on_a_card_with_its_own_play_now_channel_posts_there(build: Any) -> None:
    world = await build(own_channel=True)
    confirm = await confirm_slot(world, 1)

    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, dm_channel_of(world), confirm, rsvp.GOING_EMOJI))

    assert requests(world, "POST", PLAY_NOW_POSTS) == []
    assert len(requests(world, "POST", OWN_POSTS)) == 1


# AC2.4: the proposer's own overlapping session is named, and nobody else is ------------------------------

async def test_a_confirmation_names_only_the_proposers_own_overlapping_session(build: Any) -> None:
    world = await build()
    _, slots = await open_prompt(world)
    ark = await world.db.run(lambda c: cards.create_card(c, slug="ark", title="Ark", author_id=ADMIN, now=1))

    def overlap(c: Any) -> None:
        cards.set_state(c, ark, "live", 1)
        session_id = sessions.add(c, card_id=ark, guild_id=GUILD, channel_id=PLAY_NOW, message_id="6200",
                                  starts_at=slots[0] - 1800, ends_at=slots[0] + 1800, proposer_id=PROPOSER,
                                  now=int(NOW))
        session = sessions.get(c, session_id)
        assert session is not None
        sessions.add_presence(c, session, VOTER, "going", int(NOW))

    await world.db.run(overlap)
    prompt = dm_ids(world, "Pick a time")[-1]
    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, dm_channel_of(world), prompt, keycap(1)))

    confirm = dm_texts(world)[-1]
    assert text.SESSION_CONFLICT.format(card="Ark") in confirm
    assert "<@" not in confirm
    assert VOTER not in confirm


async def test_a_confirmation_has_no_conflict_line_without_an_overlapping_session(build: Any) -> None:
    world = await build()
    _, slots = await open_prompt(world)
    ark = await world.db.run(lambda c: cards.create_card(c, slug="ark", title="Ark", author_id=ADMIN, now=1))

    def later(c: Any) -> None:
        cards.set_state(c, ark, "live", 1)
        sessions.add(c, card_id=ark, guild_id=GUILD, channel_id=PLAY_NOW, message_id="6200",
                     starts_at=slots[0] + 86400, ends_at=slots[0] + 93600, proposer_id=PROPOSER, now=int(NOW))

    await world.db.run(later)
    prompt = dm_ids(world, "Pick a time")[-1]
    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, dm_channel_of(world), prompt, keycap(1)))

    assert "already going" not in dm_texts(world)[-1]


# AC2.5: refusals keep the draft, and other people's or old reactions do nothing ------------------------------

async def test_a_refused_typed_time_gets_the_refusal_and_example_and_keeps_the_draft(build: Any) -> None:
    world = await build()
    await open_prompt(world)

    await world.flow.on_reply(typed(PROPOSER, dm_channel_of(world), "sat 2pm"))

    assert dm_texts(world)[-1] == text.SESSION_TIME_REFUSED["format"] + " " + text.SESSION_TIME_EXAMPLE
    assert world.flow.has_draft(PROPOSER) is True


async def test_another_users_keycap_on_the_prompt_does_nothing(build: Any) -> None:
    world = await build()
    prompt, _ = await open_prompt(world)
    before = len(dm_texts(world))

    await world.flow.on_dm_reaction(dm_reaction(VOTER, dm_channel_of(world), prompt, keycap(1)))

    assert len(dm_texts(world)) == before
    assert requests(world, "POST", PLAY_NOW_POSTS) == []


async def test_a_check_on_an_old_confirmation_does_nothing_once_a_newer_one_replaced_it(build: Any) -> None:
    world = await build()
    first = await confirm_slot(world, 1)
    second = await confirm_slot(world, 2)
    channel = dm_channel_of(world)

    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, channel, first, rsvp.GOING_EMOJI))
    assert requests(world, "POST", PLAY_NOW_POSTS) == []

    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, channel, second, rsvp.GOING_EMOJI))
    assert len(requests(world, "POST", PLAY_NOW_POSTS)) == 1


async def test_a_keycap_on_the_old_prompt_after_a_second_press_does_nothing(build: Any) -> None:
    world = await build()
    old_prompt, _ = await open_prompt(world)
    await world.flow.start(press(world.card_id))
    channel = dm_channel_of(world)
    assert dm_ids(world, "Pick a time")[-1] != old_prompt

    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, channel, old_prompt, keycap(1)))

    assert dm_ids(world, "to post it") == []
    assert requests(world, "POST", PLAY_NOW_POSTS) == []


async def test_a_second_press_whose_prompt_fails_leaves_no_draft_for_the_old_prompt(build: Any) -> None:
    world = await build()
    old_prompt, _ = await open_prompt(world)
    world.state.overrides["POST /api/v1/users/@me/channels"] = Override(status=403, body=FORBIDDEN)

    await world.flow.start(press(world.card_id))

    assert world.flow.has_draft(PROPOSER) is False
    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, dm_channel_of(world), old_prompt, keycap(1)))
    assert dm_ids(world, "to post it") == []


async def test_a_check_on_an_older_confirmation_waits_for_a_typed_reply_that_is_mid_confirmation(build: Any) -> None:
    """The reply is held inside its confirmation DM. The check on the older confirmation runs meanwhile, and must see the
    newer confirmation once the reply has finished, so it posts nothing. The gate is released before the check's task
    runs, so the order is fixed by the lock, not by timing."""
    world = await build()
    older = await confirm_slot(world, 1)
    channel = dm_channel_of(world)
    gate = asyncio.Event()
    route = f"POST /api/v1/channels/{channel}/messages"
    world.state.gates[route] = gate
    reply = asyncio.create_task(world.flow.on_reply(typed(PROPOSER, channel, "tomorrow 19:30")))
    await wait_until(lambda: route in world.state.gated, waiting_for="the reply to reach its confirmation DM")

    check = asyncio.create_task(world.flow.on_dm_reaction(dm_reaction(PROPOSER, channel, older, rsvp.GOING_EMOJI)))
    gate.set()
    await asyncio.gather(reply, check)

    assert requests(world, "POST", PLAY_NOW_POSTS) == []
    assert len(dm_ids(world, "to post it")) == 2


# AC2.6: a draft lasts 15 minutes, then the prompt says so and nothing else can act on it -----------------

async def test_a_draft_expires_fifteen_minutes_after_the_press_and_the_prompt_says_so(build: Any) -> None:
    world = await build()
    prompt, _ = await open_prompt(world)

    world.clock.now = NOW + FLOW_TIMEOUT_SECONDS
    await world.flow.expire_drafts()

    assert world.flow.has_draft(PROPOSER) is False
    [edit] = requests(world, "PATCH", f"/api/v1/channels/{dm_channel_of(world)}/messages/{prompt}")
    assert edit.payload["content"] == text.SESSION_DRAFT_EXPIRED


async def test_a_check_after_the_timeout_does_nothing_even_before_the_expiry_sweep(build: Any) -> None:
    world = await build()
    confirm = await confirm_slot(world, 1)

    world.clock.now = NOW + FLOW_TIMEOUT_SECONDS
    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, dm_channel_of(world), confirm, rsvp.GOING_EMOJI))

    assert requests(world, "POST", PLAY_NOW_POSTS) == []


async def test_a_reply_after_the_expiry_sweep_does_nothing(build: Any) -> None:
    world = await build()
    await open_prompt(world)
    world.clock.now = NOW + FLOW_TIMEOUT_SECONDS
    await world.flow.expire_drafts()
    before = len(dm_texts(world))

    await world.flow.on_reply(typed(PROPOSER, dm_channel_of(world), "tomorrow 19:30"))

    assert len(dm_texts(world)) == before


# The per-user locks: an entry lives only while a step holds or awaits it -----------------------------------

async def test_an_idle_user_holds_no_lock_entry_after_a_prompt_or_a_refusal(build: Any) -> None:
    world = await build()
    await open_prompt(world)
    await world.flow.start(press(world.card_id, home=False))

    assert world.flow._user_locks == {}


async def test_a_second_step_for_the_same_user_waits_on_the_same_lock_object(build: Any) -> None:
    world = await build()

    async def step() -> None:
        async with world.flow._lock(PROPOSER):
            pass

    async with world.flow._lock(PROPOSER):
        entry = world.flow._user_locks[PROPOSER]
        waiter = asyncio.create_task(step())
        await asyncio.sleep(0)
        assert world.flow._user_locks[PROPOSER] is entry
        assert entry.users == 2
        assert not waiter.done()

    await asyncio.wait_for(waiter, timeout=1)
    assert world.flow._user_locks == {}


# An expiry sweep never waits for a step in flight: it skips that user and the next sweep ends the draft ----------

async def test_an_expiry_sweep_skips_a_user_whose_step_is_in_flight_then_ends_the_draft_later(build: Any) -> None:
    world = await build()
    prompt, _ = await open_prompt(world)
    world.clock.now = NOW + FLOW_TIMEOUT_SECONDS
    prompt_path = f"/api/v1/channels/{dm_channel_of(world)}/messages/{prompt}"

    async with world.flow._lock(PROPOSER):
        await asyncio.wait_for(world.flow.expire_drafts(), timeout=1)
        assert world.flow.has_draft(PROPOSER) is True
        assert requests(world, "PATCH", prompt_path) == []

    await world.flow.expire_drafts()
    assert world.flow.has_draft(PROPOSER) is False
    [edit] = requests(world, "PATCH", prompt_path)
    assert edit.payload["content"] == text.SESSION_DRAFT_EXPIRED


# AC2.7: far copies and cards without a session channel get a DM and no draft ---------------------------------

async def test_a_press_on_a_far_copy_gets_a_pointer_to_the_card_and_starts_no_draft(build: Any) -> None:
    world = await build()

    await world.flow.start(press(world.card_id, home=False))

    assert world.flow.has_draft(PROPOSER) is False
    [content] = dm_texts(world)
    assert content.startswith(text.SESSION_ELSEWHERE.split("{link}")[0])
    assert f"/channels/{GUILD}/{GAMES}/{CARD_MSG}" in content


async def test_a_card_without_a_session_channel_gets_a_refusal_and_no_draft(build: Any) -> None:
    world = await build(play_now=False)

    await world.flow.start(press(world.card_id))

    assert world.flow.has_draft(PROPOSER) is False
    assert dm_texts(world) == [text.SESSION_NO_CHANNEL]


# AC2.8: the caps are checked at the press and again at ✅ -------------------------------------------------

async def test_a_proposer_with_three_upcoming_sessions_is_refused_at_the_press(build: Any) -> None:
    world = await build()
    await add_sessions(world, 3, proposer=lambda _n: PROPOSER)

    await world.flow.start(press(world.card_id))

    assert world.flow.has_draft(PROPOSER) is False
    assert dm_texts(world) == [text.SESSION_CAP["proposer"].format(card="Minecraft")]


async def test_a_card_with_ten_upcoming_sessions_is_refused_at_the_press(build: Any) -> None:
    world = await build()
    await add_sessions(world, 10, proposer=lambda n: str(7000 + n))

    await world.flow.start(press(world.card_id))

    assert world.flow.has_draft(PROPOSER) is False
    assert dm_texts(world) == [text.SESSION_CAP["card"].format(card="Minecraft")]


async def test_a_card_that_fills_before_the_check_refuses_at_the_check_and_posts_nothing(build: Any) -> None:
    world = await build()
    confirm = await confirm_slot(world, 1)
    await add_sessions(world, 10, proposer=lambda n: str(7000 + n))

    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, dm_channel_of(world), confirm, rsvp.GOING_EMOJI))

    assert dm_texts(world)[-1] == text.SESSION_CAP["card"].format(card="Minecraft")
    assert requests(world, "POST", PLAY_NOW_POSTS) == []


# AC2.9: a failed post or store is told, and leaves nothing behind --------------------------------------------

async def test_a_failed_session_post_tells_the_proposer_and_stores_nothing(build: Any) -> None:
    world = await build()
    confirm = await confirm_slot(world, 1)
    world.state.overrides[f"POST {PLAY_NOW_POSTS}"] = Override(status=500, body=SERVER_ERROR)

    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, dm_channel_of(world), confirm, rsvp.GOING_EMOJI))

    assert dm_texts(world)[-1] == text.SESSION_POST_FAILED
    assert await world.db.run(lambda c: sessions.upcoming_for_card(c, world.card_id)) == []


async def test_a_failed_store_deletes_the_posted_message_and_tells_the_proposer(build: Any,
                                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    world = await build()
    confirm = await confirm_slot(world, 1)

    def refuse(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("SENTINEL")

    monkeypatch.setattr(sessions, "add", refuse)
    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, dm_channel_of(world), confirm, rsvp.GOING_EMOJI))

    [posted] = posted_ids(world)
    assert requests(world, "DELETE", f"{PLAY_NOW_POSTS}/{posted}")
    assert dm_texts(world)[-1] == text.SESSION_POST_FAILED


# AC7.1: a user in backoff gets no DM call, and a calendar press is refused -----------------------------------

async def test_a_calendar_press_during_a_backoff_makes_no_request_and_starts_no_draft(
        build: Any, caplog: pytest.LogCaptureFixture) -> None:
    world = await build()
    caplog.set_level(logging.INFO, logger="fluxer_panel.session_flow")
    world.backoff.failed(PROPOSER)

    await world.flow.start(press(world.card_id))

    assert world.state.requests == []
    assert world.flow.has_draft(PROPOSER) is False
    [refused] = [r for r in panel_records(caplog) if getattr(r, FIELDS_ATTR).get("reason") == "backoff"]
    assert getattr(refused, FIELDS_ATTR)["outcome"] == "refused"


# Logs: no user id and no typed text --------------------------------------------------------------------------

async def test_the_flow_logs_never_carry_a_user_id_or_the_typed_text(build: Any,
                                                                     caplog: pytest.LogCaptureFixture) -> None:
    world = await build()
    caplog.set_level(logging.INFO, logger="fluxer_panel.session_flow")
    await open_prompt(world)
    await world.flow.on_reply(typed(PROPOSER, dm_channel_of(world), "sat 2pm"))
    await world.flow.on_reply(typed(PROPOSER, dm_channel_of(world), "tomorrow 19:30"))
    confirm = dm_ids(world, "to post it")[-1]
    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, dm_channel_of(world), confirm, rsvp.GOING_EMOJI))

    records = panel_records(caplog)
    assert records, "the flow should have logged"
    for record in records:
        fields = getattr(record, FIELDS_ATTR, {})
        assert "user_id" not in fields
        for typed_text in ("sat 2pm", "tomorrow 19:30"):
            assert typed_text not in record.getMessage()
            assert typed_text not in str(fields)


# Limiter: keycaps and checks share the non-admin budget -------------------------------------------------------

async def test_the_sixth_keycap_in_a_minute_sends_nothing(build: Any) -> None:
    world = await build()
    prompt, _ = await open_prompt(world)
    channel = dm_channel_of(world)

    for n in (1, 2, 1, 2, 1):
        await world.flow.on_dm_reaction(dm_reaction(PROPOSER, channel, prompt, keycap(n)))
    assert len(dm_ids(world, "to post it")) == 5
    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, channel, prompt, keycap(2)))

    assert len(dm_ids(world, "to post it")) == 5


async def test_a_repeat_of_the_current_keycap_sends_no_new_confirmation(build: Any) -> None:
    world = await build()
    prompt, _ = await open_prompt(world)
    channel = dm_channel_of(world)

    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, channel, prompt, keycap(1)))
    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, channel, prompt, keycap(1)))

    assert len(dm_ids(world, "to post it")) == 1


# A check posts once: a second ✅ finds the draft gone ----------------------------------------------------------

async def test_a_double_check_posts_once(build: Any) -> None:
    world = await build()
    confirm = await confirm_slot(world, 1)
    channel = dm_channel_of(world)

    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, channel, confirm, rsvp.GOING_EMOJI))
    await world.flow.on_dm_reaction(dm_reaction(PROPOSER, channel, confirm, rsvp.GOING_EMOJI))

    assert len(requests(world, "POST", PLAY_NOW_POSTS)) == 1


# Pipeline: a gateway-shaped reaction through Presses, into the real Interactions and SessionFlow ----------------

def presses_for(world: World) -> Presses:
    return Presses(db=world.db, rest=world.rest, config=world.config, handler=world.interactions,
                   games_guild=GamesGuild(world.rest, world.config), pacer=ChannelPacer(max_calls=1000),
                   identity=BotIdentity(user_id=BOT_ID), clock=world.clock, backoff=world.backoff)


def calendar_tap(channel: str, message: str, guild: str | None) -> events.ReactionAdd:
    return events.ReactionAdd(user_id=PROPOSER, channel_id=channel, message_id=message, guild_id=guild,
                              emoji=events.Emoji(name=SESSION_EMOJI, id=None), member_role_ids=None)


async def add_copy(world: World, channel: str, guild: str, message: str) -> None:
    def tx(c: Any) -> None:
        subscriptions.add(c, world.card_id, guild, channel)
        subscriptions.set_message(c, world.card_id, channel, message, HASH)

    await world.db.run(tx)


def own_removal_path(channel: str, message: str) -> str:
    return f"/api/v1/channels/{channel}/messages/{message}/reactions/{encode_emoji(SESSION_EMOJI)}/{PROPOSER}"


async def test_a_calendar_press_on_the_card_opens_the_prompt_and_removes_the_presser_reaction(build: Any) -> None:
    world = await build()

    await presses_for(world).on_reaction(calendar_tap(GAMES, CARD_MSG, GUILD))

    assert len(dm_texts(world)) == 1 and "Pick a time" in dm_texts(world)[0]
    assert [r.raw_path for r in world.state.requests if r.method == "DELETE"] == [own_removal_path(GAMES, CARD_MSG)]


async def test_a_calendar_press_on_a_home_copy_opens_the_prompt(build: Any) -> None:
    world = await build()
    await add_copy(world, COPY_CH, GUILD, COPY_MSG)

    await presses_for(world).on_reaction(calendar_tap(COPY_CH, COPY_MSG, GUILD))

    assert len(dm_texts(world)) == 1 and "Pick a time" in dm_texts(world)[0]
    assert world.flow.has_draft(PROPOSER) is True


async def test_a_calendar_press_on_a_far_copy_gets_the_pointer_and_no_draft(build: Any) -> None:
    world = await build()
    await add_copy(world, FAR, OTHER_GUILD, FAR_MSG)

    await presses_for(world).on_reaction(calendar_tap(FAR, FAR_MSG, OTHER_GUILD))

    assert world.flow.has_draft(PROPOSER) is False
    [content] = dm_texts(world)
    assert content.startswith(text.SESSION_ELSEWHERE.split("{link}")[0])


async def test_a_calendar_press_on_a_play_now_post_sends_nothing(build: Any) -> None:
    world = await build()

    def post(c: Any) -> None:
        play_now.add(c, card_id=world.card_id, guild_id=GUILD, channel_id=PLAY_NOW, message_id=PLAY_MSG,
                     presser_id=PROPOSER, offers_wake=False, now=int(NOW), expires_at=int(NOW) + 3600)

    await world.db.run(post)
    await presses_for(world).on_reaction(calendar_tap(PLAY_NOW, PLAY_MSG, GUILD))

    assert world.state.dm_channels == {}
    assert world.flow.has_draft(PROPOSER) is False


async def test_a_calendar_press_with_the_switch_off_gives_no_dm_and_no_draft(build: Any) -> None:
    world = await build()
    await world.db.run(lambda c: interaction_settings.set_enabled(c, False))

    await presses_for(world).on_reaction(calendar_tap(GAMES, CARD_MSG, GUILD))

    assert world.state.dm_channels == {}
    assert world.flow.has_draft(PROPOSER) is False


def panel_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("fluxer_panel.session_flow")]
