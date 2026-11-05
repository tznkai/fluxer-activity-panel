# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run live check: the bot seeds three reactions on a guild message, and the script records the echo, the
bot flag, the stored emoji names, and whether reactions still arrive after an edit.

Run from the project root: .venv/bin/python tools/live/seed_reactions.py (needs .env; not run by CI).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from _live import WAIT, Live, Results, live, main, out, wait_for_enter
from fluxer_client.events import ReactionAdd, parse
from fluxer_client.rest import FluxerHTTPError

SEED_TEXT = "live test: reaction seeding"
EDITED_TEXT = "live test: reaction seeding (edited)"
SEED_EMOJI = ("\u25b6\ufe0f", "\U0001F514", "\U0001F64B")   # play, bell, raised hand
PLAY_PROMPT = "U+25B6 U+FE0F"   # the play button, named by code point so no raw emoji is printed
PLAY_BASE = "\u25b6"            # the play button without its variation selector
ECHO_WAIT = 10.0
NO_MENTIONS: dict[str, list[str]] = {"parse": []}


def code_points(name: str) -> str:
    """The emoji name as code points. Output never carries the raw emoji text."""
    return " ".join(f"U+{ord(ch):04X}" for ch in name)


def on_reaction(channel_id: str, message_id: str, user_id: str) -> Callable[[str, dict[str, Any]], bool]:
    def where(_t: str, d: dict[str, Any]) -> bool:
        return (str(d.get("channel_id")) == channel_id and str(d.get("message_id")) == message_id
                and str(d.get("user_id")) == user_id)
    return where


def normalise(name: str) -> str:
    """Drops U+FE0F, so the play button matches with or without the variation selector."""
    return name.replace("\ufe0f", "")


def on_play_add(channel_id: str, message_id: str, user_id: str) -> Callable[[str, dict[str, Any]], bool]:
    """The operator's add on this message that is the play button. An earlier leftover add of another emoji is skipped."""
    on_message = on_reaction(channel_id, message_id, user_id)

    def where(t: str, d: dict[str, Any]) -> bool:
        if not on_message(t, d):
            return False
        parsed = parse(t, d)
        return isinstance(parsed, ReactionAdd) and normalise(parsed.emoji.name) == PLAY_BASE
    return where


def bot_flag(d: dict[str, Any]) -> str:
    """Whether the reactor's member.user carries a bot key, and its value. Only the presence and value are printed."""
    member = d.get("member")
    user = member.get("user") if isinstance(member, dict) else None
    if not isinstance(user, dict) or "bot" not in user:
        return "bot flag: absent"
    return f"bot flag: present, value={user['bot']}"


async def wait_echo(lv: Live, channel: str, message_id: str, bot_id: str, index: int) -> None:
    """Informational only: a missing echo is recorded as INFO, not as a failure."""
    try:
        t, d = await lv.next_event("MESSAGE_REACTION_ADD", timeout=ECHO_WAIT,
                                   where=on_reaction(channel, message_id, bot_id))
    except TimeoutError:
        out(lv.redactor, f"INFO seed {index}: no echo within {ECHO_WAIT:.0f}s")
        return
    parsed = parse(t, d)
    stored = code_points(parsed.emoji.name) if isinstance(parsed, ReactionAdd) else "did not parse as ReactionAdd"
    out(lv.redactor, f"INFO seed {index}: echo seen ({stored})", bot_flag(d))
    out(lv.redactor, f"INFO seed {index}: echo shape:", lv.redactor.shape(d))


async def seed_one(lv: Live, res: Results, index: int, emoji: str, channel: str, message_id: str,
                   bot_id: str) -> None:
    name = f"seed {index}"
    try:
        await lv.rest.add_reaction(channel, message_id, emoji)
    except FluxerHTTPError as exc:
        res.fail(name, f"HTTP {exc.status}")
        return
    res.ok(name)
    await wait_echo(lv, channel, message_id, bot_id, index)


async def report_stored(lv: Live, channel: str, message_id: str) -> None:
    message = await lv.rest.get_message(channel, message_id)
    reactions = message.get("reactions") or []
    if not reactions:
        out(lv.redactor, "INFO stored reactions: none")
    for entry in reactions:
        name = (entry.get("emoji") or {}).get("name") or ""
        out(lv.redactor, "INFO stored reaction:", code_points(name), f"count={entry.get('count')}",
            f"me={entry.get('me')}")


async def react_after_edit(lv: Live, res: Results, channel: str, message_id: str, user: str) -> None:
    try:
        await lv.rest.edit_message(channel, message_id, content=EDITED_TEXT, allowed_mentions=NO_MENTIONS)
    except FluxerHTTPError as exc:
        res.fail("edit seed message", f"HTTP {exc.status}")
    out(lv.redactor, "React to the edited message with any emoji, then press Enter.")
    await wait_for_enter()
    try:
        await lv.next_event("MESSAGE_REACTION_ADD", where=on_reaction(channel, message_id, user))
    except TimeoutError:
        res.fail("reaction after edit", f"no event within {WAIT:.0f}s")
        return
    res.ok("reaction after edit")

    out(lv.redactor, f"Add the play button ({PLAY_PROMPT}) to the edited message yourself, then press Enter.")
    await wait_for_enter()
    try:
        t, d = await lv.next_event("MESSAGE_REACTION_ADD", where=on_play_add(channel, message_id, user))
    except TimeoutError:
        out(lv.redactor, f"INFO operator play add: no play-button event within {WAIT:.0f}s")
        return
    parsed = parse(t, d)
    if isinstance(parsed, ReactionAdd):
        out(lv.redactor, "INFO operator play add stored as:", code_points(parsed.emoji.name))
    else:
        out(lv.redactor, "INFO operator play add: did not parse as ReactionAdd")


async def run() -> Results:
    async with live(gateway=True) as lv:
        res = Results(lv.redactor)
        rest = lv.rest
        channel = lv.env["LIVE_CHANNEL_ID"]
        user = lv.env["LIVE_USER_ID"]
        bot_id = lv.bot_user_id
        if bot_id is None:
            raise RuntimeError("the bot user id is not known after READY")
        sent = await rest.send_message(channel, content=SEED_TEXT, allowed_mentions=NO_MENTIONS)
        message_id = str(sent["id"])
        lv.redactor.label(message_id, "message")
        try:
            for index, emoji in enumerate(SEED_EMOJI, start=1):
                await seed_one(lv, res, index, emoji, channel, message_id, bot_id)
            await report_stored(lv, channel, message_id)
            await react_after_edit(lv, res, channel, message_id, user)
        finally:
            try:
                await rest.delete_message(channel, message_id)
            except FluxerHTTPError as exc:
                out(lv.redactor, f"delete failed: HTTP {exc.status}")
        return res


if __name__ == "__main__":
    main(run)
