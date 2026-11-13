# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Poll replies. Pure: one DM reply in, the voter's new picks or a refusal out."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from fluxer_panel import poll_letters, session_time

MAX_ITEMS = 20
LEAD_SECONDS = int(session_time.MIN_LEAD.total_seconds())
REASONS = ("empty", "keyword_alone", "too_many", "no_option", "too_soon", "letters_only", "no_all", "repeated")


@dataclass(frozen=True)
class Clear:
    pass


@dataclass(frozen=True)
class All:
    pass


@dataclass(frozen=True)
class Letter:
    option_id: int


@dataclass(frozen=True)
class TypedTime:
    starts_at: int          # Unix seconds


@dataclass(frozen=True)
class Picks:
    items: tuple[Letter | TypedTime, ...]   # reply order kept (a runoff's rank order)


@dataclass(frozen=True)
class Refusal:
    index: int              # 1-based item position in the reply
    reason: str             # a key of REASONS or session_time.REASONS; never the text itself


Reply = Clear | All | Picks


def parse(text: str, *, runoff: bool, now: datetime, tz: ZoneInfo,
          letters: Mapping[str, int]) -> Reply | Refusal:
    """`letters` maps every option letter of the poll (shown on this ballot or not) to its option id."""
    items = [item.strip().lower() for item in text.split(",")]
    if len(items) > MAX_ITEMS:
        return Refusal(MAX_ITEMS + 1, "too_many")
    for index, item in enumerate(items, start=1):
        if not item:
            return Refusal(index, "empty")
    if items in (["clear"], ["all"]):
        if items == ["all"] and runoff:
            return Refusal(1, "no_all")
        return Clear() if items == ["clear"] else All()
    picked: list[Letter | TypedTime] = []
    for index, item in enumerate(items, start=1):
        if item in ("clear", "all"):
            return Refusal(index, "no_all" if runoff and item == "all" else "keyword_alone")
        if poll_letters.position(item) is not None:
            option_id = letters.get(item)
            if option_id is None:
                return Refusal(index, "no_option")
            choice: Letter | TypedTime = Letter(option_id)
        elif runoff:
            return Refusal(index, "letters_only")
        else:
            try:
                when = session_time.parse(item, now=now, tz=tz)
            except session_time.TimeRefused as exc:
                return Refusal(index, exc.reason)
            starts_at = int(when.timestamp())
            if starts_at - int(now.timestamp()) < LEAD_SECONDS:
                return Refusal(index, "too_soon")
            choice = TypedTime(starts_at)
        if choice in picked:
            if runoff:
                return Refusal(index, "repeated")
            continue
        picked.append(choice)
    return Picks(tuple(picked))
