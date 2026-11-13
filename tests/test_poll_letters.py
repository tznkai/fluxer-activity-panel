# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the poll option letters."""

from __future__ import annotations

import pytest

from fluxer_panel import poll_letters, polls


def test_single_letters_cover_the_alphabet() -> None:
    assert poll_letters.letter(0) == "a"
    assert poll_letters.letter(25) == "z"


def test_two_letter_labels_follow_the_single_letters() -> None:
    assert poll_letters.letter(26) == "aa"
    assert poll_letters.letter(51) == "az"
    assert poll_letters.letter(52) == "ba"
    assert poll_letters.letter(701) == "zz"


@pytest.mark.parametrize("position", [-1, 702])
def test_letter_refuses_positions_out_of_range(position: int) -> None:
    with pytest.raises(ValueError):
        poll_letters.letter(position)


def test_position_round_trips_every_letter() -> None:
    for position in range(poll_letters.COUNT):
        assert poll_letters.position(poll_letters.letter(position)) == position


@pytest.mark.parametrize("label", ["", "aaa", "A", "1", "a "])
def test_position_is_none_for_anything_that_is_not_a_label(label: str) -> None:
    assert poll_letters.position(label) is None


def test_count_matches_the_poll_option_cap() -> None:
    assert poll_letters.COUNT == polls.MAX_OPTIONS
