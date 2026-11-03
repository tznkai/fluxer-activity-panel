# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pterodactyl host manager against the fake Panel: states, failures, retries, schedules and the key."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from io import StringIO

import aiohttp
import pytest

from fluxer_panel.cards import CardFieldError
from fluxer_panel.config import Config
from fluxer_panel.logs import setup_logging
from fluxer_panel.sources import registry
from fluxer_panel.sources.base import PowerAction, PowerState, SourceError
from fluxer_panel.sources.pterodactyl import (
    PterodactylManager,
    Schedule,
    Task,
    parse_schedules,
    select_next,
)
from tests.fake_ptero import FakePanelState, FakeServer, Scripted, make_panel_app, schedule, task

KEY = "SENTINEL-PTERO-KEY-91c2"
TOKEN = "SENTINEL-TOKEN-7f3a9c"
SID = "abcdef12"
PATH = f"/api/client/servers/{SID}"
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
ERROR_BODY = b'{"errors":[{"detail":"SENTINEL-PANEL-BODY"}]}'
PLUGIN_LOGGER = "fluxer_panel.sources.pterodactyl"
PINNED_LOGGERS = ("a2s", "mcstatus")


def stats(current_state: str, *, suspended: bool = False) -> bytes:
    attributes = {"current_state": current_state, "is_suspended": suspended, "resources": {}}
    return json.dumps({"object": "stats", "attributes": attributes}).encode()


@pytest.fixture
async def panel(aiohttp_server):
    state = FakePanelState()
    server = await aiohttp_server(make_panel_app(state))
    return state, str(server.make_url("")).rstrip("/")


@pytest.fixture
async def session() -> aiohttp.ClientSession:
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture
def sleeps() -> list[float]:
    return []


def manager_for(session, base: str, sleeps: list[float], **kwargs) -> PterodactylManager:
    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    return PterodactylManager(session, base, KEY, sleep=record_sleep, **kwargs)


@pytest.fixture
def json_logs() -> Iterator:
    """setup_logging replaces the root handlers and level and pins the probe loggers: undo all of it."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_pinned = {name: logging.getLogger(name).level for name in PINNED_LOGGERS}

    def install(secrets: list[str]) -> StringIO:
        stream = StringIO()
        setup_logging("DEBUG", secrets, stream)
        return stream

    try:
        yield install
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging.captureWarnings(False)
        for name, level in saved_pinned.items():
            logging.getLogger(name).setLevel(level)


# AC2.1: current_state maps onto the power states; anything else is Unknown.
@pytest.mark.parametrize(
    ("current_state", "expected"),
    [
        ("offline", PowerState.OFFLINE),
        ("starting", PowerState.STARTING),
        ("running", PowerState.RUNNING),
        ("stopping", PowerState.STOPPING),
        ("stopped", PowerState.UNKNOWN),
        ("weird", PowerState.UNKNOWN),
    ],
)
async def test_current_state_maps_to_power_state(session, panel, sleeps, current_state, expected) -> None:
    state, base = panel
    state.servers[SID] = FakeServer(current_state=current_state)
    assert await manager_for(session, base, sleeps).power_state(SID) is expected


# AC2.2: a suspended server is Suspended, read from the details endpoint after the 409 on /resources.
async def test_suspended_server_reads_details_after_resources_conflict(session, panel, sleeps) -> None:
    state, base = panel
    state.servers[SID] = FakeServer(is_suspended=True)
    assert await manager_for(session, base, sleeps).power_state(SID) is PowerState.SUSPENDED
    assert [r.path for r in state.requests] == [f"{PATH}/resources", PATH]


async def test_is_suspended_true_wins_over_running_state(session, panel, sleeps) -> None:
    state, base = panel
    state.scripted[f"GET {PATH}/resources"] = [Scripted(body=stats("running", suspended=True))]
    assert await manager_for(session, base, sleeps).power_state(SID) is PowerState.SUSPENDED


async def test_conflict_with_details_not_suspended_is_unknown(session, panel, sleeps) -> None:
    state, base = panel
    state.scripted[f"GET {PATH}/resources"] = [Scripted(status=409, body=ERROR_BODY)]
    state.scripted[f"GET {PATH}"] = [Scripted(body=b'{"attributes":{"is_suspended":false}}')]
    assert await manager_for(session, base, sleeps).power_state(SID) is PowerState.UNKNOWN


# AC2.3: failures give Unknown and never raise.
async def test_unreachable_host_is_unknown(session, sleeps, aiohttp_server) -> None:
    closed = await aiohttp_server(make_panel_app(FakePanelState()))
    base = str(closed.make_url("")).rstrip("/")
    await closed.close()
    assert await manager_for(session, base, sleeps).power_state(SID) is PowerState.UNKNOWN


async def test_timeout_is_unknown_with_no_retry(session, panel, sleeps) -> None:
    state, base = panel
    state.scripted[f"GET {PATH}/resources"] = [Scripted(delay=1.0)]
    manager = manager_for(session, base, sleeps, request_timeout=aiohttp.ClientTimeout(total=0.2))
    assert await manager.power_state(SID) is PowerState.UNKNOWN
    assert len(state.requests) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("label", "script", "expected_requests"),
    [
        ("http_500_after_backoff", [Scripted(status=500, body=ERROR_BODY) for _ in range(3)], 3),
        ("http_403", [Scripted(status=403, body=ERROR_BODY)], 1),
        ("non_json_200", [Scripted(body=b"SENTINEL-PANEL-BODY not json")], 1),
        ("json_list_200", [Scripted(body=b"[]")], 1),
        ("http_404", None, 1),
    ],
)
async def test_failure_is_unknown_and_panel_body_never_logged(
    session, panel, sleeps, json_logs, label, script, expected_requests
) -> None:
    state, base = panel
    if script is not None:
        state.scripted[f"GET {PATH}/resources"] = script
    stream = json_logs([KEY])
    assert await manager_for(session, base, sleeps).power_state(SID) is PowerState.UNKNOWN
    assert len(state.requests) == expected_requests
    assert "SENTINEL-PANEL-BODY" not in stream.getvalue()


# AC2.4: 429 waits for Retry-After (capped); a 429 that repeats within a tick gives Unknown.
async def test_retry_after_is_honoured_then_request_succeeds(session, panel, sleeps) -> None:
    state, base = panel
    state.scripted[f"GET {PATH}/resources"] = [
        Scripted(status=429, headers={"Retry-After": "2"}),
        Scripted(body=stats("running")),
    ]
    assert await manager_for(session, base, sleeps).power_state(SID) is PowerState.RUNNING
    assert sleeps == [2.0]


async def test_retry_after_is_capped(session, panel, sleeps) -> None:
    state, base = panel
    state.scripted[f"GET {PATH}/resources"] = [
        Scripted(status=429, headers={"Retry-After": "999"}),
        Scripted(body=stats("running")),
    ]
    assert await manager_for(session, base, sleeps).power_state(SID) is PowerState.RUNNING
    assert sleeps == [10.0]


@pytest.mark.parametrize("headers", [{}, {"Retry-After": "abc"}, {"Retry-After": "-5"}, {"Retry-After": "inf"}])
async def test_missing_or_invalid_retry_after_defaults_to_one_second(session, panel, sleeps, headers) -> None:
    state, base = panel
    state.scripted[f"GET {PATH}/resources"] = [
        Scripted(status=429, headers=headers),
        Scripted(body=stats("running")),
    ]
    assert await manager_for(session, base, sleeps).power_state(SID) is PowerState.RUNNING
    assert sleeps == [1.0]


async def test_three_rate_limited_replies_give_unknown(session, panel, sleeps) -> None:
    state, base = panel
    state.scripted[f"GET {PATH}/resources"] = [Scripted(status=429, headers={"Retry-After": "1"}) for _ in range(3)]
    assert await manager_for(session, base, sleeps).power_state(SID) is PowerState.UNKNOWN
    assert len(state.requests) == 3


async def test_server_error_backoff_sleeps_are_half_then_one_second(session, panel, sleeps) -> None:
    state, base = panel
    state.scripted[f"GET {PATH}/resources"] = [Scripted(status=500, body=ERROR_BODY) for _ in range(3)]
    assert await manager_for(session, base, sleeps).power_state(SID) is PowerState.UNKNOWN
    assert sleeps == [0.5, 1.0]


# AC2.5: a bad server id is refused before any request is sent.
@pytest.mark.parametrize("ref", ["ABCDEF12", "abc", "abcdef123", "../../x", "abcdef1/"])
async def test_bad_ref_is_refused_before_any_request(session, panel, sleeps, ref) -> None:
    state, base = panel
    manager = manager_for(session, base, sleeps)
    with pytest.raises(CardFieldError) as info:
        manager.validate_ref(ref)
    assert info.value.field == "manager"
    assert await manager.power_state(ref) is PowerState.UNKNOWN
    with pytest.raises(SourceError):
        await manager.next_power_action(ref, now=NOW)
    assert state.requests == []


def test_good_ref_is_returned_unchanged(session) -> None:
    assert manager_for(session, "http://127.0.0.1:1", []).validate_ref(SID) == SID


# AC2.6: the next power action is the soonest active schedule whose first power task is allowlisted.
async def _next(session, panel, sleeps, schedules: list[dict]) -> PowerAction | None:
    state, base = panel
    state.servers[SID] = FakeServer(schedules=schedules)
    return await manager_for(session, base, sleeps).next_power_action(SID, now=NOW)


async def test_soonest_active_power_schedule_wins(session, panel, sleeps) -> None:
    action = await _next(
        session,
        panel,
        sleeps,
        [
            schedule("late", next_run_at="2026-11-02T14:00:00+00:00", tasks=(task(1, "power", "stop"),)),
            schedule("soon", next_run_at="2026-11-02T13:00:00+00:00", tasks=(task(1, "power", "restart"),)),
        ],
    )
    assert action == PowerAction("restart", datetime(2026, 11, 2, 13, tzinfo=UTC))


async def test_inactive_sooner_schedule_is_ignored(session, panel, sleeps) -> None:
    action = await _next(
        session,
        panel,
        sleeps,
        [
            schedule("off", active=False, next_run_at="2026-11-02T12:30:00+00:00", tasks=(task(1, "power", "start"),)),
            schedule("on", next_run_at="2026-11-02T15:00:00+00:00", tasks=(task(1, "power", "restart"),)),
        ],
    )
    assert action == PowerAction("restart", datetime(2026, 11, 2, 15, tzinfo=UTC))


@pytest.mark.parametrize(
    "tasks",
    [
        (task(1, "backup", ""),),
        (task(1, "command", "say hi"),),
    ],
    ids=["backup_only", "command_only"],
)
async def test_non_power_schedules_are_ignored(session, panel, sleeps, tasks) -> None:
    action = await _next(
        session,
        panel,
        sleeps,
        [
            schedule("other", next_run_at="2026-11-02T12:30:00+00:00", tasks=tasks),
            schedule("later", next_run_at="2026-11-02T16:00:00+00:00", tasks=(task(1, "power", "start"),)),
        ],
    )
    assert action == PowerAction("start", datetime(2026, 11, 2, 16, tzinfo=UTC))


async def test_first_task_by_sequence_counts_even_when_listed_out_of_order(session, panel, sleeps) -> None:
    action = await _next(
        session,
        panel,
        sleeps,
        [
            schedule(
                "shuffled",
                next_run_at="2026-11-02T13:00:00+00:00",
                tasks=(task(2, "power", "stop"), task(1, "power", "start")),
            ),
        ],
    )
    assert action == PowerAction("start", datetime(2026, 11, 2, 13, tzinfo=UTC))


async def test_first_power_task_with_bogus_payload_skips_schedule_even_if_later_power_is_allowed(
    session, panel, sleeps
) -> None:
    action = await _next(
        session,
        panel,
        sleeps,
        [
            schedule(
                "bogus-first",
                next_run_at="2026-11-02T13:00:00+00:00",
                tasks=(task(1, "power", "bogus"), task(2, "power", "start")),
            ),
            schedule("valid", next_run_at="2026-11-02T15:00:00+00:00", tasks=(task(1, "power", "stop"),)),
        ],
    )
    assert action == PowerAction("stop", datetime(2026, 11, 2, 15, tzinfo=UTC))


@pytest.mark.parametrize(
    "next_run_at",
    ["2026-11-02T11:00:00+00:00", None],
    ids=["in_the_past", "null"],
)
async def test_past_or_null_next_run_gives_no_action(session, panel, sleeps, next_run_at) -> None:
    action = await _next(
        session,
        panel,
        sleeps,
        [schedule("stale", next_run_at=next_run_at, tasks=(task(1, "power", "start"),))],
    )
    assert action is None


async def test_no_schedules_gives_no_action(session, panel, sleeps) -> None:
    assert await _next(session, panel, sleeps, []) is None


async def test_equal_times_go_to_the_first_schedule_in_panel_order(session, panel, sleeps) -> None:
    action = await _next(
        session,
        panel,
        sleeps,
        [
            schedule("first", next_run_at="2026-11-02T13:00:00+00:00", tasks=(task(1, "power", "start"),)),
            schedule("second", next_run_at="2026-11-02T13:00:00+00:00", tasks=(task(1, "power", "stop"),)),
        ],
    )
    assert action == PowerAction("start", datetime(2026, 11, 2, 13, tzinfo=UTC))


# AC2.7: next_run_at keeps its own offset; an offset-less time is ignored.
async def test_next_run_keeps_its_own_offset(session, panel, sleeps) -> None:
    action = await _next(
        session,
        panel,
        sleeps,
        [schedule("tz", next_run_at="2026-11-03T06:00:00+01:00", tasks=(task(1, "power", "start"),))],
    )
    assert action is not None
    assert action.at.utcoffset() == timedelta(hours=1)
    assert action.at == datetime(2026, 11, 3, 5, tzinfo=UTC)


async def test_offsetless_next_run_is_ignored(session, panel, sleeps) -> None:
    action = await _next(
        session,
        panel,
        sleeps,
        [schedule("naive", next_run_at="2026-11-03T06:00:00", tasks=(task(1, "power", "start"),))],
    )
    assert action is None


def test_parse_marks_unknown_actions_as_other_and_keeps_only_allowlisted_power_payloads() -> None:
    doc = {
        "data": [
            {
                "attributes": schedule(
                    "mixed",
                    tasks=(task(1, "weird", "x"), task(2, "power", "start"), task(3, "power", "bogus")),
                )
            }
        ]
    }
    (parsed,) = parse_schedules(doc)
    assert parsed.tasks == (
        Task(sequence=1, action="other", power_payload=None),
        Task(sequence=2, action="power", power_payload="start"),
        Task(sequence=3, action="power", power_payload=None),
    )


# AC2.8: command and power payloads never appear in parsed objects, reprs, logs or error text.
CMD_SENTINEL = "SENTINEL-CMD-PAYLOAD"
POWER_SENTINEL = "SENTINEL-POWER-PAYLOAD"


def _sentinel_doc() -> dict:
    tasks = (task(1, "command", CMD_SENTINEL), task(2, "power", POWER_SENTINEL))
    return {"data": [{"attributes": schedule("nightly", next_run_at="2026-11-03T06:00:00+00:00", tasks=tasks)}]}


def test_sentinel_payloads_absent_from_parsed_objects_and_reprs() -> None:
    schedules = parse_schedules(_sentinel_doc())
    assert CMD_SENTINEL not in repr(schedules)
    assert POWER_SENTINEL not in repr(schedules)
    assert select_next(schedules, NOW) is None


async def test_sentinel_payloads_absent_from_action_and_logs(session, panel, sleeps, json_logs) -> None:
    state, base = panel
    state.servers[SID] = FakeServer(schedules=[_sentinel_doc()["data"][0]["attributes"]])
    stream = json_logs([KEY])
    action = await manager_for(session, base, sleeps).next_power_action(SID, now=NOW)
    assert action is None
    assert CMD_SENTINEL not in stream.getvalue()
    assert POWER_SENTINEL not in stream.getvalue()


async def test_malformed_task_after_command_leaks_no_payload(session, panel, sleeps, json_logs) -> None:
    state, base = panel
    broken = {"action": "power", "payload": POWER_SENTINEL}  # no sequence_id
    tasks = (task(1, "command", CMD_SENTINEL), broken)
    state.servers[SID] = FakeServer(
        schedules=[schedule("broken", next_run_at="2026-11-03T06:00:00+00:00", tasks=tasks)]
    )
    stream = json_logs([KEY])
    with pytest.raises(SourceError) as info:
        await manager_for(session, base, sleeps).next_power_action(SID, now=NOW)
    assert info.value.reason == "malformed"
    for text in (str(info.value), repr(info.value), stream.getvalue()):
        assert CMD_SENTINEL not in text
        assert POWER_SENTINEL not in text


@pytest.mark.parametrize(
    "doc",
    [
        {"data": [{"attributes": {"name": "x", "is_active": True, "next_run_at": None}}]},  # no relationships
        {"data": [{"attributes": {**schedule("x"), "name": 5}}]},  # name is not text
        {"data": [{"attributes": schedule("x", tasks=({"sequence_id": "1", "action": "power", "payload": "start"},))}]},
        [],  # top level is not an object
    ],
    ids=["no_relationships", "name_not_text", "sequence_not_int", "top_level_list"],
)
def test_structural_problems_raise_malformed(doc) -> None:
    with pytest.raises(SourceError) as info:
        parse_schedules(doc)
    assert info.value.reason == "malformed"


def test_unparseable_next_run_gives_none() -> None:
    doc = {"data": [{"attributes": schedule("x", next_run_at="not a time", tasks=(task(1, "power", "start"),))}]}
    (parsed,) = parse_schedules(doc)
    assert parsed.next_run_at is None


def test_schedule_name_is_hidden_from_repr() -> None:
    doc = {"data": [{"attributes": schedule("SENTINEL-NAME", tasks=(task(1, "power", "start"),))}]}
    (parsed,) = parse_schedules(doc)
    assert isinstance(parsed, Schedule)
    assert "SENTINEL-NAME" not in repr(parsed)


# AC2.10: the key appears only in the Authorization header and nowhere in logs or repr.
async def test_key_only_appears_in_authorization_header(session, panel, sleeps) -> None:
    state, base = panel
    state.servers[SID] = FakeServer(current_state="running", schedules=[])
    manager = manager_for(session, base, sleeps)
    await manager.power_state(SID)
    await manager.next_power_action(SID, now=NOW)
    assert len(state.requests) == 2
    for recorded in state.requests:
        assert recorded.authorization == f"Bearer {KEY}"
        assert KEY not in recorded.path
        assert KEY not in (recorded.accept or "")
        assert KEY not in recorded.method


def test_repr_hides_key_and_base_url(session) -> None:
    manager = manager_for(session, "http://127.0.0.1:1", [])
    assert repr(manager) == "PterodactylManager()"


def test_redaction_removes_the_key_from_plugin_log_output(session, json_logs) -> None:
    stream = json_logs([KEY])
    logging.getLogger(PLUGIN_LOGGER).warning("x %s", KEY)
    text = stream.getvalue()
    assert KEY not in text
    assert "[REDACTED]" in text


async def test_plugin_failure_log_has_no_key_or_base_url(session, panel, sleeps, json_logs) -> None:
    state, base = panel
    state.scripted[f"GET {PATH}/resources"] = [Scripted(status=500, body=ERROR_BODY) for _ in range(3)]
    stream = json_logs([KEY])
    assert await manager_for(session, base, sleeps).power_state(SID) is PowerState.UNKNOWN
    text = stream.getvalue()
    assert "panel_request_failed" in text
    assert KEY not in text
    assert base not in text


# AC2.11: with Pterodactyl config absent, the kind is unavailable and no Panel request is made.
def _config(**overrides: str) -> Config:
    env = {"FLUXER_TOKEN": TOKEN, "FLUXER_INSTANCE_URL": "https://fluxer.invalid", "ADMIN_USER_IDS": "1001"}
    env.update(overrides)
    return Config.from_env(env)


async def test_absent_config_reports_kind_unavailable_and_sends_nothing(session, panel) -> None:
    state, _ = panel
    sources = registry.build_sources(session, _config())
    assert "pterodactyl" not in sources.managers
    assert "pterodactyl" in registry.known_manager_kinds()
    assert state.requests == []


async def test_configured_config_builds_the_manager_without_a_request(session, panel) -> None:
    state, base = panel
    sources = registry.build_sources(session, _config(PTERODACTYL_URL=base, PTERODACTYL_API_KEY=KEY))
    assert isinstance(sources.managers["pterodactyl"], PterodactylManager)
    assert state.requests == []
