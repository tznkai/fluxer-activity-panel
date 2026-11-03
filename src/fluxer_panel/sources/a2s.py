# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Source A2S_INFO probe built on python-a2s.

python-a2s does not parse the Ship's extra fields, so the game mode is never filled. The player list
(`a2s.aplayers`) is never requested. The import below is the library, not this module: absolute imports
resolve to the top-level package.
"""

from __future__ import annotations

import a2s

from fluxer_panel.sources._probe_util import _count, _detail, _guard
from fluxer_panel.sources.base import PlayerCounts, ProbeDetails, ProbeFailed, ProbeResult


class A2SProbe:
    kind = "a2s"

    async def probe(self, host: str, port: int, timeout: float) -> ProbeResult | ProbeFailed:
        async def _run() -> ProbeResult:
            info = await a2s.ainfo((host, port), timeout=timeout)
            return ProbeResult(
                PlayerCounts(_count(info.player_count), _count(info.max_players)),
                ProbeDetails(
                    map=_detail(info.map_name),
                    version=_detail(getattr(info, "version", None)),  # GoldSrc replies have no version
                    game_mode=None,
                ),
            )

        return await _guard(_run(), timeout, malformed=(a2s.BrokenMessageError,))
