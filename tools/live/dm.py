# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run live check: open a DM, send to it, receive the reply as a guild-less MESSAGE_CREATE, re-open the same channel.

Run from the project root: .venv/bin/python tools/live/dm.py (needs .env; not run by CI).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from _live import WAIT, Results, live, main, out
from fluxer_client.events import MessageCreate, parse

PROMPT = "live DM test: reply to this message with anything"


def from_user_in(channel_id: str, user_id: str) -> Callable[[str, dict[str, Any]], bool]:
    def where(_t: str, d: dict[str, Any]) -> bool:
        author = d.get("author")
        return (str(d.get("channel_id")) == channel_id and isinstance(author, dict)
                and str(author.get("id")) == user_id)
    return where


async def run() -> Results:
    async with live(gateway=True) as lv:
        res = Results(lv.redactor)
        user = lv.env["LIVE_USER_ID"]

        dm = await lv.rest.open_dm(user)
        lv.redactor.label(dm, "dm")
        sent = await lv.rest.send_message(dm, content=PROMPT)
        if isinstance(sent, dict) and sent.get("id"):
            res.ok("dm send")
            try:
                t, d = await lv.next_event("MESSAGE_CREATE", where=from_user_in(dm, user))
            except TimeoutError:
                res.fail("dm reply received", f"no event within {WAIT:.0f}s")
            else:
                out(lv.redactor, "dm reply shape:", lv.redactor.shape(d))
                parsed = parse(t, d)
                guildless = d.get("guild_id") is None
                if guildless and isinstance(parsed, MessageCreate) and parsed.guild_id is None:
                    res.ok("dm reply received as guild-less MESSAGE_CREATE")
                else:
                    res.fail("dm reply received as guild-less MESSAGE_CREATE",
                             "payload had a guild_id or did not parse as MessageCreate")
        else:
            res.fail("dm send", "no message id returned; reply not awaited")

        again = await lv.rest.open_dm(user)
        if again == dm:
            res.ok("re-open returns the same DM channel")
        else:
            res.fail("re-open returns the same DM channel", "different channel id returned")
        return res


if __name__ == "__main__":
    main(run)
