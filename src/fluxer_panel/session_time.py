# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Strict typed times and offered slots for sessions, in one IANA zone. Pure: callers pass `now`."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

MIN_LEAD = timedelta(hours=1)
HORIZON = timedelta(days=60)
SLOT_COUNT = 6
SLOT_SEARCH_DAYS = 14
REASONS = ("format", "date", "weekday_today", "gap", "fold", "past", "too_far")

_TIME = re.compile(r"([01]?[0-9]|2[0-3]):([0-5][0-9])")
_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_WEEKDAYS = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
             "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
             "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}


class TimeRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _elapsed(later: datetime, earlier: datetime) -> timedelta:
    """Real time between two aware datetimes. Python subtracts same-zone values by wall clock, so convert to UTC first."""
    return later.astimezone(timezone.utc) - earlier.astimezone(timezone.utc)


def local_kind(wall: datetime, tz: ZoneInfo) -> str:
    """'ok', 'gap' or 'fold' for a naive local wall time in tz."""
    aware = wall.replace(tzinfo=tz)
    back = aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
    if back != wall:
        return "gap"
    if aware.utcoffset() != aware.replace(fold=1).utcoffset():
        return "fold"
    return "ok"


def _resolve(wall: datetime, tz: ZoneInfo) -> datetime:
    kind = local_kind(wall, tz)
    if kind != "ok":
        raise TimeRefused(kind)
    return wall.replace(tzinfo=tz)


def parse(text: str, *, now: datetime, tz: ZoneInfo) -> datetime:
    parts = text.lower().split()
    if len(parts) != 2:
        raise TimeRefused("format")
    day, clock = parts
    m = _TIME.fullmatch(clock)
    if m is None:
        raise TimeRefused("format")
    today = now.astimezone(tz).date()
    if day == "today":
        date_ = today
    elif day == "tomorrow":
        date_ = today + timedelta(days=1)
    elif day in _WEEKDAYS:
        ahead = (_WEEKDAYS[day] - today.weekday()) % 7
        if ahead == 0:
            raise TimeRefused("weekday_today")
        date_ = today + timedelta(days=ahead)
    elif (d := _DATE.fullmatch(day)) is not None:
        try:
            date_ = date(int(d.group(1)), int(d.group(2)), int(d.group(3)))
        except ValueError:
            raise TimeRefused("date") from None
    else:
        raise TimeRefused("format")
    when = _resolve(datetime.combine(date_, time(int(m.group(1)), int(m.group(2)))), tz)
    elapsed = _elapsed(when, now)
    if elapsed <= timedelta(0):
        raise TimeRefused("past")
    if elapsed > HORIZON:
        raise TimeRefused("too_far")
    return when


def offered_slots(*, now: datetime, tz: ZoneInfo, weekday: Sequence[time], weekend: Sequence[time],
                  count: int = SLOT_COUNT) -> list[datetime]:
    today = now.astimezone(tz).date()
    out: list[datetime] = []
    for offset in range(SLOT_SEARCH_DAYS):
        day = today + timedelta(days=offset)
        for t in sorted(weekend if day.weekday() >= 5 else weekday):
            wall = datetime.combine(day, t)
            if local_kind(wall, tz) != "ok":
                continue
            when = wall.replace(tzinfo=tz)
            if _elapsed(when, now) < MIN_LEAD:
                continue
            out.append(when)
            if len(out) == count:
                return out
    return out


def next_weekly(start: datetime, *, tz: ZoneInfo, now: datetime) -> datetime:
    wall = start.astimezone(tz).replace(tzinfo=None)
    while True:
        wall += timedelta(days=7)
        if local_kind(wall, tz) != "ok":
            continue          # that week's wall time doesn't exist or is ambiguous: skip the week
        when = wall.replace(tzinfo=tz)
        if _elapsed(when, now) >= MIN_LEAD:
            return when
