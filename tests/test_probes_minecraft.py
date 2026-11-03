# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minecraft Bedrock and Java probes against the fake query servers: counts, details, failures and logs."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from io import StringIO

import pytest

from fluxer_panel.logs import setup_logging
from fluxer_panel.sources.base import PlayerCounts, ProbeDetails, ProbeFailed, ProbeResult
from fluxer_panel.sources.minecraft import BedrockProbe, JavaProbe
from tests.fake_probes import bedrock_reply, closed_port, java_server, udp_server

HOST = "127.0.0.1"
SENTINEL_PLAYER = "SENTINEL-PLAYER-a1"
SAMPLE = [{"name": SENTINEL_PLAYER, "id": "00000000-0000-0000-0000-000000000001"}]
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


def bedrock_fields(
    *,
    online: str = "3",
    maximum: str = "10",
    version: str = "1.21.50",
    map_name: str = "Bedrock level",
    gamemode: str = "Survival",
) -> list[str]:
    return ["MCPE", "My Server", "924", version, online, maximum, "1234567", map_name, gamemode]


def java_json(
    *,
    online: object = 3,
    maximum: object = 10,
    version: str = "1.21.4",
    sample: list[dict[str, str]] | None = None,
) -> bytes:
    players: dict[str, object] = {"online": online, "max": maximum}
    if sample is not None:
        players["sample"] = sample
    body = {"version": {"name": version, "protocol": 769}, "players": players, "description": "A server"}
    return json.dumps(body).encode()


# AC3.2: Bedrock and Java return the online and max counts from their fake servers.
async def test_bedrock_returns_counts_from_fake() -> None:
    async with udp_server(bedrock_reply(bedrock_fields())) as (port, _):
        result = await BedrockProbe().probe(HOST, port, timeout=2.0)
    assert isinstance(result, ProbeResult)
    assert result.counts == PlayerCounts(3, 10)


async def test_java_returns_counts_from_fake() -> None:
    async with java_server(java_json()) as (port, _):
        result = await JavaProbe().probe(HOST, port, timeout=2.0)
    assert isinstance(result, ProbeResult)
    assert result.counts == PlayerCounts(3, 10)


# AC3.3: Bedrock fills map, version and game mode; each is cleaned of control and colour codes.
async def test_bedrock_details_are_filled_and_sanitised() -> None:
    fields = bedrock_fields(version="§a1.21.50", map_name="Bedrock level\x07", gamemode="Survival")
    async with udp_server(bedrock_reply(fields)) as (port, _):
        result = await BedrockProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeResult(
        PlayerCounts(3, 10), ProbeDetails(map="Bedrock level", version="1.21.50", game_mode="Survival")
    )


# AC3.3: Java fills only the version, capped at 48 characters, and leaves map and game mode empty.
async def test_java_version_is_capped_and_other_details_stay_empty() -> None:
    async with java_server(java_json(version="v" * 60)) as (port, _):
        result = await JavaProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeResult(PlayerCounts(3, 10), ProbeDetails(version="v" * 48))


# AC3.4: Sentinel player names in a Java status sample never reach the result, its repr or the logs.
async def test_player_names_in_sample_never_reach_result_or_logs(json_logs) -> None:
    stream = json_logs([])
    async with java_server(java_json(sample=SAMPLE)) as (port, _):
        result = await JavaProbe().probe(HOST, port, timeout=2.0)
    assert isinstance(result, ProbeResult)
    assert SENTINEL_PLAYER not in repr(result)
    assert SENTINEL_PLAYER not in stream.getvalue()


# AC3.4: A malformed sample still fails without the name reaching the failure, its repr or the logs.
async def test_malformed_sample_fails_without_player_name(json_logs) -> None:
    stream = json_logs([])
    async with java_server(java_json(maximum="ten", sample=SAMPLE)) as (port, _):
        result = await JavaProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeFailed("malformed")
    assert SENTINEL_PLAYER not in repr(result)
    assert SENTINEL_PLAYER not in stream.getvalue()


# AC3.5: A silent Bedrock endpoint times out after exactly one datagram, with no retry.
async def test_silent_bedrock_endpoint_times_out_after_one_request() -> None:
    async with udp_server(lambda datagram, index: None) as (port, recorder):
        result = await BedrockProbe().probe(HOST, port, timeout=0.3)
    assert result == ProbeFailed("timeout")
    assert len(recorder.requests) == 1


# AC3.5: A silent Java endpoint times out on one connection with one handshake and one status request.
async def test_silent_java_endpoint_times_out_without_resend() -> None:
    async with java_server(None) as (port, recorder):
        result = await JavaProbe().probe(HOST, port, timeout=0.3)
    assert result == ProbeFailed("timeout")
    assert recorder.connections == 1
    assert len(recorder.requests) == 2


# AC3.5: A closed Java port is refused.
async def test_closed_java_port_is_refused() -> None:
    result = await JavaProbe().probe(HOST, closed_port(), timeout=2.0)
    assert result == ProbeFailed("refused")


# AC3.5: A closed Bedrock UDP port is refused or times out, depending on whether ICMP reaches the probe.
async def test_closed_bedrock_port_is_refused_or_times_out() -> None:
    result = await BedrockProbe().probe(HOST, closed_port(), timeout=0.3)
    assert result in (ProbeFailed("refused"), ProbeFailed("timeout"))


# AC3.5: A Bedrock reply shorter than the fixed header is malformed.
async def test_short_bedrock_reply_is_malformed() -> None:
    async with udp_server(lambda datagram, index: b"\x1c" + b"\x00" * 20) as (port, _):
        result = await BedrockProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeFailed("malformed")


# AC3.5: A Bedrock reply with too few MOTD fields is malformed.
async def test_bedrock_reply_with_three_fields_is_malformed() -> None:
    async with udp_server(bedrock_reply(["MCPE", "motd", "924"])) as (port, _):
        result = await BedrockProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeFailed("malformed")


# AC3.5: A Java reply whose JSON is a list, not an object, is malformed.
async def test_java_list_json_is_malformed() -> None:
    async with java_server(b"[]") as (port, _):
        result = await JavaProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeFailed("malformed")


# AC3.5: A Java reply that is not JSON is wrapped by mcstatus in OSError, so it maps to error.
async def test_java_non_json_reply_is_error() -> None:
    async with java_server(b"not json at all") as (port, _):
        result = await JavaProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeFailed("error")


# AC3.5: An oversized Bedrock map name is malformed.
async def test_oversized_bedrock_map_is_malformed() -> None:
    fields = bedrock_fields(map_name="m" * 4000)
    async with udp_server(bedrock_reply(fields)) as (port, _):
        result = await BedrockProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeFailed("malformed")


# AC3.5: A Java player count above the allowed maximum is malformed.
async def test_oversized_java_player_count_is_malformed() -> None:
    async with java_server(java_json(online=10**9)) as (port, _):
        result = await JavaProbe().probe(HOST, port, timeout=2.0)
    assert result == ProbeFailed("malformed")
