# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Poll recipients: the holders of a card's follow role, found by paging the member list. Ids only."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerREST
from fluxer_panel.logs import log_event

PAGE_SIZE = 1000
MAX_PAGES = 50
log = logging.getLogger(__name__)


class MembersUnavailable(Exception):
    """The member list could not be read. Raised instead of returning a partial list."""


@dataclass(frozen=True)
class Recipients:
    user_ids: tuple[str, ...]     # starter first, then holders in route order
    left_out: int                 # holders over the cap
    via: Mapping[str, int] = field(default_factory=dict)   # user id -> the both subscription reached through, if any


def _user_id(member: Mapping[str, Any]) -> str | None:
    user = member.get("user")
    uid = user.get("id") if isinstance(user, Mapping) else None
    return uid if is_snowflake(uid) else None


def _is_bot(member: Mapping[str, Any]) -> bool:
    user = member.get("user")
    return isinstance(user, Mapping) and user.get("bot") is True


def _role_ids(member: Mapping[str, Any]) -> frozenset[str]:
    roles = member.get("roles")
    return frozenset(str(r) for r in roles) if isinstance(roles, list) else frozenset()


async def guild_role_holders(rest: FluxerREST, guild_id: str, role_ids: Sequence[str], *,
                             page_size: int = PAGE_SIZE) -> dict[str, list[str]]:
    """The holders of each role in one community, read from one pass over its member list: ascending user ids per role,
    bots excluded. Pages with `after` until a short page. Raises MembersUnavailable (chained to the cause) on a failure;
    nothing is logged here."""
    holders: dict[str, list[str]] = {role_id: [] for role_id in role_ids}
    after: str | None = None
    for _ in range(MAX_PAGES):
        try:
            page = await rest.list_members(guild_id, limit=page_size, after=after)
        except Exception as exc:                    # noqa: BLE001 - any failure refuses the read (AC2.8)
            raise MembersUnavailable from exc
        ids = []
        for member in page:
            uid = _user_id(member)
            if uid is None:
                continue
            ids.append(uid)
            if _is_bot(member):
                continue
            held = _role_ids(member)
            for role_id in role_ids:
                if role_id in held:
                    holders[role_id].append(uid)
        if len(page) < page_size:
            return {role_id: sorted(set(found), key=int) for role_id, found in holders.items()}
        last = max(ids, key=int, default=None)
        if last is None or (after is not None and int(last) <= int(after)):
            raise MembersUnavailable           # the cursor did not advance
        after = last
    raise MembersUnavailable                   # more than MAX_PAGES pages


def failure_reason(exc: MembersUnavailable) -> str:
    """A reason key for a failed member read: the type of the failure it wraps, never its message."""
    return type(exc.__cause__).__name__ if exc.__cause__ is not None else "MembersUnavailable"


async def role_holders(rest: FluxerREST, guild_id: str, role_id: str, *, page_size: int = PAGE_SIZE) -> list[str]:
    """User ids holding `role_id`, ascending, bots excluded. A failure is logged by reason and raised."""
    try:
        found = await guild_role_holders(rest, guild_id, (role_id,), page_size=page_size)
    except MembersUnavailable as exc:                # any failure refuses the start (AC2.8)
        log_event(log, "poll_members_failed", level=logging.WARNING, reason=failure_reason(exc))
        raise
    return found[role_id]


def recipients(holders: Sequence[str], *, starter_id: str, cap: int) -> Recipients:
    others = [uid for uid in sorted(set(holders), key=int) if uid != starter_id]
    room = max(cap - 1, 0)
    return Recipients(user_ids=(starter_id, *others[:room]), left_out=max(len(others) - room, 0))


def combined(groups: Sequence[tuple[int | None, Sequence[str]]], *, starter_id: str, cap: int) -> Recipients:
    """The poll's recipients from several groups of holders, in route order: the starter, then each group's holders by
    ascending id. A user already placed by an earlier group keeps the earlier route, so the home group (sub None) wins
    over a both subscription. The cap is applied to that order, so home holders are kept first. `via` maps each kept
    user that came from a subscription group to that subscription's id."""
    seen = {starter_id}
    ordered: list[str] = []
    origin: dict[str, int] = {}
    for sub_id, holders in groups:
        for uid in sorted(set(holders), key=int):
            if uid in seen:
                continue
            seen.add(uid)
            ordered.append(uid)
            if sub_id is not None:
                origin[uid] = sub_id
    kept = ordered[:max(cap - 1, 0)]
    return Recipients(user_ids=(starter_id, *kept), left_out=len(ordered) - len(kept),
                      via={uid: origin[uid] for uid in kept if uid in origin})
