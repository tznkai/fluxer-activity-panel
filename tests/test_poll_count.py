# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for poll counting: usable options, approval totals and instant-runoff rounds."""

from __future__ import annotations

from dataclasses import dataclass

from fluxer_panel import poll_count

NOW = 1_000_000


@dataclass(frozen=True)
class Opt:
    id: int
    starts_at: int


def test_usable_keeps_an_option_exactly_an_hour_away() -> None:
    option = Opt(1, NOW + 3600)
    assert poll_count.usable([option], now=NOW) == [option]


def test_usable_drops_an_option_less_than_an_hour_away() -> None:
    assert poll_count.usable([Opt(1, NOW + 3599)], now=NOW) == []


def test_usable_keeps_the_order_of_the_options() -> None:
    later, sooner = Opt(1, NOW + 9000), Opt(2, NOW + 4000)
    assert poll_count.usable([later, sooner], now=NOW) == [later, sooner]


# AC6.4: approval ranking orders by votes, ties to the soonest time.
def test_approval_orders_by_votes_and_breaks_ties_to_the_soonest() -> None:
    options = [Opt(1, NOW + 300), Opt(2, NOW + 200), Opt(3, NOW + 100)]
    votes = [("u1", 1), ("u2", 2)]
    assert poll_count.approval(options, votes) == [(2, 1), (1, 1), (3, 0)]


def test_approval_lists_unpicked_options_last_soonest_first() -> None:
    options = [Opt(4, NOW + 400), Opt(5, NOW + 150)]
    assert poll_count.approval(options, []) == [(5, 0), (4, 0)]


def test_approval_ignores_votes_for_unknown_options() -> None:
    options = [Opt(1, NOW + 100)]
    assert poll_count.approval(options, [("u1", 99)]) == [(1, 0)]


def test_ranked_with_votes_drops_options_nobody_picked() -> None:
    options = [Opt(1, NOW + 300), Opt(2, NOW + 200), Opt(3, NOW + 100)]
    assert poll_count.ranked_with_votes(options, [("u1", 1), ("u2", 1)]) == [(1, 2)]


# AC6.5: a first-choice majority ends the count in round 1.
def test_first_choice_majority_wins_in_round_one() -> None:
    options = [Opt(1, NOW + 100), Opt(2, NOW + 200), Opt(3, NOW + 300)]
    ballots = [[1], [1], [1], [2], [3]]
    result = poll_count.instant_runoff(options, ballots)
    assert len(result.rounds) == 1
    assert result.rounds[0].dropped is None
    assert result.winner == 1


# AC6.6: the last place is dropped and its ballots move on; a ballot with no choice left is exhausted.
# Round 1: 1 has 2 votes, 2 has 3, 3 has 3 of 8 ballots, so no majority and option 1 is dropped.
# Round 2: 1's ballot [1, 2] moves to 2 (now 4 of the 7 still counting, a majority), and [1] is exhausted.
# Option 2 wins on 4 of 7, though 4 of all 8 ballots would not be a majority.
def test_last_place_is_dropped_and_its_ballots_move_to_the_next_choice() -> None:
    options = [Opt(1, NOW + 100), Opt(2, NOW + 200), Opt(3, NOW + 300)]
    ballots = [[1, 2], [1], [2], [2], [2], [3], [3], [3]]
    result = poll_count.instant_runoff(options, ballots)

    assert len(result.rounds) == 2
    assert result.rounds[0].counts == ((2, 3), (3, 3), (1, 2))
    assert result.rounds[0].dropped == 1
    assert result.rounds[0].exhausted == 0
    assert result.rounds[1].dropped is None
    assert result.rounds[1].exhausted == 1
    assert result.winner == 2


# AC6.7: a last-place tie drops the latest time.
def test_a_last_place_tie_drops_the_latest_time() -> None:
    options = [Opt(1, NOW + 100), Opt(2, NOW + 200), Opt(3, NOW + 300)]
    ballots = [[1], [1], [2], [2], [3], [3]]
    result = poll_count.instant_runoff(options, ballots)
    assert result.rounds[0].dropped == 3


# AC6.7: a final-round tie goes to the soonest.
def test_a_final_round_tie_goes_to_the_soonest() -> None:
    options = [Opt(1, NOW + 100), Opt(2, NOW + 200), Opt(3, NOW + 300)]
    ballots = [[1], [1], [2], [2], [3], [3]]
    result = poll_count.instant_runoff(options, ballots)
    assert result.winner == 1


def test_a_two_way_tie_goes_to_the_soonest() -> None:
    options = [Opt(1, NOW + 200), Opt(2, NOW + 100)]
    result = poll_count.instant_runoff(options, [[1], [2]])
    assert result.winner == 2


def test_a_single_option_wins_with_any_vote() -> None:
    result = poll_count.instant_runoff([Opt(1, NOW + 100)], [[1]])
    assert len(result.rounds) == 1
    assert result.winner == 1


def test_no_ballots_means_no_winner() -> None:
    result = poll_count.instant_runoff([Opt(1, NOW + 100), Opt(2, NOW + 200)], [])
    assert result.winner is None


def test_ballots_ranking_only_unknown_options_mean_no_winner() -> None:
    result = poll_count.instant_runoff([Opt(1, NOW + 100)], [[99]])
    assert result.winner is None
