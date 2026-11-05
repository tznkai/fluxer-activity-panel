# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The press handler used in production. `Presses` has already filtered, rate limited and logged each press, and
`Interactions` does what the press asks.

The play-now action is still a stub in this phase: it logs that the press is unhandled. Later phases fill it in.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from fluxer_client.rest import FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import cards, follow_roles, notify, render, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import Card
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.follow_roles import Toggle
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.logs import log_event
from fluxer_panel.presses import Action, Press, Surface
from fluxer_panel.seeding import Seeder
from fluxer_panel.wake import WakeRequests

log = logging.getLogger(__name__)


class Interactions:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, locks: CardLocks, games_guild: GamesGuild,
                 seeder: Seeder, identity: BotIdentity, roles: RoleCache, clock: Callable[[], float] = time.time,
                 kick: Callable[[], None] = lambda: None, wake: WakeRequests | None = None) -> None:
        self._db = db
        self._wake = wake
        self._rest = rest
        self._config = config
        self._locks = locks
        self._games_guild = games_guild
        self._seeder = seeder
        self._identity = identity
        self._roles = roles
        self._clock = clock
        self._kick = kick
        self._follow_deps = follow_roles.FollowDeps(db=db, rest=rest, config=config, roles=roles, identity=identity)
        self._follow_alerted: set[tuple[int, str]] = set()   # (card id, reason) whose admin alert was sent

    async def handle(self, press: Press) -> None:
        match press.action:
            case Action.WAKE:
                await self.wake(press)
            case Action.FOLLOW:
                await self.follow(press)
            case Action.PLAY_NOW:
                await self.play_now(press)

    async def wake(self, press: Press) -> None:
        """A wake press on any surface (card, copy or play-now post) wakes the card (AC4.8)."""
        if self._wake is None:
            return
        await self._wake.press(press.card_id, press.user_id)

    async def follow(self, press: Press) -> None:
        card = await self._db.run(lambda c: cards.get_by_id(c, press.card_id))
        if card is None or card.state != "live":
            return
        guild = await self._games_guild.get()
        if not press.surface.home_community:
            await self._follow_elsewhere(press.user_id, card, guild)
            return
        if guild is None:   # no games community to toggle in: nothing is changed
            await self._dm_presser(press.user_id, text.FOLLOW_UNAVAILABLE, card)
            return
        async with self._locks.get(card.id):
            result = await follow_roles.toggle_follow(self._follow_deps, guild, press.user_id, card,
                                                      member_role_ids=press.member_role_ids)
        if result.outcome is Toggle.ADDED:
            await self._dm_presser(press.user_id, text.FOLLOW_ADDED, card)
        elif result.outcome is Toggle.REMOVED:
            await self._dm_presser(press.user_id, text.FOLLOW_REMOVED, card)
        else:
            await self._dm_presser(press.user_id, text.FOLLOW_UNAVAILABLE, card)
            # A card with no follow role was cleared on purpose: the press is unavailable, and nothing is wrong to report.
            cleared = result.reason == "not_allowlisted" and card.follow_role is None
            if result.reason is not None and result.reason != "no_bot_id" and not cleared:
                await self._alert_admins_once(card, result.reason)

    async def _follow_elsewhere(self, user_id: str, card: Card, guild: str | None) -> None:
        """A copy outside the games community: no role call, only a DM that points at the card in the games channel."""
        title = render.escape(card.title)
        if self._config.instance_url and guild and card.channel_id and card.message_id:
            link = render.jump_link(text.JUMP_LABEL_CARD, self._config.instance_url, guild, card.channel_id,
                                    card.message_id)
            content = text.FOLLOW_ELSEWHERE.format(card=title, link=link)
        else:
            content = text.FOLLOW_ELSEWHERE_NO_LINK.format(card=title)
        await notify.dm_user(self._rest, user_id, content)

    async def _dm_presser(self, user_id: str, template: str, card: Card) -> None:
        await notify.dm_user(self._rest, user_id, template.format(card=render.escape(card.title)))

    async def _alert_admins_once(self, card: Card, reason: str) -> None:
        """One admin DM per card per reason. The mark is set before the send, so a failed DM is not retried by a press."""
        key = (card.id, reason)
        if key in self._follow_alerted:
            return
        self._follow_alerted.add(key)
        content = text.FOLLOW_ADMIN_ALERT.format(slug=card.slug, reason=text.FOLLOW_REASON[reason])
        await notify.dm_admins(self._rest, self._config, content, failed_event="admin_alert_failed")

    async def play_now(self, press: Press) -> None:
        log_event(log, "press_unhandled", card_id=press.card_id, action=press.action.value)

    async def me_too_changed(self, card_id: int, surface: Surface, user_id: str) -> None:
        log_event(log, "press_unhandled", card_id=card_id, action=Action.PLAY_NOW.value, surface=surface.kind)
