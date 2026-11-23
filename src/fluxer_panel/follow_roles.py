# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Follow roles: the only module in `fluxer_panel` that changes a role (see REVIEW.md, "Role assignment").

`toggle_follow` is the only caller of `add_member_role` and `remove_member_role`. Before any role call it re-reads the
allowlist (`cards.follow_role_ids`) and checks the hierarchy against freshly fetched roles and the bot's member
record. Role ids come only from `card_follow_role`, never from event or command text.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import cards
from fluxer_panel.cards import Card, FollowRole
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity
from fluxer_panel.logs import log_event

log = logging.getLogger(__name__)

REASON = "card follow role"   # X-Audit-Log-Reason: a fixed string, never user text


class FollowLocks:
    """One asyncio.Lock per card id for the card's role step (publish, manual set, clear). Separate from
    `card_commands.CardLocks`, so the role step never runs under the card's lock (the bell toggle does, by design)."""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    def get(self, card_id: int) -> asyncio.Lock:
        return self._locks.setdefault(card_id, asyncio.Lock())


@dataclass(frozen=True)
class FollowDeps:
    db: Database
    rest: FluxerREST
    config: Config
    roles: RoleCache
    identity: BotIdentity
    # The publish and set role steps of one bot share one FollowLocks: a context builds its deps with its own.
    locks: FollowLocks = field(default_factory=FollowLocks)


class Toggle(enum.Enum):
    ADDED = "added"
    REMOVED = "removed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ToggleResult:
    outcome: Toggle
    reason: str | None = None   # not_allowlisted | role_missing | role_permissions | hierarchy | forbidden | no_bot_id


def zero_permissions(role: Mapping[str, Any]) -> bool:
    """Fluxer sends permissions as a decimal string; a bare 0 is accepted too."""
    return str(role.get("permissions")) == "0"


def rank(role: Mapping[str, Any]) -> tuple[int, int]:
    """Higher is more senior: position first, then the lower id ranks higher (the tie rule)."""
    return (int(role["position"]), -int(role["id"]))


def bot_top(roles: list[Mapping[str, Any]], bot_role_ids: Iterable[str], guild_id: str) -> tuple[int, int]:
    """The bot's highest rank: the max over its member roles, or @everyone (id == guild id) when it has none.
    With neither, the bot manages nothing."""
    held = {str(r) for r in bot_role_ids}
    ranks = [rank(r) for r in roles if str(r["id"]) in held]
    if ranks:
        return max(ranks)
    everyone = [r for r in roles if str(r["id"]) == guild_id]
    if everyone:
        return rank(everyone[0])
    return (-1, 0)


def manageable(target: Mapping[str, Any], roles: list[Mapping[str, Any]], bot_role_ids: Iterable[str],
               guild_id: str) -> bool:
    return bot_top(roles, bot_role_ids, guild_id) > rank(target)


def _role_ids(member: Mapping[str, Any]) -> frozenset[str]:
    roles = member.get("roles")
    return frozenset(str(r) for r in roles) if isinstance(roles, list) else frozenset()


def _refused(card: Card, reason: str) -> ToggleResult:
    log_event(log, "follow_refused", card_id=card.id, reason=reason)
    return ToggleResult(Toggle.UNAVAILABLE, reason)


async def toggle_follow(deps: FollowDeps, guild_id: str, user_id: str, card: Card, *,
                        member_role_ids: frozenset[str] | None) -> ToggleResult:
    # 1. Fresh allowlist and stored role. Nothing from the event or the card snapshot reaches the REST call.
    stored, allow = await deps.db.run(lambda c: (cards.get_follow_role(c, card.id), cards.follow_role_ids(c)))
    if stored is None or stored.role_id not in allow or stored.guild_id != guild_id or card.follow_role != stored:
        return _refused(card, "not_allowlisted")
    bot_id = deps.identity.user_id
    if bot_id is None:
        return _refused(card, "no_bot_id")
    role_id = stored.role_id

    # 2. Hierarchy against freshly fetched roles and the bot's own roles.
    rest = deps.rest
    roles = await rest.get_roles(guild_id)
    target = next((r for r in roles if str(r.get("id")) == role_id), None)
    if target is None:
        return _refused(card, "role_missing")
    if not zero_permissions(target):
        return _refused(card, "role_permissions")
    bot = await rest.get_member(guild_id, bot_id)
    if not manageable(target, roles, _role_ids(bot), guild_id):
        return _refused(card, "hierarchy")

    # 3. The presser's current roles, from the event when it carries them.
    if member_role_ids is None:
        member_role_ids = _role_ids(await rest.get_member(guild_id, user_id))
    has = role_id in member_role_ids

    # 4. The only role calls in the module, and only for a role that passed every check above.
    try:
        if has:
            await rest.remove_member_role(guild_id, user_id, role_id, reason=REASON)
        else:
            await rest.add_member_role(guild_id, user_id, role_id, reason=REASON)
    except FluxerHTTPError as exc:
        if exc.status != 403:
            raise
        return _refused(card, "forbidden")
    outcome = Toggle.REMOVED if has else Toggle.ADDED
    log_event(log, "follow_toggled", card_id=card.id, outcome=outcome.value, role_id=role_id)
    return ToggleResult(outcome)


# Ensure: create, adopt or validate a card's follow role ----------------------------------------------

class Ensured(enum.Enum):
    REUSED = "reused"
    ADOPTED = "adopted"
    CREATED = "created"
    SET = "set"
    REFUSED = "refused"
    FAILED = "failed"


@dataclass(frozen=True)
class EnsureResult:
    outcome: Ensured
    role_id: str | None = None
    # other_community | everyone | role_permissions | not_mentionable | hierarchy | taken | missing |
    # duplicate_name | no_bot_id | create_failed | mentionable_failed | lookup_failed
    reason: str | None = None


@dataclass(frozen=True)
class RoleTarget:
    """Whose follow role this is: the card's (sub_id None) or one subscription's."""
    card: Card
    sub_id: int | None = None


def _stored(conn: sqlite3.Connection, target: RoleTarget) -> FollowRole | None:
    if target.sub_id is None:
        return cards.get_follow_role(conn, target.card.id)
    return cards.get_subscription_role(conn, target.sub_id)


# Fluxer errors, transport errors and timeouts. A role call that fails with one of these is a result, not a raise.
_TRANSPORT = (FluxerHTTPError, aiohttp.ClientError, TimeoutError)


def role_name(config: Config, card: Card) -> str:
    return f"{config.follow_role_prefix}{card.slug}"


def _mentionable(role: object) -> bool:
    """True only for a returned role that reads back as mentionable. A missing or odd response is not."""
    return isinstance(role, Mapping) and role.get("mentionable") is True


def _ensured(target: RoleTarget, result: EnsureResult) -> EnsureResult:
    log_event(log, "follow_role_ensured", card_id=target.card.id, sub_id=target.sub_id,
              outcome=result.outcome.value, reason=result.reason, role_id=result.role_id)
    return result


async def _store(deps: FollowDeps, target: RoleTarget, guild_id: str, role_id: str) -> bool:
    """Stores the role on the target. False when another card or subscription already has it (the store's own check).
    A subscription's role lives in the subscription's community, so guild_id is stored only for the card."""
    card, sub_id = target.card, target.sub_id
    try:
        if sub_id is None:
            await deps.db.run(lambda c: cards.set_follow_role(c, card.id, guild_id, role_id))
        else:
            await deps.db.run(lambda c: cards.set_subscription_role(c, sub_id, role_id))
    except cards.FollowRoleTaken:
        return False
    return True


async def ensure_follow_role(deps: FollowDeps, guild_id: str, card: Card, *,
                             role_id: str | None = None, sub_id: int | None = None) -> EnsureResult:
    """Publish (role_id None) reuses, adopts or creates the role of the card (sub_id None) or of one subscription in
    `guild_id`. A manual set (role_id given) validates that role and stores it. Nothing is stored unless the result is
    REUSED, ADOPTED, CREATED or SET."""
    target = RoleTarget(card, sub_id)
    bot_id = deps.identity.user_id
    if bot_id is None:
        return _ensured(target, EnsureResult(Ensured.REFUSED, reason="no_bot_id"))
    # One role step per card at a time, for the card's role and every subscription's. The stored role is read under
    # this lock, so a publish that waits behind another one (or behind a manual set) reuses the role that one stored,
    # instead of creating a second.
    async with deps.locks.get(card.id):
        return await _role_step(deps, guild_id, target, role_id, bot_id)


async def _role_step(deps: FollowDeps, guild_id: str, target: RoleTarget, role_id: str | None,
                     bot_id: str) -> EnsureResult:
    try:
        roles = await deps.rest.get_roles(guild_id)
        bot_roles = _role_ids(await deps.rest.get_member(guild_id, bot_id))
    except _TRANSPORT:
        return _ensured(target, EnsureResult(Ensured.FAILED, reason="lookup_failed"))
    if role_id is None:
        return _ensured(target, await _publish(deps, guild_id, target, roles, bot_roles))
    return _ensured(target, await _set(deps, guild_id, target, role_id, roles, bot_roles))


async def _publish(deps: FollowDeps, guild_id: str, target: RoleTarget, roles: list[Mapping[str, Any]],
                   bot_roles: frozenset[str]) -> EnsureResult:
    stored, allow = await deps.db.run(lambda c: (_stored(c, target), cards.follow_role_ids(c)))
    if stored is not None and stored.role_id in {str(r.get("id")) for r in roles}:
        return EnsureResult(Ensured.REUSED, role_id=stored.role_id)
    name = role_name(deps.config, target.card)
    matches = [r for r in roles if str(r.get("name", "")).casefold() == name.casefold()]
    if len(matches) > 1:
        return EnsureResult(Ensured.REFUSED, reason="duplicate_name")
    if matches:
        return await _adopt(deps, guild_id, target, matches[0], roles, bot_roles, allow)
    return await _create(deps, guild_id, target, name)


async def _adopt(deps: FollowDeps, guild_id: str, target: RoleTarget, role: Mapping[str, Any],
                 roles: list[Mapping[str, Any]], bot_roles: frozenset[str],
                 allow: frozenset[str]) -> EnsureResult:
    role_id = str(role["id"])
    # Every check runs before the PATCH, so a role owned by another card, or failing a check, is never written.
    if not zero_permissions(role):
        return EnsureResult(Ensured.REFUSED, reason="role_permissions")
    if not manageable(role, roles, bot_roles, guild_id):
        return EnsureResult(Ensured.REFUSED, reason="hierarchy")
    if role_id in allow:
        return EnsureResult(Ensured.REFUSED, reason="taken")
    if not _mentionable(role):
        try:
            updated = await deps.rest.set_role_mentionable(guild_id, role_id, mentionable=True, reason=REASON)
        except _TRANSPORT:
            return EnsureResult(Ensured.FAILED, reason="mentionable_failed")
        if not _mentionable(updated):
            return EnsureResult(Ensured.FAILED, reason="mentionable_failed")
    if not await _store(deps, target, guild_id, role_id):
        return EnsureResult(Ensured.REFUSED, reason="taken")
    return EnsureResult(Ensured.ADOPTED, role_id=role_id)


async def _create(deps: FollowDeps, guild_id: str, target: RoleTarget, name: str) -> EnsureResult:
    try:
        created = await deps.rest.create_role(guild_id, name=name, mentionable=True, reason=REASON)
    except (*_TRANSPORT, ValueError):
        return EnsureResult(Ensured.FAILED, reason="create_failed")
    finally:
        # A timeout or a 5xx can land after Fluxer has created the role, so the cached name list is stale on every outcome.
        deps.roles.invalidate(guild_id)
    role_id = created.get("id") if isinstance(created, Mapping) else None
    if not is_snowflake(role_id):
        return EnsureResult(Ensured.FAILED, reason="create_failed")
    try:
        updated = await deps.rest.set_role_mentionable(guild_id, role_id, mentionable=True, reason=REASON)
    except _TRANSPORT:
        return EnsureResult(Ensured.FAILED, reason="mentionable_failed")
    if not _mentionable(updated):
        return EnsureResult(Ensured.FAILED, reason="mentionable_failed")
    if not await _store(deps, target, guild_id, role_id):
        return EnsureResult(Ensured.REFUSED, reason="taken")
    return EnsureResult(Ensured.CREATED, role_id=role_id)


async def _set(deps: FollowDeps, guild_id: str, target: RoleTarget, role_id: str, roles: list[Mapping[str, Any]],
               bot_roles: frozenset[str]) -> EnsureResult:
    role = next((r for r in roles if str(r.get("id")) == role_id), None)
    if role is None:   # Fluxer lists only the games community's roles, so another community's role is missing too
        return EnsureResult(Ensured.REFUSED, reason="missing")
    if role_id == guild_id:
        return EnsureResult(Ensured.REFUSED, reason="everyone")
    if not zero_permissions(role):
        return EnsureResult(Ensured.REFUSED, reason="role_permissions")
    if not _mentionable(role):
        return EnsureResult(Ensured.REFUSED, reason="not_mentionable")
    if not manageable(role, roles, bot_roles, guild_id):
        return EnsureResult(Ensured.REFUSED, reason="hierarchy")
    if not await _store(deps, target, guild_id, role_id):
        return EnsureResult(Ensured.REFUSED, reason="taken")
    return EnsureResult(Ensured.SET, role_id=role_id)
