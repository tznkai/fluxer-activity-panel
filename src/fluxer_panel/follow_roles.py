# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Follow roles: the only module in `fluxer_panel` that changes a role (see REVIEW.md, "Role assignment").

`toggle_follow` is the only caller of `add_member_role` and `remove_member_role`. Before any role call it re-reads the
allowlist (`cards.follow_role_ids`) and checks the hierarchy against freshly fetched roles and the bot's member
record. Role ids come only from `card_follow_role`, never from event or command text.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import cards
from fluxer_panel.cards import Card
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity
from fluxer_panel.logs import log_event

log = logging.getLogger(__name__)

REASON = "card follow role"   # X-Audit-Log-Reason: a fixed string, never user text


@dataclass(frozen=True)
class FollowDeps:
    db: Database
    rest: FluxerREST
    config: Config
    roles: RoleCache
    identity: BotIdentity


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
