# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Result types and plugin protocols shared by every source plugin."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class PowerState(enum.Enum):
    SUSPENDED = "suspended"
    OFFLINE = "offline"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    UNKNOWN = "unknown"


POWER_KINDS = ("start", "stop", "restart", "kill")


@dataclass(frozen=True)
class PowerAction:
    kind: str  # one of POWER_KINDS
    at: datetime  # timezone-aware; keeps the Panel's own offset


@dataclass(frozen=True)
class PlayerCounts:
    online: int
    max: int


@dataclass(frozen=True)
class ProbeDetails:
    map: str | None = None
    version: str | None = None
    game_mode: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    counts: PlayerCounts
    details: ProbeDetails


@dataclass(frozen=True)
class ProbeFailed:
    reason: str  # "timeout" | "refused" | "malformed" | "error"


class SourceError(Exception):
    """Fixed-text failure from a source; carries a reason key, never a body or a library message."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HostManager(Protocol):
    kind: str

    def validate_ref(self, ref: str) -> str:
        """Return the ref, or raise cards.CardFieldError("manager", rule)."""
        ...

    async def power_state(self, ref: str) -> PowerState:
        """Never raises."""
        ...

    async def next_power_action(self, ref: str, now: datetime | None = None) -> PowerAction | None:
        """May raise SourceError. `now` defaults to wall-clock UTC."""
        ...


class Probe(Protocol):
    kind: str

    async def probe(self, host: str, port: int, timeout: float) -> ProbeResult | ProbeFailed:
        """Never raises."""
        ...


DETAIL_MAX = 48
_MC_CODE = re.compile("§.?", re.S)


def sanitize_detail(value: object) -> str | None:
    """Strip Minecraft colour codes and control characters, collapse whitespace, cap at DETAIL_MAX."""
    if not isinstance(value, str):
        return None
    text = _MC_CODE.sub("", value)
    text = "".join(ch for ch in text if ch.isprintable())
    text = " ".join(text.split())
    return text[:DETAIL_MAX] or None
