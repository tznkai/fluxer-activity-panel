# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One full session scenario against the fake Fluxer REST: propose, RSVP, waitlist, move, recurrence, end and scrub.

Nothing from a session may reach the games channel (AC10.4), and the logs of the session paths carry ids, choices and
reasons only, never a user id, typed text or a location (AC11.3). Every component shares one stub clock and one
CardLocks. The test moves the clock, and the pacer never sleeps."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from fluxer_client import events
from fluxer_client.roles import RoleCache
from fluxer_client.rest import FluxerREST
from fluxer_panel import board, cards, sessions, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.dm_router import DmRouter, NonAdminLimiter
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.logs import FIELDS_ATTR, JsonFormatter
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poller import Poller
from fluxer_panel.presses import Action, Press, Surface
from fluxer_panel.purge import run_purge
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_commands import SessionCommands
from fluxer_panel.session_flow import SessionFlow
from fluxer_panel.session_jobs import SessionJobs
from fluxer_panel.session_post import SessionPost
from fluxer_panel.sources.registry import Sources
from tests.fakes import FakeFluxerState, make_rest_app
from tests.test_board_poller import GAMES, GAMES_MESSAGES, TOKEN, FakeClock, board_posts, card_posts, cells

BOT_ID = "1900"
ADMIN = "1001"
PROPOSER = "6001"
VOTER = "6002"           # the second account: waits, then is promoted
GUILD = "2001"           # the games community
PLAY_NOW = "3200"        # PLAY_NOW_CHANNEL_ID: a second channel in GUILD
INSTANCE = "https://fluxer.invalid"
SENTINEL_LOC = "SENTINEL-LOC-7a1f"
SENTINEL_TYPED = "SENTINEL-TYPED"
PLAY_NOW_MESSAGES = f"/api/v1/channels/{PLAY_NOW}/messages"
NOW = datetime(2026, 11, 11, 8, 0, tzinfo=UTC).timestamp()   # a Wednesday, 08:00 UTC
DAY = 86400
GOING = "✅"
SESSION_EVENTS = ("session_flow", "rsvp_changed", "session_command", "session_recurred")


def keycap(n: int) -> str:
    return f"{n}️⃣"


class Scenario:
    """Every component under test, wired as `__main__.run` wires them, on one clock and one CardLocks."""

    def __init__(self, **parts: Any) -> None:
        self.__dict__.update(parts)


@pytest.fixture
async def scenario(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[Scenario]:
    database = Database.open(tmp_path / "panel.db")
    state = FakeFluxerState(bot_user_id=BOT_ID)
    state.channels[GAMES] = {"id": GAMES, "name": "games", "type": 0, "guild_id": GUILD}
    state.channels[PLAY_NOW] = {"id": PLAY_NOW, "name": "play-now", "type": 0, "guild_id": GUILD}
    try:
        async with aiohttp.ClientSession() as client:
            server = await aiohttp_server(make_rest_app(state))
            rest = FluxerREST(client, f"{str(server.make_url('/')).rstrip('/')}/api", TOKEN)
            clock = FakeClock()
            clock.now = NOW
            config = Config.from_env({
                "FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": INSTANCE,
                "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES, "PLAY_NOW_CHANNEL_ID": PLAY_NOW,
                "POLL_INTERVAL_SECONDS": "3600",
            })
            locks = CardLocks()
            pacer = ChannelPacer(max_calls=1000)
            games_guild = GamesGuild(rest, config)
            backoff = DmBackoff(config.dm_backoff_seconds, clock=clock)
            limiter = NonAdminLimiter(config, clock=clock)
            seeder = Seeder(rest=rest, pacer=pacer, db=database)
            post = SessionPost(db=database, rest=rest, config=config, pacer=pacer, backoff=backoff)
            flow = SessionFlow(db=database, rest=rest, config=config, locks=locks, session_post=post,
                               games_guild=games_guild, pacer=pacer, identity=BotIdentity(user_id=BOT_ID),
                               backoff=backoff, limiter=limiter, clock=clock)
            jobs = SessionJobs(db=database, rest=rest, config=config, locks=locks, session_post=post,
                               session_flow=flow, status_of=lambda _card_id: None, clock=clock, backoff=backoff)
            commands = SessionCommands(db=database, rest=rest, config=config, locks=locks, session_post=post,
                                       clock=clock)
            post.set_on_gone(commands.on_message_gone)
            interactions = Interactions(db=database, rest=rest, config=config, locks=locks, games_guild=games_guild,
                                        seeder=seeder, pacer=pacer, identity=BotIdentity(user_id=BOT_ID),
                                        roles=RoleCache(rest), clock=clock, session_post=post, backoff=backoff,
                                        session_flow=flow)
            poller = Poller(db=database, rest=rest, sources=Sources(managers={}, probes={}), config=config,
                            locks=locks, clock=clock, games_guild=games_guild, seeder=seeder)
            poller.set_session_jobs(jobs)
            poller.set_interactions(interactions)
            router = DmRouter(config=config, rest=rest, card_ctx=None, limiter=limiter, session_flow=flow,
                              session_commands=commands.handle)

            def make_card(conn: Any) -> int:
                card_id = cards.create_card(conn, slug="mc", title="Minecraft", author_id=ADMIN, now=1)
                cards.set_state(conn, card_id, "live", 1)
                cards.set_location(conn, card_id, cards.Location(text=SENTINEL_LOC))
                cards.set_seats(conn, card_id, 1)
                return card_id

            card_id = await database.run(make_card)
            yield Scenario(db=database, state=state, rest=rest, clock=clock, config=config, poller=poller,
                           flow=flow, post=post, jobs=jobs, interactions=interactions, router=router,
                           card_id=card_id, status_path=tmp_path / "purge-status.json")
    finally:
        database.close()


@pytest.fixture
def captured(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


# Helpers ---------------------------------------------------------------------------------------------------

def dm_channel(s: Scenario, user: str = PROPOSER) -> str:
    channel = s.state.dm_channels.get(user)
    assert channel is not None, "the user has no DM channel yet"
    return channel


def last_dm(s: Scenario, needle: str, user: str = PROPOSER) -> str:
    """The id of the newest DM to the user whose text contains `needle`."""
    channel = dm_channel(s, user)
    ids = [mid for (cid, mid), payload in s.state.messages.items()
           if cid == channel and needle in str(payload.get("content", ""))]
    assert ids, f"no DM containing {needle!r}"
    return ids[-1]


def reaction(user: str, channel: str, message: str, name: str) -> events.ReactionAdd:
    return events.ReactionAdd(user_id=user, channel_id=channel, message_id=message, guild_id=None,
                              emoji=events.Emoji(name=name, id=None), member_role_ids=None)


def typed(user: str, channel: str, content: str) -> events.MessageCreate:
    return events.MessageCreate(message_id="7001", channel_id=channel, guild_id=None, author_id=user,
                                author_is_bot=False, member_role_ids=None, content=content)


def card_press(s: Scenario, card_message: str) -> Press:
    surface = Surface(kind="card", channel_id=GAMES, message_id=card_message, guild_id=GUILD, home_community=True)
    return Press(card_id=s.card_id, action=Action.SESSION, user_id=PROPOSER, member_role_ids=None, surface=surface)


def requests(s: Scenario, method: str, path: str) -> list[Any]:
    return [r for r in s.state.requests if r.method == method and r.path == path]


def session_posts(s: Scenario) -> int:
    """Every POST that sends a message into the play-now or the games channel. The games channel is counted too, so a
    session posted there would show up here as well."""
    return len([r for r in s.state.requests
                if r.method == "POST" and r.path in (PLAY_NOW_MESSAGES, GAMES_MESSAGES)])


def records_named(captured: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in captured.records if r.getMessage() == name]


async def session_message_ids(s: Scenario) -> set[str]:
    """Every stored session message id, whatever channel it is in."""
    return await s.db.run(lambda conn: {row[0] for row in conn.execute("SELECT message_id FROM sessions")})


async def stored_in(s: Scenario, channel: str) -> int:
    return await s.db.run(lambda conn: conn.execute("SELECT COUNT(*) FROM sessions WHERE channel_id = ?",
                                                    (channel,)).fetchone()[0])


# The scenario ----------------------------------------------------------------------------------------------

async def test_a_full_session_scenario_never_touches_the_games_channel_and_logs_no_user_or_text(
        scenario: Scenario, captured: pytest.LogCaptureFixture) -> None:
    s = scenario

    # 1. The card and the board are posted to the games channel.
    await s.poller.tick()
    card = await s.db.run(lambda c: cards.get_by_id(c, s.card_id))
    assert card is not None and card.message_id is not None
    card_message = card.message_id

    # 2. Propose: the calendar on the card, keycap 1 on the prompt, then the check on the confirmation.
    await s.flow.start(card_press(s, card_message))
    channel = dm_channel(s)
    await s.flow.on_dm_reaction(reaction(PROPOSER, channel, last_dm(s, "Pick a time"), keycap(1)))
    await s.flow.on_dm_reaction(reaction(PROPOSER, channel, last_dm(s, "to post it"), GOING))
    first = await s.db.run(lambda c: sessions.get(c, 1))   # the first session row, whatever channel it was posted in
    assert first is not None
    first_id = first.id
    first_path = f"/api/v1/channels/{first.channel_id}/messages/{first.message_id}"

    # 3. RSVP: the voter takes the check and waits for the one seat; the proposer says no and the voter is promoted.
    await s.interactions.rsvp_changed(first_id, VOTER, "going", True)
    rsvps = await s.db.run(lambda c: sessions.rsvps_of(c, first_id))
    assert rsvps[VOTER][0] == "waiting"
    await s.interactions.rsvp_changed(first_id, PROPOSER, "no", True)
    rsvps = await s.db.run(lambda c: sessions.rsvps_of(c, first_id))
    assert rsvps[VOTER][0] == "going"

    # 4. A second draft, a refused typed time, then a move through the DM router. Each non-admin DM action counts
    #    against the limit of 5 a minute, so the clock moves 61 s first.
    s.clock.now += 61
    await s.flow.start(card_press(s, card_message))
    channel = dm_channel(s)
    await s.router.route(typed(PROPOSER, channel, f"sat {SENTINEL_TYPED}"))
    await s.router.route(typed(PROPOSER, channel, f"!session move {first_id} tomorrow 19:30"))
    moved = await s.db.run(lambda c: sessions.get(c, first_id))
    assert moved is not None and moved.starts_at > first.starts_at

    # 5. Recurrence on, another 61 s later.
    s.clock.now += 61
    await s.router.route(typed(PROPOSER, channel, f"!session repeat {first_id} weekly"))

    # 6. The moved session ends at its end time: its message is edited, and nothing is posted.
    posts_before_end = session_posts(s)
    s.clock.now = moved.ends_at
    await s.jobs.run()
    ended = await s.db.run(lambda c: sessions.get(c, first_id))
    assert ended is not None and ended.state == "ended"
    assert requests(s, "PATCH", first_path)
    assert session_posts(s) == posts_before_end

    # 7. A day after the start, the weekly series posts its successor in the session's own channel.
    s.clock.now = moved.starts_at + DAY
    await s.jobs.run()
    assert session_posts(s) == posts_before_end + 1
    first_after = await s.db.run(lambda c: sessions.get(c, first_id))
    assert first_after is not None and first_after.next_posted is True
    successor = await s.db.run(lambda c: sessions.upcoming_for_card(c, s.card_id))
    assert len(successor) == 1 and successor[0].recurrence == "weekly"
    # While the successor is upcoming, the card's Next session line shows it: the card is edited in the games channel.
    await s.poller.tick()

    # 8. Scrub: past the first occurrence's closed time plus the retention, the purge scrubs it and deletes its row.
    retention = s.config.session_retention_seconds
    assert ended.closed_at is not None
    s.clock.now = ended.closed_at + retention + 1
    result = await run_purge(s.db, s.status_path, int(s.clock.now), scrub=s.post.scrub, retention=retention)
    assert result is not None and result.sessions == 1
    assert await s.db.run(lambda c: sessions.get(c, first_id)) is None
    scrub_patch = requests(s, "PATCH", first_path)[-1]
    assert SENTINEL_LOC not in str(scrub_patch.payload)

    # 9. The board check runs again.
    await s.poller.tick()

    # AC10.4: the games channel gets the card and the board, and nothing from a session.
    game_posts = [r for r in s.state.requests if r.method == "POST" and r.path == GAMES_MESSAGES]
    assert len(game_posts) == 2
    assert len(card_posts(s.state)) == 1 and len(board_posts(s.state)) == 1
    assert len(game_posts) == len(card_posts(s.state)) + len(board_posts(s.state))

    session_messages = await session_message_ids(s)
    assert session_messages, "the scenario should have stored session messages"
    game_reactions = [r for r in s.state.requests
                      if r.method in ("PUT", "DELETE") and r.path.startswith(f"/api/v1/channels/{GAMES}/messages/")
                      and "/reactions/" in r.path]
    for request in game_reactions:
        message = request.path.split("/messages/", 1)[1].split("/", 1)[0]
        assert message not in session_messages, "a reaction call in the games channel targets a session message"
    assert await stored_in(s, GAMES) == 0

    board_now = await s.db.run(board.get)
    assert board_now.message_id is not None
    newest = max(int(mid) for (cid, mid) in s.state.messages if cid == GAMES)
    assert newest == int(board_now.message_id), "the board is no longer the games channel's newest message"

    card_patches = [r for r in s.state.requests if r.method == "PATCH" and r.path == f"{GAMES_MESSAGES}/{card_message}"]
    assert any(text.FIELD_NEXT_SESSION in [name for name, _ in cells(r.payload)] for r in card_patches), \
        "a card edit in the games channel should carry the Next session line"

    # AC11.3: no user id on a session, RSVP or user DM record (the admin DM failure is the one named exemption).
    checked = 0
    for record in captured.records:
        event = record.getMessage()
        if not event.startswith(("session", "rsvp", "user_dm")) or event == "session_headsup_failed":
            continue
        assert "user_id" not in getattr(record, FIELDS_ATTR, {}), event
        checked += 1
    assert checked > 0

    # No record at all carries the location or the typed text, in its message, its fields or its exception text.
    for record in captured.records:
        rendered = JsonFormatter().format(record)
        assert SENTINEL_LOC not in rendered, record.getMessage()
        assert SENTINEL_TYPED not in rendered, record.getMessage()

    # The session records name the session or the card they are about.
    for name in SESSION_EVENTS:
        found = records_named(captured, name)
        assert found, f"expected at least one {name} record"
        for record in found:
            fields = getattr(record, FIELDS_ATTR, {})
            assert "session_id" in fields or "card_id" in fields, name
