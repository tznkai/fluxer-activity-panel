# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One full poll scenario against the fake Fluxer REST: start, vote, sort, close, schedule and runoff.

Nothing from a poll may reach the games channel (AC9.4), and the logs of the poll paths carry ids, counts and reasons
only, never a user id, typed text or a member name (AC9.3). Every component shares one stub clock and one CardLocks,
wired as `__main__.run` wires them. The test moves the clock, and the pacer never sleeps."""

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
from fluxer_panel import cards, polls, sessions
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.dm_router import DmRouter, NonAdminLimiter
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.logs import FIELDS_ATTR, JsonFormatter
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poll_close import PollCloser
from fluxer_panel.poll_commands import PollCommands
from fluxer_panel.poll_flow import PollFlow
from fluxer_panel.poll_jobs import PollJobs
from fluxer_panel.poll_post import PollPost
from fluxer_panel.poller import Poller
from fluxer_panel.presses import POLL_EMOJI, RUNOFF_EMOJI, SESSION_EMOJI, Presses
from fluxer_panel.seeding import Seeder
from fluxer_panel.session_flow import SessionFlow
from fluxer_panel.session_post import SessionPost
from fluxer_panel.sources.registry import Sources
from tests.fakes import FakeFluxerState, make_rest_app
from tests.test_board_poller import GAMES, GAMES_MESSAGES, TOKEN, FakeClock, board_posts, card_posts

BOT_ID = "1900"
ADMIN = "1001"
STARTER = "6001"                        # the presser and the starter; holds no follow role
HOLDERS = ("6002", "6003", "6004")      # the follow role's holders: the recipients with the starter
OUTSIDER = "6005"                       # a member without the role: never a recipient
GUILD = "2001"                          # the games community
PLAY = "3200"                           # the card's session channel (PLAY_NOW_CHANNEL_ID), in GUILD
ROLE = "7001"                           # the card's follow role
INSTANCE = "https://fluxer.invalid"
SENTINEL_TYPED = "SENTINEL-TYPED"
MEMBER_NAME = "SENTINEL MEMBER 4c8d"    # a member name: it appears in no record
PLAY_MESSAGES = f"/api/v1/channels/{PLAY}/messages"
NOW = datetime(2026, 11, 11, 8, 0, tzinfo=UTC).timestamp()        # a Wednesday, 08:00 UTC
DAY = 86400
HOUR = 3600
# The poll closes a day after it opens, and `!poll extend 1 12h` adds twelve hours: Thursday 20:00.
THU_CLOSE = NOW + DAY + 12 * HOUR
# The typed times the voters name: Friday and Saturday at 20:00, in UTC.
FRI_20 = datetime(2026, 11, 13, 20, 0, tzinfo=UTC).timestamp()
SAT_20 = datetime(2026, 11, 14, 20, 0, tzinfo=UTC).timestamp()
# The runoff closes at the earliest candidate (Friday 20:00) less an hour and the ten-minute margin: Friday 18:50.
FRI_1850 = FRI_20 - HOUR - 600


class Scenario:
    """Every component under test, wired as `__main__.run` wires them, on one clock and one CardLocks."""

    def __init__(self, **parts: Any) -> None:
        self.__dict__.update(parts)


@pytest.fixture
async def scenario(tmp_path: Path, aiohttp_server: Any) -> AsyncIterator[Scenario]:
    database = Database.open(tmp_path / "panel.db")
    state = FakeFluxerState(bot_user_id=BOT_ID)
    state.guilds[GUILD] = {"id": GUILD}
    state.channels[GAMES] = {"id": GAMES, "name": "games", "type": 0, "guild_id": GUILD}
    state.channels[PLAY] = {"id": PLAY, "name": "play-now", "type": 0, "guild_id": GUILD}
    for uid in (STARTER, OUTSIDER):
        state.members[(GUILD, uid)] = {"user": {"username": MEMBER_NAME, "bot": False}, "roles": []}
    for uid in HOLDERS:
        state.members[(GUILD, uid)] = {"user": {"username": MEMBER_NAME, "bot": False}, "roles": [ROLE]}
    try:
        async with aiohttp.ClientSession() as client:
            server = await aiohttp_server(make_rest_app(state))
            rest = FluxerREST(client, f"{str(server.make_url('/')).rstrip('/')}/api", TOKEN)
            clock = FakeClock()
            clock.now = NOW
            config = Config.from_env({
                "FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC", "FLUXER_INSTANCE_URL": INSTANCE,
                "ADMIN_USER_IDS": ADMIN, "GAMES_CHANNEL_ID": GAMES, "PLAY_NOW_CHANNEL_ID": PLAY,
            })
            locks = CardLocks()
            pacer = ChannelPacer(max_calls=1000)
            identity = BotIdentity(user_id=BOT_ID)
            games_guild = GamesGuild(rest, config)
            backoff = DmBackoff(config.dm_backoff_seconds, clock=clock)
            limiter = NonAdminLimiter(config, clock=clock)
            seeder = Seeder(rest=rest, pacer=pacer, db=database)
            session_post = SessionPost(db=database, rest=rest, config=config, pacer=pacer, backoff=backoff)
            session_flow = SessionFlow(db=database, rest=rest, config=config, locks=locks,
                                       session_post=session_post, games_guild=games_guild, pacer=pacer,
                                       identity=identity, backoff=backoff, limiter=limiter, clock=clock)
            # The poll wiring, as `__main__.run` builds it: posts, flow, closer, commands, jobs, then the interactions.
            poll_post = PollPost(db=database, rest=rest, config=config, pacer=pacer, identity=identity,
                                 backoff=backoff, edit_delay=0)
            poll_flow = PollFlow(db=database, rest=rest, config=config, locks=locks, poll_post=poll_post,
                                 backoff=backoff, games_guild=games_guild, clock=clock, session_flow=session_flow)
            poll_closer = PollCloser(db=database, locks=locks, poll_post=poll_post, clock=clock)
            poll_post.set_on_gone(poll_closer.on_message_gone)
            poll_commands = PollCommands(db=database, rest=rest, config=config, locks=locks, poll_post=poll_post,
                                         poll_flow=poll_flow, closer=poll_closer, clock=clock)
            poll_jobs = PollJobs(db=database, rest=rest, config=config, closer=poll_closer, backoff=backoff,
                                 clock=clock)
            interactions = Interactions(db=database, rest=rest, config=config, locks=locks, games_guild=games_guild,
                                        seeder=seeder, pacer=pacer, identity=identity, roles=RoleCache(rest),
                                        clock=clock, session_post=session_post, backoff=backoff,
                                        session_flow=session_flow, poll_flow=poll_flow)
            poller = Poller(db=database, rest=rest, sources=Sources(managers={}, probes={}), config=config,
                            locks=locks, clock=clock, games_guild=games_guild, seeder=seeder)
            poller.set_poll_jobs(poll_jobs)
            poller.set_interactions(interactions)
            presses = Presses(db=database, rest=rest, config=config, handler=interactions, games_guild=games_guild,
                              pacer=pacer, identity=identity, clock=clock, backoff=backoff)
            router = DmRouter(config=config, rest=rest, card_ctx=None, limiter=limiter, session_flow=session_flow,
                              poll_flow=poll_flow, poll_commands=poll_commands.handle)

            def make_card(conn: Any) -> int:
                card_id = cards.create_card(conn, slug="mc", title="Minecraft", author_id=ADMIN, now=1)
                cards.set_state(conn, card_id, "live", 1)
                cards.set_seats(conn, card_id, 1)
                cards.set_follow_role(conn, card_id, GUILD, ROLE)
                return card_id

            card_id = await database.run(make_card)
            yield Scenario(db=database, state=state, rest=rest, clock=clock, config=config, poller=poller,
                           presses=presses, router=router, poll_post=poll_post, card_id=card_id)
    finally:
        database.close()


@pytest.fixture
def captured(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


# Helpers ---------------------------------------------------------------------------------------------------

def dm_channel(s: Scenario, user: str) -> str:
    channel = s.state.dm_channels.get(user)
    assert channel is not None, "the user has no DM channel yet"
    return channel


def dm_count(s: Scenario, user: str) -> int:
    """How many messages the bot has put in the user's DM channel so far."""
    channel = s.state.dm_channels.get(user)
    return 0 if channel is None else len([key for key in s.state.messages if key[0] == channel])


def reaction(user: str, channel: str, message: str, name: str, *, guild: str | None) -> events.ReactionAdd:
    return events.ReactionAdd(user_id=user, channel_id=channel, message_id=message, guild_id=guild,
                              emoji=events.Emoji(name=name, id=None), member_role_ids=None)


async def say(s: Scenario, user: str, content: str) -> None:
    """A DM from the user to the bot, through the router as the gateway delivers it."""
    await s.router.route(events.MessageCreate(message_id="7001", channel_id=dm_channel(s, user), guild_id=None,
                                              author_id=user, author_is_bot=False, member_role_ids=None,
                                              content=content))


def channel_of(path: str) -> str:
    """The channel id in a `/api/v1/channels/{id}/...` path."""
    return path.split("/")[4]


def records_named(captured: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in captured.records if r.getMessage() == name]


def poll_message_ids(conn: Any) -> set[str]:
    return {row[0] for row in conn.execute("SELECT message_id FROM polls")}


# The scenario ----------------------------------------------------------------------------------------------

async def test_a_full_poll_scenario_never_touches_the_games_channel_and_logs_no_user_or_text(
        scenario: Scenario, captured: pytest.LogCaptureFixture) -> None:
    s = scenario

    # 1. The card and the board are posted to the games channel.
    await s.poller.tick()
    card = await s.db.run(lambda c: cards.get_by_id(c, s.card_id))
    assert card is not None and card.message_id is not None
    card_message = card.message_id

    # 2. A member presses 🗳️ on the card in the games channel: a poll message in the session channel, ballots DMed to
    #    the starter and the three holders, and nobody else.
    await s.presses.on_reaction(reaction(STARTER, GAMES, card_message, POLL_EMOJI, guild=GUILD))
    await s.poll_post.drain()
    poll = await s.db.run(lambda c: polls.open_for_card(c, s.card_id))
    assert poll is not None, "the 🗳️ press should open a poll"
    assert poll.id == 1 and poll.channel_id == PLAY and poll.closes_at == NOW + DAY
    poll_id, poll_message = poll.id, poll.message_id
    assert set(s.state.dm_channels) == {STARTER, *HOLDERS}, "ballots go to the starter and the holders only"
    for uid in (STARTER, *HOLDERS):
        assert await s.db.run(lambda c, u=uid: polls.ballot(c, poll_id, u)) is not None

    # 3. Votes from the recipients. A typed time adds an option; each reply replaces the last; a refused reply echoes
    #    the sentinel and stores nothing.
    await say(s, HOLDERS[0], "fri 20:00")                # Friday 20:00: a new option, and this holder's pick
    await say(s, HOLDERS[1], "a, c")                     # letters, while they are still on the offered list
    await say(s, HOLDERS[1], "sat 20:00")                # replaces the letters with Saturday 20:00
    await say(s, HOLDERS[2], "all")
    await say(s, HOLDERS[2], "clear")                    # replaces `all` with no picks
    await say(s, STARTER, f"sat {SENTINEL_TYPED}")       # refused: not a time
    typed_starts = await s.db.run(lambda c: {o.starts_at for o in polls.options(c, poll_id) if o.origin == "typed"})
    assert typed_starts == {FRI_20, SAT_20}

    # 4. The starter sorts, shows three per ballot and extends by twelve hours. Each is a `!poll` command in DM.
    await say(s, STARTER, "!poll sort 1")
    await say(s, STARTER, "!poll shown 1 3")
    await say(s, STARTER, "!poll extend 1 12h")
    opened = await s.db.run(lambda c: polls.get(c, poll_id))
    assert opened is not None and opened.closes_at == THU_CLOSE

    # 5. The clock reaches the close time and a tick closes the poll. Friday 20:00 and Saturday 20:00 each have one
    #    pick, and both are still usable, so the soonest, Friday, wins the tie. The poll's message gets 📅 and 🔁.
    s.clock.now = THU_CLOSE
    await s.poller.tick()
    await s.poll_post.drain()
    closed = await s.db.run(lambda c: polls.get(c, poll_id))
    assert closed is not None and closed.state == "closed" and closed.outcome == "winner"
    winner = await s.db.run(lambda c: polls.options(c, poll_id))
    assert [o.starts_at for o in winner if o.id == closed.winner_option_id] == [FRI_20]
    seeded = s.state.reactions.get((PLAY, poll_message), {})
    assert SESSION_EMOJI in seeded and RUNOFF_EMOJI in seeded, "a closed poll's message gets 📅 and 🔁"

    # 6. The starter presses 🔁: a runoff of the top candidates opens in the session channel. Its recipients rank.
    #    Friday 20:00 is the earliest candidate, so the runoff closes at Friday 18:50.
    await s.presses.on_reaction(reaction(STARTER, PLAY, poll_message, RUNOFF_EMOJI, guild=GUILD))
    await s.poll_post.drain()
    runoff = await s.db.run(lambda c: polls.child_of(c, poll_id))
    assert runoff is not None and runoff.is_runoff and runoff.state == "open"
    assert runoff.closes_at == FRI_1850
    await say(s, HOLDERS[0], "b, a")                     # Saturday first, then Friday
    await say(s, HOLDERS[1], "a")
    await say(s, HOLDERS[2], "a")
    s.clock.now = FRI_1850
    await s.poller.tick()
    await s.poll_post.drain()
    runoff_closed = await s.db.run(lambda c: polls.get(c, runoff.id))
    assert runoff_closed is not None and runoff_closed.state == "closed" and runoff_closed.outcome == "winner"

    # 7. The starter presses 📅 on the closed runoff: a session in the session channel. The starter and each voter for
    #    Friday get one DM link each.
    before = {uid: dm_count(s, uid) for uid in (STARTER, *HOLDERS)}
    await s.presses.on_reaction(reaction(STARTER, PLAY, runoff.message_id, SESSION_EMOJI, guild=GUILD))
    await s.poll_post.drain()
    scheduled = await s.db.run(lambda c: polls.get(c, runoff.id))
    assert scheduled is not None and scheduled.session_id is not None
    session = await s.db.run(lambda c: sessions.get(c, scheduled.session_id))
    assert session is not None and session.channel_id == PLAY
    for uid in (STARTER, *HOLDERS):
        assert dm_count(s, uid) == before[uid] + 1, "each of them gets one DM link"

    # 8. The board check runs again.
    await s.poller.tick()

    # AC9.4: the games channel gets the card and the board, and nothing from a poll.
    game_posts = [r for r in s.state.requests if r.method == "POST" and r.path == GAMES_MESSAGES]
    assert len(game_posts) == len(card_posts(s.state)) + len(board_posts(s.state))
    assert len(card_posts(s.state)) == 1 and len(board_posts(s.state)) == 1

    poll_messages = await s.db.run(lambda c: poll_message_ids(c))
    assert {poll_message, runoff.message_id} <= poll_messages
    games_edits = [r for r in s.state.requests
                   if r.method == "PATCH" and r.path.startswith(f"/api/v1/channels/{GAMES}/messages/")]
    for request in games_edits:
        assert request.path.rsplit("/", 1)[1] not in poll_messages, "an edit in the games channel targets a poll message"
    games_reactions = [r for r in s.state.requests
                       if r.method in ("PUT", "DELETE") and r.path.startswith(f"/api/v1/channels/{GAMES}/messages/")
                       and "/reactions/" in r.path]
    for request in games_reactions:
        assert request.path.split("/messages/", 1)[1].split("/", 1)[0] not in poll_messages, \
            "a reaction call in the games channel targets a poll message"
    games_rows = await s.db.run(lambda c: c.execute("SELECT COUNT(*) FROM polls WHERE channel_id = ?",
                                                    (GAMES,)).fetchone()[0])
    assert games_rows == 0, "no poll row may name the games channel"

    # Every poll send, edit and ballot DM carries no mentions.
    dm_channels = set(s.state.dm_channels.values())
    poll_writes = [r for r in s.state.requests
                   if r.method in ("POST", "PATCH") and channel_of(r.path) in (PLAY, *dm_channels)]
    assert poll_writes
    for request in poll_writes:
        assert request.payload.get("allowed_mentions") == {"parse": []}, request.path

    # AC9.3: no poll record carries a user id.
    checked = 0
    for record in captured.records:
        if not record.getMessage().startswith("poll"):
            continue
        assert "user_id" not in getattr(record, FIELDS_ATTR, {}), record.getMessage()
        checked += 1
    assert checked > 0

    # No record at all carries the typed text or a member name, in its message or its fields.
    for record in captured.records:
        rendered = JsonFormatter().format(record)
        assert SENTINEL_TYPED not in rendered, record.getMessage()
        assert MEMBER_NAME not in rendered, record.getMessage()

    # The poll records name the poll they are about.
    for name in ("poll_started", "poll_vote", "poll_command", "poll_closed"):
        found = records_named(captured, name)
        assert found, f"expected at least one {name} record"
        assert any("poll_id" in getattr(record, FIELDS_ATTR, {}) for record in found), name
