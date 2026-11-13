# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the poll reply parser: one reply in, picks or a refusal out."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fluxer_panel import poll_reply, session_time, text

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=NY)  # a Wednesday
LETTERS = {"a": 10, "b": 11, "c": 12, "h": 17}


def ballot(reply: str, *, runoff: bool = False) -> poll_reply.Reply | poll_reply.Refusal:
    return poll_reply.parse(reply, runoff=runoff, now=NOW, tz=NY, letters=LETTERS)


def fri_8pm() -> int:
    return int(datetime(2026, 11, 13, 20, 0, tzinfo=NY).timestamp())


# AC3.1: a reply sets the picks to exactly those letters ----------------------------------------------

def test_letters_become_picks_in_reply_order() -> None:
    assert ballot("a, c") == poll_reply.Picks((poll_reply.Letter(10), poll_reply.Letter(12)))


def test_case_and_spaces_are_ignored() -> None:
    assert ballot("A ,C") == poll_reply.Picks((poll_reply.Letter(10), poll_reply.Letter(12)))


# AC3.2: clear and all are whole replies -------------------------------------------------------------

def test_clear_is_its_own_reply_in_any_case() -> None:
    assert ballot("clear") == poll_reply.Clear()
    assert ballot("CLEAR") == poll_reply.Clear()


def test_all_is_its_own_reply_for_the_caller_to_expand() -> None:
    assert ballot("all") == poll_reply.All()


# AC3.5: a letter for an option not on the ballot is accepted --------------------------------------

def test_a_letter_outside_the_shown_ballot_is_accepted() -> None:
    assert ballot("h") == poll_reply.Picks((poll_reply.Letter(17),))


# AC3.6: an unreadable item is refused with its position ---------------------------------------------

def test_unreadable_clock_is_refused_as_format() -> None:
    assert ballot("a, sat 2pm") == poll_reply.Refusal(2, "format")


def test_unknown_letter_is_refused_as_no_option() -> None:
    assert ballot("zz") == poll_reply.Refusal(1, "no_option")


def test_time_less_than_an_hour_away_is_too_soon() -> None:
    assert ballot("today 12:30") == poll_reply.Refusal(1, "too_soon")


def test_time_already_passed_is_past() -> None:
    assert ballot("today 11:00") == poll_reply.Refusal(1, "past")


def test_empty_item_is_refused_with_its_position() -> None:
    assert ballot("a,,c") == poll_reply.Refusal(2, "empty")


def test_all_with_another_item_is_keyword_alone() -> None:
    assert ballot("all, fri 20:00") == poll_reply.Refusal(1, "keyword_alone")


def test_clear_with_another_item_is_keyword_alone() -> None:
    assert ballot("a, clear") == poll_reply.Refusal(2, "keyword_alone")


def test_more_than_the_item_cap_names_item_twenty_one() -> None:
    reply = ", ".join(["a"] * 21)
    assert ballot(reply) == poll_reply.Refusal(21, "too_many")


def test_weekday_name_for_today_is_weekday_today() -> None:
    assert ballot("wed 14:00") == poll_reply.Refusal(1, "weekday_today")


# Typed times -----------------------------------------------------------------------------------------

def test_a_typed_time_becomes_a_timestamp_pick() -> None:
    assert ballot("a, fri 20:00") == poll_reply.Picks(
        (poll_reply.Letter(10), poll_reply.TypedTime(fri_8pm())))


def test_a_time_exactly_one_hour_away_is_accepted() -> None:
    expected = int(datetime(2026, 11, 11, 13, 0, tzinfo=NY).timestamp())
    assert ballot("today 13:00") == poll_reply.Picks((poll_reply.TypedTime(expected),))


def test_a_repeated_letter_in_an_approval_reply_is_dropped() -> None:
    assert ballot("a, a") == poll_reply.Picks((poll_reply.Letter(10),))


# AC7.5: a runoff takes letters only, once each, and may be partial ---------------------------------

def test_a_runoff_refuses_a_typed_time() -> None:
    assert ballot("fri 20:00", runoff=True) == poll_reply.Refusal(1, "letters_only")


def test_a_runoff_refuses_all() -> None:
    assert ballot("all", runoff=True) == poll_reply.Refusal(1, "no_all")


def test_a_runoff_refuses_a_repeated_letter() -> None:
    assert ballot("a, b, a", runoff=True) == poll_reply.Refusal(3, "repeated")


def test_a_runoff_accepts_a_partial_ranking_in_rank_order() -> None:
    assert ballot("c, a", runoff=True) == poll_reply.Picks((poll_reply.Letter(12), poll_reply.Letter(10)))


def test_a_runoff_accepts_clear() -> None:
    assert ballot("clear", runoff=True) == poll_reply.Clear()


# Text parity -----------------------------------------------------------------------------------------

def test_every_refusal_reason_has_a_text() -> None:
    assert set(text.POLL_REPLY_REFUSED) == set(poll_reply.REASONS)


def test_poll_reasons_do_not_collide_with_session_time_reasons() -> None:
    assert set(poll_reply.REASONS).isdisjoint(session_time.REASONS)
