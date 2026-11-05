# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run live check for a test role: two role-ping shapes, the non-mentionable drop, the created role's position
against the bot's top role, role assignment, and the role-above-the-bot 403 (needs LIVE_HIGH_ROLE_ID).

Run from the project root: .venv/bin/python tools/live/role_ping.py (needs .env; not run by CI).
The script never deletes roles: the operator deletes the test role by hand.
"""
from __future__ import annotations

from typing import Any

from _live import Live, LiveRefused, Results, live, load_env, main, out, wait_for_enter
from fluxer_client.rest import FluxerHTTPError
from roles import has_role

REASON = "fluxer panel live test"
ROLE_NAME = "live_ping_test"
ROLE_LABEL = "test-role"


def shapes(role_id: str) -> list[tuple[str, dict[str, Any]]]:
    """The two allowed_mentions shapes under test, in the order they are tried."""
    return [
        ("A", {"parse": [], "roles": [role_id], "replied_user": False}),
        ("B", {"roles": [role_id], "replied_user": False}),
    ]


async def ask(lv: Live, question: str) -> None:
    """Prints the question, waits for Enter and records the operator's typed answer (redacted by out)."""
    out(lv.redactor, question)
    answer = (await wait_for_enter()).strip()
    out(lv.redactor, "operator:", answer or "(no answer)")


async def send_ping(lv: Live, role_id: str, text: str, allowed: dict[str, Any], sent: list[str]) -> None:
    """Sends one role ping and records its message id for the cleanup. Raises FluxerHTTPError on refusal."""
    message = await lv.rest.send_message(lv.env["LIVE_CHANNEL_ID"], content=f"<@&{role_id}> {text}",
                                         allowed_mentions=allowed)
    message_id = str(message["id"])
    lv.redactor.label(message_id, "message")
    sent.append(message_id)


async def report_position(lv: Live, guild: str, bot_id: str, role_id: str, role: dict[str, Any]) -> None:
    """Prints the created role's position and where it sits against the bot's top role."""
    roles = await lv.rest.get_roles(guild)
    member = await lv.rest.get_member(guild, bot_id)
    bot_role_ids = {str(r) for r in (member.get("roles") or [])}
    bot_positions = [r.get("position") for r in roles
                     if str(r.get("id")) in bot_role_ids and isinstance(r.get("position"), int)]
    bot_top = max(bot_positions, default=None)
    position = role.get("position")
    out(lv.redactor, "INFO created role:", f"position={position}", f"permissions={role.get('permissions')}",
        f"mentionable={role.get('mentionable')}")
    above = "unknown"
    if isinstance(position, int) and bot_top is not None:
        above = "yes" if position > bot_top else "no"
    out(lv.redactor, f"INFO bot top role position={bot_top}; created role above the bot: {above}")
    peers = [r for r in roles if str(r.get("id")) != role_id and r.get("position") == position]
    out(lv.redactor, f"INFO other roles sharing the created role's position: {len(peers)}")


async def ping_shapes(lv: Live, res: Results, role_id: str, sent: list[str]) -> dict[str, Any] | None:
    """Sends shape A then shape B. Returns the first shape the server accepted, or None."""
    accepted: dict[str, Any] | None = None
    for name, allowed in shapes(role_id):
        try:
            await send_ping(lv, role_id, f"live ping {name}", allowed, sent)
        except FluxerHTTPError as exc:
            res.fail(f"shape {name} accepted", f"HTTP {exc.status} {exc.code}")
            continue
        res.ok(f"shape {name} accepted")
        if accepted is None:
            accepted = allowed
    return accepted


async def non_mentionable(lv: Live, role_id: str, allowed: dict[str, Any], sent: list[str]) -> None:
    """With the role no longer mentionable, the accepted shape is re-sent. The operator reports any notification."""
    await ask(lv, "Turn Mentionable off for live_ping_test in the client, then press Enter.")
    try:
        await send_ping(lv, role_id, "live ping non-mentionable", allowed, sent)
    except FluxerHTTPError as exc:
        out(lv.redactor, f"INFO non-mentionable ping refused: HTTP {exc.status} {exc.code}")
        return
    await ask(lv, "Did a notification arrive for the non-mentionable ping? Type a short answer and press Enter.")


async def assignment(lv: Live, res: Results, role_id: str) -> None:
    """Removes and re-adds the test role, then tries the role above the bot, which Fluxer should refuse with 403."""
    rest = lv.rest
    guild, user, high = lv.env["LIVE_GUILD_ID"], lv.env["LIVE_USER_ID"], lv.env["LIVE_HIGH_ROLE_ID"]
    try:
        await rest.remove_member_role(guild, user, role_id, reason=REASON)
        if await has_role(rest, guild, user, role_id):
            res.fail("remove test role", "role still in member roles after remove")
        else:
            res.ok("remove test role")
        await rest.add_member_role(guild, user, role_id, reason=REASON)
        if await has_role(rest, guild, user, role_id):
            res.ok("add test role")
        else:
            res.fail("add test role", "role not in member roles after add")
    except FluxerHTTPError as exc:
        res.fail("role assignment", f"HTTP {exc.status} {exc.code}")

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


async def run() -> Results:
    if not load_env().get("LIVE_HIGH_ROLE_ID"):
        raise LiveRefused("LIVE_HIGH_ROLE_ID is missing")
    async with live(gateway=False) as lv:
        res = Results(lv.redactor)
        rest = lv.rest
        guild, user = lv.env["LIVE_GUILD_ID"], lv.env["LIVE_USER_ID"]

        bot = await rest.request("GET", "/users/@me")
        bot_id = str(bot["id"])
        lv.redactor.label(bot_id, "bot")

        try:
            role = await rest.create_role(guild, name=ROLE_NAME, mentionable=True, reason=REASON)
        except FluxerHTTPError as exc:
            res.fail("create role", f"HTTP {exc.status} {exc.code}")
            return res
        role_id = str(role["id"])
        lv.redactor.label(role_id, ROLE_LABEL)
        res.ok("create role")

        sent: list[str] = []
        try:
            await report_position(lv, guild, bot_id, role_id, role)
            try:
                await rest.add_member_role(guild, user, role_id, reason=REASON)
            except FluxerHTTPError as exc:
                res.fail("grant test role", f"HTTP {exc.status} {exc.code}")

            accepted = await ping_shapes(lv, res, role_id, sent)
            if accepted is None:
                out(lv.redactor, "SKIP non-mentionable check: neither shape was accepted")
            else:
                await non_mentionable(lv, role_id, accepted, sent)

            await assignment(lv, res, role_id)
        finally:
            for message_id in sent:
                try:
                    await rest.delete_message(lv.env["LIVE_CHANNEL_ID"], message_id)
                except FluxerHTTPError as exc:
                    out(lv.redactor, f"delete ping message failed: HTTP {exc.status}")
            try:
                await rest.remove_member_role(guild, user, role_id, reason=REASON)
            except FluxerHTTPError as exc:
                out(lv.redactor, f"cleanup: removing the test role failed: HTTP {exc.status} {exc.code}")
            out(lv.redactor, f"Manual: delete the role '{ROLE_NAME}' in the client. The script cannot delete roles.")
        return res


if __name__ == "__main__":
    main(run)
