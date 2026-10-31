# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the live-test harness (tools/live/_live.py) against the fake REST server."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import sys
from pathlib import Path

import aiohttp
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "live"))
import _live  # noqa: E402
from _live import (  # noqa: E402
    Live,
    LiveRefused,
    Redactor,
    capture_version,
    check_safety,
    load_env,
    out,
    read_env_file,
)
from fluxer_client.rest import FluxerREST  # noqa: E402
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
        "emoji": {"id": None, "name": "👍", "animated": False},
        "content": "secret chat text",
    }
    shaped = redactor.shape(event)
    assert LONG_RUN.search(repr(shaped)) is None
    assert shaped["emoji"]["name"] == "👍"
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
