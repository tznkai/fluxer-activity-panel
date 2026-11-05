# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run live check: the reactor list (with paging), removal of one emoji's reactions, removal of all reactions,
and a rate probe of reaction calls. Records the bot flag and whether removal events arrive.

Run from the project root: .venv/bin/python tools/live/reaction_bulk.py (needs .env; not run by CI).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from _live import Live, Results, live, main, out, wait_for_enter
from fluxer_client.rest import FluxerHTTPError, FluxerREST, encode_emoji

NO_MENTIONS: dict[str, list[str]] = {"parse": []}
TEXT = "live test: reaction bulk"
RAISED_HAND = "\U0001F64B"     # added by the bot, then by the operator
PLAY = "\u25b6\ufe0f"  # added by the bot, removed by emoji
RAISED_HAND_PROMPT = "U+1F64B"  # named by code point so no raw emoji is printed
REMOVE_EVENT_WAIT = 10.0
RATE_CALLS = 30


def code_points(name: str) -> str:
    """The emoji name as code points. Output never carries the raw emoji text."""
    return " ".join(f"U+{ord(ch):04X}" for ch in name)


def normalise(name: str) -> str:
    """Drops U+FE0F, so a stored play button matches with or without the variation selector."""
    return name.replace("\ufe0f", "")


def on_message(channel_id: str, message_id: str) -> Callable[[str, dict[str, Any]], bool]:
    def where(_t: str, d: dict[str, Any]) -> bool:
        return str(d.get("channel_id")) == channel_id and str(d.get("message_id")) == message_id
    return where


def users_path(channel: str, message: str, emoji: str, *, limit: int, after: str | None = None) -> str:
    """The reactor-list path that FluxerREST.get_reaction_users requests, built here for the raw-shape probe."""
    path = (f"/channels/{channel}/messages/{message}/reactions/{encode_emoji(emoji)}"
            f"{FluxerREST.REACTION_USERS_SUFFIX}?limit={limit}")
    if after is not None:
        path += f"&after={after}"
    return path


def reactor_label(lv: Live, user_id: str) -> str:
    """The registered label for a known id (<bot>, <user>), otherwise a stable <reactor#n> label."""
    return lv.redactor.labels.get(user_id) or lv.redactor.label_text(user_id, "reactor")


def bot_key(user: dict[str, Any]) -> str:
    """Whether the reactor object carries a bot key, and its value. Only presence and value are printed."""
    return f"bot={user['bot']}" if "bot" in user else "bot key absent"


def report_page(lv: Live, name: str, users: list[dict[str, Any]]) -> None:
    out(lv.redactor, f"INFO {name}: {len(users)} user(s)")
    for user in users:
        uid = str(user.get("id") or "")
        label = reactor_label(lv, uid) if uid else "<no id>"
        out(lv.redactor, f"INFO {name}: {label} {bot_key(user)}")


async def report_event(lv: Live, event_type: str, label: str, channel: str, message_id: str) -> None:
    """Informational only: the gate is the removal call and the stored state, so a missing event never fails."""
    try:
        await lv.next_event(event_type, timeout=REMOVE_EVENT_WAIT, where=on_message(channel, message_id))
    except TimeoutError:
        out(lv.redactor, f"INFO {label}: not within {REMOVE_EVENT_WAIT:.0f}s")
        return
    out(lv.redactor, f"INFO {label}: arrived")


async def stored_names(lv: Live, channel: str, message_id: str, label: str) -> list[str]:
    message = await lv.rest.get_message(channel, message_id)
    reactions = message.get("reactions") or []
    if not reactions:
        out(lv.redactor, f"INFO {label}: no reactions stored")
    names: list[str] = []
    for entry in reactions:
        name = (entry.get("emoji") or {}).get("name") or ""
        names.append(name)
        out(lv.redactor, f"INFO {label}: stored", code_points(name), f"count={entry.get('count')}",
            f"me={entry.get('me')}")
    return names


async def bot_adds(lv: Live, res: Results, channel: str, message_id: str) -> None:
    for index, emoji in enumerate((RAISED_HAND, PLAY), start=1):
        name = f"bot add {index}"
        try:
            await lv.rest.add_reaction(channel, message_id, emoji)
        except FluxerHTTPError as exc:
            res.fail(name, f"HTTP {exc.status}")
        else:
            res.ok(name)


def bare_path(channel: str, message: str) -> str:
    """The bare emoji path (no /users suffix), the alternative reactor-list shape to probe."""
    return f"/channels/{channel}/messages/{message}/reactions/{encode_emoji(RAISED_HAND)}?limit=1"


async def users_page(lv: Live, channel: str, message_id: str, name: str,
                     *, after: str | None = None) -> list[dict[str, Any]] | None:
    """One page of the /users reactor list. A refusal is printed with its status and returns None."""
    try:
        users = await lv.rest.get_reaction_users(channel, message_id, RAISED_HAND, limit=1, after=after)
    except FluxerHTTPError as exc:
        out(lv.redactor, f"INFO {name}: HTTP {exc.status} {exc.code}")
        return None
    report_page(lv, name, users)
    return users


async def probe_path(lv: Live, name: str, path: str) -> bool:
    """One raw GET of a reactor-list path. Prints its status, or its response shape. Returns whether it answered."""
    try:
        raw = await lv.rest.request("GET", path)
    except FluxerHTTPError as exc:
        out(lv.redactor, f"INFO {name}: HTTP {exc.status} {exc.code}")
        return False
    out(lv.redactor, f"INFO {name}: answered, shape:", lv.redactor.shape(raw))
    return True


async def list_reactors(lv: Live, res: Results, channel: str, message_id: str) -> None:
    """Probes both reactor-list paths. Each request handles its own refusal, so the removal and rate steps still run."""
    first = await users_page(lv, channel, message_id, "/users page 1 (limit=1)")
    second: list[dict[str, Any]] | None = None
    if first:
        second = await users_page(lv, channel, message_id, "/users page 2 (after page 1)",
                                  after=str(first[0]["id"]))
    await probe_path(lv, "raw /users response", users_path(channel, message_id, RAISED_HAND, limit=1))
    bare_answered = await probe_path(lv, "bare emoji path (no /users)", bare_path(channel, message_id))

    answered = [label for label, ok in (("/users", first is not None), ("bare emoji path", bare_answered)) if ok]
    out(lv.redactor, "INFO reactor list path(s) that answered:", ", ".join(answered) or "none")

    if first is None:
        res.fail("reactor list via /users", "the /users path was refused")
        return
    if not first:
        res.fail("reactor list page 1", "no users returned")
        return
    seen = {str(user.get("id")) for user in [*first, *(second or [])]}
    if str(lv.bot_user_id) in seen and lv.env["LIVE_USER_ID"] in seen:
        res.ok("reactor list has the bot and the operator")
    else:
        res.fail("reactor list has the bot and the operator", "one of them is missing from the pages fetched")


async def remove_one(lv: Live, res: Results, channel: str, message_id: str) -> None:
    try:
        await lv.rest.remove_emoji_reactions(channel, message_id, PLAY)
    except FluxerHTTPError as exc:
        res.fail("remove one emoji's reactions", f"HTTP {exc.status} {exc.code}")
        return
    res.ok("remove one emoji's reactions")
    await report_event(lv, "MESSAGE_REACTION_REMOVE_EMOJI", "remove-emoji event", channel, message_id)
    remaining = {normalise(name) for name in await stored_names(lv, channel, message_id, "after remove one emoji")}
    if normalise(PLAY) in remaining:
        res.fail("play reaction gone after remove one emoji", "still stored")
    else:
        res.ok("play reaction gone after remove one emoji")
    if normalise(RAISED_HAND) not in remaining:
        res.fail("raised hand kept after remove one emoji", "missing")


async def remove_all(lv: Live, res: Results, channel: str, message_id: str) -> None:
    try:
        await lv.rest.remove_all_reactions(channel, message_id)
    except FluxerHTTPError as exc:
        res.fail("remove all reactions", f"HTTP {exc.status} {exc.code}")
        return
    res.ok("remove all reactions")
    await report_event(lv, "MESSAGE_REACTION_REMOVE_ALL", "remove-all event", channel, message_id)
    left = await stored_names(lv, channel, message_id, "after remove all")
    if left:
        res.fail("reactions empty after remove all", f"{len(left)} left")
    else:
        res.ok("reactions empty after remove all")


async def rate_probe(lv: Live, res: Results, channel: str, message_id: str, bot_id: str) -> None:
    """Alternating add and remove of the raised hand by the bot, on a client with retries off, so a 429 surfaces."""
    client = FluxerREST(lv.session, lv.rest.api_base, lv.env["FLUXER_TOKEN"], max_retries=0)
    first_429: int | None = None
    count_429 = 0
    for index in range(RATE_CALLS):
        try:
            if index % 2 == 0:
                await client.add_reaction(channel, message_id, RAISED_HAND)
            else:
                await client.remove_reaction(channel, message_id, RAISED_HAND, bot_id)
        except FluxerHTTPError as exc:
            if exc.status != 429:
                res.fail("rate probe call", f"call {index}: HTTP {exc.status} {exc.code}")
                break
            count_429 += 1
            if first_429 is None:
                first_429 = index
                out(lv.redactor, f"INFO rate probe: first 429 at call {index}: {exc.message}")
    shown = "none" if first_429 is None else str(first_429)
    out(lv.redactor, f"INFO rate probe: {RATE_CALLS} calls, {count_429} 429(s), first 429 at call index {shown}")


async def run() -> Results:
    async with live(gateway=True) as lv:
        res = Results(lv.redactor)
        channel = lv.env["LIVE_CHANNEL_ID"]
        bot_id = lv.bot_user_id
        if bot_id is None:
            raise RuntimeError("the bot user id is not known after READY")
        sent = await lv.rest.send_message(channel, content=TEXT, allowed_mentions=NO_MENTIONS)
        message_id = str(sent["id"])
        lv.redactor.label(message_id, "message")
        try:
            await bot_adds(lv, res, channel, message_id)
            out(lv.redactor, f"Add the raised hand ({RAISED_HAND_PROMPT}) to the message yourself, then press Enter.")
            await wait_for_enter()
            await list_reactors(lv, res, channel, message_id)
            await remove_one(lv, res, channel, message_id)
            await remove_all(lv, res, channel, message_id)
            await rate_probe(lv, res, channel, message_id, bot_id)
        finally:
            try:
                await lv.rest.delete_message(channel, message_id)
            except FluxerHTTPError as exc:
                out(lv.redactor, f"delete failed: HTTP {exc.status}")
        return res


if __name__ == "__main__":
    main(run)
