# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Routes typed gateway events to the panel's handlers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fluxer_client import events
from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import admin
from fluxer_panel.card_commands import CardContext
from fluxer_panel.config import Config
from fluxer_panel.games import BotIdentity
from fluxer_panel.logs import log_event
from fluxer_panel.presses import Presses

log = logging.getLogger(__name__)


class Bot:
    def __init__(
        self,
        *,
        rest: FluxerREST,
        config: Config,
        roles: RoleCache,
        on_message_delete: Callable[[events.MessageDelete], Awaitable[None]] | None = None,
        card_ctx: CardContext | None = None,
        presses: Presses | None = None,
        identity: BotIdentity | None = None,
        on_games_message: Callable[[str], None] | None = None,
    ) -> None:
        self.rest = rest
        self.config = config
        self.roles = roles
        self.on_message_delete = on_message_delete
        self.card_ctx = card_ctx
        self.presses = presses
        self.identity = identity
        self.on_games_message = on_games_message
        self.user_id: str | None = None

    async def on_dispatch(self, t: str, d: dict[str, Any]) -> None:
        event = events.parse(t, d)
        match event:
            case events.Ready(user_id=user_id):
                self.user_id = user_id
                if self.identity is not None:
                    self.identity.user_id = user_id
                log_event(log, "ready", user_id=user_id)
            case events.Resumed():
                log_event(log, "resumed")
            case events.GuildRolesChanged(guild_id=guild_id):
                self.roles.invalidate(guild_id)
            case events.MessageCreate() as msg:
                if msg.author_id == self.user_id:
                    return  # the bot's own messages never reach a handler, so its replies cannot loop
                if self.on_games_message is not None:
                    self.on_games_message(msg.channel_id)   # arms the board check when this is the board's channel
                # handle ignores what is not a command. A command from a bot or from a guild channel is refused by
                # authorize: it logs admin_denied and posts nothing, so the guild channel gets no reply.
                await admin.handle(msg, self.config, self.rest, self.card_ctx)
            case events.ReactionAdd() | events.ReactionRemove() as r:
                # The user id is never logged here (AC6.3): an absorbed press logs it in the adapter, and nothing else does.
                log_event(log, "reaction", event_type=t, channel_id=r.channel_id,
                          message_id=r.message_id, guild_id=r.guild_id)
                if self.presses is not None:
                    await self.presses.on_reaction(r)
            case events.ReactionRemoveAll() | events.ReactionRemoveEmoji() as r:
                log_event(log, "reaction", event_type=t, channel_id=r.channel_id,
                          message_id=r.message_id, guild_id=r.guild_id)
            case events.MessageDelete() as deleted:
                if self.on_message_delete is not None:
                    await self.on_message_delete(deleted)
            case _:
                log_event(log, "dispatch_ignored", level=logging.DEBUG, event_type=t)
