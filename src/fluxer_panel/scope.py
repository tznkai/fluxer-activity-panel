# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scopes: the unit that owns play-now calls, sessions and polls. Home (sub None) or one local/both subscription.

`resolve_scope` turns a press's surface into a scope, read again inside the caller's transaction, so a mode changed
since the press was filtered wins. A surface outside the games community with no scope of its own (a redirect copy,
or a far card or play-now post) resolves to `ELSEWHERE`, which keeps the redirect behaviour (a DM that points at the
card).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, subscriptions
from fluxer_panel.cards import Card, FollowRole
from fluxer_panel.config import Config
from fluxer_panel.logs import log_event
from fluxer_panel.presses import Surface
from fluxer_panel.subscriptions import Subscription

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Scope:
    card_id: int
    sub: Subscription | None        # None = home
    guild_id: str | None            # the scope's community; home: the games community (may be unknown)
    role: FollowRole | None         # card.follow_role for home; the subscription's stored role otherwise

    @property
    def sub_id(self) -> int | None:
        return None if self.sub is None else self.sub.id


class Elsewhere:
    """The press is on a surface outside the games community with no scope of its own (a redirect copy, or a far card
    or play-now post): today's *_ELSEWHERE behaviour."""


ELSEWHERE = Elsewhere()


def home_scope(card: Card, games_guild_id: str | None) -> Scope:
    return Scope(card_id=card.id, sub=None, guild_id=games_guild_id, role=card.follow_role)


def scope_for_sub(conn: sqlite3.Connection, card: Card, sub: Subscription) -> Scope:
    return Scope(card_id=card.id, sub=sub, guild_id=sub.guild_id, role=cards.get_subscription_role(conn, sub.id))


def resolve_scope(conn: sqlite3.Connection, card: Card, surface: Surface, games_guild_id: str | None
                  ) -> Scope | Elsewhere:
    """A card message, a play-now post or a home copy in the games community is the home scope. A far card, play-now
    post or redirect copy is ELSEWHERE, and so is a far copy whose subscription is not a scope. A far copy in local or
    both is its subscription's scope. The subscription is read here, inside the caller's transaction, so a mode changed
    since the press was filtered wins."""
    if surface.sub_id is None:
        return home_scope(card, games_guild_id) if surface.home_community else ELSEWHERE
    sub = subscriptions.get_by_id(conn, surface.sub_id)
    if sub is not None and sub.is_scope:
        return scope_for_sub(conn, card, sub)
    if surface.home_community:
        return home_scope(card, games_guild_id)
    return ELSEWHERE


def scope_target(card: Card, config: Config, scope: Scope) -> str | None:
    """Home: the card's verified play-now channel, else PLAY_NOW_CHANNEL_ID (no REST call). Subscription: its
    local_channel_id, else the copy's own channel."""
    if scope.sub is None:
        own = card.play_now_channel
        if own is not None and own.verified:
            return own.channel_id
        return config.play_now_channel_id
    return scope.sub.local_channel_id or scope.sub.channel_id


async def checked_target(rest: FluxerREST, config: Config, card: Card, scope: Scope, *,
                         failed_event: str = "play_now_failed") -> tuple[tuple[str, str] | None, str | None]:
    """Home: the card's target channel as (channel id, community id), with the reason there is none: "no_channel"
    or "unavailable". The verified channel the card links to, else PLAY_NOW_CHANNEL_ID, looked up over REST. A failed
    lookup is logged under `failed_event`, with ids and the exception type only.
    Subscription: (scope_target, sub.guild_id) with no REST call, since the channel was verified when it was set."""
    if scope.sub is not None:
        channel_id = scope_target(card, config, scope)
        if channel_id is None or scope.guild_id is None:
            return None, "no_channel"
        return (channel_id, scope.guild_id), None
    own = card.play_now_channel
    if own is not None and own.verified:
        return (own.channel_id, own.guild_id), None
    configured = config.play_now_channel_id
    if configured is None:
        return None, "no_channel"
    try:
        channel = await rest.get_channel(configured)
    except Exception as exc:  # noqa: BLE001 - an unreachable channel is refused, never raised
        log_event(log, failed_event, level=logging.WARNING, card_id=card.id, channel_id=configured,
                  reason=type(exc).__name__)
        return None, "unavailable"
    guild_id = channel.get("guild_id")
    if not is_snowflake(guild_id):
        return None, "unavailable"
    return (configured, guild_id), None
