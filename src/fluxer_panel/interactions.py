# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The press handler used in production. `Presses` has already filtered, rate limited and logged each press, and
`Interactions` does what the press asks.

The action methods are stubs in this phase: each logs that the press is unhandled. Later phases fill them in.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.logs import log_event
from fluxer_panel.presses import Action, Press, Surface
from fluxer_panel.seeding import Seeder

log = logging.getLogger(__name__)


class Interactions:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, locks: CardLocks, games_guild: GamesGuild,
                 seeder: Seeder, identity: BotIdentity, roles: RoleCache, clock: Callable[[], float] = time.time,
                 kick: Callable[[], None] = lambda: None) -> None:
        self._db = db
        self._rest = rest
        self._config = config
        self._locks = locks
        self._games_guild = games_guild
        self._seeder = seeder
        self._identity = identity
        self._roles = roles
        self._clock = clock
        self._kick = kick

    async def handle(self, press: Press) -> None:
        match press.action:
            case Action.WAKE:
                await self.wake(press)
            case Action.FOLLOW:
                await self.follow(press)
            case Action.PLAY_NOW:
                await self.play_now(press)

    async def wake(self, press: Press) -> None:
        log_event(log, "press_unhandled", card_id=press.card_id, action=press.action.value)

    async def follow(self, press: Press) -> None:
        log_event(log, "press_unhandled", card_id=press.card_id, action=press.action.value)

    async def play_now(self, press: Press) -> None:
        log_event(log, "press_unhandled", card_id=press.card_id, action=press.action.value)

    async def me_too_changed(self, card_id: int, surface: Surface, user_id: str) -> None:
        log_event(log, "press_unhandled", card_id=card_id, action=Action.PLAY_NOW.value, surface=surface.kind)
