# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared harness for the hand-run live capability scripts. Never prints the token or raw ids."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

import aiohttp

from fluxer_client.gateway import Gateway
from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerHTTPError, FluxerREST, discover_api

PROJECT = Path(__file__).resolve().parents[2]
REQUIRED = ("FLUXER_TOKEN", "LIVE_GUILD_ID", "LIVE_CHANNEL_ID", "LIVE_USER_ID")
LONG_NUMBER = re.compile(r"\d{15,}")
MAX_MEMBERS = 2
WAIT = 120.0   # seconds a script waits for the human to act
SHOW_KEYS = frozenset({"type", "t", "code", "animated", "bot"})
NUMBERED_KINDS = frozenset({"message"})

_redactor: Redactor | None = None   # set by live(); used by main() for error output


class LiveRefused(Exception):
    """Safety check failed; the message names the check, never a value."""


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; '#' comments and blanks skipped; surrounding quotes stripped. No interpolation."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env(path: Path | None = None, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """os.environ wins over .env. Raises LiveRefused naming (not showing) a missing/invalid variable.
    Requires FLUXER_TOKEN, FLUXER_INSTANCE_URL or FLUXER_API_URL, and LIVE_GUILD_ID/CHANNEL_ID/USER_ID
    as snowflakes (LIVE_ROLE_ID / LIVE_HIGH_ROLE_ID are validated if set)."""
    env_file = PROJECT / ".env" if path is None else path
    merged = {**read_env_file(env_file), **(dict(os.environ) if environ is None else environ)}
    if not merged.get("FLUXER_TOKEN"):
        raise LiveRefused("FLUXER_TOKEN is missing")
    if not (merged.get("FLUXER_INSTANCE_URL") or merged.get("FLUXER_API_URL")):
        raise LiveRefused("FLUXER_INSTANCE_URL or FLUXER_API_URL is missing")
    for name in ("LIVE_GUILD_ID", "LIVE_CHANNEL_ID", "LIVE_USER_ID"):
        if not is_snowflake(merged.get(name)):
            raise LiveRefused(f"{name} is missing or not a snowflake id")
    for name in ("LIVE_ROLE_ID", "LIVE_HIGH_ROLE_ID"):
        value = merged.get(name)
        if value and not is_snowflake(value):
            raise LiveRefused(f"{name} is not a snowflake id")
    return merged


@dataclass
class Redactor:
    """Maps known ids to stable placeholders; any other 15-25 digit run becomes <id>; the token becomes [REDACTED]."""
    token: str = field(repr=False)
    labels: dict[str, str] = field(default_factory=dict)   # id -> "<guild>", "<channel>", "<user>", "<role>", "<message:1>", ...
    _counts: dict[str, int] = field(default_factory=dict, repr=False)

    def label(self, snowflake: str, kind: str) -> None:
        if snowflake in self.labels:
            return
        if kind in NUMBERED_KINDS:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self.labels[snowflake] = f"<{kind}:{self._counts[kind]}>"
        else:
            self.labels[snowflake] = f"<{kind}>"

    def text(self, s: str) -> str:
        if self.token:
            s = s.replace(self.token, "[REDACTED]")
        for snowflake in sorted(self.labels, key=len, reverse=True):
            s = re.sub(rf"(?<!\d){re.escape(snowflake)}(?!\d)", self.labels[snowflake], s)
        return LONG_NUMBER.sub("<id>", s)

    def shape(self, value: Any, parent: str | None = None) -> Any:
        """Structure of a payload with values hidden: snowflake strings -> label/<id>; other strings -> "<str len=N>"
        except for keys in SHOW_KEYS, and 'name' only when the parent key is 'emoji' (guild/channel/role/user
        names stay hidden); numbers/bools kept; dict/list recursed, passing the key down as `parent`."""
        if isinstance(value, dict):
            shaped: dict[str, Any] = {}
            for key, child in value.items():
                if key == "name" and parent == "emoji" and isinstance(child, str):
                    shaped[key] = self.text(child)
                else:
                    shaped[key] = self.shape(child, parent=key)
            return shaped
        if isinstance(value, list):
            return [self.shape(item, parent=parent) for item in value]
        if isinstance(value, str):
            if is_snowflake(value):
                return self.labels.get(value, "<id>")
            if parent in SHOW_KEYS:
                return self.text(value)
            return f"<str len={len(value)}>"
        return value

    def log_handler(self, stream: TextIO | None = None) -> logging.Handler:
        """A stderr handler whose formatter passes the fully formatted record (message + traceback) through
        self.text(), so fluxer_client warnings (e.g. 'rate limited on PUT /channels/<id>/...') never print raw
        ids or the token."""
        redactor = self

        class _RedactingFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                return redactor.text(super().format(record))

        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        handler.setFormatter(_RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
        return handler


def out(redactor: Redactor, *parts: object) -> None:
    """The only way scripts print: every part passes through redactor.text()."""
    print(redactor.text(" ".join(str(p) for p in parts)), flush=True)


class Results:
    """PASS/FAIL lines for one script. Any FAIL sets `failed`, which makes the script exit 1."""

    def __init__(self, redactor: Redactor) -> None:
        self.redactor = redactor
        self.failed = False

    def ok(self, name: str) -> None:
        out(self.redactor, f"PASS {name}")

    def fail(self, name: str, detail: str) -> None:
        self.failed = True
        out(self.redactor, f"FAIL {name}: {detail}")


@dataclass
class Live:
    env: dict[str, str] = field(repr=False)
    session: aiohttp.ClientSession
    rest: FluxerREST
    redactor: Redactor
    version: dict[str, Any]          # {"x_fluxer_version": str|None, "api_code_version": int|None}
    events: asyncio.Queue[tuple[str, dict[str, Any]]]
    gateway: Gateway | None = None

    seen_other: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def next_event(self, *types: str, timeout: float = WAIT, where: Callable[[str, dict[str, Any]], bool] | None = None
                         ) -> tuple[str, dict[str, Any]]:
        """Wait for the next dispatch whose type is in `types` and for which where(t, d) is true.
        Non-matching dispatches are appended to `seen_other` (not discarded) so scripts can report them."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("no matching event before the deadline")
            event_type, data = await asyncio.wait_for(self.events.get(), remaining)
            if event_type in types and (where is None or where(event_type, data)):
                return event_type, data
            self.seen_other.append((event_type, data))


async def check_safety(rest: FluxerREST, env: Mapping[str, str]) -> None:
    """Refuse unless GET /channels/{LIVE_CHANNEL_ID}.guild_id == LIVE_GUILD_ID, GET /guilds/{LIVE_GUILD_ID}/members/
    {LIVE_USER_ID} succeeds (a 404 means a mistyped user id: never DM a stranger), and, if GET /guilds/{LIVE_GUILD_ID}
    reports member_count, it is <= MAX_MEMBERS. Uses rest.request('GET', ...) / rest.get_member directly."""
    guild_id, channel_id, user_id = env["LIVE_GUILD_ID"], env["LIVE_CHANNEL_ID"], env["LIVE_USER_ID"]
    channel = await rest.request("GET", f"/channels/{channel_id}")
    if not isinstance(channel, dict) or channel.get("guild_id") is None or str(channel["guild_id"]) != guild_id:
        raise LiveRefused("LIVE_CHANNEL_ID is not in LIVE_GUILD_ID")
    try:
        await rest.get_member(guild_id, user_id)
    except FluxerHTTPError as exc:
        if exc.status == 404:
            raise LiveRefused("LIVE_USER_ID is not a member of LIVE_GUILD_ID") from None
        raise
    guild = await rest.request("GET", f"/guilds/{guild_id}")
    count = guild.get("member_count") if isinstance(guild, dict) else None
    if isinstance(count, int) and count > MAX_MEMBERS:
        raise LiveRefused("guild has more than 2 members")


async def capture_version(session: aiohttp.ClientSession, env: Mapping[str, str], api_base: str) -> dict[str, Any]:
    """X-Fluxer-Version from a GET of {api_base}/v1/gateway/bot (Authorization: Bot <token>, never printed) and
    api_code_version from {FLUXER_INSTANCE_URL}/.well-known/fluxer when the instance URL is set."""
    headers = {"Authorization": f"Bot {env['FLUXER_TOKEN']}"}
    async with session.get(api_base.rstrip("/") + "/v1/gateway/bot", headers=headers) as resp:
        x_version = resp.headers.get("X-Fluxer-Version")
    api_code_version: int | None = None
    instance = env.get("FLUXER_INSTANCE_URL")
    if instance:
        async with session.get(instance.rstrip("/") + "/.well-known/fluxer") as resp:
            data = await resp.json(content_type=None)
        code = data.get("api_code_version") if isinstance(data, dict) else None
        if isinstance(code, int) and not isinstance(code, bool):
            api_code_version = code
    return {"x_fluxer_version": x_version, "api_code_version": api_code_version}


@contextlib.asynccontextmanager
async def live(*, gateway: bool) -> AsyncIterator[Live]:
    """install redactor.log_handler() as the only root handler (level WARNING) -> load_env -> session -> api base (FLUXER_API_URL or discover_api) -> FluxerREST -> check_safety ->
    capture_version -> label the env ids -> print the version block -> optionally start a Gateway whose
    on_dispatch puts (t, d) on the queue and wait for READY (label the bot user as <bot>). On exit: gateway.stop()."""
    global _redactor
    env = load_env()
    redactor = Redactor(token=env["FLUXER_TOKEN"])
    _redactor = redactor
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = [redactor.log_handler()]
    root.setLevel(logging.WARNING)
    gw: Gateway | None = None
    gw_task: asyncio.Task[None] | None = None
    try:
        async with aiohttp.ClientSession() as session:
            api_base = env.get("FLUXER_API_URL") or await discover_api(session, env["FLUXER_INSTANCE_URL"])
            rest = FluxerREST(session, api_base, env["FLUXER_TOKEN"])
            await check_safety(rest, env)
            version = await capture_version(session, env, api_base)
            redactor.label(env["LIVE_GUILD_ID"], "guild")
            redactor.label(env["LIVE_CHANNEL_ID"], "channel")
            redactor.label(env["LIVE_USER_ID"], "user")
            for name, kind in (("LIVE_ROLE_ID", "role"), ("LIVE_HIGH_ROLE_ID", "high-role")):
                if env.get(name):
                    redactor.label(env[name], kind)
            out(redactor, "Fluxer version:", f"X-Fluxer-Version={version['x_fluxer_version']}",
                f"api_code_version={version['api_code_version']}")

            events: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
            lv = Live(env=env, session=session, rest=rest, redactor=redactor, version=version, events=events)
            if gateway:
                ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()

                async def on_dispatch(event_type: str, data: dict[str, Any]) -> None:
                    await events.put((event_type, data))

                def on_ready(bot_user_id: str) -> None:
                    redactor.label(bot_user_id, "bot")
                    if not ready.done():
                        ready.set_result(None)

                gateway_url = env.get("FLUXER_GATEWAY_URL") or await rest.get_gateway_url()
                gw = Gateway(session, gateway_url, env["FLUXER_TOKEN"], on_dispatch, on_ready=on_ready)
                lv.gateway = gw
                gw_task = asyncio.create_task(gw.run())
                done, _ = await asyncio.wait({ready, gw_task}, timeout=WAIT, return_when=asyncio.FIRST_COMPLETED)
                if ready not in done:
                    raise RuntimeError("gateway did not reach READY")
            yield lv
    finally:
        if gw is not None:
            await gw.stop()
        if gw_task is not None:
            gw_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await gw_task
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def main(coro_fn: Callable[[], Any]) -> None:
    """asyncio.run; LiveRefused -> print 'REFUSED: <check>' and exit 2; any FAIL from the script -> exit 1;
    other exceptions -> print the redacted type and message only (no traceback, which could hold headers) and exit 1.
    coro_fn returns a Results (or None when it has nothing to report)."""
    redactor = _redactor or Redactor(token="")
    try:
        results = asyncio.run(coro_fn())
    except LiveRefused as exc:
        out(redactor, f"REFUSED: {exc}")
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001 - reported with type and message only, never a traceback
        out(redactor, f"ERROR {type(exc).__name__}: {exc}")
        sys.exit(1)
    sys.exit(1 if results is not None and results.failed else 0)
