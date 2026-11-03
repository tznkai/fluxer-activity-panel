# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only Pterodactyl host manager: power state from /resources and the next power action from /schedules.

The client reads three endpoints of the server's client API with GET, and nothing else. A suspended
server answers 409 on the read endpoints, so the details endpoint is the fallback that reports suspension.
Panel text (schedule names, command payloads) is never logged or put in an error message, and a schedule name is kept out of reprs.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiohttp

from fluxer_panel.cards import CardFieldError
from fluxer_panel.config import Config
from fluxer_panel.logs import log_event
from fluxer_panel.sources.base import POWER_KINDS, PowerAction, PowerState, SourceError

KIND = "pterodactyl"
log = logging.getLogger(__name__)

_SERVER_ID = re.compile(r"[0-9a-f]{8}")
_ENDPOINTS = ("", "/resources", "/schedules")
_STATES = {
    "offline": PowerState.OFFLINE,
    "starting": PowerState.STARTING,
    "running": PowerState.RUNNING,
    "stopping": PowerState.STOPPING,
}
_ACTIONS = ("power", "command", "backup")
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
MAX_ATTEMPTS = 3
RETRY_AFTER_CAP = 10.0
RETRY_AFTER_DEFAULT = 1.0
BACKOFF = (0.5, 1.0)


@dataclass(frozen=True)
class Task:
    sequence: int
    action: str  # "power" | "command" | "backup" | "other"
    power_payload: str | None  # only when action == "power" and the payload is allowlisted


@dataclass(frozen=True)
class Schedule:
    name: str = field(repr=False)
    is_active: bool
    next_run_at: datetime | None
    tasks: tuple[Task, ...]


class PterodactylManager:
    kind = KIND

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        api_key: str,
        *,
        sleep: Callable[[float], Any] = asyncio.sleep,
        request_timeout: aiohttp.ClientTimeout = REQUEST_TIMEOUT,
    ) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/vnd.pterodactyl.v1+json"}
        self._sleep = sleep
        self._timeout = request_timeout

    def __repr__(self) -> str:
        return "PterodactylManager()"  # never show the base URL or key

    def validate_ref(self, ref: str) -> str:
        if not _SERVER_ID.fullmatch(ref):
            raise CardFieldError("manager", "pterodactyl_ref")
        return ref

    async def _get(self, server_id: str, endpoint: str) -> Any:
        if endpoint not in _ENDPOINTS:  # explicit check, not assert (asserts vanish under -O)
            raise SourceError("bad_endpoint")
        if not _SERVER_ID.fullmatch(server_id):
            raise SourceError("bad_ref")
        url = f"{self._base}/api/client/servers/{server_id}{endpoint}"
        for attempt in range(MAX_ATTEMPTS):
            try:
                async with self._session.get(
                    url, headers=self._headers, timeout=self._timeout, allow_redirects=False
                ) as resp:
                    if resp.status == 200:
                        try:
                            return await resp.json(content_type=None)
                        except ValueError:
                            raise SourceError("malformed") from None
                    retry = resp.status == 429 or resp.status >= 500
                    delay = _delay(resp, attempt)
                    status = resp.status
            except (aiohttp.ClientError, TimeoutError):
                raise SourceError("unreachable") from None
            log_event(log, "panel_request_failed", level=logging.WARNING, status=status, kind=KIND)
            if not retry or attempt == MAX_ATTEMPTS - 1:
                raise SourceError("conflict" if status == 409 else "http")
            await self._sleep(delay)
        raise SourceError("http")

    async def power_state(self, ref: str) -> PowerState:
        """Never raises. A bad ref gives Unknown with no request."""
        if not _SERVER_ID.fullmatch(ref):
            return PowerState.UNKNOWN
        try:
            try:
                resources = await self._get(ref, "/resources")
            except SourceError as exc:
                if exc.reason != "conflict":
                    raise
                details = await self._get(ref, "")
                return PowerState.SUSPENDED if details["attributes"]["is_suspended"] is True else PowerState.UNKNOWN
            attributes = resources["attributes"]
            if attributes["is_suspended"] is True:
                return PowerState.SUSPENDED
            return _STATES.get(attributes["current_state"], PowerState.UNKNOWN)
        except (SourceError, KeyError, TypeError):
            return PowerState.UNKNOWN

    async def next_power_action(self, ref: str, now: datetime | None = None) -> PowerAction | None:
        """May raise SourceError; the caller hides the line."""
        doc = await self._get(ref, "/schedules")
        return select_next(parse_schedules(doc), datetime.now(UTC) if now is None else now)


def _delay(resp: aiohttp.ClientResponse, attempt: int) -> float:
    if resp.status == 429:
        return _retry_after(resp.headers.get("Retry-After"))
    return BACKOFF[min(attempt, len(BACKOFF) - 1)]


def _retry_after(raw: str | None) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return RETRY_AFTER_DEFAULT
    if not math.isfinite(value) or value < 0:
        return RETRY_AFTER_DEFAULT
    return min(value, RETRY_AFTER_CAP)


def parse_schedules(doc: object) -> list[Schedule]:
    """Read only the fields the plugin needs. Any structural problem raises SourceError("malformed")."""
    try:
        return [_parse_schedule(item["attributes"]) for item in doc["data"]]
    except (KeyError, TypeError):
        raise SourceError("malformed") from None


def _parse_schedule(attributes: dict[str, Any]) -> Schedule:
    name = attributes["name"]
    if not isinstance(name, str):
        raise SourceError("malformed")
    tasks = tuple(_parse_task(item["attributes"]) for item in attributes["relationships"]["tasks"]["data"])
    return Schedule(
        name=name,
        is_active=attributes["is_active"] is True,
        next_run_at=_parse_when(attributes["next_run_at"]),
        tasks=tasks,
    )


def _parse_task(attributes: dict[str, Any]) -> Task:
    sequence = attributes["sequence_id"]
    if not isinstance(sequence, int) or isinstance(sequence, bool):
        raise SourceError("malformed")
    action = attributes["action"] if attributes["action"] in _ACTIONS else "other"
    # The raw payload is read inline and returned only when it is an allowlisted power kind.
    power_payload = (
        attributes["payload"] if action == "power" and attributes["payload"] in POWER_KINDS else None
    )
    return Task(sequence=sequence, action=action, power_payload=power_payload)


def _parse_when(value: object) -> datetime | None:
    """A time with an offset, or None. A naive time or a parse error is never given a zone."""
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def select_next(schedules: Iterable[Schedule], now: datetime) -> PowerAction | None:
    best: PowerAction | None = None
    for schedule in schedules:
        if not schedule.is_active or schedule.next_run_at is None or schedule.next_run_at < now:
            continue
        first_power = next((t for t in sorted(schedule.tasks, key=lambda t: t.sequence) if t.action == "power"), None)
        if first_power is None or first_power.power_payload is None:
            continue
        if best is None or schedule.next_run_at < best.at:
            best = PowerAction(kind=first_power.power_payload, at=schedule.next_run_at)
    return best


def create(session: aiohttp.ClientSession, config: Config) -> PterodactylManager | None:
    if not config.pterodactyl_configured:
        return None
    return PterodactylManager(session, config.pterodactyl_url, config.pterodactyl_api_key)
