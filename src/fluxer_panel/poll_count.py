# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Poll counting. Pure: options and votes in, approval totals or instant-runoff rounds out."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from fluxer_panel import session_time

LEAD_SECONDS = int(session_time.MIN_LEAD.total_seconds())


class OptionLike(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def starts_at(self) -> int: ...


@dataclass(frozen=True)
class Round:
    counts: tuple[tuple[int, int], ...]   # (option id, first-choice count), count desc then soonest
    dropped: int | None                    # option id dropped after this round, or None for the last round
    exhausted: int                         # ballots with no remaining choice in this round


@dataclass(frozen=True)
class Rounds:
    rounds: tuple[Round, ...]
    winner: int | None                     # None when no ballot ranks any option


def usable[T: OptionLike](options: Iterable[T], *, now: int) -> list[T]:
    """Options at least an hour away (MIN_LEAD), order kept."""
    return [option for option in options if option.starts_at - now >= LEAD_SECONDS]


def approval(options: Sequence[OptionLike], votes: Iterable[tuple[str, int]]) -> list[tuple[int, int]]:
    """(option id, count) for every option, count desc, then soonest. Votes for other options are ignored."""
    counts = {option.id: 0 for option in options}
    for _user, option_id in votes:
        if option_id in counts:
            counts[option_id] += 1
    when = {option.id: option.starts_at for option in options}
    return sorted(counts.items(), key=lambda pair: (-pair[1], when[pair[0]], pair[0]))


def ranked_with_votes(options: Sequence[OptionLike], votes: Iterable[tuple[str, int]]) -> list[tuple[int, int]]:
    """approval() without the options nobody picked."""
    return [pair for pair in approval(options, votes) if pair[1] > 0]


def instant_runoff(options: Sequence[OptionLike], ranked_ballots: Iterable[Sequence[int]]) -> Rounds:
    """Each ballot is option ids, favourite first. Ids not in `options` are skipped.

    A first-choice majority of the ballots still counting wins. Otherwise the last-place option is dropped
    (a tie drops the latest time) and its ballots move to their next remaining choice. With two left and
    no majority (a tie), the soonest wins.
    """
    when = {option.id: option.starts_at for option in options}
    ballots = [[oid for oid in ballot if oid in when] for ballot in ranked_ballots]
    remaining = set(when)
    rounds: list[Round] = []
    while True:
        counts = dict.fromkeys(remaining, 0)
        exhausted = 0
        for ballot in ballots:
            top = next((oid for oid in ballot if oid in remaining), None)
            if top is None:
                exhausted += 1
            else:
                counts[top] += 1
        ordered = tuple(sorted(counts.items(), key=lambda pair: (-pair[1], when[pair[0]], pair[0])))
        active = len(ballots) - exhausted
        if active == 0:
            rounds.append(Round(ordered, None, exhausted))
            return Rounds(tuple(rounds), None)
        leader, lead = ordered[0]
        if lead * 2 > active or len(remaining) <= 2:
            rounds.append(Round(ordered, None, exhausted))
            return Rounds(tuple(rounds), leader)
        low = min(counts.values())
        dropped = max((oid for oid, count in counts.items() if count == low), key=lambda oid: (when[oid], oid))
        rounds.append(Round(ordered, dropped, exhausted))
        remaining.discard(dropped)
