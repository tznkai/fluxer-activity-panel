# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RSVP engine: pure rules, no database and no REST."""

from __future__ import annotations

from fluxer_panel.rsvp import Counts, Presence, apply, choice_for, counts, effective


def P(user: str, choice: str, seq: int, implicit: bool = False) -> Presence:
    return Presence(user_id=user, choice=choice, seq=seq, implicit=implicit)  # type: ignore[arg-type]


# AC4.1: one reaction gives its state; the emoji map -------------------------------------------------

def test_a_single_reaction_sets_the_state() -> None:
    assert apply({}, [P("1001", "going", 1)], None).rsvps == {"1001": ("going", None)}
    assert apply({}, [P("1001", "maybe", 1)], None).rsvps == {"1001": ("maybe", None)}
    assert apply({}, [P("1001", "no", 1)], None).rsvps == {"1001": ("no", None)}


def test_emoji_names_map_to_choices_and_others_map_to_none() -> None:
    assert choice_for("\u2705\ufe0f") == "going"
    assert choice_for("✅") == "going"
    assert choice_for("❔") == "maybe"
    assert choice_for("❌") == "no"
    assert choice_for("\U0001F642") is None


# AC4.2: the newest row decides ----------------------------------------------------------------------

def test_the_newest_row_decides_the_state() -> None:
    presence = [P("1001", "going", 2), P("1001", "no", 5)]

    assert effective(presence, "1001") == "no"
    assert apply({}, presence, None).rsvps == {"1001": ("no", None)}


# AC4.3: removing rows falls back, removing the last clears -------------------------------------------

def test_removing_the_newest_row_falls_back_to_the_next_newest() -> None:
    current = {"1001": ("no", None)}
    remaining = [P("1001", "going", 2)]   # the seq 5 row was removed

    assert apply(current, remaining, None).rsvps == {"1001": ("going", None)}


def test_removing_the_last_row_clears_the_rsvp() -> None:
    current = {"1001": ("going", None)}

    assert apply(current, [], None).rsvps == {}


# AC4.4: the proposer's implicit going row ----------------------------------------------------------

def test_the_proposers_implicit_going_row_counts_as_going() -> None:
    presence = [P("1001", "going", 1, implicit=True)]

    assert apply({}, presence, None).rsvps == {"1001": ("going", None)}


def test_a_real_reaction_replaces_the_implicit_row() -> None:
    current = {"1001": ("going", None)}
    presence = [P("1001", "maybe", 2)]   # the implicit row is gone; only the real reaction remains

    assert apply(current, presence, None).rsvps == {"1001": ("maybe", None)}


# AC4.5: past the seats, going waits at the back ------------------------------------------------------

def test_past_the_seats_a_going_reaction_waits_at_the_back() -> None:
    presence = [P("a", "going", 1), P("b", "going", 2), P("c", "going", 3)]

    first = apply({}, presence, 2)

    assert first.rsvps == {"a": ("going", None), "b": ("going", None), "c": ("waiting", 1)}
    assert first.promoted == []


def test_a_newcomer_with_a_seat_free_is_not_promoted() -> None:
    current = {"a": ("going", None)}
    presence = [P("a", "going", 1), P("b", "going", 2)]

    outcome = apply(current, presence, 2)

    assert outcome.rsvps == {"a": ("going", None), "b": ("going", None)}
    assert outcome.promoted == []


def test_a_fourth_going_reaction_waits_at_position_two() -> None:
    current = {"a": ("going", None), "b": ("going", None), "c": ("waiting", 1)}
    presence = [P("a", "going", 1), P("b", "going", 2), P("c", "going", 3), P("d", "going", 4)]

    outcome = apply(current, presence, 2)

    assert outcome.rsvps["c"] == ("waiting", 1)
    assert outcome.rsvps["d"] == ("waiting", 2)


# AC4.6: a going user leaving promotes; a waiting user leaving renumbers -----------------------------

_QUEUE = {"a": ("going", None), "b": ("going", None), "c": ("waiting", 1), "d": ("waiting", 2)}


def test_a_going_user_changing_to_maybe_promotes_the_front_of_the_queue() -> None:
    presence = [P("a", "going", 1), P("a", "maybe", 5), P("b", "going", 2), P("c", "going", 3), P("d", "going", 4)]

    outcome = apply(_QUEUE, presence, 2)

    assert outcome.rsvps == {
        "a": ("maybe", None),
        "b": ("going", None),
        "c": ("going", None),
        "d": ("waiting", 1),
    }
    assert outcome.promoted == ["c"]


def test_a_going_users_only_row_removed_promotes_the_front_of_the_queue() -> None:
    presence = [P("b", "going", 2), P("c", "going", 3), P("d", "going", 4)]

    outcome = apply(_QUEUE, presence, 2)

    assert outcome.rsvps == {"b": ("going", None), "c": ("going", None), "d": ("waiting", 1)}
    assert outcome.promoted == ["c"]


def test_a_waiting_user_leaving_is_removed_and_nobody_is_promoted() -> None:
    presence = [P("a", "going", 1), P("b", "going", 2), P("c", "going", 3), P("c", "no", 5), P("d", "going", 4)]

    outcome = apply(_QUEUE, presence, 2)

    assert outcome.rsvps == {
        "a": ("going", None),
        "b": ("going", None),
        "c": ("no", None),
        "d": ("waiting", 1),
    }
    assert outcome.promoted == []


# AC4.7: changing the seats -------------------------------------------------------------------------

def test_raising_the_seats_promotes_from_the_front_while_there_is_room() -> None:
    current = {"a": ("going", None), "b": ("waiting", 1), "c": ("waiting", 2)}
    presence = [P("a", "going", 1), P("b", "going", 2), P("c", "going", 3)]

    outcome = apply(current, presence, 3)

    assert outcome.rsvps == {"a": ("going", None), "b": ("going", None), "c": ("going", None)}
    assert outcome.promoted == ["b", "c"]


def test_lowering_the_seats_moves_nobody_out_of_going() -> None:
    current = {"a": ("going", None), "b": ("going", None), "c": ("going", None)}
    presence = [P("a", "going", 1), P("b", "going", 2), P("c", "going", 3)]

    outcome = apply(current, presence, 1)

    assert outcome.rsvps == {"a": ("going", None), "b": ("going", None), "c": ("going", None)}
    assert outcome.promoted == []


def test_a_new_going_reaction_after_lowering_the_seats_waits() -> None:
    current = {"a": ("going", None), "b": ("going", None), "c": ("going", None)}
    presence = [P("a", "going", 1), P("b", "going", 2), P("c", "going", 3), P("d", "going", 4)]

    outcome = apply(current, presence, 1)

    assert outcome.rsvps["d"] == ("waiting", 1)


def test_no_seat_limit_promotes_everyone_waiting_in_order() -> None:
    current = {"a": ("going", None), "b": ("waiting", 1), "c": ("waiting", 2)}
    presence = [P("a", "going", 1), P("b", "going", 2), P("c", "going", 3)]

    outcome = apply(current, presence, None)

    assert outcome.rsvps == {"a": ("going", None), "b": ("going", None), "c": ("going", None)}
    assert outcome.promoted == ["b", "c"]


# counts ----------------------------------------------------------------------------------------------

def test_counts_total_going_maybe_and_waiting_and_ignore_no() -> None:
    rsvps = {
        "a": ("going", None),
        "b": ("going", None),
        "c": ("maybe", None),
        "d": ("no", None),
        "e": ("waiting", 1),
        "f": ("waiting", 2),
    }

    assert counts(rsvps) == Counts(going=2, maybe=1, waiting=2)
