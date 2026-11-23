# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Play-now fan-out into both subscriptions (AC3.1, AC3.2, AC3.7): a home play-now call is mirrored into each both
copy's channel with only that copy's role mentioned, a Me Too on any of the messages counts once everywhere, a press on
a both copy stays in that copy, and an expiry ends every mirror."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import cards, mirrors, play_now, render, subscriptions, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import FOLLOW_EMOJI, PLAY_NOW_EMOJI, Press, Presses, Surface
from fluxer_panel.seeding import Seeder
from tests.fakes import FakeFluxerState, Recorded, make_rest_app

TOKEN = "play-now-mirror-token"
BOT_ID = "1900"
ADMIN = "1001"
PRESSER = "6001"
REACTOR_U = "6101"
REACTOR_V = "6102"
GAMES_GUILD = "3000"
GAMES = "3001"          # the games channel: the card's message lives here
HOME = "3002"           # PLAY_NOW_CHANNEL_ID: the home play-now channel, in the games community
OTHER_GUILD = "4000"
F1 = "4101"             # both subscription B1's channel (its local scope target)
F2 = "4102"             # both subscription B2's channel
FL = "4103"             # local subscription L: never mirrored into
ALPHA_ROLE = "7001"     # the card's follow role
R1 = "7101"             # B1's follow role, in OTHER_GUILD
R2 = "7102"             # B2's follow role, in OTHER_GUILD
CARD_ID = 1
CARD_TITLE = "Minecraft"
CARD_MSG = "3101"
B1_COPY_MSG = "4201"    # B1's card copy, in F1
B2_COPY_MSG = "4202"    # B2's card copy, in F2
L_COPY_MSG = "4203"     # L's card copy, in FL
JUMP_URL = "https://fluxer.invalid"
NOW = 1000
HASH = "a" * 64


class ProbePacer(ChannelPacer):
    """Records whether the card's lock is held each time a reaction call waits for its budget."""

    def __init__(self, locks: CardLocks, card_id: int) -> None:
        super().__init__(max_calls=1000)
        self._card_locks = locks   # not `_locks`: ChannelPacer keeps its own per-channel locks under that name
        self._card_id = card_id
        self.held: list[bool] = []

    async def wait(self, channel_id: str) -> None:
        self.held.append(self._card_locks.get(self._card_id).locked())
        await super().wait(channel_id)


@dataclass
class Clock:
    now: float = NOW

    def __call__(self) -> float:
        return self.now


def config_for() -> Config:
    return Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": JUMP_URL,
                            "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES, "PLAY_NOW_CHANNEL_ID": HOME})


class RecordingHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def handle(self, press: Press) -> None:
        self.calls.append(("press", press))

    async def me_too_changed(self, card_id: int, surface: Surface, user_id: str) -> None:
        self.calls.append(("me_too", surface))

    async def rsvp_changed(self, session_id: int, user_id: str, choice: Any, added: bool, **_: Any) -> None:
        self.calls.append(("rsvp", session_id))

    async def poll_reaction(self, poll_id: int, user_id: str, emoji: str, channel_id: str, message_id: str) -> None:
        self.calls.append(("poll", poll_id))


@pytest.fixture
async def world(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[SimpleNamespace]:
    database = Database.open(tmp_path / "play_now_mirrors.db")
    state = FakeFluxerState(bot_user_id=BOT_ID, next_message_id=9001)
    state.channels.update({
        GAMES: {"id": GAMES, "guild_id": GAMES_GUILD, "type": 0},
        HOME: {"id": HOME, "guild_id": GAMES_GUILD, "type": 0},
        F1: {"id": F1, "guild_id": OTHER_GUILD, "type": 0},
        F2: {"id": F2, "guild_id": OTHER_GUILD, "type": 0},
        FL: {"id": FL, "guild_id": OTHER_GUILD, "type": 0},
    })
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    config = config_for()
    clock = Clock()
    async with aiohttp.ClientSession() as client:
        rest = FluxerREST(client, f"{base}/api", TOKEN)
        locks = CardLocks()
        pacer = ProbePacer(locks, CARD_ID)
        games_guild = GamesGuild(rest, config)
        identity = BotIdentity(user_id=BOT_ID)
        interactions = Interactions(db=database, rest=rest, config=config, locks=locks, games_guild=games_guild,
                                    seeder=Seeder(rest=rest, pacer=pacer, db=database), pacer=pacer,
                                    identity=identity, roles=RoleCache(rest), clock=clock)
        presses = Presses(db=database, rest=rest, config=config, handler=interactions, games_guild=games_guild,
                          pacer=pacer, identity=identity, clock=clock)

        def build(conn: Any) -> dict[str, Any]:
            card = cards.create_card(conn, slug="mc", title=CARD_TITLE, author_id=ADMIN, now=1)
            cards.set_state(conn, card, "live", 1)
            cards.set_message(conn, card, GAMES, CARD_MSG, HASH)
            cards.set_follow_role(conn, card, GAMES_GUILD, ALPHA_ROLE)
            sub_1 = subscriptions.add(conn, card, OTHER_GUILD, F1)
            assert subscriptions.set_mode_if(conn, sub_1, "redirect", "both")
            subscriptions.set_message(conn, card, F1, B1_COPY_MSG, HASH)
            cards.set_subscription_role(conn, sub_1, R1)
            sub_2 = subscriptions.add(conn, card, OTHER_GUILD, F2)
            assert subscriptions.set_mode_if(conn, sub_2, "redirect", "both")
            subscriptions.set_message(conn, card, F2, B2_COPY_MSG, HASH)
            cards.set_subscription_role(conn, sub_2, R2)
            sub_l = subscriptions.add(conn, card, OTHER_GUILD, FL)
            assert subscriptions.set_mode_if(conn, sub_l, "redirect", "local")
            subscriptions.set_message(conn, card, FL, L_COPY_MSG, HASH)
            return {"card": card, "sub_1": sub_1, "sub_2": sub_2, "sub_l": sub_l}

        ids = await database.run(build)
        yield SimpleNamespace(db=database, state=state, rest=rest, config=config, clock=clock, presses=presses,
                              interactions=interactions, games_guild=games_guild, pacer=pacer, identity=identity,
                              locks=locks, **ids)
    database.close()


def press(emoji: str, *, user: str = PRESSER, channel: str = GAMES, message: str = CARD_MSG,
          guild: str = GAMES_GUILD) -> events.ReactionAdd:
    return events.ReactionAdd(user_id=user, channel_id=channel, message_id=message, guild_id=guild,
                              emoji=events.Emoji(name=emoji, id=None), member_role_ids=frozenset(), user_is_bot=False)


def far_me_too(user: str, channel: str, message: str) -> events.ReactionAdd:
    guild = GAMES_GUILD if channel == HOME else OTHER_GUILD
    return press(PLAY_NOW_EMOJI, user=user, channel=channel, message=message, guild=guild)


def posts_to(state: FakeFluxerState, channel: str) -> list[Recorded]:
    path = f"/api/v1/channels/{channel}/messages"
    return [r for r in state.requests if r.method == "POST" and r.path == path]


def messages_in(state: FakeFluxerState, channel: str) -> list[str]:
    return [mid for (cid, mid) in state.messages if cid == channel]


def edits_to(state: FakeFluxerState, channel: str, message: str) -> list[Recorded]:
    path = f"/api/v1/channels/{channel}/messages/{message}"
    return [r for r in state.requests if r.method == "PATCH" and r.path == path]


def reaction_wipes(state: FakeFluxerState, channel: str, message: str) -> list[Recorded]:
    path = f"/api/v1/channels/{channel}/messages/{message}/reactions"
    return [r for r in state.requests if r.method == "DELETE" and r.path == path]


def dm_texts(state: FakeFluxerState, user: str) -> list[str]:
    channel = state.dm_channels.get(user)
    if channel is None:
        return []
    return [r.payload["content"] for r in posts_to(state, channel)]


def seeded(state: FakeFluxerState, channel: str, message: str) -> set[str]:
    return set(state.reactions.get((channel, message), {}))


def status_of(recorded: Recorded) -> str:
    [value] = [f["value"] for f in recorded.payload["embeds"][0]["fields"] if f["name"] == text.FIELD_STATUS]
    return value


def set_reactors(state: FakeFluxerState, channel: str, message: str, users: list[str]) -> None:
    """Puts these play-now reactors on one message, after the bot's own."""
    state.reactions[(channel, message)][PLAY_NOW_EMOJI] = [BOT_ID, *users]


async def home_row(world: SimpleNamespace) -> play_now.PlayNow:
    row = await world.db.run(lambda c: play_now.latest_for_card(c, CARD_ID))
    assert row is not None
    return row


async def press_home(world: SimpleNamespace) -> None:
    await world.presses.on_reaction(press(PLAY_NOW_EMOJI))


async def mirror_rows(world: SimpleNamespace, row: play_now.PlayNow) -> list[mirrors.Mirror]:
    return await world.db.run(lambda c: mirrors.for_object(c, "play_now", row.id))


# Posting: AC3.1 --------------------------------------------------------------------------------------------------


async def test_a_home_press_posts_the_home_call_and_one_mirror_into_each_both_copy(world: SimpleNamespace) -> None:
    await press_home(world)
    [home] = posts_to(world.state, HOME)
    assert home.payload["allowed_mentions"] == {"parse": [], "roles": [ALPHA_ROLE], "replied_user": False}
    [mirror_1] = posts_to(world.state, F1)
    [mirror_2] = posts_to(world.state, F2)
    assert mirror_1.payload["allowed_mentions"] == {"parse": [], "roles": [R1], "replied_user": False}
    assert mirror_2.payload["allowed_mentions"] == {"parse": [], "roles": [R2], "replied_user": False}
    assert mirror_1.payload["content"].endswith(f"<@&{R1}>")
    assert R2 not in mirror_1.payload["content"]
    assert mirror_2.payload["content"].endswith(f"<@&{R2}>")


async def test_nothing_is_posted_into_a_local_subscription(world: SimpleNamespace) -> None:
    await press_home(world)
    assert posts_to(world.state, FL) == []


async def test_each_mirror_is_stored_against_the_home_row_and_its_subscription(world: SimpleNamespace) -> None:
    await press_home(world)
    row = await home_row(world)
    stored = await mirror_rows(world, row)
    assert sorted((m.sub_id, m.channel_id) for m in stored) == sorted([(world.sub_1, F1), (world.sub_2, F2)])


async def test_a_both_copy_with_no_role_gets_a_mirror_that_mentions_nobody(world: SimpleNamespace) -> None:
    await world.db.run(lambda c: cards.clear_subscription_role(c, world.sub_2))
    await press_home(world)
    [mirror_2] = posts_to(world.state, F2)
    assert mirror_2.payload["allowed_mentions"] == {"parse": []}
    assert "<@&" not in mirror_2.payload["content"]


async def test_each_mirror_is_seeded_with_the_me_too_emoji(world: SimpleNamespace) -> None:
    await press_home(world)
    [mid_1] = messages_in(world.state, F1)
    [mid_2] = messages_in(world.state, F2)
    assert seeded(world.state, F1, mid_1) == {PLAY_NOW_EMOJI}
    assert seeded(world.state, F2, mid_2) == {PLAY_NOW_EMOJI}


# Me Too counting: AC3.2 -----------------------------------------------------------------------------------------


async def test_a_me_too_on_a_mirror_recounts_the_home_post_and_every_mirror_to_two(world: SimpleNamespace) -> None:
    await press_home(world)
    [home_mid] = messages_in(world.state, HOME)
    [mid_1] = messages_in(world.state, F1)
    set_reactors(world.state, F1, mid_1, [REACTOR_U])
    await world.presses.on_reaction(far_me_too(REACTOR_U, F1, mid_1))
    counts = [status_of(e) for e in edits_to(world.state, HOME, home_mid) + edits_to(world.state, F1, mid_1)
              + edits_to(world.state, F2, messages_in(world.state, F2)[0])]
    assert counts == [text.PLAY_NOW_PLAYING.format(n=2)] * 3


async def test_a_me_too_on_the_home_post_by_the_same_user_keeps_the_count_at_two(world: SimpleNamespace) -> None:
    await press_home(world)
    [home_mid] = messages_in(world.state, HOME)
    [mid_1] = messages_in(world.state, F1)
    set_reactors(world.state, F1, mid_1, [REACTOR_U])
    set_reactors(world.state, HOME, home_mid, [REACTOR_U])
    await world.presses.on_reaction(far_me_too(REACTOR_U, HOME, home_mid))
    assert status_of(edits_to(world.state, HOME, home_mid)[-1]) == text.PLAY_NOW_PLAYING.format(n=2)
    assert status_of(edits_to(world.state, F1, mid_1)[-1]) == text.PLAY_NOW_PLAYING.format(n=2)


async def test_a_me_too_by_another_user_on_the_second_mirror_gives_three_everywhere(world: SimpleNamespace) -> None:
    await press_home(world)
    [home_mid] = messages_in(world.state, HOME)
    [mid_1] = messages_in(world.state, F1)
    [mid_2] = messages_in(world.state, F2)
    set_reactors(world.state, F1, mid_1, [REACTOR_U])
    set_reactors(world.state, F2, mid_2, [REACTOR_V])
    await world.presses.on_reaction(far_me_too(REACTOR_V, F2, mid_2))
    counts = [status_of(edits_to(world.state, HOME, home_mid)[-1]),
              status_of(edits_to(world.state, F1, mid_1)[-1]),
              status_of(edits_to(world.state, F2, mid_2)[-1])]
    assert counts == [text.PLAY_NOW_PLAYING.format(n=3)] * 3


async def test_a_me_too_on_a_mirror_never_posts_or_seeds_a_new_message(world: SimpleNamespace) -> None:
    await press_home(world)
    [mid_1] = messages_in(world.state, F1)
    before = len(world.state.requests)
    await world.presses.on_reaction(far_me_too(REACTOR_U, F1, mid_1))
    new = world.state.requests[before:]
    assert not [r for r in new if r.method == "POST" and r.path.endswith("/messages")]
    assert not [r for r in new if r.method == "PUT"]


# Subscription scopes: AC3.7 -----------------------------------------------------------------------------------


async def test_a_press_on_a_both_copy_posts_in_its_own_channel_only(world: SimpleNamespace) -> None:
    await world.presses.on_reaction(press(PLAY_NOW_EMOJI, channel=F1, message=B1_COPY_MSG, guild=OTHER_GUILD))
    assert len(posts_to(world.state, F1)) == 1
    assert posts_to(world.state, HOME) == []
    assert posts_to(world.state, F2) == []
    assert await world.db.run(lambda c: c.execute("SELECT COUNT(*) FROM mirror_message").fetchone()[0]) == 0
    row = await world.db.run(lambda c: play_now.live_for_scope(c, CARD_ID, world.sub_1))
    assert len(row) == 1


# Expiry -----------------------------------------------------------------------------------------------------------


async def test_expiry_ends_every_mirror_and_clears_its_reactions(world: SimpleNamespace) -> None:
    await press_home(world)
    [home_mid] = messages_in(world.state, HOME)
    [mid_1] = messages_in(world.state, F1)
    [mid_2] = messages_in(world.state, F2)
    world.clock.now = NOW + world.config.play_now_expiry_seconds
    await world.interactions.expire_play_now()
    ended = text.PLAY_NOW_ENDED_CONTENT.format(card=render.escape(CARD_TITLE))
    for channel, message in ((HOME, home_mid), (F1, mid_1), (F2, mid_2)):
        [edit] = edits_to(world.state, channel, message)
        assert edit.payload["content"] == ended
        assert edit.payload["allowed_mentions"] == {"parse": []}
        assert len(reaction_wipes(world.state, channel, message)) == 1


# Surfaces: a mirror is never home ---------------------------------------------------------------------------------


async def test_a_press_on_a_mirror_surfaces_as_the_home_play_now_post_and_never_as_home(world: SimpleNamespace) -> None:
    await press_home(world)
    [home_mid] = messages_in(world.state, HOME)
    [mid_1] = messages_in(world.state, F1)
    handler = RecordingHandler()
    presses = Presses(db=world.db, rest=world.rest, config=world.config, handler=handler,
                      games_guild=world.games_guild, pacer=world.pacer, identity=world.identity, clock=world.clock)
    await presses.on_reaction(far_me_too(REACTOR_U, F1, mid_1))
    [(kind, surface)] = handler.calls
    assert kind == "me_too"
    assert surface.kind == "play_now"
    assert surface.message_id == mid_1
    assert surface.mirror_of == home_mid
    assert surface.home_community is False
    assert surface.sub_id is None


async def test_a_bell_press_on_a_mirror_makes_no_role_call_and_is_pointed_at_the_card(world: SimpleNamespace) -> None:
    await press_home(world)
    [mid_1] = messages_in(world.state, F1)
    await world.presses.on_reaction(press(FOLLOW_EMOJI, user=REACTOR_U, channel=F1, message=mid_1, guild=OTHER_GUILD))
    assert [r for r in world.state.requests if "/roles/" in r.path] == []
    assert len(dm_texts(world.state, REACTOR_U)) == 1


async def test_a_bell_press_on_a_mirror_reaches_the_handler_as_a_play_now_surface_outside_home(
        world: SimpleNamespace) -> None:
    await press_home(world)
    [mid_1] = messages_in(world.state, F1)
    handler = RecordingHandler()
    presses = Presses(db=world.db, rest=world.rest, config=world.config, handler=handler,
                      games_guild=world.games_guild, pacer=world.pacer, identity=world.identity, clock=world.clock)
    await presses.on_reaction(press(FOLLOW_EMOJI, user=REACTOR_U, channel=F1, message=mid_1, guild=OTHER_GUILD))
    [(kind, found)] = handler.calls
    assert kind == "press" and isinstance(found, Press)
    assert found.surface.kind == "play_now"
    assert found.surface.home_community is False


async def test_the_reaction_seeds_of_a_home_call_and_its_mirrors_wait_outside_the_card_lock(world: SimpleNamespace
                                                                                          ) -> None:
    await press_home(world)
    assert len(world.pacer.held) == 4   # the home seed, one seed per mirror, then the presser's own removal
    assert not any(world.pacer.held)
    assert world.locks.get(CARD_ID).locked() is False
