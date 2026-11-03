# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Source base types and the static plugin registry: detail sanitising and the kind-to-plugin map."""

from fluxer_panel.sources.base import DETAIL_MAX, sanitize_detail


def test_sanitize_removes_minecraft_colour_codes_and_joins_words() -> None:
    assert sanitize_detail("§aHello§r World") == "Hello World"


def test_sanitize_removes_control_characters_and_trailing_lone_section_sign() -> None:
    assert sanitize_detail("ab\x07cd\x1b\ny§") == "abcdy"


def test_sanitize_truncates_to_detail_max() -> None:
    result = sanitize_detail("x" * 100)
    assert result == "x" * DETAIL_MAX
    assert len(result) == 48


def test_sanitize_empty_and_whitespace_give_none() -> None:
    assert sanitize_detail("") is None
    assert sanitize_detail("   \t  ") is None


def test_sanitize_non_str_gives_none() -> None:
    assert sanitize_detail(None) is None
    assert sanitize_detail(42) is None
    assert sanitize_detail(b"bytes") is None
