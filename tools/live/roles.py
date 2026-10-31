# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run live check: add and remove a test member role, and the role-hierarchy 403 (if LIVE_HIGH_ROLE_ID is set).

Run from the project root: .venv/bin/python tools/live/roles.py (needs .env; not run by CI).
"""
from __future__ import annotations

from typing import Any

from _live import LiveRefused, Results, live, load_env, main, out
from fluxer_client.rest import FluxerHTTPError, FluxerREST

REASON = "fluxer panel live test"


async def has_role(rest: FluxerREST, guild: str, user: str, role: str) -> bool:
    member: Any = await rest.get_member(guild, user)
    roles = member.get("roles") if isinstance(member, dict) else None
    return isinstance(roles, list) and role in {str(r) for r in roles}


async def run() -> Results:
    if not load_env().get("LIVE_ROLE_ID"):
        raise LiveRefused("LIVE_ROLE_ID is missing")
    async with live(gateway=False) as lv:
        res = Results(lv.redactor)
        rest = lv.rest
        guild, user, role = lv.env["LIVE_GUILD_ID"], lv.env["LIVE_USER_ID"], lv.env["LIVE_ROLE_ID"]

        try:
            await rest.add_member_role(guild, user, role, reason=REASON)
            granted = await has_role(rest, guild, user, role)
        except FluxerHTTPError as exc:
            res.fail("add member role", f"HTTP {exc.status} {exc.code}")
        else:
            if granted:
                res.ok("add member role")
            else:
                res.fail("add member role", "role not in member roles after add")

        try:
            await rest.remove_member_role(guild, user, role, reason=REASON)
            granted = await has_role(rest, guild, user, role)
        except FluxerHTTPError as exc:
            res.fail("remove member role", f"HTTP {exc.status} {exc.code}")
        else:
            if not granted:
                res.ok("remove member role")
            else:
                res.fail("remove member role", "role still in member roles after remove")

        high = lv.env.get("LIVE_HIGH_ROLE_ID")
        if not high:
            out(lv.redactor, "SKIP hierarchy check: LIVE_HIGH_ROLE_ID not set")
        else:
            try:
                await rest.add_member_role(guild, user, high, reason=REASON)
            except FluxerHTTPError as exc:
                if exc.status == 403:
                    res.ok(f"hierarchy add refused with 403 (code {exc.code})")
                else:
                    res.fail("hierarchy add", f"expected 403, got HTTP {exc.status} {exc.code}")
            else:
                res.fail("hierarchy add", "succeeded; expected 403")
                try:
                    await rest.remove_member_role(guild, user, high, reason=REASON)
                except FluxerHTTPError as exc:
                    out(lv.redactor, f"cleanup of the high role failed: HTTP {exc.status} {exc.code}")

        out(lv.redactor, f"Manual: check the guild audit log for the reason text '{REASON}'.")
        return res


if __name__ == "__main__":
    main(run)
