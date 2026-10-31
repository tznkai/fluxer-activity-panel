# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run live check: observe reaction add and remove on a guild message and in a DM.

Run from the project root: .venv/bin/python tools/live/reactions.py (needs .env; not run by CI).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from _live import WAIT, Live, Results, live, main, out
from fluxer_client.events import ReactionAdd, ReactionRemove, parse

PROMPT = "live test: react to this message with any emoji, then remove the reaction"
INFO_TYPES = frozenset({"MESSAGE_REACTION_ADD_MANY", "MESSAGE_REACTION_REMOVE_ALL", "MESSAGE_REACTION_REMOVE_EMOJI"})


def on_reaction(channel_id: str, message_id: str, user_id: str) -> Callable[[str, dict[str, Any]], bool]:
    def where(_t: str, d: dict[str, Any]) -> bool:
        return (str(d.get("channel_id")) == channel_id and str(d.get("message_id")) == message_id
                and str(d.get("user_id")) == user_id)
    return where


async def expect(lv: Live, res: Results, name: str, event_type: str, cls: type, *, channel_id: str,
                 message_id: str, want_guild: bool) -> None:
    try:
        t, d = await lv.next_event(event_type, where=on_reaction(channel_id, message_id, lv.env["LIVE_USER_ID"]))
    except TimeoutError:
        res.fail(name, f"no event within {WAIT:.0f}s")
        return
    parsed = parse(t, d)
    out(lv.redactor, f"{name} shape:", lv.redactor.shape(d))
    out(lv.redactor, f"{name} parse:", type(parsed).__name__ if parsed is not None else "None")
    if isinstance(parsed, cls) and (parsed.guild_id is not None) == want_guild:
        res.ok(name)
    else:
        res.fail(name, "event did not parse with the expected guild_id")


async def send_prompt(lv: Live, channel_id: str) -> str:
    sent = await lv.rest.send_message(channel_id, content=PROMPT)
    message_id = str(sent["id"])
    lv.redactor.label(message_id, "message")
    return message_id


async def run() -> Results:
    async with live(gateway=True) as lv:
        res = Results(lv.redactor)
        guild_channel = lv.env["LIVE_CHANNEL_ID"]
        guild_msg = await send_prompt(lv, guild_channel)
        out(lv.redactor, "guild: react to the message, then remove the reaction")
        await expect(lv, res, "guild reaction add", "MESSAGE_REACTION_ADD", ReactionAdd,
                     channel_id=guild_channel, message_id=guild_msg, want_guild=True)
        await expect(lv, res, "guild reaction remove", "MESSAGE_REACTION_REMOVE", ReactionRemove,
                     channel_id=guild_channel, message_id=guild_msg, want_guild=True)

        dm_channel = await lv.rest.open_dm(lv.env["LIVE_USER_ID"])
        lv.redactor.label(dm_channel, "dm")
        dm_msg = await send_prompt(lv, dm_channel)
        out(lv.redactor, "dm: react to the message, then remove the reaction")
        await expect(lv, res, "dm reaction add", "MESSAGE_REACTION_ADD", ReactionAdd,
                     channel_id=dm_channel, message_id=dm_msg, want_guild=False)
        await expect(lv, res, "dm reaction remove", "MESSAGE_REACTION_REMOVE", ReactionRemove,
                     channel_id=dm_channel, message_id=dm_msg, want_guild=False)

        for t, d in lv.seen_other:
            if t in INFO_TYPES:
                out(lv.redactor, f"info {t} shape:", lv.redactor.shape(d))
        return res


if __name__ == "__main__":
    main(run)
