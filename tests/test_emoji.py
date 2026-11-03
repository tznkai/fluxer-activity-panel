# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reaction emoji encoding and audit-reason validation (no requests involved)."""

from __future__ import annotations

import pytest

from fluxer_client.rest import _check_reason, encode_emoji


@pytest.mark.parametrize(
    ("emoji", "expected"),
    [
        ("👍", "%F0%9F%91%8D"),
        ("✅", "%E2%9C%85"),
        ("👍🏽", "%F0%9F%91%8D%F0%9F%8F%BD"),
        ("👨\u200d👩\u200d👧", "%F0%9F%91%A8%E2%80%8D%F0%9F%91%A9%E2%80%8D%F0%9F%91%A7"),
        ("party_parrot:123456", "party_parrot:123456"),
    ],
)
def test_encode_emoji_accepts_and_encodes(emoji: str, expected: str) -> None:
    assert encode_emoji(emoji) == expected


@pytest.mark.parametrize(
    "emoji",
    [
        "",
        "   ",
        "a" * 65,
        "a/b",
        "..",
        "a..b",
        "a?b",
        "a#b",
        "a b",
        "\n",
        "name:",
        ":123",
        "na-me:123",
        "name:12a",
        "name:1:2",
        "a" * 33 + ":123",
        "%F0",
    ],
)
def test_encode_emoji_rejects_bad_input(emoji: str) -> None:
    with pytest.raises(ValueError):
        encode_emoji(emoji)


def test_encode_emoji_accepts_exactly_64_characters() -> None:
    assert encode_emoji("a" * 64) == "a" * 64


def test_encode_emoji_error_does_not_echo_the_value() -> None:
    with pytest.raises(ValueError) as info:
        encode_emoji("SENTINEL/bad")
    assert "SENTINEL" not in str(info.value)


def test_check_reason_none_gives_no_header() -> None:
    assert _check_reason(None) == {}


def test_check_reason_valid_gives_audit_header() -> None:
    assert _check_reason("panel rsvp") == {"X-Audit-Log-Reason": "panel rsvp"}


@pytest.mark.parametrize("reason", ["", "x" * 129, "é", "a\nb", "a\tb"])
def test_check_reason_rejects_bad_input(reason: str) -> None:
    with pytest.raises(ValueError):
        _check_reason(reason)


def test_check_reason_accepts_exactly_128_characters() -> None:
    assert _check_reason("x" * 128) == {"X-Audit-Log-Reason": "x" * 128}
