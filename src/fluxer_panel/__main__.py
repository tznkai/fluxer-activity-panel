# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entry point: python -m fluxer_panel [run|purge]

Exit codes: 0 clean stop, 1 fatal runtime or gateway error, 2 config or usage error,
3 database refused (quarantined, schema newer than the code, or unavailable; see README).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
import time

import aiohttp

from fluxer_client.gateway import FatalGatewayError, Gateway
from fluxer_client.rest import FluxerREST, resolve_endpoints
from fluxer_client.roles import RoleCache
from fluxer_panel.bot import Bot
from fluxer_panel.card_commands import CardContext
from fluxer_panel.config import Config, ConfigError
from fluxer_panel.db import Database, DatabaseRefused
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import Interactions
from fluxer_panel.logs import log_event, setup_logging
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.poller import Poller
from fluxer_panel.presses import Presses
from fluxer_panel.purge import purge_loop, run_purge
from fluxer_panel.seeding import Seeder
from fluxer_panel.sources.registry import build_sources
from fluxer_panel.wake import WakeRequests

log = logging.getLogger("fluxer_panel")

# REST calls and discovery. The gateway websocket has its own timeouts, which this
# session-wide total does not limit: aiohttp applies it to plain requests only.
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
IDENTIFY_PROPERTIES = {"os": "linux", "browser": "fluxer-activity-panel", "device": "fluxer-activity-panel"}
USAGE = "usage: python -m fluxer_panel [run|purge]"


async def run(config: Config, db: Database) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    stop_task = asyncio.create_task(stop.wait())
    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            # Discovery can take up to HTTP_TIMEOUT per request: a stop signal must not wait for it.
            resolve_task = asyncio.create_task(resolve_endpoints(
                session, token=config.token, instance_url=config.instance_url,
                api_url=config.api_url, gateway_url=config.gateway_url))
            try:
                await asyncio.wait({resolve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                if not resolve_task.done():
                    resolve_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await resolve_task
            if resolve_task.cancelled():          # a stop signal arrived first
                log_event(log, "shutting_down")
                return
            api, gateway_url = resolve_task.result()      # re-raises a discovery error
            log_event(log, "endpoints_resolved")             # URLs are instance details: not logged
            rest = FluxerREST(session, api, config.token)
            sources = build_sources(session, config)
            # One identity, one role cache, one games-community lookup and one reaction pacer, shared by every part that
            # needs them. The seeder and the presses use the same pacer, so each channel's reaction calls share a budget.
            identity = BotIdentity()
            roles = RoleCache(rest)
            games_guild = GamesGuild(rest, config)
            pacer = ChannelPacer()
            seeder = Seeder(rest=rest, pacer=pacer, db=db)
            card_ctx = CardContext(db=db, rest=rest, config=config, sources=sources, roles=roles,
                                   games_guild=games_guild, seeder=seeder, identity=identity)
            # One lock registry for the commands and the poller, so an archive never interleaves with a post.
            poller = Poller(db=db, rest=rest, sources=sources, config=config, locks=card_ctx.locks,
                            games_guild=games_guild)
            card_ctx.kick = poller.kick
            # The wake requests read the poller's statuses, so they are built after the poller and handed to it here.
            wake = WakeRequests(db=db, rest=rest, config=config, sources=sources, locks=card_ctx.locks,
                                status_of=poller.status_of, kick=poller.kick)
            poller.set_wake(wake)
            card_ctx.wake = wake
            interactions = Interactions(db=db, rest=rest, config=config, locks=card_ctx.locks,
                                        games_guild=games_guild, seeder=seeder, pacer=pacer, identity=identity,
                                        roles=roles, kick=poller.kick, wake=wake, status_of=poller.status_of)
            poller.set_interactions(interactions)   # each tick ends the play-now messages that are due
            presses = Presses(db=db, rest=rest, config=config, handler=interactions, games_guild=games_guild,
                              pacer=pacer, identity=identity)
            bot = Bot(rest=rest, config=config, roles=roles, card_ctx=card_ctx,
                      on_message_delete=poller.on_message_delete, presses=presses, identity=identity,
                      on_games_message=poller.on_games_message)
            gateway = Gateway(session, gateway_url, config.token, bot.on_dispatch, properties=IDENTIFY_PROPERTIES)
            gw_task = asyncio.create_task(gateway.run())
            # Started only now: a stop that won discovery above never starts a purge or the poller. The purge loop
            # purges at once, then every interval, and never ends by itself.
            purge_task = asyncio.create_task(purge_loop(db, config.purge_status_path, config.purge_interval_seconds))
            poller_task = asyncio.create_task(poller.run())
            try:
                done, _ = await asyncio.wait({gw_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                if gw_task in done:
                    gw_task.result()      # re-raises FatalGatewayError
                    return
                log_event(log, "shutting_down")
                await gateway.stop()
            finally:
                # every exit (fatal gateway, stop, a failing stop, external cancel). The gateway task goes first, so
                # the session never closes under a live reconnect. On a fatal exit it already finished, so cancel()
                # is a no-op and awaiting it re-raises the same FatalGatewayError. The poller goes next, so no card
                # write is in flight when the session closes. A transaction already on the DB thread runs to commit
                # or rollback; main's db.close() waits for it.
                try:
                    gw_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await gw_task
                finally:
                    try:
                        poller_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await poller_task
                    finally:
                        purge_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await purge_task
    finally:
        stop_task.cancel()


def purge_once(config: Config, db: Database) -> int:
    """`purge` subcommand: one purge and one status write, never the network. 0 on success, 1 if the purge failed."""
    result = asyncio.run(run_purge(db, config.purge_status_path, int(time.time())))
    return 0 if result is not None else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "run"
    if command not in ("run", "purge") or len(args) > 1:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(config.log_level, [s for s in (config.token, config.pterodactyl_api_key) if s])
    try:
        db = Database.open(config.db_path)
    except DatabaseRefused:
        return 3        # db.py already logged db_quarantined / db_quarantine_pending / db_schema_newer / db_unavailable
    except Exception:  # noqa: BLE001 - e.g. a failing migration or mkdir: redacted JSON log, never a raw traceback
        log.exception("fatal_error")
        return 1
    try:
        if command == "purge":
            return purge_once(config, db)
        asyncio.run(run(config, db))
    except FatalGatewayError:
        log_event(log, "gateway_fatal", level=logging.ERROR)
        return 1
    except Exception:  # noqa: BLE001 - last resort: log (redacted) and exit 1, never a raw traceback on stderr
        log.exception("fatal_error")
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
