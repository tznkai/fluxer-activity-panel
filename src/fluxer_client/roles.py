# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-guild cache of role ids by name, invalidated on GUILD_ROLE_* events."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fluxer_client.rest import FluxerREST


class RoleCache:
    def __init__(self, rest: FluxerREST) -> None:
        self.rest = rest
        self._roles: dict[str, list[tuple[str, str]]] = {}   # guild -> [(role_id, casefolded name)]

    async def role_ids(self, guild_id: str, name: str) -> frozenset[str]:
        if guild_id not in self._roles:
            roles = await self.rest.get_roles(guild_id)
            self._roles[guild_id] = [(str(r["id"]), str(r.get("name", "")).casefold()) for r in roles]
        wanted = name.casefold()
        return frozenset(rid for rid, rname in self._roles[guild_id] if rname == wanted)

    async def member_has_role(self, guild_id: str, user_id: str, name: str,
                              member_role_ids: Iterable[str] | None) -> bool:
        """Uses the roles carried by the event when present; otherwise fetches the member."""
        if member_role_ids is None:
            member = await self.rest.get_member(guild_id, user_id)
            member_role_ids = member.get("roles", []) if isinstance(member.get("roles"), list) else []
        return bool({str(r) for r in member_role_ids} & await self.role_ids(guild_id, name))

    def invalidate(self, guild_id: str) -> None:
        self._roles.pop(guild_id, None)

    def handle_event(self, event: str, data: dict[str, Any]) -> None:
        if event.startswith("GUILD_ROLE_") and "guild_id" in data:
            self.invalidate(str(data["guild_id"]))
