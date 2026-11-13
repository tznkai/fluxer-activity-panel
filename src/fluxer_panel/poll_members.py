# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Poll recipients: the holders of a card's follow role, found by paging the member list. Ids only."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    user_ids: tuple[str, ...]     # starter first, then holders by ascending id
    left_out: int                 # holders over the cap


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


async def role_holders(rest: FluxerREST, guild_id: str, role_id: str, *, page_size: int = PAGE_SIZE) -> list[str]:
    """User ids holding `role_id`, ascending, bots excluded. Pages with `after` until a short page."""
    holders: list[str] = []
    after: str | None = None
    for _ in range(MAX_PAGES):
        try:
            page = await rest.list_members(guild_id, limit=page_size, after=after)
        except Exception as exc:                    # noqa: BLE001 - any failure refuses the start (AC2.8)
            log_event(log, "poll_members_failed", level=logging.WARNING, reason=type(exc).__name__)
            raise MembersUnavailable from None
        ids = []
        for member in page:
            uid = _user_id(member)
            if uid is None:
                continue
            ids.append(uid)
            if role_id in _role_ids(member) and not _is_bot(member):
                holders.append(uid)
        if len(page) < page_size:
            return sorted(set(holders), key=int)
        last = max(ids, key=int, default=None)
        if last is None or (after is not None and int(last) <= int(after)):
            raise MembersUnavailable           # the cursor did not advance
        after = last
    raise MembersUnavailable                   # more than MAX_PAGES pages


def recipients(holders: Sequence[str], *, starter_id: str, cap: int) -> Recipients:
    others = [uid for uid in sorted(set(holders), key=int) if uid != starter_id]
    room = max(cap - 1, 0)
    return Recipients(user_ids=(starter_id, *others[:room]), left_out=max(len(others) - room, 0))
