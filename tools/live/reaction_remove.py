# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run live check: the bot removes the user's reaction with Manage Messages and gets 403 without it.

Run from the project root: .venv/bin/python tools/live/reaction_remove.py (needs .env; not run by CI).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from _live import WAIT, Live, Results, live, main, out
from fluxer_client.events import Emoji, ReactionAdd, ReactionRemove, parse
from fluxer_client.rest import FluxerHTTPError

PROMPT = "react with 👍, then wait"
MANUAL = "Now remove the bot's Manage Messages permission on this channel, react again, then press Enter"
RESTORE = "Restore the permission."
REMOVE_EVENT_WAIT = 10.0


def emoji_path(emoji: Emoji) -> str:
    """name:id for custom emoji, otherwise the unicode name."""
    return f"{emoji.name}:{emoji.id}" if emoji.id else emoji.name


def on_reaction(channel_id: str, message_id: str, user_id: str) -> Callable[[str, dict[str, Any]], bool]:
    def where(_t: str, d: dict[str, Any]) -> bool:
        return (str(d.get("channel_id")) == channel_id and str(d.get("message_id")) == message_id
                and str(d.get("user_id")) == user_id)
    return where


async def wait_add(lv: Live, res: Results, name: str, channel_id: str, message_id: str) -> str | None:
    """The user's reaction add on the message; returns the emoji path, or None after recording a FAIL."""
    try:
        t, d = await lv.next_event("MESSAGE_REACTION_ADD",
                                   where=on_reaction(channel_id, message_id, lv.env["LIVE_USER_ID"]))
    except TimeoutError:
        res.fail(name, f"no event within {WAIT:.0f}s")
        return None
    parsed = parse(t, d)
    out(lv.redactor, f"{name} shape:", lv.redactor.shape(d))
    if not isinstance(parsed, ReactionAdd):
        res.fail(name, "event did not parse as ReactionAdd")
        return None
    res.ok(name)
    return emoji_path(parsed.emoji)


async def report_remove_event(lv: Live, channel_id: str, message_id: str) -> None:
    """Informational only: the gate is the removal call and the 403, so a missing event never fails the run."""
    try:
        t, d = await lv.next_event("MESSAGE_REACTION_REMOVE",
                                   timeout=REMOVE_EVENT_WAIT,
                                   where=on_reaction(channel_id, message_id, lv.env["LIVE_USER_ID"]))
    except TimeoutError:
        out(lv.redactor, f"INFO remove event: not within {REMOVE_EVENT_WAIT:.0f}s")
        return
    parsed = "parsed as ReactionRemove" if isinstance(parse(t, d), ReactionRemove) else "did not parse as ReactionRemove"
    out(lv.redactor, f"INFO remove event: arrived, {parsed}; shape:", lv.redactor.shape(d))


async def run() -> Results:
    async with live(gateway=True) as lv:
        res = Results(lv.redactor)
        rest, redactor = lv.rest, lv.redactor
        channel = lv.env["LIVE_CHANNEL_ID"]
        user = lv.env["LIVE_USER_ID"]
        sent = await rest.send_message(channel, content=PROMPT)
        message_id = str(sent["id"])
        redactor.label(message_id, "message")

        emoji = await wait_add(lv, res, "user reaction add", channel, message_id)
        if emoji is None:
            out(redactor, "SKIP remove steps: no reaction observed")
            return res

        try:
            await rest.remove_reaction(channel, message_id, emoji, user)
        except FluxerHTTPError as exc:
            res.fail("remove user reaction", f"HTTP {exc.status} {exc.code}")
        else:
            res.ok("remove user reaction")
        await report_remove_event(lv, channel, message_id)

        out(redactor, MANUAL)
        await asyncio.to_thread(input)
        try:
            again = await wait_add(lv, res, "re-react after permission removed", channel, message_id)
            if again is None:
                out(redactor, "SKIP remove without permission: no reaction observed")
                return res
            try:
                await rest.remove_reaction(channel, message_id, again, user)
            except FluxerHTTPError as exc:
                if exc.status == 403:
                    res.ok(f"remove without Manage Messages is 403 (code {exc.code})")
                else:
                    res.fail("remove without Manage Messages",
                             f"expected 403, got HTTP {exc.status} {exc.code}")
            else:
                res.fail("remove without Manage Messages", "removal succeeded; expected 403")
        finally:
            out(redactor, RESTORE)
        return res


if __name__ == "__main__":
    main(run)
