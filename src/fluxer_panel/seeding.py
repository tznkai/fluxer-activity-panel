# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seeding the bot's own reactions on a card's message, a copy, or a play-now message.

The bot adds the emoji each surface should carry and leaves the rest alone. Every reaction call goes through the
channel's pacer. Seeding never takes a card lock, and callers must not hold one either, so a 429 retry never stalls a
publish. A 403 is logged once per channel, and the panel never logs the emoji itself.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_panel import interaction_settings
from fluxer_panel.cards import Card
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.logs import log_event
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import FOLLOW_EMOJI, PLAY_NOW_EMOJI, SESSION_EMOJI, WAKE_EMOJI

log = logging.getLogger(__name__)


def wanted_emoji(card: Card, *, play_now_target: bool, session_target: bool = False) -> list[str]:
    """WAKE_EMOJI always; FOLLOW_EMOJI only when the card has a follow role; PLAY_NOW_EMOJI only when there is a
    play-now target; SESSION_EMOJI last, only when there is a session channel. A copy outside the games community
    carries the same emoji, and a press there gets a DM that points at the card."""
    wanted = [WAKE_EMOJI]
    if card.follow_role is not None:
        wanted.append(FOLLOW_EMOJI)
    if play_now_target:
        wanted.append(PLAY_NOW_EMOJI)
    if session_target:
        wanted.append(SESSION_EMOJI)
    return wanted


def has_session_channel(card: Card, config: Config, *, surface_kind: str, home: bool) -> bool:
    """A card surface can plan sessions when the card has its own verified play-now channel, or PLAY_NOW_CHANNEL_ID is
    set (the session channel is the card's, else the configured one, as for play-now). A home copy the same. A far copy
    never gets the calendar: its press only points at the card. A play-now post never gets it either."""
    if surface_kind == "play_now":
        return False
    if surface_kind == "copy" and not home:
        return False
    own = card.play_now_channel is not None and card.play_now_channel.verified
    return own or config.play_now_channel_id is not None


def has_play_now_target(card: Card, config: Config, *, surface_kind: str, home: bool) -> bool:
    """A card surface has a target when the card has its own verified play-now channel or the configured one is set. A
    home copy always has one, since a press on it posts in the copy's own channel. A copy outside the games community
    has one when the card does: a press there posts nothing and gets a DM pointing at the card's own button. A play-now
    surface never has one: its emoji are seeded separately."""
    if surface_kind == "play_now":
        return False
    if surface_kind == "copy" and home:
        return True
    own = card.play_now_channel is not None and card.play_now_channel.verified
    return own or config.play_now_channel_id is not None


def _bare(emoji: str) -> str:
    return emoji.replace("\ufe0f", "")


def _present(message: Mapping[str, Any], emoji: str) -> bool:
    """Whether the bot's own reaction with this emoji is already on the message. The stored name may or may not carry
    U+FE0F, so both sides are compared without it."""
    for reaction in message.get("reactions") or []:
        name = (reaction.get("emoji") or {}).get("name")
        if reaction.get("me") is True and isinstance(name, str) and _bare(name) == _bare(emoji):
            return True
    return False


class Seeder:
    def __init__(self, *, rest: FluxerREST, pacer: ChannelPacer, db: Database) -> None:
        self._rest = rest
        self._pacer = pacer
        self._db = db
        self._forbidden: set[str] = set()   # channels whose 403 has already been logged

    async def seed(self, channel_id: str, message_id: str, wanted: Sequence[str],
                   message: Mapping[str, Any] | None = None, *, card_id: int) -> None:
        """Adds each wanted emoji the bot has not added yet, in order. `message` is a payload already fetched; without
        one the message is fetched. Seeding is asked again for `card_id` after each emoji's budget wait and before its
        add, so a kill switch turned off meanwhile stops the seed. A PUT already sent when the switch flips can still
        land; a later `!card interactions off` removes it. A 403 stops this message and is logged once per channel. Any
        other failure is logged and stops this message."""
        try:
            if message is None:
                message = await self._rest.get_message(channel_id, message_id)
            for emoji in wanted:
                if _present(message, emoji):
                    continue
                await self._pacer.wait(channel_id)
                if not await self.enabled_for(card_id):
                    return
                await self._rest.add_reaction(channel_id, message_id, emoji)
        except FluxerHTTPError as exc:
            if exc.status == 403:
                self._log_forbidden(channel_id)
            else:
                log_event(log, "seed_failed", level=logging.WARNING, channel_id=channel_id,
                          reason=type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - one message's failed seeding must not stop the others
            log_event(log, "seed_failed", level=logging.WARNING, channel_id=channel_id, reason=type(exc).__name__)

    async def unseed(self, channel_id: str, message_id: str, emojis: Sequence[str]) -> None:
        """Removes all reactions of each emoji (the bot's seeds and any users' left over), through the pacer (the kill
        switch). A 404 means none are left, and the next emoji is still removed. A 403 stops this message and is logged
        once per channel. Any other failure is logged and stops this message."""
        for emoji in emojis:
            await self._pacer.wait(channel_id)
            try:
                await self._rest.remove_emoji_reactions(channel_id, message_id, emoji)
            except FluxerHTTPError as exc:
                if exc.status == 404:
                    continue
                if exc.status == 403:
                    self._log_forbidden(channel_id)
                else:
                    log_event(log, "unseed_failed", level=logging.WARNING, channel_id=channel_id,
                              reason=type(exc).__name__)
                return
            except Exception as exc:  # noqa: BLE001 - one message's failed removal must not stop the others
                log_event(log, "unseed_failed", level=logging.WARNING, channel_id=channel_id,
                          reason=type(exc).__name__)
                return

    async def enabled_for(self, card_id: int) -> bool:
        """Whether seeding applies to this card: the global switch is on and the card is not switched off."""
        return await self._db.run(
            lambda c: interaction_settings.enabled(c) and not interaction_settings.card_off(c, card_id))

    def _log_forbidden(self, channel_id: str) -> None:
        if channel_id in self._forbidden:
            return
        self._forbidden.add(channel_id)
        log_event(log, "seed_forbidden", level=logging.WARNING, channel_id=channel_id)
