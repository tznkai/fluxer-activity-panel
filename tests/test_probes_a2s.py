# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A2S probe against the fake Source server: challenge handshake, details, failures and the DEBUG log stream."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from io import StringIO

import pytest

from fluxer_panel.logs import setup_logging
from fluxer_panel.sources.a2s import A2SProbe
from fluxer_panel.sources.base import PlayerCounts, ProbeDetails, ProbeFailed, ProbeResult
from fluxer_panel.sources.minecraft import BedrockProbe
from tests.fake_probes import a2s_reply, bedrock_reply, udp_server

HOST = "127.0.0.1"
CHALLENGE = b"\x01\x02\x03\x04"  # must be non-zero: python-a2s only appends a truthy challenge
INFO = {"players": 2, "max": 16, "map": "oilrig", "version": "1.0.4"}
SOURCE_QUERY = b"Source Engine Query"
PINNED_LOGGERS = ("a2s", "mcstatus")


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


# AC3.1: A plain Source server gives its player counts, map and version.
async def test_plain_source_server_gives_counts_and_details() -> None:
    async with udp_server(a2s_reply(INFO)) as (port, _):
        result = await A2SProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeResult(PlayerCounts(2, 16), ProbeDetails(map="oilrig", version="1.0.4"))


# AC3.1: A server that first sends a 0x41 challenge is answered again with the challenge appended.
async def test_challenge_server_is_answered_with_the_challenge() -> None:
    async with udp_server(a2s_reply(INFO, challenge=CHALLENGE)) as (port, recorder):
        result = await A2SProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeResult(PlayerCounts(2, 16), ProbeDetails(map="oilrig", version="1.0.4"))
    assert len(recorder.requests) == 2
    assert recorder.requests[1].endswith(CHALLENGE)


# AC3.1: A legacy GoldSrc reply has no version field, so the version is left empty and the counts still come through.
async def test_goldsrc_reply_has_counts_and_no_version() -> None:
    async with udp_server(a2s_reply(INFO, goldsrc=True)) as (port, _):
        result = await A2SProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeResult(PlayerCounts(2, 16), ProbeDetails(map="oilrig", version=None))


# AC3.3: The map name is cleaned of colour codes and control characters; the A2S game mode stays empty.
async def test_map_name_is_sanitised_and_game_mode_stays_empty() -> None:
    info = {**INFO, "map": "§coil\x07rig"}
    async with udp_server(a2s_reply(info)) as (port, _):
        result = await A2SProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeResult(PlayerCounts(2, 16), ProbeDetails(map="oilrig", version="1.0.4"))
    assert isinstance(result, ProbeResult)
    assert result.details.game_mode is None


# AC3.5: A silent A2S endpoint times out after exactly one datagram, with no retry.
async def test_silent_a2s_endpoint_times_out_after_one_datagram() -> None:
    async with udp_server(lambda datagram, index: None) as (port, recorder):
        result = await A2SProbe().probe(HOST, port, timeout=0.3)
    assert result == ProbeFailed("timeout")
    assert len(recorder.requests) == 1


# AC3.5: A server that answers every request with a challenge is malformed once the library gives up.
async def test_endless_challenge_is_malformed() -> None:
    async with udp_server(a2s_reply(INFO, challenge=CHALLENGE, always_challenge=True)) as (port, _):
        result = await A2SProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeFailed("malformed")


# AC3.5: A 0x49 reply cut off after the map string is malformed.
async def test_truncated_info_reply_is_malformed() -> None:
    async with udp_server(a2s_reply(INFO, truncate_after_map=True)) as (port, _):
        result = await A2SProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeFailed("malformed")


# AC3.5: An unknown response header is malformed.
async def test_unknown_header_is_malformed() -> None:
    async with udp_server(a2s_reply(INFO, unknown_header=True)) as (port, _):
        result = await A2SProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeFailed("malformed")


# AC3.5: A 2000-character map name is an oversized reply and is malformed.
async def test_oversized_map_name_is_malformed() -> None:
    async with udp_server(a2s_reply({**INFO, "map": "m" * 2000})) as (port, _):
        result = await A2SProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeFailed("malformed")


# AC3.6: At DEBUG, no a2s or mcstatus record is emitted, and the raw query never reaches the log stream.
async def test_debug_logging_emits_no_probe_library_records(json_logs) -> None:
    stream = json_logs(["SENTINEL-TOKEN"])
    async with udp_server(a2s_reply(INFO)) as (a2s_port, _):
        a2s_result = await A2SProbe().probe(HOST, a2s_port, timeout=2.0)
    bedrock_fields = ["MCPE", "motd", "924", "1.21.50", "3", "10", "1234567", "map", "Survival"]
    async with udp_server(bedrock_reply(bedrock_fields)) as (bedrock_port, _):
        bedrock_result = await BedrockProbe().probe(HOST, bedrock_port, timeout=2.0)
    assert isinstance(a2s_result, ProbeResult)
    assert isinstance(bedrock_result, ProbeResult)

    lines = [line for line in stream.getvalue().splitlines() if line]
    loggers = [json.loads(line)["logger"] for line in lines]
    assert not [name for name in loggers if name.startswith(("a2s", "mcstatus"))]
    assert SOURCE_QUERY.decode() not in stream.getvalue()
    assert logging.getLogger("a2s").isEnabledFor(logging.DEBUG) is False
