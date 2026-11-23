# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session fan-out into both subscriptions (AC3.3, AC3.7): a home session is mirrored to every both copy's channel, its
edits reach each mirror, a session that a both scope starts is never mirrored, and a purge scrub reaches the mirrors."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, mirrors, rsvp, sessions, subscriptions
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.dm_router import NonAdminLimiter
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.mirror_post import MirrorPost
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.session_flow import SessionFlow
from fluxer_panel.session_post import SessionPost
from tests.fakes import FakeFluxerState, make_rest_app

TOKEN = "session-mirror-token"
ADMIN = "1001"
PROPOSER = "1101"
GAMES = "3001"          # the games channel: never a target
GAMES_GUILD = "3000"
HOME = "3002"           # the home play-now channel, where a home session is posted
OTHER_GUILD = "4000"
FAR_B = "4102"          # both subscription B's own channel (its scope target)
FAR_L = "4103"          # local subscription L: never mirrored into
FAR_R = "4104"          # redirect copy: never mirrored into
SLOT = 100_000
NOW = 1_000.0
LOCATION = "Hangar seven"


def config_for() -> Config:
    return Config.from_env({"FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC",
                            "FLUXER_INSTANCE_URL": "https://fluxer.invalid", "ADMIN_USER_IDS": ADMIN,
                            "GAMES_CHANNEL_ID": GAMES, "PLAY_NOW_CHANNEL_ID": HOME})


@pytest.fixture
async def world(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[SimpleNamespace]:
    database = Database.open(tmp_path / "session_mirrors.db")
    state = FakeFluxerState(bot_user_id="1900", next_message_id=9001)
    state.channels.update({
        GAMES: {"id": GAMES, "guild_id": GAMES_GUILD, "type": 0},
        HOME: {"id": HOME, "guild_id": GAMES_GUILD, "type": 0},
        FAR_B: {"id": FAR_B, "guild_id": OTHER_GUILD, "type": 0},
        FAR_L: {"id": FAR_L, "guild_id": OTHER_GUILD, "type": 0},
        FAR_R: {"id": FAR_R, "guild_id": OTHER_GUILD, "type": 0},
    })
    server = await aiohttp_server(make_rest_app(state))
    base = str(server.make_url("/")).rstrip("/")
    config = config_for()
    async with aiohttp.ClientSession() as client:
        rest = FluxerREST(client, f"{base}/api", TOKEN)
        pacer = ChannelPacer(max_calls=1000)
        backoff = DmBackoff(config.dm_backoff_seconds, clock=lambda: NOW)
        mirror = MirrorPost(db=database, rest=rest, config=config)
        post = SessionPost(db=database, rest=rest, config=config, pacer=pacer, backoff=backoff, mirrors=mirror)
        games_guild = GamesGuild(rest, config)
        flow = SessionFlow(db=database, rest=rest, config=config, locks=CardLocks(), session_post=post,
                           games_guild=games_guild, pacer=pacer, identity=BotIdentity(user_id="1900"),
                           backoff=backoff, limiter=NonAdminLimiter(config, clock=lambda: NOW),
                           clock=lambda: NOW, kick=lambda: None)

        def build(conn: Any) -> dict[str, int]:
            card_id = cards.create_card(conn, slug="mc", title="Minecraft", author_id=ADMIN, now=1)
            cards.set_state(conn, card_id, "live", 1)
            sub_b = subscriptions.add(conn, card_id, OTHER_GUILD, FAR_B)
            assert subscriptions.set_mode_if(conn, sub_b, "redirect", "both")
            sub_l = subscriptions.add(conn, card_id, OTHER_GUILD, FAR_L)
            assert subscriptions.set_mode_if(conn, sub_l, "redirect", "local")
            sub_r = subscriptions.add(conn, card_id, OTHER_GUILD, FAR_R)
            return {"card": card_id, "sub_b": sub_b, "sub_l": sub_l, "sub_r": sub_r}

        ids = await database.run(build)
        yield SimpleNamespace(db=database, state=state, rest=rest, post=post, flow=flow, config=config,
                              **ids)
    database.close()


async def publish(world: SimpleNamespace, *, scope_sub_id: int | None = None) -> int:
    outcome = await world.flow.publish(world.card, PROPOSER, SLOT, scope_sub_id=scope_sub_id)
    assert outcome.session_id is not None, outcome.reply
    return outcome.session_id


def requests(world: SimpleNamespace, method: str, path: str) -> list[Any]:
    return [r for r in world.state.requests if r.method == method and r.path == path]


def messages_path(channel_id: str, message_id: str | None = None) -> str:
    base = f"/api/v1/channels/{channel_id}/messages"
    return base if message_id is None else f"{base}/{message_id}"


def _add_going(conn: Any, session: sessions.Session, user_id: str) -> None:
    """One real RSVP, as rsvp_changed writes it: the presence row, then the engine's computed state."""
    sessions.add_presence(conn, session, user_id, "going", int(NOW))
    sessions.recompute(conn, session, None, int(NOW))


async def mirror_row(world: SimpleNamespace, sub_id: int, session_id: int) -> mirrors.Mirror | None:
    return await world.db.run(lambda c: mirrors.get(c, "session", session_id, sub_id))


async def edit(world: SimpleNamespace, session_id: int, *, scrubbed: bool = False) -> None:
    await world.post.edit_session(session_id, scrubbed=scrubbed)


async def mirror_patches(world: SimpleNamespace, sub_id: int, session_id: int) -> list[Any]:
    row = await mirror_row(world, sub_id, session_id)
    assert row is not None
    return requests(world, "PATCH", messages_path(row.channel_id, row.message_id))


async def test_publishing_a_home_session_posts_one_mirror_to_each_both_copy(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    row = await mirror_row(world, world.sub_b, session_id)
    assert row is not None and row.channel_id == FAR_B
    [post] = requests(world, "POST", messages_path(FAR_B))
    assert post.payload["allowed_mentions"] == {"parse": []}
    assert requests(world, "POST", messages_path(FAR_L)) == []
    assert requests(world, "POST", messages_path(FAR_R)) == []


async def test_a_new_mirror_gets_the_three_rsvp_seeds(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    row = await mirror_row(world, world.sub_b, session_id)
    assert row is not None
    seeded = world.state.reactions.get((FAR_B, row.message_id), {})
    assert set(rsvp.RSVP_EMOJI) <= set(seeded)


async def test_an_rsvp_count_change_patches_the_mirror(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    session = await world.db.run(lambda c: sessions.get(c, session_id))
    assert session is not None
    await world.db.run(lambda c: _add_going(c, session, "1201"))
    before = len(await mirror_patches(world, world.sub_b, session_id))
    await edit(world, session_id)
    assert len(await mirror_patches(world, world.sub_b, session_id)) == before + 1


async def test_an_edit_with_no_change_does_not_patch_the_mirror(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    await edit(world, session_id)
    before = len(await mirror_patches(world, world.sub_b, session_id))
    await edit(world, session_id)
    assert len(await mirror_patches(world, world.sub_b, session_id)) == before


async def test_a_move_patches_the_mirror(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    before = len(await mirror_patches(world, world.sub_b, session_id))
    moved = await world.db.run(lambda c: sessions.move_if(c, session_id, starts_at=SLOT + 3600,
                                                           ends_at=SLOT + 7200))
    assert moved
    await edit(world, session_id)
    assert len(await mirror_patches(world, world.sub_b, session_id)) == before + 1


async def test_a_cancel_patches_the_mirror(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    before = len(await mirror_patches(world, world.sub_b, session_id))
    assert await world.db.run(lambda c: sessions.cancel_if(c, session_id, int(NOW)))
    await edit(world, session_id)
    assert len(await mirror_patches(world, world.sub_b, session_id)) == before + 1


async def test_a_both_scope_session_is_posted_only_in_its_own_channel(world: SimpleNamespace) -> None:
    await publish(world, scope_sub_id=world.sub_b)
    assert len(requests(world, "POST", messages_path(FAR_B))) == 1
    assert requests(world, "POST", messages_path(HOME)) == []
    assert requests(world, "POST", messages_path(FAR_L)) == []
    rows = await world.db.run(lambda c: mirrors.for_sub(c, world.sub_b))
    assert rows == []


async def test_a_purge_scrub_patches_each_mirror_without_the_location(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    await world.db.run(lambda c: sessions.set_location_if(c, session_id, LOCATION))
    await edit(world, session_id)
    session = await world.db.run(lambda c: sessions.get(c, session_id))
    assert session is not None
    assert LOCATION in str((await mirror_patches(world, world.sub_b, session_id))[-1].payload)
    await world.post.scrub(session)
    assert LOCATION not in str((await mirror_patches(world, world.sub_b, session_id))[-1].payload)
