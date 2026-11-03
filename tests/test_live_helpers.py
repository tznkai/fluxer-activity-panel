# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the live-test harness (tools/live/_live.py) against the fake REST server."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pytest
from yarl import URL

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "live"))
import _live  # noqa: E402
from _live import (  # noqa: E402
    Live,
    LiveRefused,
    Redactor,
    Results,
    capture_version,
    check_safety,
    load_env,
    out,
    read_env_file,
)
import markup_live  # noqa: E402
import ptero_live  # noqa: E402
import probes_live  # noqa: E402
from fluxer_client.rest import FluxerHTTPError, FluxerREST  # noqa: E402
from fluxer_panel.sources.base import PlayerCounts, PowerAction, ProbeDetails, ProbeFailed, ProbeResult  # noqa: E402
from fluxer_panel.sources.pterodactyl import Schedule, Task  # noqa: E402
from tests.fakes import FakeFluxerState, Override, make_rest_app  # noqa: E402

TOKEN = "SENTINEL-TOKEN-xyz"
GUILD = "1" * 19
CHANNEL = "2" * 19
USER = "3" * 19
MESSAGE = "6" * 19
ROLE = "7" * 19
LONG_RUN = re.compile(r"\d{15,}")


def required_env() -> dict[str, str]:
    return {
        "FLUXER_TOKEN": TOKEN,
        "FLUXER_API_URL": "https://fluxer.invalid/api",
        "LIVE_GUILD_ID": GUILD,
        "LIVE_CHANNEL_ID": CHANNEL,
        "LIVE_USER_ID": USER,
    }


# read_env_file / load_env


def test_read_env_file_parses_pairs_quotes_comments_and_blanks(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# a comment\n"
        "\n"
        "PLAIN=value\n"
        'QUOTED="quoted value"\n'
        "SINGLE='single'\n"
        "NOT_A_PAIR\n"
        "SPACED = spaced \n",
        encoding="utf-8",
    )
    assert read_env_file(path) == {
        "PLAIN": "value",
        "QUOTED": "quoted value",
        "SINGLE": "single",
        "SPACED": "spaced",
    }


def test_read_env_file_missing_file_is_empty(tmp_path):
    assert read_env_file(tmp_path / "absent.env") == {}


def test_load_env_missing_guild_refuses_by_name_without_token(tmp_path):
    env = required_env()
    del env["LIVE_GUILD_ID"]
    with pytest.raises(LiveRefused) as info:
        load_env(path=tmp_path / ".env", environ=env)
    message = str(info.value)
    assert "LIVE_GUILD_ID" in message
    assert TOKEN not in message


def test_load_env_refuses_non_snowflake_channel_by_name(tmp_path):
    env = required_env()
    env["LIVE_CHANNEL_ID"] = "12 3"
    with pytest.raises(LiveRefused) as info:
        load_env(path=tmp_path / ".env", environ=env)
    assert "LIVE_CHANNEL_ID" in str(info.value)
    assert "12 3" not in str(info.value)


def test_load_env_environ_overrides_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("LOG_LEVEL=DEBUG\nLIVE_USER_ID=" + "8" * 19 + "\n", encoding="utf-8")
    env = {**required_env(), "LOG_LEVEL": "INFO"}
    loaded = load_env(path=path, environ=env)
    assert loaded["LOG_LEVEL"] == "INFO"
    assert loaded["LIVE_USER_ID"] == USER


def test_load_env_refuses_bad_optional_role_by_name(tmp_path):
    env = {**required_env(), "LIVE_ROLE_ID": "not a role"}
    with pytest.raises(LiveRefused) as info:
        load_env(path=tmp_path / ".env", environ=env)
    assert "LIVE_ROLE_ID" in str(info.value)


def test_load_env_accepts_empty_optional_role(tmp_path):
    env = {**required_env(), "LIVE_ROLE_ID": ""}
    assert load_env(path=tmp_path / ".env", environ=env)["LIVE_ROLE_ID"] == ""


# Redactor


def test_text_replaces_token():
    assert Redactor(TOKEN).text(f"auth Bot {TOKEN}") == "auth Bot [REDACTED]"


def test_text_uses_labels_and_numbers_messages():
    redactor = Redactor(TOKEN)
    redactor.label(GUILD, "guild")
    redactor.label(MESSAGE, "message")
    other = "9" * 19
    redactor.label(other, "message")
    assert redactor.text(f"g {GUILD} m {MESSAGE} n {other}") == "g <guild> m <message:1> n <message:2>"


def test_label_is_stable_for_the_same_id():
    redactor = Redactor(TOKEN)
    redactor.label(MESSAGE, "message")
    redactor.label(MESSAGE, "message")
    assert redactor.labels[MESSAGE] == "<message:1>"


def test_text_turns_unknown_long_numbers_into_id_and_keeps_short_ones():
    unknown = "5" * 18
    assert Redactor(TOKEN).text(f"user {unknown}") == "user <id>"
    assert Redactor(TOKEN).text("rate 403 then 204") == "rate 403 then 204"


def test_shape_of_reaction_add_keeps_emoji_name_and_hides_content():
    redactor = Redactor(TOKEN)
    event = {
        "user_id": USER,
        "channel_id": CHANNEL,
        "message_id": MESSAGE,
        "guild_id": GUILD,
        "emoji": {"id": None, "name": "\U0001f44d", "animated": False},
        "content": "secret chat text",
    }
    shaped = redactor.shape(event)
    assert LONG_RUN.search(repr(shaped)) is None
    assert shaped["emoji"]["name"] == "\U0001f44d"
    assert shaped["content"] == "<str len=16>"
    assert shaped["emoji"]["animated"] is False


def test_shape_hides_names_outside_an_emoji_parent():
    shaped = Redactor(TOKEN).shape(
        {"guilds": [{"name": "My Guild"}], "user": {"username": "x"}}
    )
    assert "My Guild" not in repr(shaped)
    assert "'x'" not in repr(shaped)


def test_leak_check_over_shape_and_text_hides_token_and_ids():
    redactor = Redactor(TOKEN)
    payload = {
        "token": TOKEN,
        "guild_id": GUILD,
        "channel_id": CHANNEL,
        "author": {"id": USER},
        "content": f"hello {TOKEN} {GUILD}",
    }
    shaped = repr(redactor.shape(payload))
    rendered = redactor.text(repr(payload))
    for result in (shaped, rendered):
        assert LONG_RUN.search(result) is None
        assert TOKEN not in result


def test_log_handler_redacts_warnings_and_tracebacks():
    stream = io.StringIO()
    redactor = Redactor(TOKEN)
    handler = redactor.log_handler(stream=stream)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        logging.getLogger("fluxer_client.rest").warning("rate limited on %s", "PUT /channels/" + CHANNEL + "/messages")
        try:
            raise RuntimeError(f"boom {TOKEN}")
        except RuntimeError:
            logging.getLogger("fluxer_client.rest").exception("request failed")
    finally:
        root.removeHandler(handler)
    text = stream.getvalue()
    assert "rate limited" in text
    assert "request failed" in text
    assert LONG_RUN.search(text) is None
    assert TOKEN not in text


def test_out_passes_token_and_labels_through_redaction(capsys):
    redactor = Redactor(TOKEN)
    redactor.label(GUILD, "guild")
    out(redactor, "token", TOKEN, "guild", GUILD, "status", 403)
    printed = capsys.readouterr().out
    assert TOKEN not in printed
    assert GUILD not in printed
    assert "[REDACTED]" in printed
    assert "<guild>" in printed
    assert "403" in printed


# Live.next_event


async def test_next_event_skips_other_types_and_keeps_them_for_reporting():
    queue: asyncio.Queue = asyncio.Queue()
    lv = Live(env={}, session=None, rest=None, redactor=Redactor(TOKEN), version={}, events=queue)  # type: ignore[arg-type]
    await queue.put(("MESSAGE_CREATE", {"id": "1"}))
    await queue.put(("MESSAGE_REACTION_ADD", {"message_id": "stale"}))
    await queue.put(("MESSAGE_REACTION_ADD", {"message_id": "wanted"}))
    event_type, data = await lv.next_event(
        "MESSAGE_REACTION_ADD", timeout=1.0, where=lambda t, d: d["message_id"] == "wanted"
    )
    assert (event_type, data) == ("MESSAGE_REACTION_ADD", {"message_id": "wanted"})
    assert [t for t, _ in lv.seen_other] == ["MESSAGE_CREATE", "MESSAGE_REACTION_ADD"]


async def test_next_event_times_out():
    queue: asyncio.Queue = asyncio.Queue()
    lv = Live(env={}, session=None, rest=None, redactor=Redactor(TOKEN), version={}, events=queue)  # type: ignore[arg-type]
    with pytest.raises(TimeoutError):
        await lv.next_event("NEVER_SENT", timeout=0.05)


# check_safety and capture_version, against the fake REST server


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState()
    state.channels[CHANNEL] = {"id": CHANNEL, "guild_id": GUILD}
    state.guilds[GUILD] = {"id": GUILD, "member_count": 2}
    state.members[(GUILD, USER)] = {"user": {"id": USER}, "roles": []}
    server = await aiohttp_server(make_rest_app(state))
    return state, str(server.make_url("/")).rstrip("/")


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


def rest_for(session, base: str) -> FluxerREST:
    return FluxerREST(session, f"{base}/api", TOKEN)


def safety_env() -> dict[str, str]:
    return {"LIVE_GUILD_ID": GUILD, "LIVE_CHANNEL_ID": CHANNEL, "LIVE_USER_ID": USER}


async def test_safety_passes_for_channel_in_guild_with_two_members(fake, session):
    _, base = fake
    await check_safety(rest_for(session, base), safety_env())


async def test_safety_refuses_channel_in_another_guild(fake, session):
    state, base = fake
    state.channels[CHANNEL] = {"id": CHANNEL, "guild_id": "4" * 19}
    with pytest.raises(LiveRefused) as info:
        await check_safety(rest_for(session, base), safety_env())
    assert str(info.value) == "LIVE_CHANNEL_ID is not in LIVE_GUILD_ID"


async def test_safety_refuses_guild_with_more_than_two_members(fake, session):
    state, base = fake
    state.guilds[GUILD] = {"id": GUILD, "member_count": 3}
    with pytest.raises(LiveRefused) as info:
        await check_safety(rest_for(session, base), safety_env())
    assert str(info.value) == "guild has more than 2 members"


async def test_safety_passes_when_member_count_is_absent(fake, session):
    state, base = fake
    state.guilds[GUILD] = {"id": GUILD}
    await check_safety(rest_for(session, base), safety_env())


async def test_safety_refuses_user_who_is_not_a_member_by_name(fake, session):
    state, base = fake
    state.members.clear()
    with pytest.raises(LiveRefused) as info:
        await check_safety(rest_for(session, base), safety_env())
    assert str(info.value) == "LIVE_USER_ID is not a member of LIVE_GUILD_ID"


async def test_safety_refusals_never_contain_ids(fake, session):
    state, base = fake
    cases = [
        lambda: state.channels.__setitem__(CHANNEL, {"id": CHANNEL, "guild_id": "4" * 19}),
        lambda: state.guilds.__setitem__(GUILD, {"id": GUILD, "member_count": 3}),
        lambda: state.members.clear(),
    ]
    for break_it in cases:
        state.channels[CHANNEL] = {"id": CHANNEL, "guild_id": GUILD}
        state.guilds[GUILD] = {"id": GUILD, "member_count": 2}
        state.members[(GUILD, USER)] = {"user": {"id": USER}, "roles": []}
        break_it()
        with pytest.raises(LiveRefused) as info:
            await check_safety(rest_for(session, base), safety_env())
        for value in (GUILD, CHANNEL, USER):
            assert value not in str(info.value)


async def test_capture_version_reads_header_and_api_code_version(fake, session):
    state, base = fake
    state.overrides["GET /api/v1/gateway/bot"] = Override(
        status=200,
        body=b'{"url": "ws://unused"}',
        headers={"X-Fluxer-Version": "9.9.9-test"},
    )
    version = await capture_version(session, {"FLUXER_TOKEN": TOKEN, "FLUXER_INSTANCE_URL": base}, f"{base}/api")
    assert version == {"x_fluxer_version": "9.9.9-test", "api_code_version": 1}
    assert state.requests_to("/api/v1/gateway/bot")[-1].authorization == f"Bot {TOKEN}"


async def test_capture_version_without_instance_url_has_no_code_version(fake, session):
    state, base = fake
    state.overrides["GET /api/v1/gateway/bot"] = Override(
        status=200,
        body=b'{"url": "ws://unused"}',
        headers={"X-Fluxer-Version": "9.9.9-test"},
    )
    version = await capture_version(session, {"FLUXER_TOKEN": TOKEN}, f"{base}/api")
    assert version == {"x_fluxer_version": "9.9.9-test", "api_code_version": None}


def test_harness_import_has_no_side_effects():
    assert hasattr(_live, "live") and hasattr(_live, "main")


# wait_for_enter: stdin is read in a daemon thread, so Ctrl+C is not held up by a blocked read


async def test_wait_for_enter_resolves_with_the_line_written_to_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    assert await _live.wait_for_enter() == "\n"


async def test_wait_for_enter_raises_eoferror_when_stdin_closes_first(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(EOFError):
        await _live.wait_for_enter()


async def test_cancelled_wait_drops_a_late_line_without_error(monkeypatch):
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stdin)
    task = asyncio.create_task(_live.wait_for_enter())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    os.write(write_fd, b"late\n")
    os.close(write_fd)
    await asyncio.sleep(0.2)   # the reader thread delivers the line to a future that is already cancelled
    stdin.close()


SIGINT_TIMEOUT = 10.0
WAIT_SCRIPT = """
import sys
sys.path.insert(0, {live_dir!r})
import _live

async def script():
    print("READY", flush=True)
    await _live.wait_for_enter()
    print("ENTERED", flush=True)

_live.main(script)
"""


@pytest.mark.skipif(os.name != "posix", reason="delivers SIGINT, which only POSIX test runs use")
def test_ctrl_c_while_waiting_for_enter_exits_130_promptly(tmp_path):
    script = tmp_path / "wait_for_enter_script.py"
    script.write_text(WAIT_SCRIPT.format(live_dir=str(Path(_live.__file__).parent)), encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.PIPE,   # held open: the script waits for an Enter that never comes
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Backstop only: kills a hung child so the test cannot run forever. The real bound is SIGINT_TIMEOUT below.
    backstop = threading.Timer(3 * SIGINT_TIMEOUT, proc.kill)
    backstop.start()
    try:
        assert proc.stdout.readline().strip() == "READY"
        proc.send_signal(signal.SIGINT)
        try:
            code = proc.wait(timeout=SIGINT_TIMEOUT)
        except subprocess.TimeoutExpired:
            pytest.fail(f"still running {SIGINT_TIMEOUT:.0f}s after SIGINT while waiting for Enter")
        stdout, stderr = proc.stdout.read(), proc.stderr.read()
    finally:
        backstop.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()
    assert code == 130
    assert "INTERRUPTED" in stdout
    assert "ENTERED" not in stdout
    assert "Traceback" not in stderr


# main(): exit codes and error output. _redactor is reset per test; live() sets it in the real run.


@pytest.fixture
def no_live_redactor(monkeypatch):
    monkeypatch.setattr(_live, "_redactor", None)


def exit_code_of(main_fn) -> int:
    with pytest.raises(SystemExit) as info:
        main_fn()
    return info.value.code


def test_main_error_output_redacts_token_and_labels_ids_bound_during_the_run(no_live_redactor, capsys):
    unknown = "9" * 18

    async def script():
        redactor = Redactor(token=TOKEN)
        _live._redactor = redactor   # the same path live() takes, before any error is raised
        redactor.label(GUILD, "guild")
        raise RuntimeError(f"auth {TOKEN} guild {GUILD} other {unknown}")

    assert exit_code_of(lambda: _live.main(script)) == 1
    printed = capsys.readouterr().out
    assert "ERROR RuntimeError:" in printed
    assert TOKEN not in printed
    assert unknown not in printed
    assert GUILD not in printed
    assert "[REDACTED]" in printed
    assert "<guild>" in printed


def test_main_error_before_live_still_masks_long_ids(no_live_redactor, capsys):
    unknown = "9" * 18

    async def script():
        raise RuntimeError(f"id {unknown}")

    assert exit_code_of(lambda: _live.main(script)) == 1
    printed = capsys.readouterr().out
    assert unknown not in printed
    assert "<id>" in printed


def test_main_refusal_exits_2_with_refused_line(no_live_redactor, capsys):
    async def script():
        raise LiveRefused("LIVE_USER_ID is not a member of LIVE_GUILD_ID")

    assert exit_code_of(lambda: _live.main(script)) == 2
    assert capsys.readouterr().out.startswith("REFUSED: LIVE_USER_ID is not a member of LIVE_GUILD_ID")


def test_main_failed_results_exit_1(no_live_redactor):
    async def script():
        results = _live.Results(Redactor(token=TOKEN))
        results.fail("check", "went wrong")
        return results

    assert exit_code_of(lambda: _live.main(script)) == 1


def test_main_passing_results_exit_0(no_live_redactor, capsys):
    async def script():
        results = _live.Results(Redactor(token=TOKEN))
        results.ok("check")
        return results

    assert exit_code_of(lambda: _live.main(script)) == 0
    assert "PASS check" in capsys.readouterr().out


def test_main_none_result_exits_0(no_live_redactor):
    async def script():
        return None

    assert exit_code_of(lambda: _live.main(script)) == 0


def test_main_ctrl_c_prints_interrupted_without_traceback_and_exits_130(no_live_redactor, capsys):
    async def script():
        raise KeyboardInterrupt

    assert exit_code_of(lambda: _live.main(script)) == 130
    captured = capsys.readouterr()
    assert captured.out.strip() == "INTERRUPTED"
    assert "Traceback" not in captured.err


# gateway_failure: the READY-timeout message names the gateway task's exception type, never its message


def test_gateway_failure_names_exception_type_and_never_its_message():
    async def dies():
        raise ConnectionError(f"dial failed with {TOKEN} for {'8' * 18}")

    async def scenario() -> str:
        task = asyncio.create_task(dies())
        await asyncio.wait({task})
        return _live.gateway_failure(task, Redactor(token=TOKEN))

    message = asyncio.run(scenario())
    assert message == "gateway did not reach READY (gateway task failed: ConnectionError)"
    assert TOKEN not in message


def test_gateway_failure_for_a_task_still_running_has_no_cause():
    async def scenario() -> str:
        task = asyncio.create_task(asyncio.sleep(10))
        message = _live.gateway_failure(task, Redactor(token=TOKEN))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return message

    assert asyncio.run(scenario()) == "gateway did not reach READY"


# docs/fluxer-notes.md: the recorded live capability results carry no raw ids or tokens


NOTES = Path(__file__).resolve().parents[1] / "docs" / "fluxer-notes.md"
TOKEN_LIKE = re.compile(r"Bot [A-Za-z0-9._-]{20,}")


def test_fluxer_notes_have_no_raw_ids_or_tokens():
    text = NOTES.read_text(encoding="utf-8")
    assert re.search(r"\d{15,}", text) is None
    assert TOKEN_LIKE.search(text) is None
    assert "X-Fluxer-Version" in text


# Redactor: extra secrets and free-text labels


def test_extra_secrets_are_redacted_in_text():
    redactor = Redactor(TOKEN, extra_secrets=("panel.invalid", "other-secret"))
    assert redactor.text("at panel.invalid with other-secret") == "at [REDACTED] with [REDACTED]"


def test_a_longer_extra_secret_is_replaced_before_its_prefix():
    redactor = Redactor("", extra_secrets=("panel.invalid", "panel.invalid:8443"))
    assert redactor.text("reach panel.invalid:8443") == "reach [REDACTED]"


def test_empty_extra_secrets_leave_text_unchanged():
    redactor = Redactor(TOKEN, extra_secrets=("", ""))
    assert redactor.text("status 200 ok") == "status 200 ok"


# A non-ASCII host is written with escapes. Its punycode form is built from parts, so no literal host is in the file.
IDN_HOST = "b\u00fccher.invalid"
PUNYCODE_HOST = ".".join(["xn--bcher-kva", "invalid"])


def test_extra_secrets_match_regardless_of_case():
    redactor = Redactor("", extra_secrets=("fluxer.invalid",))
    assert redactor.text("reached FLUXER.Invalid and fluxer.INVALID") == "reached [REDACTED] and [REDACTED]"


def test_a_mixed_case_instance_url_hides_its_host_in_every_spelling():
    secrets = markup_live.markup_secrets({"FLUXER_INSTANCE_URL": "https://Fluxer.Invalid:8443"})
    redactor = Redactor(TOKEN, extra_secrets=secrets)
    assert redactor.text("https://Fluxer.Invalid:8443/ fluxer.invalid:8443 FLUXER.INVALID") == \
        "[REDACTED]/ [REDACTED] [REDACTED]"


def test_punycode_host_is_redacted_where_an_error_names_host_and_port():
    secrets = markup_live.markup_secrets({"FLUXER_INSTANCE_URL": "https://" + IDN_HOST})
    redactor = Redactor(TOKEN, extra_secrets=secrets)
    assert redactor.text(f"cannot connect to {PUNYCODE_HOST}:443") == "cannot connect to [REDACTED]"


def test_punycode_url_is_redacted_where_an_error_names_the_url():
    secrets = markup_live.markup_secrets({"FLUXER_INSTANCE_URL": "https://" + IDN_HOST})
    redactor = Redactor(TOKEN, extra_secrets=secrets)
    assert redactor.text("GET " + "https://" + PUNYCODE_HOST + "/x") == "GET [REDACTED]/x"


def test_punycode_host_in_upper_case_is_redacted():
    secrets = markup_live.markup_secrets({"FLUXER_INSTANCE_URL": "https://" + IDN_HOST})
    redactor = Redactor(TOKEN, extra_secrets=secrets)
    assert redactor.text(PUNYCODE_HOST.upper()) == "[REDACTED]"


def test_instance_url_forms_include_the_punycode_host_and_its_port_forms():
    assert markup_live.markup_secrets({"FLUXER_INSTANCE_URL": "https://" + IDN_HOST}) == (
        "https://" + IDN_HOST, "https://" + PUNYCODE_HOST,
        IDN_HOST, f"{IDN_HOST}:443", f"[{IDN_HOST}]:443",
        PUNYCODE_HOST, f"{PUNYCODE_HOST}:443", f"[{PUNYCODE_HOST}]:443")


def test_label_text_is_stable_per_value_and_counts_per_kind():
    redactor = Redactor(TOKEN)
    first = redactor.label_text("alpha.invalid", "target")
    assert first == "<target#1>"
    assert redactor.label_text("alpha.invalid", "target") == first
    assert redactor.label_text("beta.invalid", "target") == "<target#2>"
    assert redactor.label_text("27015", "port") == "<port#1>"


def test_label_text_never_echoes_the_value():
    value = "panel-host.invalid"
    assert value not in Redactor(TOKEN).label_text(value, "target")


def test_label_text_refuses_an_empty_value():
    with pytest.raises(ValueError):
        Redactor(TOKEN).label_text("", "target")


def test_redacted_logging_restores_root_handlers_and_level():
    root = logging.getLogger()
    before_handlers, before_level = root.handlers[:], root.level
    with _live.redacted_logging(Redactor(TOKEN)):
        assert len(root.handlers) == 1
        assert root.level == logging.WARNING
    assert root.handlers == before_handlers
    assert root.level == before_level


def test_read_env_merges_file_under_environ(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=file\nB=file\n", encoding="utf-8")
    assert _live.read_env(path=path, environ={"B": "env"}) == {"A": "file", "B": "env"}


def test_require_vars_names_missing_variables_and_never_values():
    with pytest.raises(LiveRefused) as info:
        _live.require_vars({"A": "secret-value", "B": ""}, ("A", "B", "C"))
    message = str(info.value)
    assert "B" in message and "C" in message
    assert "secret-value" not in message


def test_require_vars_passes_when_all_are_set():
    _live.require_vars({"A": "x", "B": "y"}, ("A", "B"))


# ptero_live: pure helpers (no network)

WHEN = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def test_parse_server_ids_accepts_a_comma_list_with_spaces():
    assert ptero_live.parse_server_ids(" 0a1b2c3d, 9f8e7d6c ") == ["0a1b2c3d", "9f8e7d6c"]


@pytest.mark.parametrize("raw", ["", " , ", "0a1b2c3", "0A1B2C3D", "0a1b2c3d,zz"])
def test_parse_server_ids_refuses_empty_or_bad_entries_without_echoing(raw):
    with pytest.raises(LiveRefused) as info:
        ptero_live.parse_server_ids(raw)
    message = str(info.value)
    assert "LIVE_PTERO_SERVERS" in message
    for token in raw.replace(",", " ").split():
        assert token not in message


def test_user_permissions_reads_meta_and_sorts():
    doc = {"meta": {"user_permissions": ["websocket.connect", "schedule.read"]}}
    assert ptero_live.user_permissions(doc) == ["schedule.read", "websocket.connect"]


@pytest.mark.parametrize("doc", [[], {}, {"meta": {}}, {"meta": {"user_permissions": "schedule.read"}},
                                 {"meta": {"user_permissions": [1]}}])
def test_user_permissions_refuses_missing_or_mistyped(doc):
    with pytest.raises(ValueError):
        ptero_live.user_permissions(doc)


def test_extra_permissions_flags_only_what_the_plugin_does_not_need():
    assert ptero_live.extra_permissions(["websocket.connect", "schedule.read"]) == []
    assert ptero_live.extra_permissions(["control.console", "schedule.read", "websocket.connect",
                                         "backup.read"]) == ["backup.read", "control.console"]


def test_schedule_summary_hides_name_and_reports_first_power_kind():
    tasks = (
        Task(sequence=2, action="command", power_payload=None),
        Task(sequence=3, action="power", power_payload="stop"),
        Task(sequence=1, action="power", power_payload="restart"),
    )
    summary = ptero_live.schedule_summary(
        Schedule(name="SECRET NAME", is_active=True, next_run_at=WHEN, tasks=tasks))
    assert summary == f"name_len=11 active=True next={WHEN.isoformat()} first_power=restart"
    assert "SECRET" not in summary


def test_schedule_summary_without_power_task_or_next_run():
    summary = ptero_live.schedule_summary(Schedule(name="x", is_active=False, next_run_at=None, tasks=()))
    assert summary == "name_len=1 active=False next=none first_power=none"


def test_schedule_summary_marks_an_unlisted_power_payload_as_other():
    tasks = (Task(sequence=1, action="power", power_payload=None),)
    summary = ptero_live.schedule_summary(Schedule(name="x", is_active=True, next_run_at=WHEN, tasks=tasks))
    assert summary.endswith("first_power=other")


def test_next_action_text_for_none_and_for_an_action():
    assert ptero_live.next_action_text(None) == "none"
    assert ptero_live.next_action_text(PowerAction(kind="restart", at=WHEN)) == f"restart at {WHEN.isoformat()}"


# probes_live: pure helpers (no network)

PROBE_KINDS = frozenset({"a2s", "minecraft_bedrock"})


def test_parse_probe_targets_splits_kind_host_and_port():
    targets = probes_live.parse_probe_targets("a2s:game.invalid:27015, minecraft_bedrock:mc.invalid:19132",
                                              PROBE_KINDS)
    assert targets == [
        probes_live.Target("a2s", "game.invalid", 27015),
        probes_live.Target("minecraft_bedrock", "mc.invalid", 19132),
    ]


@pytest.mark.parametrize("raw", ["", "unknown:h.invalid:1", "a2s:h.invalid", "a2s::1", "a2s:h.invalid:0",
                                 "a2s:h.invalid:70000", "a2s:h.invalid:x", "a2s:h.invalid:+1"])
def test_parse_probe_targets_refuses_bad_entries_without_echoing(raw):
    with pytest.raises(LiveRefused) as info:
        probes_live.parse_probe_targets(raw, PROBE_KINDS)
    message = str(info.value)
    assert "LIVE_PROBES" in message
    assert "h.invalid" not in message


def test_probe_summary_reports_counts_and_only_present_details():
    result = ProbeResult(counts=PlayerCounts(online=3, max=20),
                         details=ProbeDetails(map="survival", version=None, game_mode="creative"))
    assert probes_live.probe_summary(result) == "reachable counts=3/20 map_len=8 game_mode=creative"


def test_probe_summary_prints_the_map_name_only_as_its_length():
    map_name = "private-world-name"   # an operator-chosen name, so it must not appear in the output
    result = ProbeResult(counts=PlayerCounts(online=0, max=8),
                         details=ProbeDetails(map=map_name, version="1.21", game_mode=None))
    summary = probes_live.probe_summary(result)
    assert summary == f"reachable counts=0/8 map_len={len(map_name)} version=1.21"
    assert "world" not in summary


def test_probe_summary_keeps_an_empty_map_as_length_zero():
    result = ProbeResult(counts=PlayerCounts(online=1, max=2), details=ProbeDetails(map="", version=None,
                                                                                    game_mode=None))
    assert probes_live.probe_summary(result) == "reachable counts=1/2 map_len=0"


def test_probe_summary_for_an_unreachable_target_gives_the_reason():
    assert probes_live.probe_summary(ProbeFailed(reason="timeout")) == "unreachable reason=timeout"


# markup_live: pure helpers (no network)

OTHER = "4" * 19
VOICE = "5" * 19
PROBE_IPV6 = ":".join(["fd00", "", "1"])
MARKUP_ENV = {
    "LIVE_GUILD_ID": GUILD,
    "LIVE_VOICE_CHANNEL_ID": VOICE,
    "LIVE_OTHER_GUILD_ID": OTHER,
    "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
}


def test_markup_env_accepts_complete_settings():
    markup_live.check_markup_env(MARKUP_ENV)


@pytest.mark.parametrize("name", ["LIVE_VOICE_CHANNEL_ID", "LIVE_OTHER_GUILD_ID", "FLUXER_INSTANCE_URL"])
def test_markup_env_refuses_each_missing_setting_by_name(name):
    env = {key: value for key, value in MARKUP_ENV.items() if key != name}
    with pytest.raises(LiveRefused) as info:
        markup_live.check_markup_env(env)
    assert name in str(info.value)


def test_markup_env_refuses_a_non_snowflake_voice_channel_without_echoing_it():
    with pytest.raises(LiveRefused) as info:
        markup_live.check_markup_env({**MARKUP_ENV, "LIVE_VOICE_CHANNEL_ID": "12 3"})
    assert "LIVE_VOICE_CHANNEL_ID" in str(info.value)
    assert "12 3" not in str(info.value)


def test_markup_env_refuses_the_test_guild_as_the_other_community():
    with pytest.raises(LiveRefused) as info:
        markup_live.check_markup_env({**MARKUP_ENV, "LIVE_OTHER_GUILD_ID": GUILD})
    assert "LIVE_OTHER_GUILD_ID" in str(info.value)


@pytest.mark.parametrize(("line", "expected"), [
    ("y", ("pass", "")),
    (" Y ", ("pass", "")),
    ("n", ("fail", "")),
    ("N", ("fail", "")),
    ("only the mobile one", ("note", "only the mobile one")),
    ("", ("note", "")),
])
def test_parse_verdict_maps_y_n_and_notes(line, expected):
    assert markup_live.parse_verdict(line) == expected


def test_burst_line_counts_successes_rate_limits_and_other_errors():
    line = markup_live.burst_line([None, None, 429, 429, 500], 1.234)
    assert line == "edits=5 ok=2 rate_limited_429=2 other_errors=1 seconds=1.23"


def markup_ctx(session, base: str, env: dict[str, str] = MARKUP_ENV) -> markup_live.Ctx:
    """A markup context over the real REST client and the fake server. Only the posting channel is the text channel."""
    redactor = Redactor(token=TOKEN, extra_secrets=markup_live.markup_secrets(env))
    lv = Live(env=dict(env), session=session, rest=rest_for(session, base), redactor=redactor,
              version={}, events=asyncio.Queue())
    return markup_live.Ctx(lv=lv, res=Results(redactor), instance=env["FLUXER_INSTANCE_URL"], guild=GUILD,
                           channel=CHANNEL, user=USER, voice=VOICE, other=OTHER)


async def test_channel_links_and_mention_point_at_the_voice_channel_not_the_posting_channel(
        fake, session, monkeypatch, capsys):
    state, base = fake
    answers = iter(["y"])

    async def operator_answer() -> str:
        return next(answers)

    monkeypatch.setattr(markup_live, "wait_for_enter", operator_answer)
    ctx = markup_ctx(session, base)

    await markup_live.check_channel_links(ctx)

    ((posted_channel, _), payload), = state.messages.items()
    assert posted_channel == CHANNEL   # the test message is posted to the text channel ...
    link = f"https://fluxer.invalid/channels/{GUILD}/{VOICE}"   # ... but every link and mention names the voice one
    embed = payload["embeds"][0]
    assert link in payload["content"]
    assert f"[voice]({link})" in embed["description"]
    assert f"<#{VOICE}>" in payload["content"]
    assert embed["fields"][0]["value"] == f"<#{VOICE}>"
    assert CHANNEL not in str(payload)   # nothing in the message refers to the channel it was posted in
    assert ctx.res.failed is False
    assert "voice channel" in capsys.readouterr().out


async def test_report_prints_the_type_of_the_posting_text_channel(fake, session, capsys):
    state, base = fake
    state.channels[CHANNEL] = {"id": CHANNEL, "type": 0, "guild_id": GUILD, "name": "general"}
    ctx = markup_ctx(session, base)

    await markup_live.report_text_channel(ctx)

    assert "text channel type: 0" in capsys.readouterr().out


async def test_report_says_when_the_posting_text_channel_cannot_be_read(fake, session, capsys):
    state, base = fake
    state.channels.pop(CHANNEL)
    ctx = markup_ctx(session, base)

    await markup_live.report_text_channel(ctx)

    assert "text channel: HTTP 404" in capsys.readouterr().out


def test_markup_secrets_hide_the_instance_url_its_host_and_the_default_port():
    assert markup_live.markup_secrets(MARKUP_ENV) == (
        "https://fluxer.invalid", "fluxer.invalid", "fluxer.invalid:443", "[fluxer.invalid]:443")


def test_markup_secrets_for_an_explicit_port_and_an_ipv6_host():
    url = "https://[" + PROBE_IPV6 + "]:8443/"
    assert markup_live.markup_secrets({"FLUXER_INSTANCE_URL": url}) == (
        url, PROBE_IPV6, f"{PROBE_IPV6}:8443", f"[{PROBE_IPV6}]:8443")


def test_markup_secrets_hide_only_the_host_when_the_url_has_no_known_port():
    url = "foo://fluxer.invalid/"
    assert markup_live.markup_secrets({"FLUXER_INSTANCE_URL": url}) == (url, "fluxer.invalid")


async def test_live_hides_the_extra_secrets_in_every_line_it_prints(fake, monkeypatch, capsys, no_live_redactor):
    _, base = fake
    monkeypatch.setattr(_live, "load_env", lambda: {**required_env(), "FLUXER_API_URL": f"{base}/api"})

    async with _live.live(gateway=False, extra_secrets=markup_live.markup_secrets(MARKUP_ENV)) as lv:
        out(lv.redactor, "reached fluxer.invalid:443 and https://fluxer.invalid")

    printed = capsys.readouterr().out
    assert "fluxer.invalid" not in printed
    assert printed.splitlines()[-1] == "reached [REDACTED] and [REDACTED]"


@pytest.mark.parametrize(("instance", "note", "shown"), [
    ("https://fluxer.invalid", "host fluxer.invalid", "host [REDACTED]"),
    ("https://fluxer.invalid", "url https://fluxer.invalid", "url [REDACTED]"),
    ("https://fluxer.invalid", "port fluxer.invalid:443", "port [REDACTED]"),
    ("https://fluxer.invalid:8443", "at fluxer.invalid:8443", "at [REDACTED]"),
    ("https://[" + PROBE_IPV6 + "]:8443", f"at [{PROBE_IPV6}]:8443", "at [REDACTED]"),
    ("https://Fluxer.Invalid", "host FLUXER.invalid", "host [REDACTED]"),
    ("https://" + IDN_HOST, "host " + PUNYCODE_HOST, "host [REDACTED]"),
    ("https://" + IDN_HOST, "host " + IDN_HOST, "host [REDACTED]"),
])
async def test_a_note_echoing_the_instance_host_is_redacted(fake, session, monkeypatch, capsys, instance, note,
                                                            shown):
    _, base = fake
    env = {**MARKUP_ENV, "FLUXER_INSTANCE_URL": instance}

    async def operator_note() -> str:
        return note

    monkeypatch.setattr(markup_live, "wait_for_enter", operator_note)
    ctx = markup_ctx(session, base, env)

    await markup_live.ask(ctx, "channel links", "look for it")

    printed = capsys.readouterr().out
    assert f"NOTE channel links: {shown}" in printed.splitlines()
    assert "fluxer.invalid" not in printed
    assert PROBE_IPV6 not in printed


# Scripts that do not call live() register their redactor, so main() redacts an escaped error. no_live_redactor
# restores the registered redactor after each test, so a registration never leaks into another test.
PANEL_HOST = ".".join(["panel", "invalid"])
PANEL_URL = "https://" + PANEL_HOST + ":8443/"
PANEL_KEY = "ptero" + "-KEY-SECRET"
PROBE_HOST = ".".join(["probe", "invalid"])


def test_error_line_is_redacted_by_the_registered_redactor(no_live_redactor, capsys):
    _live.use_redactor(Redactor(token=PANEL_KEY, extra_secrets=(PANEL_URL, PANEL_HOST)))

    async def failing_run():
        raise RuntimeError(f"cannot reach {PANEL_HOST}:443 with key {PANEL_KEY}")

    with pytest.raises(SystemExit) as info:
        _live.main(failing_run)

    assert info.value.code == 1
    assert capsys.readouterr().out.strip() == "ERROR RuntimeError: cannot reach [REDACTED]:443 with key [REDACTED]"


def test_error_redactor_is_empty_when_no_script_registered_one(no_live_redactor):
    assert _live.error_redactor().token == ""


PANEL_PORT = 8443
PROBE_PORT = 27015


def test_ptero_secrets_cover_the_panel_url_its_host_host_port_and_the_bot_token():
    env = {"PTERODACTYL_URL": PANEL_URL, "PTERODACTYL_API_KEY": PANEL_KEY, "FLUXER_TOKEN": "bot-token-value"}
    assert ptero_live.ptero_secrets(env) == (PANEL_URL, PANEL_HOST, f"{PANEL_HOST}:{PANEL_PORT}",
                                             f"[{PANEL_HOST}]:{PANEL_PORT}", "bot-token-value")


def test_ptero_secrets_include_the_default_port_when_the_url_has_none():
    secrets = ptero_live.ptero_secrets({"PTERODACTYL_URL": "https://" + PANEL_HOST + "/"})
    assert f"{PANEL_HOST}:443" in secrets


def test_ptero_secrets_hide_only_the_host_when_the_url_has_no_known_port():
    url = "foo://" + PANEL_HOST + "/"
    assert ptero_live.ptero_secrets({"PTERODACTYL_URL": url}) == (url, PANEL_HOST)


def test_ptero_secrets_skip_a_host_that_is_not_there_and_a_missing_or_empty_bot_token():
    assert ptero_live.ptero_secrets({"PTERODACTYL_URL": "/relative/path"}) == ("/relative/path",)
    assert ptero_live.ptero_secrets({"PTERODACTYL_URL": "/relative/path", "FLUXER_TOKEN": ""}) == ("/relative/path",)


def test_a_panel_error_naming_host_and_port_is_redacted_through_ptero_redactor(no_live_redactor, capsys):
    env = {"PTERODACTYL_URL": PANEL_URL, "PTERODACTYL_API_KEY": PANEL_KEY}
    ptero_live.ptero_redactor(env)

    async def failing_run():
        raise RuntimeError(f"{PANEL_HOST}:{PANEL_PORT} rejected {PANEL_KEY}")

    with pytest.raises(SystemExit):
        _live.main(failing_run)

    out = capsys.readouterr().out
    assert PANEL_HOST not in out
    assert str(PANEL_PORT) not in out
    assert PANEL_KEY not in out
    assert out.strip() == "ERROR RuntimeError: [REDACTED] rejected [REDACTED]"


@pytest.mark.parametrize(("host", "error_text"), [
    (PROBE_HOST, f"probe of {PROBE_HOST}:{PROBE_PORT} failed"),
    (PROBE_IPV6, f"probe of [{PROBE_IPV6}]:{PROBE_PORT} failed"),
])
def test_a_probe_error_naming_the_target_is_redacted_through_probe_redactor(
        no_live_redactor, capsys, host, error_text):
    probes_live.probe_redactor({"FLUXER_TOKEN": "bot-token-value"},
                               [probes_live.Target(kind="a2s", host=host, port=PROBE_PORT)])

    async def failing_run():
        raise RuntimeError(error_text)

    with pytest.raises(SystemExit):
        _live.main(failing_run)

    out = capsys.readouterr().out
    assert host not in out
    assert str(PROBE_PORT) not in out
    assert out.strip() == "ERROR RuntimeError: probe of [REDACTED] failed"


def test_target_secrets_include_the_bare_host_of_each_target():
    target = probes_live.Target(kind="a2s", host=PROBE_HOST, port=PROBE_PORT)
    assert probes_live.target_secrets([target]) == (
        PROBE_HOST, f"{PROBE_HOST}:{PROBE_PORT}", f"[{PROBE_HOST}]:{PROBE_PORT}")


def test_a_probe_error_naming_only_the_bare_host_is_redacted_through_probe_redactor(no_live_redactor, capsys):
    probes_live.probe_redactor({"FLUXER_TOKEN": "bot-token-value"},
                               [probes_live.Target(kind="a2s", host=PROBE_HOST, port=PROBE_PORT)])

    async def failing_run():
        raise RuntimeError(f"cannot resolve {PROBE_HOST}")

    with pytest.raises(SystemExit):
        _live.main(failing_run)

    assert capsys.readouterr().out.strip() == "ERROR RuntimeError: cannot resolve [REDACTED]"


class _DeleteRest:
    """A test double for the one FluxerREST method the test-message cleanup calls. It raises for the ids it is given."""

    def __init__(self, failures: dict[str, Exception]) -> None:
        self.failures = failures
        self.attempted: list[str] = []
        self.deleted: list[str] = []

    async def delete_message(self, channel_id: str, message_id: str) -> None:
        self.attempted.append(message_id)
        if message_id in self.failures:
            raise self.failures[message_id]
        self.deleted.append(message_id)


@pytest.mark.parametrize("error", [
    FluxerHTTPError(500, None, "server error"),
    aiohttp.ClientError("connection reset"),
    TimeoutError(),
])
def test_one_failed_delete_does_not_stop_the_rest_of_the_cleanup(error, capsys):
    redactor = Redactor(token="")
    for message_id in ("1001", "1002", "1003"):
        redactor.label(message_id, "message")
    rest = _DeleteRest({"1002": error})

    asyncio.run(markup_live.delete_sent(rest, "1000", ["1001", "1002", "1003"], redactor))

    assert rest.attempted == ["1001", "1002", "1003"]
    assert rest.deleted == ["1001", "1003"]
    assert capsys.readouterr().out.strip() == "delete failed: <message:2>"


def test_an_unexpected_error_during_cleanup_is_not_swallowed():
    rest = _DeleteRest({"1001": RuntimeError("unexpected")})
    with pytest.raises(RuntimeError):
        asyncio.run(markup_live.delete_sent(rest, "1000", ["1001"], Redactor(token="")))


async def test_run_hides_the_instance_host_in_a_note_the_operator_types(fake, monkeypatch, capsys, no_live_redactor):
    """markup_live.run() passes the instance URL's forms to live(), so a note that echoes the host is printed redacted."""
    _, base = fake
    env = {**required_env(), **MARKUP_ENV, "FLUXER_API_URL": f"{base}/api", "FLUXER_INSTANCE_URL": base}
    parsed = URL(base)
    hostport = f"{parsed.host}:{parsed.port}"
    monkeypatch.setattr(_live, "load_env", lambda: dict(env))
    monkeypatch.setattr(markup_live, "load_env", lambda: dict(env))

    async def operator_note() -> str:
        return f"seen at {hostport}"

    monkeypatch.setattr(markup_live, "wait_for_enter", operator_note)
    await markup_live.run()

    printed = capsys.readouterr().out
    assert "NOTE channel links: seen at [REDACTED]" in printed.splitlines()
    assert hostport not in printed
