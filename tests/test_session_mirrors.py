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

from fluxer_client import events
from fluxer_client.roles import RoleCache
from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, mirrors, rsvp, sessions, subscriptions
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.dm_router import NonAdminLimiter
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.mirror_post import MirrorPost
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import Presses
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_commands import SessionCommands
from fluxer_panel.session_flow import SessionFlow
from fluxer_panel.session_post import SessionPost
from tests.fakes import FakeFluxerState, Override, make_rest_app

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
        locks = CardLocks()
        identity = BotIdentity(user_id="1900")
        flow = SessionFlow(db=database, rest=rest, config=config, locks=locks, session_post=post,
                           games_guild=games_guild, pacer=pacer, identity=identity,
                           backoff=backoff, limiter=NonAdminLimiter(config, clock=lambda: NOW),
                           clock=lambda: NOW, kick=lambda: None)
        interactions = Interactions(db=database, rest=rest, config=config, locks=locks, games_guild=games_guild,
                                    seeder=Seeder(rest=rest, pacer=pacer, db=database), pacer=pacer,
                                    identity=identity, roles=RoleCache(rest), clock=lambda: NOW,
                                    kick=lambda: None, session_post=post, backoff=backoff, session_flow=flow)
        presses = Presses(db=database, rest=rest, config=config, handler=interactions, games_guild=games_guild,
                          pacer=pacer, identity=identity, clock=lambda: NOW, backoff=backoff)
        commands = SessionCommands(db=database, rest=rest, config=config, locks=locks, session_post=post,
                                   clock=lambda: NOW)

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
                              interactions=interactions, presses=presses, commands=commands, mirror=mirror,
                              locks=locks,
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


# Task 4: RSVPs from mirrors count once in the home roster. A far user's DM links the mirror, never the home message.
FAR_USER = "1201"
OTHER_FAR_USER = "1202"
QUIET_FAR_USER = "1203"
HOME_ONLY_USER = "1204"


def reaction(user_id: str, channel_id: str, message_id: str, *, added: bool = True) -> Any:
    cls = events.ReactionAdd if added else events.ReactionRemove
    return cls(user_id=user_id, channel_id=channel_id, message_id=message_id, guild_id=GAMES_GUILD,
               emoji=events.Emoji(name=rsvp.GOING_EMOJI, id=None), member_role_ids=None)


def set_going(world: SimpleNamespace, channel_id: str, message_id: str, users: list[str]) -> None:
    """Puts the users' going reactions into the fake's state, as Fluxer would hold them after a real reaction."""
    message = world.state.reactions.setdefault((channel_id, message_id), {})
    if users:
        message[rsvp.GOING_EMOJI] = list(users)
    else:
        message.pop(rsvp.GOING_EMOJI, None)
        if not message:
            world.state.reactions.pop((channel_id, message_id), None)


async def mirror_message(world: SimpleNamespace, sub_id: int, session_id: int) -> str:
    row = await mirror_row(world, sub_id, session_id)
    assert row is not None
    return row.message_id


async def home_message(world: SimpleNamespace, session_id: int) -> str:
    session = await world.db.run(lambda c: sessions.get(c, session_id))
    assert session is not None and session.message_id is not None
    return session.message_id


async def going(world: SimpleNamespace, session_id: int) -> list[str]:
    rows = await world.db.run(lambda c: sessions.rsvps_of(c, session_id))
    return sorted(user for user, (state, _) in rows.items() if state == "going")


async def origin(world: SimpleNamespace, session_id: int, user_id: str) -> int | None:
    return await world.db.run(lambda c: sessions.presence_via(c, session_id, user_id))


def dm_contents(world: SimpleNamespace, user_id: str) -> list[str]:
    channel = world.state.dm_channels.get(user_id)
    if channel is None:
        return []
    return [str(payload.get("content", "")) for (cid, _), payload in world.state.messages.items() if cid == channel]


async def test_a_going_on_the_mirror_joins_the_home_roster_once(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    set_going(world, FAR_B, mirror_id, [FAR_USER])
    await world.presses.on_reaction(reaction(FAR_USER, FAR_B, mirror_id))
    assert await going(world, session_id) == sorted([PROPOSER, FAR_USER])
    assert await origin(world, session_id, FAR_USER) == world.sub_b
    assert len(requests(world, "PATCH", messages_path(FAR_B, mirror_id))) >= 1
    counts = await world.db.run(lambda c: sessions.counts_of(c, session_id))
    assert counts.going == 2


async def test_a_user_on_the_home_and_the_mirror_is_counted_once_and_is_home(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    home_id = await home_message(world, session_id)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    set_going(world, HOME, home_id, [FAR_USER])
    set_going(world, FAR_B, mirror_id, [FAR_USER])
    await world.presses.on_reaction(reaction(FAR_USER, HOME, home_id))
    await world.presses.on_reaction(reaction(FAR_USER, FAR_B, mirror_id))
    assert await going(world, session_id) == sorted([PROPOSER, FAR_USER])
    assert await origin(world, session_id, FAR_USER) is None


async def test_removing_the_mirror_reaction_keeps_a_home_reaction(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    home_id = await home_message(world, session_id)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    set_going(world, HOME, home_id, [FAR_USER])
    set_going(world, FAR_B, mirror_id, [FAR_USER])
    await world.presses.on_reaction(reaction(FAR_USER, HOME, home_id))
    await world.presses.on_reaction(reaction(FAR_USER, FAR_B, mirror_id))
    set_going(world, FAR_B, mirror_id, [])
    await world.presses.on_reaction(reaction(FAR_USER, FAR_B, mirror_id, added=False))
    assert await going(world, session_id) == sorted([PROPOSER, FAR_USER])


async def test_removing_from_both_messages_drops_the_rsvp(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    set_going(world, FAR_B, mirror_id, [FAR_USER])
    await world.presses.on_reaction(reaction(FAR_USER, FAR_B, mirror_id))
    set_going(world, FAR_B, mirror_id, [])
    await world.presses.on_reaction(reaction(FAR_USER, FAR_B, mirror_id, added=False))
    assert await going(world, session_id) == [PROPOSER]


async def test_removing_the_home_reaction_of_a_mirror_user_keeps_them_and_moves_their_origin(
        world: SimpleNamespace) -> None:
    session_id = await publish(world)
    home_id = await home_message(world, session_id)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    set_going(world, HOME, home_id, [FAR_USER])
    set_going(world, FAR_B, mirror_id, [FAR_USER])
    await world.presses.on_reaction(reaction(FAR_USER, HOME, home_id))
    await world.presses.on_reaction(reaction(FAR_USER, FAR_B, mirror_id))
    set_going(world, HOME, home_id, [])
    await world.presses.on_reaction(reaction(FAR_USER, HOME, home_id, added=False))
    assert FAR_USER in await going(world, session_id)
    assert await origin(world, session_id, FAR_USER) == world.sub_b


async def test_a_failed_recheck_keeps_the_rsvp(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    home_id = await home_message(world, session_id)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    set_going(world, FAR_B, mirror_id, [FAR_USER])
    await world.presses.on_reaction(reaction(FAR_USER, FAR_B, mirror_id))
    world.state.overrides[f"GET {messages_path(HOME, home_id)}/reactions/{rsvp.GOING_EMOJI}/users"] = Override(
        status=500, body=b"{}")
    set_going(world, FAR_B, mirror_id, [])
    await world.presses.on_reaction(reaction(FAR_USER, FAR_B, mirror_id, added=False))
    assert FAR_USER in await going(world, session_id)


async def test_a_waiting_mirror_user_is_promoted_with_a_mirror_link(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    home_id = await home_message(world, session_id)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    await world.db.run(lambda c: sessions.set_seats_if(c, session_id, 2))
    session = await world.db.run(lambda c: sessions.get(c, session_id))
    assert session is not None
    await world.db.run(lambda c: _add_going(c, session, FAR_USER))
    set_going(world, HOME, home_id, [FAR_USER])
    set_going(world, FAR_B, mirror_id, [OTHER_FAR_USER])
    await world.presses.on_reaction(reaction(OTHER_FAR_USER, FAR_B, mirror_id))
    assert await going(world, session_id) == sorted([PROPOSER, FAR_USER])
    set_going(world, HOME, home_id, [])
    await world.presses.on_reaction(reaction(FAR_USER, HOME, home_id, added=False))
    assert OTHER_FAR_USER in await going(world, session_id)
    [dm] = dm_contents(world, OTHER_FAR_USER)
    assert mirror_id in dm and home_id not in dm and HOME not in dm


async def test_a_startup_reconcile_keeps_a_mirror_only_user_with_their_origin(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    set_going(world, FAR_B, mirror_id, [QUIET_FAR_USER])
    await world.interactions.reconcile_sessions()
    assert QUIET_FAR_USER in await going(world, session_id)
    assert await origin(world, session_id, QUIET_FAR_USER) == world.sub_b


async def test_a_far_user_s_move_dm_links_the_mirror_and_a_home_user_s_links_home(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    home_id = await home_message(world, session_id)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    set_going(world, FAR_B, mirror_id, [FAR_USER])
    await world.presses.on_reaction(reaction(FAR_USER, FAR_B, mirror_id))
    assert await origin(world, session_id, FAR_USER) == world.sub_b
    await world.commands.handle(f"move {session_id} 1970-01-02 19:00",
                                events.MessageCreate(message_id="7000", channel_id=HOME, guild_id=None,
                                                     author_id=PROPOSER, author_is_bot=False,
                                                     member_role_ids=None, content=""))
    far_dm = " ".join(dm_contents(world, FAR_USER))
    home_dm = " ".join(dm_contents(world, PROPOSER))
    assert mirror_id in far_dm and home_id not in far_dm
    assert home_id in home_dm and mirror_id not in home_dm


async def test_a_far_user_has_no_link_once_their_mirror_is_gone(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    session = await world.db.run(lambda c: sessions.get(c, session_id))
    assert session is not None
    set_going(world, FAR_B, mirror_id, [FAR_USER])
    await world.presses.on_reaction(reaction(FAR_USER, FAR_B, mirror_id))
    assert await origin(world, session_id, FAR_USER) == world.sub_b
    row = await mirror_row(world, world.sub_b, session_id)
    assert row is not None
    await world.mirror.delete(row)
    assert await world.post.link_for(session, FAR_USER) is None


async def test_a_card_wide_cancel_under_the_lock_posts_no_new_mirror(world: SimpleNamespace) -> None:
    # The session predates the copy's both mode, so it has no mirror. A cancel runs under the card lock, where a new
    # mirror would be posted and seeded: the cancel only edits the mirrors that already exist.
    await world.db.run(lambda c: subscriptions.set_mode_if(c, world.sub_b, "both", "redirect"))
    session_id = await publish(world)
    await world.db.run(lambda c: subscriptions.set_mode_if(c, world.sub_b, "redirect", "both"))
    async with world.locks.get(world.card):
        assert await world.commands.cancel_card_sessions_locked(world.card, ADMIN) == 1
    assert await mirror_row(world, world.sub_b, session_id) is None
    assert requests(world, "POST", messages_path(FAR_B)) == []


async def test_a_mirror_dropped_on_a_404_is_not_posted_again_by_a_later_edit(world: SimpleNamespace) -> None:
    """Decision 5: a mirror the copy's members deleted stays gone. Later edits of the home session (an RSVP, the end)
    edit the mirrors that are stored and post none, so only a mode change back into both posts it again."""
    session_id = await publish(world)
    row = await mirror_row(world, world.sub_b, session_id)
    assert row is not None
    world.state.overrides[f"PATCH {messages_path(FAR_B, row.message_id)}"] = Override(
        404, b'{"code": "UNKNOWN_MESSAGE", "message": "Unknown Message"}')
    await world.db.run(lambda c: _add_going(c, sessions.get(c, session_id), "1105"))
    await edit(world, session_id)                 # the mirror's content changed: the edit gets the 404 and drops it
    assert await mirror_row(world, world.sub_b, session_id) is None

    await edit(world, session_id)
    await edit(world, session_id)

    assert len(requests(world, "POST", messages_path(FAR_B))) == 1, "only the publish posted the mirror"


# An unreadable mirror: a 403 or 404 on its reactor reads never blocks a home removal or the startup reconcile
# (AC3.8) -----------------------------------------------------------------------------------------------------

def mirror_reactors_path(channel_id: str, message_id: str) -> str:
    return f"GET {messages_path(channel_id, message_id)}/reactions/{rsvp.GOING_EMOJI}/users"


async def remove_home_going(world: SimpleNamespace, session_id: int, user_id: str) -> None:
    home_id = await home_message(world, session_id)
    set_going(world, HOME, home_id, [])
    await world.presses.on_reaction(reaction(user_id, HOME, home_id, added=False))


async def far_and_home_going(world: SimpleNamespace) -> tuple[int, str, str]:
    """A session with FAR_USER going on the home message and on B's mirror. Returns (session id, home id, mirror id)."""
    session_id = await publish(world)
    home_id = await home_message(world, session_id)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    set_going(world, HOME, home_id, [FAR_USER])
    set_going(world, FAR_B, mirror_id, [FAR_USER])
    await world.presses.on_reaction(reaction(FAR_USER, HOME, home_id))
    await world.presses.on_reaction(reaction(FAR_USER, FAR_B, mirror_id))
    return session_id, home_id, mirror_id


async def test_a_mirror_answering_403_does_not_block_a_home_removal(world: SimpleNamespace,
                                                                    caplog: pytest.LogCaptureFixture) -> None:
    session_id, _home_id, mirror_id = await far_and_home_going(world)
    world.state.overrides[mirror_reactors_path(FAR_B, mirror_id)] = Override(
        403, b'{"code": "MISSING_PERMISSIONS", "message": "Missing Permissions"}')
    await remove_home_going(world, session_id, FAR_USER)
    assert FAR_USER not in await going(world, session_id)
    row = await mirror_row(world, world.sub_b, session_id)
    assert row is not None and row.message_id == mirror_id      # a 403 keeps the row
    assert any(r.getMessage() == "mirror_unreadable" for r in caplog.records)


async def test_a_mirror_answering_404_is_dropped_and_the_home_removal_applies(world: SimpleNamespace) -> None:
    session_id, _home_id, mirror_id = await far_and_home_going(world)
    world.state.overrides[mirror_reactors_path(FAR_B, mirror_id)] = Override(
        404, b'{"code": "UNKNOWN_MESSAGE", "message": "Unknown Message"}')
    await remove_home_going(world, session_id, FAR_USER)
    assert FAR_USER not in await going(world, session_id)
    assert await mirror_row(world, world.sub_b, session_id) is None


async def test_a_transient_failure_on_a_mirror_keeps_the_rsvp(world: SimpleNamespace) -> None:
    session_id, _home_id, mirror_id = await far_and_home_going(world)
    world.state.overrides[mirror_reactors_path(FAR_B, mirror_id)] = Override(
        500, b'{"code": "INTERNAL", "message": "Internal Error"}')
    await remove_home_going(world, session_id, FAR_USER)
    assert FAR_USER in await going(world, session_id)
    row = await mirror_row(world, world.sub_b, session_id)
    assert row is not None and row.message_id == mirror_id


async def test_a_startup_reconcile_with_a_403_mirror_still_reconciles_the_home_message(
        world: SimpleNamespace) -> None:
    session_id = await publish(world)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    home_id = await home_message(world, session_id)
    set_going(world, HOME, home_id, [HOME_ONLY_USER])
    world.state.overrides[mirror_reactors_path(FAR_B, mirror_id)] = Override(
        403, b'{"code": "MISSING_PERMISSIONS", "message": "Missing Permissions"}')
    await world.interactions.reconcile_sessions()
    assert HOME_ONLY_USER in await going(world, session_id)
    assert await origin(world, session_id, HOME_ONLY_USER) is None


async def test_a_startup_reconcile_with_a_404_mirror_drops_that_mirror(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    world.state.overrides[mirror_reactors_path(FAR_B, mirror_id)] = Override(
        404, b'{"code": "UNKNOWN_MESSAGE", "message": "Unknown Message"}')
    await world.interactions.reconcile_sessions()
    assert await mirror_row(world, world.sub_b, session_id) is None


# The startup reconcile carries a user over when their only RSVP is on a mirror that answers 403 (the bot cannot read
# it).

FORBIDDEN_BODY = b'{"code": "MISSING_PERMISSIONS", "message": "Missing Permissions"}'


async def test_a_user_whose_only_going_is_on_a_403_mirror_is_still_going_after_the_startup_reconcile(
        world: SimpleNamespace) -> None:
    session_id = await publish(world)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    set_going(world, FAR_B, mirror_id, [FAR_USER])
    await world.presses.on_reaction(reaction(FAR_USER, FAR_B, mirror_id))
    assert await origin(world, session_id, FAR_USER) == world.sub_b
    world.state.overrides[mirror_reactors_path(FAR_B, mirror_id)] = Override(403, FORBIDDEN_BODY)
    await world.interactions.reconcile_sessions()
    assert FAR_USER in await going(world, session_id)
    assert await origin(world, session_id, FAR_USER) == world.sub_b


async def test_a_home_only_user_whose_home_reaction_is_gone_is_removed_by_the_same_reconcile(
        world: SimpleNamespace) -> None:
    session_id = await publish(world)
    home_id = await home_message(world, session_id)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    set_going(world, HOME, home_id, [HOME_ONLY_USER])
    await world.presses.on_reaction(reaction(HOME_ONLY_USER, HOME, home_id))
    set_going(world, HOME, home_id, [QUIET_FAR_USER])          # HOME_ONLY_USER un-reacted; another user reacted
    world.state.overrides[mirror_reactors_path(FAR_B, mirror_id)] = Override(403, FORBIDDEN_BODY)
    await world.interactions.reconcile_sessions()
    assert HOME_ONLY_USER not in await going(world, session_id)
    assert QUIET_FAR_USER in await going(world, session_id)


async def test_a_user_who_reacted_on_both_is_unaffected_by_a_403_mirror(world: SimpleNamespace) -> None:
    session_id = await publish(world)
    home_id = await home_message(world, session_id)
    mirror_id = await mirror_message(world, world.sub_b, session_id)
    set_going(world, FAR_B, mirror_id, [FAR_USER])
    await world.presses.on_reaction(reaction(FAR_USER, FAR_B, mirror_id))
    set_going(world, HOME, home_id, [FAR_USER])                # also on the readable home message
    world.state.overrides[mirror_reactors_path(FAR_B, mirror_id)] = Override(403, FORBIDDEN_BODY)
    await world.interactions.reconcile_sessions()
    assert FAR_USER in await going(world, session_id)
    assert await origin(world, session_id, FAR_USER) is None
