# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed session times and offered slots: pure functions with a fixed clock, in one IANA zone."""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from fluxer_panel import session_time, text
from fluxer_panel.session_time import TimeRefused, local_kind, next_weekly, offered_slots, parse

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=NY)          # Wednesday noon, after the 2026 fall-back
WEEKDAY = (time(16, 0), time(19, 0))
WEEKEND = (time(10, 0), time(14, 0), time(19, 0))


def local(when: datetime) -> tuple[int, int, int, int, int]:
    return (when.year, when.month, when.day, when.hour, when.minute)


def at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NY)


def refused_reason(raw: str, *, now: datetime = NOW) -> str:
    with pytest.raises(TimeRefused) as info:
        parse(raw, now=now, tz=NY)
    return info.value.reason


# AC1.1: valid forms parse to the local wall time.

@pytest.mark.parametrize(("raw", "expected"), [
    ("sat 14:00", (2026, 11, 14, 14, 0)),
    ("tomorrow 19:30", (2026, 11, 12, 19, 30)),
    ("2026-11-14 14:00", (2026, 11, 14, 14, 0)),
    ("  SAT   14:00 ", (2026, 11, 14, 14, 0)),
    ("Tomorrow 19:30", (2026, 11, 12, 19, 30)),
])
def test_valid_times_parse_to_the_local_wall_time(raw, expected):
    when = parse(raw, now=NOW, tz=NY)
    assert local(when) == expected
    assert when.tzinfo == NY


def test_today_with_a_single_digit_hour_parses():
    when = parse("today 9:05", now=at(2026, 11, 11, 8, 0), tz=NY)
    assert local(when) == (2026, 11, 11, 9, 5)


# AC1.2: refused forms name their reason.

@pytest.mark.parametrize("raw", [
    "sat 2pm", "sat 2:00pm", "sat noon", "sat 14:00:00", "sat at 14:00",
    "sat", "sat 24:00", "sat 9:5", "funday 14:00", "14:00",
])
def test_unreadable_times_are_format_refusals(raw):
    assert refused_reason(raw) == "format"


def test_an_invalid_calendar_date_is_a_date_refusal():
    assert refused_reason("2026-02-30 14:00") == "date"


def test_every_refusal_reason_has_a_reply_and_the_example_is_backticked():
    for reason in session_time.REASONS:
        assert text.SESSION_TIME_REFUSED[reason].strip()
    assert set(text.SESSION_TIME_REFUSED) == set(session_time.REASONS)
    assert "`" in text.SESSION_TIME_EXAMPLE


# AC1.3: a weekday name for today is refused.

def test_a_weekday_name_for_todays_weekday_is_refused_with_a_hint():
    assert refused_reason("wed 14:00") == "weekday_today"
    message = text.SESSION_TIME_REFUSED["weekday_today"]
    assert "today HH:MM" in message
    assert "2026-" in message


# AC1.4: DST gaps and folds in America/New_York, in 2026 and 2027.

@pytest.mark.parametrize(("raw", "now", "reason"), [
    ("2026-03-08 02:30", at(2026, 2, 1), "gap"),
    ("2026-11-01 01:30", at(2026, 10, 1), "fold"),
    ("2027-03-14 02:30", at(2027, 2, 1), "gap"),
    ("2027-11-07 01:30", at(2027, 10, 1), "fold"),
])
def test_dst_gap_and_fold_times_are_refused(raw, now, reason):
    assert refused_reason(raw, now=now) == reason


@pytest.mark.parametrize(("wall", "kind"), [
    (datetime(2026, 3, 8, 2, 30), "gap"),
    (datetime(2026, 11, 1, 1, 30), "fold"),
    (datetime(2027, 3, 14, 2, 30), "gap"),
    (datetime(2027, 11, 7, 1, 30), "fold"),
    (datetime(2026, 11, 14, 14, 0), "ok"),
])
def test_local_kind_classifies_wall_times(wall, kind):
    assert local_kind(wall, NY) == kind


# AC1.5: past and horizon checks.

def test_a_time_equal_to_now_or_earlier_today_is_past():
    assert refused_reason("today 12:00") == "past"
    assert refused_reason("today 11:59") == "past"


def test_exactly_sixty_days_ahead_is_accepted_and_a_minute_more_is_too_far():
    # 2026-06-01 to 2026-07-31 spans no DST change, so 60 local days are exactly 60 days.
    now = at(2026, 6, 1, 12)
    when = parse("2026-07-31 12:00", now=now, tz=NY)
    assert when - now == timedelta(days=60)
    assert refused_reason("2026-07-31 12:01", now=now) == "too_far"


def test_the_horizon_counts_real_time_across_the_fall_back():
    # 2026-10-01 to 2026-11-30 is 60 wall-clock days, but the fall-back hour makes it 60 days and 1 hour.
    now = at(2026, 10, 1, 12)
    assert refused_reason("2026-11-30 12:00", now=now) == "too_far"


def test_the_horizon_counts_real_time_across_the_spring_forward():
    # 2026-02-01 to 2026-04-02 is 60 wall-clock days and 30 minutes, but the spring-forward hour makes it
    # 59 days and 23.5 hours of real time, so it is inside the horizon.
    now = at(2026, 2, 1, 12)
    when = parse("2026-04-02 12:30", now=now, tz=NY)
    assert local(when) == (2026, 4, 2, 12, 30)
    assert when.astimezone(timezone.utc) - now.astimezone(timezone.utc) < timedelta(days=60)


# AC1.6: offered slots.

def test_offered_slots_skip_under_an_hour_and_use_the_weekend_templates():
    now = at(2026, 11, 13, 15, 30)                          # Friday; Friday 16:00 is 30 minutes away
    slots = offered_slots(now=now, tz=NY, weekday=WEEKDAY, weekend=WEEKEND)
    assert [local(s) for s in slots] == [
        (2026, 11, 13, 19, 0),
        (2026, 11, 14, 10, 0), (2026, 11, 14, 14, 0), (2026, 11, 14, 19, 0),
        (2026, 11, 15, 10, 0), (2026, 11, 15, 14, 0),
    ]


def test_offered_slots_count_caps_the_list_and_stays_increasing():
    now = at(2026, 11, 13, 15, 30)
    slots = offered_slots(now=now, tz=NY, weekday=WEEKDAY, weekend=WEEKEND, count=2)
    assert len(slots) == 2
    everything = offered_slots(now=now, tz=NY, weekday=WEEKDAY, weekend=WEEKEND)
    assert len(everything) == 6
    assert everything == sorted(everything)
    assert all(a < b for a, b in zip(everything, everything[1:], strict=False))


def test_offered_slots_skip_a_fall_back_fold_and_keep_ten_oclock_local():
    now = at(2026, 10, 31, 12)                              # Saturday, EDT
    slots = offered_slots(now=now, tz=NY, weekday=(time(16, 0),), weekend=(time(1, 30), time(10, 0)))
    locals_ = [local(s) for s in slots]
    assert (2026, 11, 1, 1, 30) not in locals_
    assert (2026, 11, 1, 10, 0) in locals_
    assert all(s.utcoffset() == timedelta(hours=-5) for s in slots if s.date().isoformat() == "2026-11-01")


def test_offered_slots_skip_a_spring_forward_gap():
    now = at(2026, 3, 6, 12)                                # Friday, EST
    slots = offered_slots(now=now, tz=NY, weekday=(time(16, 0),), weekend=(time(2, 30), time(10, 0)))
    assert (2026, 3, 8, 2, 30) not in [local(s) for s in slots]
    sunday_ten = next(s for s in slots if local(s) == (2026, 3, 8, 10, 0))
    assert sunday_ten.utcoffset() == timedelta(hours=-4)


def test_the_one_hour_lead_is_measured_in_real_time_across_a_fall_back():
    # 01:30 EDT and 02:00 EST are 90 real minutes apart, though only 30 wall-clock minutes apart.
    now = at(2026, 11, 1, 1, 30)
    slots = offered_slots(now=now, tz=NY, weekday=(), weekend=(time(2, 0),), count=1)
    assert [local(s) for s in slots] == [(2026, 11, 1, 2, 0)]


# AC1.7: weekly repeats keep the wall time and skip whole weeks.

def test_next_weekly_keeps_the_wall_time_across_the_fall_back():
    start = at(2026, 10, 31, 14, 0)                         # Saturday 14:00 EDT
    when = next_weekly(start, tz=NY, now=start)
    assert local(when) == (2026, 11, 7, 14, 0)
    assert when.utcoffset() == timedelta(hours=-5)
    real = when.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    assert real == timedelta(days=7, hours=1)               # one real hour longer across the change


def test_next_weekly_after_downtime_takes_the_first_week_an_hour_ahead():
    start = at(2026, 10, 31, 14, 0)
    assert local(next_weekly(start, tz=NY, now=at(2026, 11, 21, 12, 0))) == (2026, 11, 21, 14, 0)


def test_next_weekly_skips_a_week_less_than_an_hour_away():
    start = at(2026, 10, 31, 14, 0)
    assert local(next_weekly(start, tz=NY, now=at(2026, 11, 21, 13, 30))) == (2026, 11, 28, 14, 0)


def test_next_weekly_skips_a_week_whose_wall_time_is_in_a_gap():
    start = at(2027, 3, 7, 2, 30)                           # Sunday 02:30 EST; the next week's 02:30 is a gap
    when = next_weekly(start, tz=NY, now=start)
    assert local(when) == (2027, 3, 21, 2, 30)
    assert when.utcoffset() == timedelta(hours=-4)


def test_next_weekly_skips_a_week_whose_wall_time_is_in_a_fold():
    start = at(2026, 10, 25, 1, 30)                         # Sunday 01:30 EDT; the next week's 01:30 is a fold
    when = next_weekly(start, tz=NY, now=start)
    assert local(when) == (2026, 11, 8, 1, 30)
    assert when.utcoffset() == timedelta(hours=-5)


def test_utc_inputs_give_the_same_instants_as_local_inputs():
    now_utc = NOW.astimezone(timezone.utc)
    assert parse("sat 14:00", now=now_utc, tz=NY) == parse("sat 14:00", now=NOW, tz=NY)
