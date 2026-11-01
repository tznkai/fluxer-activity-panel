# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Entry point: python -m fluxer_panel [run|purge]

Exit codes: 0 clean stop, 1 fatal runtime or gateway error, 2 config or usage error.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

import aiohttp

from fluxer_client.gateway import FatalGatewayError, Gateway
from fluxer_client.rest import FluxerREST, resolve_endpoints
from fluxer_client.roles import RoleCache
from fluxer_panel.bot import Bot
from fluxer_panel.config import Config, ConfigError
from fluxer_panel.logs import log_event, setup_logging

log = logging.getLogger("fluxer_panel")

# REST calls and discovery. The gateway websocket has its own timeouts, which this
# session-wide total does not limit: aiohttp applies it to plain requests only.
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)
IDENTIFY_PROPERTIES = {"os": "linux", "browser": "fluxer-activity-panel", "device": "fluxer-activity-panel"}
USAGE = "usage: python -m fluxer_panel [run|purge]"


async def run(config: Config) -> None:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        api, gateway_url = await resolve_endpoints(
            session, token=config.token, instance_url=config.instance_url,
            api_url=config.api_url, gateway_url=config.gateway_url)
        log_event(log, "endpoints_resolved")             # URLs are instance details: not logged
        rest = FluxerREST(session, api, config.token)
        bot = Bot(rest=rest, config=config, roles=RoleCache(rest))
        gateway = Gateway(session, gateway_url, config.token, bot.on_dispatch, properties=IDENTIFY_PROPERTIES)
        gw_task = asyncio.create_task(gateway.run())
        stop_task = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({gw_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if gw_task in done:
            stop_task.cancel()
            gw_task.result()      # re-raises FatalGatewayError
            return
        log_event(log, "shutting_down")
        await gateway.stop()
        gw_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await gw_task


def purge_once(config: Config) -> int:
    """Stub until Phase 6."""
    log_event(log, "purge_not_implemented", level=logging.WARNING)
    return 0


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
    setup_logging(config.log_level, config.token)
    try:
        if command == "purge":
            return purge_once(config)
        asyncio.run(run(config))
    except FatalGatewayError:
        log_event(log, "gateway_fatal", level=logging.ERROR)
        return 1
    except Exception:  # noqa: BLE001 - last resort: log (redacted) and exit 1, never a raw traceback on stderr
        log.exception("fatal_error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
