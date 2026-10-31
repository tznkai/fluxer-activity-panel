# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run live check: edit content, embeds and both; probe what null content does and the rapid-edit limit.

Run from the project root: .venv/bin/python tools/live/edit.py (needs .env; not run by CI).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from _live import Redactor, Results, live, main, out
from fluxer_client.rest import FluxerHTTPError, FluxerREST

RAPID = 6


def has_fields(reply: Any, *, content: str | None = None, embed: str | None = None) -> bool:
    if not isinstance(reply, dict):
        return False
    if content is not None and reply.get("content") != content:
        return False
    if embed is not None:
        embeds = reply.get("embeds")
        if not isinstance(embeds, list) or not any(isinstance(e, dict) and e.get("description") == embed
                                                   for e in embeds):
            return False
    return True


async def check(res: Results, redactor: Redactor, name: str, call: Callable[[], Awaitable[Any]],
                verify: Callable[[Any], bool]) -> None:
    try:
        reply = await call()
    except FluxerHTTPError as exc:
        res.fail(name, f"HTTP {exc.status} {exc.code}")
        return
    if verify(reply):
        res.ok(name)
    else:
        res.fail(name, f"returned fields did not match; shape {redactor.shape(reply)}")


async def run() -> Results:
    async with live(gateway=False) as lv:
        res = Results(lv.redactor)
        rest, redactor = lv.rest, lv.redactor
        channel = lv.env["LIVE_CHANNEL_ID"]
        sent = await rest.send_message(channel, content="live edit test v0")
        mid = str(sent["id"])
        redactor.label(mid, "message")

        await check(res, redactor, "edit content",
                    lambda: rest.edit_message(channel, mid, content="live edit test v1"),
                    lambda r: has_fields(r, content="live edit test v1"))
        await check(res, redactor, "edit embed",
                    lambda: rest.edit_message(channel, mid, embeds=[{"description": "embed v1"}]),
                    lambda r: has_fields(r, content="live edit test v1", embed="embed v1"))
        await check(res, redactor, "edit content and embed",
                    lambda: rest.edit_message(channel, mid, content="live edit test v2",
                                              embeds=[{"description": "embed v2"}]),
                    lambda r: has_fields(r, content="live edit test v2", embed="embed v2"))

        # Informational: the docs do not say what null content does.
        try:
            reply = await rest.edit_message(channel, mid, content=None, embeds=[{"description": "embed only"}])
        except FluxerHTTPError as exc:
            out(redactor, f"content=null: HTTP {exc.status} {exc.code}")
        else:
            content = reply.get("content") if isinstance(reply, dict) else None
            out(redactor, "content=null: ok; content shape", redactor.shape({"content": content}))

        # Informational: raw 429s, so the client's own retry does not hide them.
        raw = FluxerREST(lv.session, rest.api_base, lv.env["FLUXER_TOKEN"], max_retries=0)
        first_429: int | None = None
        for i in range(RAPID):
            try:
                await raw.edit_message(channel, mid, content=f"rapid {i}")
            except FluxerHTTPError as exc:
                out(redactor, f"rapid edit {i}: HTTP {exc.status} {exc.code}")
                if exc.status == 429 and first_429 is None:
                    first_429 = i
            else:
                out(redactor, f"rapid edit {i}: ok")
        if first_429 is None:
            out(redactor, f"no rate limit in {RAPID} edits")
        else:
            out(redactor, f"rate limited after {first_429} edits")
        return res


if __name__ == "__main__":
    main(run)
