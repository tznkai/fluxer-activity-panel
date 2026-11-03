# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Minecraft Bedrock and Java status probes built on mcstatus.

Both probes call `async_status` with `tries=1`, so a silent server costs one request. The library's name
lookup is never used: the host is passed as given.
"""

from __future__ import annotations

from mcstatus import BedrockServer, JavaServer

from fluxer_panel.sources._probe_util import _count, _detail, _guard
from fluxer_panel.sources.base import PlayerCounts, ProbeDetails, ProbeFailed, ProbeResult


class BedrockProbe:
    kind = "minecraft_bedrock"

    async def probe(self, host: str, port: int, timeout: float) -> ProbeResult | ProbeFailed:
        async def _run() -> ProbeResult:
            status = await BedrockServer(host, port, timeout=timeout).async_status(tries=1)
            return ProbeResult(
                PlayerCounts(_count(status.players.online), _count(status.players.max)),
                ProbeDetails(
                    map=_detail(status.map_name),
                    version=_detail(status.version.name),
                    game_mode=_detail(status.gamemode),
                ),
            )

        return await _guard(_run(), timeout)


class JavaProbe:
    kind = "minecraft_java"

    async def probe(self, host: str, port: int, timeout: float) -> ProbeResult | ProbeFailed:
        # mcstatus has already built the player sample into objects; it is discarded here, immediately after
        # parse, and never read.
        async def _run() -> ProbeResult:
            status = await JavaServer(host, port, timeout=timeout).async_status(tries=1)
            return ProbeResult(
                PlayerCounts(_count(status.players.online), _count(status.players.max)),
                ProbeDetails(version=_detail(status.version.name)),
            )

        return await _guard(_run(), timeout)
