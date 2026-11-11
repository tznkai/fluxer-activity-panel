# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RSVP engine. Pure: presence rows and the current states in, new states and promotions out."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Choice = Literal["going", "maybe", "no"]
State = Literal["going", "waiting", "maybe", "no"]
Rsvps = dict[str, tuple[State, int | None]]   # user id -> (state, waitlist position)

GOING_EMOJI = "✅"
MAYBE_EMOJI = "❔"
NO_EMOJI = "❌"
RSVP_EMOJI: tuple[str, ...] = (GOING_EMOJI, MAYBE_EMOJI, NO_EMOJI)   # seed order
_BY_EMOJI: dict[str, Choice] = {GOING_EMOJI: "going", MAYBE_EMOJI: "maybe", NO_EMOJI: "no"}


def choice_for(name: str) -> Choice | None:
    return _BY_EMOJI.get(name.replace("\ufe0f", ""))


@dataclass(frozen=True)
class Presence:
    user_id: str
    choice: Choice
    seq: int
    implicit: bool


@dataclass(frozen=True)
class Outcome:
    rsvps: Rsvps
    promoted: list[str]


@dataclass(frozen=True)
class Counts:
    going: int
    maybe: int
    waiting: int


def effective(presence: Sequence[Presence], user_id: str) -> Choice | None:
    mine = [p for p in presence if p.user_id == user_id]
    return max(mine, key=lambda p: p.seq).choice if mine else None


def apply(current: Rsvps, presence: Sequence[Presence], seats: int | None) -> Outcome:
    users = sorted({p.user_id for p in presence})
    wanted = {u: effective(presence, u) for u in users}
    going_seq = {p.user_id: p.seq for p in presence if p.choice == "going"}
    state = {u: current.get(u, ("", None))[0] for u in users}

    going = [u for u in users if wanted[u] == "going" and state[u] == "going"]
    waiting = sorted((u for u in users if wanted[u] == "going" and state[u] == "waiting"),
                     key=lambda u: current[u][1] or 0)
    newcomers = sorted((u for u in users if wanted[u] == "going" and state[u] not in ("going", "waiting")),
                       key=lambda u: going_seq[u])
    for u in newcomers:
        if seats is None or len(going) < seats:
            going.append(u)
        else:
            waiting.append(u)
    promoted: list[str] = []
    while waiting and (seats is None or len(going) < seats):
        u = waiting.pop(0)
        going.append(u)
        if state[u] == "waiting":
            promoted.append(u)
    out: Rsvps = {u: ("going", None) for u in going}
    out.update({u: ("waiting", i) for i, u in enumerate(waiting, start=1)})
    out.update({u: (wanted[u], None) for u in users if wanted[u] in ("maybe", "no")})
    return Outcome(rsvps=out, promoted=promoted)


def counts(rsvps: Rsvps) -> Counts:
    states = [s for s, _ in rsvps.values()]
    return Counts(going=states.count("going"), maybe=states.count("maybe"), waiting=states.count("waiting"))
