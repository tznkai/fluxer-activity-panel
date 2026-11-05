# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run live check for the channel's last-message pointer: which message last_message_id names after two sends,
after deleting the newer one, and which message get_messages returns first. Then a delete-and-repost sequence, with
the operator's report on the unread marker and notifications on web and mobile.

Run from the project root: .venv/bin/python tools/live/board_last.py (needs .env; not run by CI).
Only the test messages this script posts are deleted, in a finally.
"""
from __future__ import annotations

from typing import Any

from _live import Live, Results, live, main, out, wait_for_enter
from fluxer_client.rest import FluxerHTTPError

NO_MENTIONS: dict[str, list[str]] = {"parse": []}
NOT_FOUND = 404


async def send(lv: Live, channel: str, text: str, name: str, names: dict[str, str], pending: set[str]) -> str:
    """Posts one test message, labels it by its letter and tracks it for the cleanup. Returns its id."""
    message = await lv.rest.send_message(channel, content=text, allowed_mentions=NO_MENTIONS)
    message_id = str(message["id"])
    lv.redactor.label(message_id, f"msg-{name.lower()}")
    names[message_id] = name
    pending.add(message_id)
    return message_id


async def delete_one(lv: Live, channel: str, message_id: str, pending: set[str]) -> bool:
    """Deletes one test message. Returns True when it is gone: deleted now, or already gone (404). A 404 means it is
    no longer pending. Any other failure is printed and returns False, and the message stays pending."""
    try:
        await lv.rest.delete_message(channel, message_id)
    except FluxerHTTPError as exc:
        if exc.status != NOT_FOUND:
            out(lv.redactor, f"delete failed: HTTP {exc.status}")
            return False
    pending.discard(message_id)
    return True


def which(names: dict[str, str], message_id: str | None) -> str:
    """The letter of a test message, 'other' for any other id, or 'none' when there is no pointer."""
    if message_id is None:
        return "none"
    return names.get(message_id, "other")


async def report_last(lv: Live, channel: str, names: dict[str, str], when: str) -> None:
    """Prints which test message the channel's last_message_id names at this point."""
    info = await lv.rest.get_channel(channel)
    raw = info.get("last_message_id")
    pointer = str(raw) if raw is not None else None
    out(lv.redactor, f"INFO last_message_id {when}: {which(names, pointer)}")


async def sequence(lv: Live, res: Results, channel: str, names: dict[str, str], pending: set[str]) -> None:
    """The A, B, delete-B, get_messages and delete-and-repost steps. Raises FluxerHTTPError on a refused send."""
    await send(lv, channel, "live test: board last A", "A", names, pending)
    b_id = await send(lv, channel, "live test: board last B", "B", names, pending)
    await report_last(lv, channel, names, "after sending A then B")

    if await delete_one(lv, channel, b_id, pending):
        await report_last(lv, channel, names, "after deleting B")
    else:
        res.fail("delete B", "message B was not deleted, so the after-delete pointer report is skipped")

    messages: list[dict[str, Any]] = await lv.rest.get_messages(channel, limit=1)
    labels = [which(names, str(m.get("id"))) for m in messages]
    out(lv.redactor, f"INFO get_messages limit=1: {len(messages)} message(s), label={labels}")

    c_id = await send(lv, channel, "live test: board last C (board)", "C", names, pending)
    await send(lv, channel, "live test: board last D (below the board)", "D", names, pending)
    await send(lv, channel, "live test: board last C2 (reposted board)", "C2", names, pending)
    if await delete_one(lv, channel, c_id, pending):
        await report_last(lv, channel, names, "after the repost and deleting C")
    else:
        res.fail("delete C", "message C was not deleted, so the after-repost pointer report is skipped")

    out(lv.redactor, "Compare the unread marker and notifications for the test messages on web and on mobile.")
    out(lv.redactor, "Type a short answer and press Enter.")
    answer = (await wait_for_enter()).strip()
    out(lv.redactor, "operator:", answer or "(no answer)")


async def run() -> Results:
    async with live(gateway=False) as lv:
        res = Results(lv.redactor)
        channel = lv.env["LIVE_CHANNEL_ID"]
        names: dict[str, str] = {}
        pending: set[str] = set()
        try:
            await sequence(lv, res, channel, names, pending)
        except FluxerHTTPError as exc:
            res.fail("board last sequence", f"HTTP {exc.status} {exc.code}")
        finally:
            for message_id in list(pending):
                await delete_one(lv, channel, message_id, pending)
            if pending:
                out(lv.redactor, f"cleanup: {len(pending)} test message(s) could not be deleted")
        return res


if __name__ == "__main__":
    main(run)
