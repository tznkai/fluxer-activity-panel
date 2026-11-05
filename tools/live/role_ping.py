# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run live check for a test role. It measures role mentions from the message object's `mention_roles` array.

Checks: the created role's `mentionable` (create, then a PATCH to true and a read-back), the bot's own MENTION_EVERYONE
and ADMINISTRATOR bits and whether it is the guild owner, two ping shapes (A and B) whose accepted sends must list the
role in `mention_roles`, a control ping with `allowed_mentions` parse=[] that must not, a non-mentionable PATCH and a
re-sent ping (a measurement, reported as INFO), role assignment, and the role-above-the-bot 403 (needs LIVE_HIGH_ROLE_ID).

Run from the project root: .venv/bin/python tools/live/role_ping.py (needs .env; not run by CI).
The script never deletes roles: the operator deletes the test role by hand.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from _live import Live, LiveRefused, Results, live, load_env, main, out, wait_for_enter
from fluxer_client.rest import FluxerHTTPError
from roles import has_role

REASON = "fluxer panel live test"
AUDIT_HEADERS = {"X-Audit-Log-Reason": REASON}   # the same header FluxerREST.create_role sends for REASON
ROLE_NAME = "live_ping_test"
ROLE_LABEL = "test-role"
MENTION_EVERYONE = 1 << 17
ADMINISTRATOR = 1 << 3


def shapes(role_id: str) -> list[tuple[str, dict[str, Any]]]:
    """The two allowed_mentions shapes under test, in the order they are tried."""
    return [
        ("A", {"parse": [], "roles": [role_id], "replied_user": False}),
        ("B", {"roles": [role_id], "replied_user": False}),
    ]


def mention_roles(message: Any) -> list[str] | None:
    """The string ids of the message's `mention_roles`, or None when the field is absent or not a list."""
    if not isinstance(message, dict):
        return None
    value = message.get("mention_roles")
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, str)]


def role_in_mentions(message: Any, role_id: str) -> bool:
    """True only when the message's `mention_roles` lists the role."""
    return role_id in (mention_roles(message) or [])


def parse_bits(value: Any) -> int | None:
    """A role's `permissions` as an int. Fluxer sends it as a decimal string; None when it is not readable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def effective_permissions(roles: list[dict[str, Any]], held_role_ids: set[str], guild_id: str) -> int | None:
    """The OR of @everyone (the role whose id is the guild id) and the member's roles. None when any of those roles
    is missing from the list or has unreadable permissions, so no bit is reported as absent by mistake."""
    wanted = {*held_role_ids, guild_id}
    total = 0
    seen: set[str] = set()
    for role in roles:
        role_id = str(role.get("id"))
        if role_id not in wanted:
            continue
        bits = parse_bits(role.get("permissions"))
        if bits is None:
            return None
        total |= bits
        seen.add(role_id)
    if seen != wanted:
        return None
    return total


def flag(perms: int | None, bit: int) -> str:
    """'yes', 'no', or 'unknown' when the effective permissions could not be read."""
    if perms is None:
        return "unknown"
    return "yes" if perms & bit else "no"


@dataclass
class MentionState:
    """Whether the role may still be mentionable. Create asked for True; only a read-back-confirmed PATCH to False clears it."""
    may_be_mentionable: bool = True


async def ask(lv: Live, question: str) -> None:
    """Prints the question, waits for Enter and records the operator's typed answer (redacted by out)."""
    out(lv.redactor, question)
    answer = (await wait_for_enter()).strip()
    out(lv.redactor, "operator:", answer or "(no answer)")


async def send_ping(lv: Live, role_id: str, text: str, allowed: dict[str, Any], sent: list[str]) -> dict[str, Any]:
    """Sends one role ping, records its message id for the cleanup and returns the response message.
    Raises FluxerHTTPError on refusal."""
    message = await lv.rest.send_message(lv.env["LIVE_CHANNEL_ID"], content=f"<@&{role_id}> {text}",
                                         allowed_mentions=allowed)
    message_id = str(message["id"])
    lv.redactor.label(message_id, "message")
    sent.append(message_id)
    return message


def report_mention_fields(lv: Live, prefix: str, role_id: str, message: dict[str, Any]) -> None:
    """Prints whether the role is in the message's mention_roles, the length of that array and mention_everyone."""
    ids = mention_roles(message)
    present = "yes" if role_in_mentions(message, role_id) else "no"
    count = "absent" if ids is None else len(ids)
    everyone = message.get("mention_everyone", "absent") if isinstance(message, dict) else "absent"
    out(lv.redactor, f"INFO {prefix}: {ROLE_LABEL} in mention_roles: {present}; "
                     f"len(mention_roles)={count}; mention_everyone={everyone}")


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


async def report_bot_permissions(lv: Live, guild: str, bot_id: str) -> None:
    """Prints the bot's effective MENTION_EVERYONE and ADMINISTRATOR bits and whether it owns the guild (yes/no only)."""
    roles = await lv.rest.get_roles(guild)
    member = await lv.rest.get_member(guild, bot_id)
    held = {str(r) for r in (member.get("roles") or [])}
    perms = effective_permissions(roles, held, guild)
    out(lv.redactor, f"INFO bot MENTION_EVERYONE: {flag(perms, MENTION_EVERYONE)}; "
                     f"ADMINISTRATOR: {flag(perms, ADMINISTRATOR)}")
    info = await lv.rest.get_guild(guild)
    owner = info.get("owner_id")
    if owner is None:
        out(lv.redactor, "INFO bot is guild owner: unknown (owner field absent)")
    else:
        out(lv.redactor, f"INFO bot is guild owner: {'yes' if str(owner) == bot_id else 'no'}")


async def read_mentionable(lv: Live, guild: str, role_id: str) -> Any:
    """The role's `mentionable` as the server lists it now (None when the role is not in the list)."""
    for role in await lv.rest.get_roles(guild):
        if str(role.get("id")) == role_id:
            return role.get("mentionable")
    return None


async def patch_mentionable(lv: Live, res: Results, guild: str, role_id: str, value: bool, name: str,
                            state: MentionState) -> None:
    """PATCH {"mentionable": value} (never permissions), read it back and report it as ok or fail under `name`."""
    if value:
        state.may_be_mentionable = True   # set before the send: a PATCH that errors may still have applied
    try:
        await lv.rest.request("PATCH", f"/guilds/{guild}/roles/{role_id}", json_body={"mentionable": value},
                              headers=AUDIT_HEADERS)
        readback = await read_mentionable(lv, guild, role_id)
    except FluxerHTTPError as exc:
        res.fail(name, f"HTTP {exc.status} {exc.code}")
        return
    out(lv.redactor, f"INFO read-back after {name}: mentionable={readback}")
    if readback is value:
        if not value:
            state.may_be_mentionable = False
        res.ok(name)
    else:
        res.fail(name, f"mentionable={readback} after the PATCH, expected {value}")


async def ping_shapes(lv: Live, res: Results, role_id: str, sent: list[str]) -> dict[str, Any] | None:
    """Sends shape A then shape B. Each accepted send must list the role in mention_roles. Returns the first accepted
    shape's allowed_mentions, or None."""
    accepted: dict[str, Any] | None = None
    for name, allowed in shapes(role_id):
        try:
            message = await send_ping(lv, role_id, f"live ping {name}", allowed, sent)
        except FluxerHTTPError as exc:
            res.fail(f"shape {name} accepted", f"HTTP {exc.status} {exc.code}")
            continue
        res.ok(f"shape {name} accepted")
        report_mention_fields(lv, f"shape {name}", role_id, message)
        if role_in_mentions(message, role_id):
            res.ok(f"shape {name} mentions role")
        else:
            res.fail(f"shape {name} mentions role", "role not in mention_roles")
        if accepted is None:
            accepted = allowed
    return accepted


async def control_ping(lv: Live, role_id: str, sent: list[str]) -> None:
    """Control: the role is in the content but allowed_mentions allows nothing. Expected: not in mention_roles (INFO)."""
    try:
        message = await send_ping(lv, role_id, "live ping control", {"parse": []}, sent)
    except FluxerHTTPError as exc:
        out(lv.redactor, f"INFO control (parse=[]) refused: HTTP {exc.status} {exc.code}")
        return
    report_mention_fields(lv, "control (parse=[]; expected: no)", role_id, message)


async def non_mentionable(lv: Live, res: Results, guild: str, role_id: str, allowed: dict[str, Any],
                          sent: list[str], state: MentionState) -> None:
    """With the role made non-mentionable by PATCH, the accepted shape is re-sent. This is a measurement (INFO)."""
    await patch_mentionable(lv, res, guild, role_id, False, "patch mentionable off", state)
    try:
        message = await send_ping(lv, role_id, "live ping non-mentionable", allowed, sent)
    except FluxerHTTPError as exc:
        out(lv.redactor, f"INFO non-mentionable ping: refused HTTP {exc.status} {exc.code}")
        return
    verdict = "delivered in mention_roles" if role_in_mentions(message, role_id) else "dropped from mention_roles"
    out(lv.redactor, f"INFO non-mentionable ping: {verdict}")
    report_mention_fields(lv, "non-mentionable ping", role_id, message)


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
        state = MentionState()
        try:
            await report_position(lv, guild, bot_id, role_id, role)
            await report_bot_permissions(lv, guild, bot_id)
            try:
                await rest.add_member_role(guild, user, role_id, reason=REASON)
            except FluxerHTTPError as exc:
                res.fail("grant test role", f"HTTP {exc.status} {exc.code}")

            await patch_mentionable(lv, res, guild, role_id, True, "patch mentionable on", state)
            accepted = await ping_shapes(lv, res, role_id, sent)
            if accepted is None:
                out(lv.redactor, "SKIP optional notification question: neither shape was accepted")
            else:
                await ask(lv, "INFO optional: did a notification arrive for shape A or B? Type a short answer, "
                              "or press Enter to skip.")
            await control_ping(lv, role_id, sent)
            if accepted is None:
                out(lv.redactor, "SKIP non-mentionable check: neither shape was accepted")
            else:
                await non_mentionable(lv, res, guild, role_id, accepted, sent, state)

            await assignment(lv, res, role_id)
        finally:
            for message_id in sent:
                try:
                    await rest.delete_message(lv.env["LIVE_CHANNEL_ID"], message_id)
                except FluxerHTTPError as exc:
                    out(lv.redactor, f"delete ping message failed: HTTP {exc.status}")
            if state.may_be_mentionable:
                try:
                    await patch_mentionable(lv, res, guild, role_id, False, "patch mentionable off (cleanup)", state)
                except Exception as exc:  # noqa: BLE001 - best effort; the type and redacted message only
                    res.fail("patch mentionable off (cleanup)", f"{type(exc).__name__}")
            try:
                await rest.remove_member_role(guild, user, role_id, reason=REASON)
            except FluxerHTTPError as exc:
                out(lv.redactor, f"cleanup: removing the test role failed: HTTP {exc.status} {exc.code}")
            out(lv.redactor, f"Manual: delete the role '{ROLE_NAME}' in the client. The script cannot delete roles.")
        return res


if __name__ == "__main__":
    main(run)
