# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Turns source results into one card status, and the wording for the next scheduled power action.

Pure functions only. The state tables live here and nowhere else.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta

from fluxer_panel import text
from fluxer_panel.sources.base import (
    PlayerCounts,
    PowerAction,
    PowerState,
    ProbeDetails,
    ProbeFailed,
    ProbeResult,
)

# Set once Fluxer timestamp markup is confirmed live; until then the relative and absolute forms are plain text.
USE_TIMESTAMP_MARKUP = False


class StatusKind(enum.Enum):
    UNAVAILABLE = "unavailable"
    ASLEEP = "asleep"
    WAKING = "waking"
    GOING_TO_SLEEP = "going_to_sleep"
    UNKNOWN = "unknown"
    READY = "ready"
    NOT_RESPONDING = "not_responding"


@dataclass(frozen=True)
class CardStatus:
    kind: StatusKind
    counts: PlayerCounts | None = None
    details: ProbeDetails | None = None


@dataclass(frozen=True)
class Resolution:
    status: CardStatus | None  # None means no status line
    failing_since: int | None
    alert_sent: bool  # persisted flag
    alert_now: bool  # send the DM now


_MANAGER_ONLY = {
    PowerState.SUSPENDED: StatusKind.UNAVAILABLE,
    PowerState.OFFLINE: StatusKind.ASLEEP,
    PowerState.STARTING: StatusKind.WAKING,
    PowerState.STOPPING: StatusKind.GOING_TO_SLEEP,
    PowerState.UNKNOWN: StatusKind.UNKNOWN,
}

_LABELS = {
    StatusKind.UNAVAILABLE: text.STATUS_UNAVAILABLE,
    StatusKind.ASLEEP: text.STATUS_ASLEEP,
    StatusKind.WAKING: text.STATUS_WAKING,
    StatusKind.GOING_TO_SLEEP: text.STATUS_GOING_TO_SLEEP,
    StatusKind.UNKNOWN: text.STATUS_UNKNOWN,
    StatusKind.READY: text.STATUS_READY,
    StatusKind.NOT_RESPONDING: text.STATUS_NOT_RESPONDING,
}

_NEXT_LABELS = {
    "restart": text.NEXT_RESTART,
    "stop": text.NEXT_SLEEP,
    "kill": text.NEXT_SLEEP,
    "start": text.NEXT_WAKE,
}


def resolve(
    *,
    has_manager: bool,
    power: PowerState | None,
    has_probe: bool,
    probe: ProbeResult | ProbeFailed | None,
    failing_since: int | None,
    alert_sent: bool,
    now: int,
    grace: int,
) -> Resolution:
    """Resolve one card's status. A probe slot with no result (`probe is None`) counts as a failed probe."""
    if not has_manager and not has_probe:
        return Resolution(None, None, False, False)

    if not has_manager:
        if isinstance(probe, ProbeResult):
            return Resolution(_ready_with(probe), None, False, False)
        return Resolution(CardStatus(StatusKind.NOT_RESPONDING), None, alert_sent, False)

    if power is not PowerState.RUNNING:
        return Resolution(CardStatus(_MANAGER_ONLY[power]), None, alert_sent, False)

    if not has_probe:
        return Resolution(CardStatus(StatusKind.READY), None, False, False)

    if isinstance(probe, ProbeResult):
        return Resolution(_ready_with(probe), None, False, False)

    since = failing_since if failing_since is not None else now
    if now - since < grace:
        return Resolution(CardStatus(StatusKind.WAKING), since, alert_sent, False)
    return Resolution(CardStatus(StatusKind.READY), since, True, not alert_sent)


def _ready_with(probe: ProbeResult) -> CardStatus:
    return CardStatus(StatusKind.READY, counts=probe.counts, details=probe.details)


def status_label(status: CardStatus) -> str:
    if status.kind is StatusKind.READY and status.counts is not None:
        return text.READY_COUNTS.format(online=status.counts.online, max=status.counts.max)
    return _LABELS[status.kind]


def next_action_text(action: PowerAction, now: datetime) -> str:
    label = _NEXT_LABELS[action.kind]
    delta = action.at - now
    if USE_TIMESTAMP_MARKUP:
        ts = int(action.at.timestamp())
        when = f"<t:{ts}:R>" if delta < timedelta(days=1) else f"<t:{ts}:f>"
    elif delta < timedelta(hours=2):
        when = text.IN_MINUTES.format(n=max(1, int(delta.total_seconds() // 60)))
    elif delta < timedelta(days=1):
        when = text.IN_HOURS.format(n=int(delta.total_seconds() // 3600))
    else:
        when = action.at.strftime("%a %H:%M")  # in next_run_at's own offset
    return f"{label} {when}"
