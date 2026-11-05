# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The games community: which community a games channel belongs to, looked up once per channel and kept.

`BotIdentity` holds the bot's own user id, which Ready sets once and every component that must ignore the bot's own
reactions or messages shares.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerREST
from fluxer_panel.config import Config
from fluxer_panel.logs import log_event

log = logging.getLogger(__name__)


@dataclass
class BotIdentity:
    user_id: str | None = None   # the bot's own user id, set once from READY


class GamesGuild:
    def __init__(self, rest: FluxerREST, config: Config) -> None:
        self._rest = rest
        self._config = config
        self._guilds: dict[str, str] = {}   # each channel's community, once a lookup has answered

    async def get(self, channel_id: str | None = None) -> str | None:
        """The community of `channel_id`, or of the games channel when none is given. None when there is no channel,
        or when the lookup fails or the channel has no community. Only an answer is kept; a failure is retried on the
        next call."""
        if channel_id is None:
            channel_id = self._config.games_channel_id
        if channel_id is None:
            return None
        known = self._guilds.get(channel_id)
        if known is not None:
            return known
        try:
            channel = await self._rest.get_channel(channel_id)
        except Exception as exc:  # noqa: BLE001 - without the community the links are plain text, and the lookup retries
            log_event(log, "games_channel_lookup_failed", level=logging.WARNING, reason=type(exc).__name__)
            return None
        guild_id = channel.get("guild_id")
        if not is_snowflake(guild_id):
            log_event(log, "games_channel_lookup_failed", level=logging.WARNING, reason="no_community")
            return None
        self._guilds[channel_id] = guild_id
        return guild_id
