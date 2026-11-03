# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Status resolution: the manager and probe state tables, the grace and alert rules, and next-action wording."""

import inspect
from datetime import UTC, datetime, timedelta, timezone

import pytest

from fluxer_panel import resolver
from fluxer_panel.resolver import StatusKind, next_action_text, resolve, status_label
from fluxer_panel.sources.base import (
    PlayerCounts,
    PowerAction,
    PowerState,
    ProbeDetails,
    ProbeFailed,
    ProbeResult,
)

OK_3_10 = ProbeResult(counts=PlayerCounts(online=3, max=10), details=ProbeDetails(map="Plains"))
OK_2_8 = ProbeResult(counts=PlayerCounts(online=2, max=8), details=ProbeDetails())
FAILED = ProbeFailed(reason="timeout")
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the plain-text branch, which is kept for a future switch back."""
    monkeypatch.setattr(resolver, "USE_TIMESTAMP_MARKUP", False)


def run(
    *,
    has_manager: bool = True,
    power: PowerState | None = PowerState.RUNNING,
    has_probe: bool = False,
    probe: ProbeResult | ProbeFailed | None = None,
    failing_since: int | None = None,
    alert_sent: bool = False,
    now: int = 1000,
    grace: int = 600,
):
    return resolve(
        has_manager=has_manager,
        power=power,
        has_probe=has_probe,
        probe=probe,
        failing_since=failing_since,
        alert_sent=alert_sent,
        now=now,
        grace=grace,
    )


# AC4.1: every manager row gives its label.
@pytest.mark.parametrize(
    ("power", "kind", "label"),
    [
        (PowerState.SUSPENDED, StatusKind.UNAVAILABLE, "Unavailable"),
        (PowerState.OFFLINE, StatusKind.ASLEEP, "Asleep"),
        (PowerState.STARTING, StatusKind.WAKING, "Waking up"),
        (PowerState.STOPPING, StatusKind.GOING_TO_SLEEP, "Going to sleep"),
        (PowerState.UNKNOWN, StatusKind.UNKNOWN, "Status unknown"),
    ],
)
def test_manager_only_state_table(power: PowerState, kind: StatusKind, label: str) -> None:
    res = run(power=power)
    assert res.status is not None
    assert res.status.kind is kind
    assert status_label(res.status) == label


def test_running_manager_without_probe_is_ready() -> None:
    res = run(power=PowerState.RUNNING, has_probe=False)
    assert res.status is not None
    assert res.status.kind is StatusKind.READY
    assert res.status.counts is None
    assert status_label(res.status) == "Ready"


def test_running_manager_with_successful_probe_shows_counts() -> None:
    res = run(has_probe=True, probe=OK_3_10)
    assert res.status is not None
    assert res.status.kind is StatusKind.READY
    assert res.status.counts == PlayerCounts(online=3, max=10)
    assert res.status.details == ProbeDetails(map="Plains")
    assert status_label(res.status) == "Ready · 3 / 10"


@pytest.mark.parametrize("power", [PowerState.SUSPENDED, PowerState.OFFLINE, PowerState.STARTING])
def test_non_running_manager_ignores_probe(power: PowerState) -> None:
    res = run(power=power, has_probe=True, probe=OK_3_10)
    assert res.status is not None
    assert res.status.counts is None
    assert res.status.details is None


# AC4.2: a probe failure is Waking up inside the grace period, and alerts once at it.
def test_failure_inside_grace_starts_waking_and_records_since() -> None:
    res = run(has_probe=True, probe=FAILED, failing_since=None, now=1000, grace=600)
    assert res.status is not None
    assert res.status.kind is StatusKind.WAKING
    assert res.failing_since == 1000
    assert res.alert_now is False


def test_failure_one_second_inside_grace_is_still_waking() -> None:
    res = run(has_probe=True, probe=FAILED, failing_since=1000, now=1599, grace=600)
    assert res.status is not None
    assert res.status.kind is StatusKind.WAKING
    assert res.failing_since == 1000
    assert res.alert_now is False
    assert res.alert_sent is False


def test_failure_inside_grace_keeps_the_alert_flag() -> None:
    res = run(has_probe=True, probe=FAILED, failing_since=None, alert_sent=True, now=1000, grace=600)
    assert res.status is not None
    assert res.status.kind is StatusKind.WAKING
    assert res.failing_since == 1000
    assert res.alert_sent is True
    assert res.alert_now is False


def test_probe_slot_without_result_counts_as_failure() -> None:
    res = run(has_probe=True, probe=None, failing_since=None, now=1000, grace=600)
    assert res.status is not None
    assert res.status.kind is StatusKind.WAKING
    assert res.failing_since == 1000


def test_failure_at_grace_boundary_is_ready_and_alerts() -> None:
    res = run(has_probe=True, probe=FAILED, failing_since=1000, now=1600, grace=600)
    assert res.status is not None
    assert res.status.kind is StatusKind.READY
    assert res.status.counts is None
    assert res.failing_since == 1000
    assert res.alert_now is True
    assert res.alert_sent is True


def test_failure_after_grace_does_not_alert_again() -> None:
    res = run(has_probe=True, probe=FAILED, failing_since=1000, alert_sent=True, now=1700, grace=600)
    assert res.status is not None
    assert res.status.kind is StatusKind.READY
    assert res.failing_since == 1000
    assert res.alert_now is False
    assert res.alert_sent is True


def test_failure_record_stays_fixed_across_ticks_past_grace() -> None:
    # Each tick is fed the previous Resolution. The start stays at 1000, so the card does not flip back to Waking up.
    first = run(has_probe=True, probe=FAILED, failing_since=None, now=1000, grace=600)
    second = run(
        has_probe=True, probe=FAILED, failing_since=first.failing_since,
        alert_sent=first.alert_sent, now=1600, grace=600,
    )
    assert second.status is not None
    assert second.status.kind is StatusKind.READY
    assert second.failing_since == 1000
    assert second.alert_now is True
    assert second.alert_sent is True
    third = run(
        has_probe=True, probe=FAILED, failing_since=second.failing_since,
        alert_sent=second.alert_sent, now=1700, grace=600,
    )
    assert third.status is not None
    assert third.status.kind is StatusKind.READY
    assert third.failing_since == 1000
    assert third.alert_now is False
    assert third.alert_sent is True


def test_offline_flap_does_not_realert_until_a_probe_succeeds() -> None:
    # Alert once, flap through Offline, then fail again in and past the grace period. Each call is fed the last result.
    first = run(has_probe=True, probe=FAILED, failing_since=None, alert_sent=False, now=1000, grace=600)
    second = run(
        has_probe=True, probe=FAILED, failing_since=first.failing_since,
        alert_sent=first.alert_sent, now=1600, grace=600,
    )
    assert second.alert_now is True
    assert second.alert_sent is True
    offline = run(
        power=PowerState.OFFLINE, has_probe=True, probe=FAILED, failing_since=second.failing_since,
        alert_sent=second.alert_sent, now=1650, grace=600,
    )
    assert offline.failing_since is None
    assert offline.alert_sent is True
    assert offline.alert_now is False
    inside = run(
        has_probe=True, probe=FAILED, failing_since=offline.failing_since,
        alert_sent=offline.alert_sent, now=1700, grace=600,
    )
    assert inside.status is not None
    assert inside.status.kind is StatusKind.WAKING
    assert inside.failing_since == 1700
    assert inside.alert_sent is True
    assert inside.alert_now is False
    past = run(
        has_probe=True, probe=FAILED, failing_since=inside.failing_since,
        alert_sent=inside.alert_sent, now=2300, grace=600,
    )
    assert past.status is not None
    assert past.status.kind is StatusKind.READY
    assert past.alert_sent is True
    assert past.alert_now is False


# AC4.3: a success or a non-Running state clears the failure record.
def test_success_after_failure_clears_since_and_alert_flag() -> None:
    res = run(has_probe=True, probe=OK_3_10, failing_since=1000, alert_sent=True, now=1700)
    assert res.failing_since is None
    assert res.alert_sent is False
    assert res.alert_now is False


def test_offline_clears_failing_since() -> None:
    res = run(power=PowerState.OFFLINE, has_probe=True, probe=FAILED, failing_since=1000, now=1100)
    assert res.failing_since is None
    assert res.alert_now is False


@pytest.mark.parametrize("alert_sent", [True, False])
@pytest.mark.parametrize(
    "power",
    [PowerState.SUSPENDED, PowerState.OFFLINE, PowerState.STARTING, PowerState.STOPPING, PowerState.UNKNOWN],
)
def test_non_running_keeps_alert_flag_unchanged(power: PowerState, alert_sent: bool) -> None:
    res = run(power=power, has_probe=True, probe=FAILED, failing_since=1000, alert_sent=alert_sent, now=5000)
    assert res.failing_since is None
    assert res.alert_sent is alert_sent
    assert res.alert_now is False


@pytest.mark.parametrize("alert_sent", [True, False])
def test_running_without_probe_clears_failure_record(alert_sent: bool) -> None:
    res = run(has_probe=False, failing_since=1000, alert_sent=alert_sent)
    assert res.failing_since is None
    assert res.alert_sent is False
    assert res.alert_now is False


# AC4.4: probe only, with no manager.
def test_probe_only_success_is_ready_with_counts() -> None:
    res = run(has_manager=False, power=None, has_probe=True, probe=OK_2_8)
    assert res.status is not None
    assert res.status.kind is StatusKind.READY
    assert status_label(res.status) == "Ready · 2 / 8"
    assert res.failing_since is None
    assert res.alert_sent is False


def test_probe_only_success_clears_alert_flag() -> None:
    res = run(has_manager=False, power=None, has_probe=True, probe=OK_2_8, alert_sent=True)
    assert res.alert_sent is False


@pytest.mark.parametrize("alert_sent", [True, False])
def test_probe_only_failure_is_not_responding_without_alert(alert_sent: bool) -> None:
    res = run(
        has_manager=False, power=None, has_probe=True, probe=FAILED,
        failing_since=1, alert_sent=alert_sent, now=10_000, grace=600,
    )
    assert res.status is not None
    assert res.status.kind is StatusKind.NOT_RESPONDING
    assert status_label(res.status) == "Not responding"
    assert res.failing_since is None
    assert res.alert_now is False
    assert res.alert_sent is alert_sent


# AC4.5: neither source means no status line, and nothing is kept.
def test_card_with_neither_source_has_no_status() -> None:
    res = run(has_manager=False, power=None, has_probe=False, probe=None, failing_since=1000, alert_sent=True)
    assert res == resolver.Resolution(None, None, False, False)


# AC4.7: a schedules error cannot change the status. This holds by construction, because resolve takes no
# schedule input. The signature check below is the guard: a schedule argument added later would fail it.
def test_resolve_has_no_schedule_parameter() -> None:
    """AC4.7 holds by construction: no schedules outcome can reach resolve, so its signature has no schedule argument."""
    assert set(inspect.signature(resolve).parameters) == {
        "has_manager", "power", "has_probe", "probe", "failing_since", "alert_sent", "now", "grace",
    }


# AC4.6: next-action labels.
def test_default_next_action_uses_timestamp_markup() -> None:
    at = NOW + timedelta(minutes=30)
    assert next_action_text(PowerAction(kind="restart", at=at), NOW) == f"Restarts <t:{int(at.timestamp())}:R>"


@pytest.mark.usefixtures("plain_text")
@pytest.mark.parametrize(
    ("kind", "label"),
    [("restart", "Restarts"), ("stop", "Goes to sleep"), ("kill", "Goes to sleep"), ("start", "Wakes up")],
)
def test_next_action_label_per_kind(kind: str, label: str) -> None:
    action = PowerAction(kind=kind, at=NOW + timedelta(minutes=30))
    assert next_action_text(action, NOW) == f"{label} in 30 min"


@pytest.mark.usefixtures("plain_text")
@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(0), "in 1 min"),          # the minutes floor
        (timedelta(seconds=30), "in 1 min"),
        (timedelta(minutes=1), "in 1 min"),
        (timedelta(minutes=2), "in 2 min"),
        (timedelta(minutes=119, seconds=59), "in 119 min"),
        (timedelta(hours=2), "in 2 h"),       # minutes switch to hours at two hours
        (timedelta(hours=5), "in 5 h"),
        (timedelta(hours=23, minutes=59), "in 23 h"),
    ],
)
def test_relative_time_thresholds(delta: timedelta, expected: str) -> None:
    action = PowerAction(kind="stop", at=NOW + delta)
    assert next_action_text(action, NOW) == f"Goes to sleep {expected}"


@pytest.mark.usefixtures("plain_text")
def test_one_day_away_uses_weekday_and_time_in_action_offset() -> None:
    at = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    assert next_action_text(PowerAction(kind="restart", at=at), NOW) == "Restarts Tue 12:00"


@pytest.mark.usefixtures("plain_text")
def test_three_days_away_uses_offset_of_next_run_at() -> None:
    at = datetime(2026, 11, 5, 13, 0, tzinfo=timezone(timedelta(hours=1)))
    assert next_action_text(PowerAction(kind="start", at=at), NOW) == "Wakes up Thu 13:00"


def test_timestamp_markup_is_relative_under_a_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolver, "USE_TIMESTAMP_MARKUP", True)
    at = NOW + timedelta(minutes=30)
    ts = int(at.timestamp())
    assert next_action_text(PowerAction(kind="restart", at=at), NOW) == f"Restarts <t:{ts}:R>"


def test_timestamp_markup_is_full_format_at_one_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolver, "USE_TIMESTAMP_MARKUP", True)
    at = NOW + timedelta(days=1)
    ts = int(at.timestamp())
    assert next_action_text(PowerAction(kind="start", at=at), NOW) == f"Wakes up <t:{ts}:f>"


def test_timestamp_markup_just_under_a_day_is_relative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolver, "USE_TIMESTAMP_MARKUP", True)
    at = NOW + timedelta(days=1) - timedelta(seconds=1)
    ts = int(at.timestamp())
    assert next_action_text(PowerAction(kind="stop", at=at), NOW) == f"Goes to sleep <t:{ts}:R>"
