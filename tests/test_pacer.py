# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-channel reaction pacer: at most `max_calls` per channel in any `per_seconds` window, on a fake clock."""

from __future__ import annotations

import asyncio
from typing import Any

from fluxer_panel.pacer import ChannelPacer


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay
        await asyncio.sleep(0)


def make_pacer(clock: FakeClock, **kwargs: Any) -> ChannelPacer:
    return ChannelPacer(clock=clock.time, sleep=clock.sleep, **kwargs)


async def test_twenty_calls_in_one_channel_do_not_sleep() -> None:
    clock = FakeClock()
    pacer = make_pacer(clock)

    for _ in range(20):
        await pacer.wait("2001")

    assert clock.sleeps == []


async def test_the_twenty_first_call_sleeps_until_the_oldest_leaves_the_window() -> None:
    clock = FakeClock()
    pacer = make_pacer(clock)
    for _ in range(20):
        await pacer.wait("2001")

    await pacer.wait("2001")

    assert clock.sleeps == [10.0]
    assert clock.now == 10.0


async def test_a_second_channel_has_its_own_window() -> None:
    clock = FakeClock()
    pacer = make_pacer(clock)
    for _ in range(20):
        await pacer.wait("2001")

    await pacer.wait("2002")

    assert clock.sleeps == []


async def test_concurrent_waiters_never_exceed_the_limit_in_any_window() -> None:
    clock = FakeClock()
    pacer = make_pacer(clock)
    finished: list[float] = []

    async def call() -> None:
        await pacer.wait("2001")
        finished.append(clock.now)

    await asyncio.gather(*(call() for _ in range(45)))

    assert len(finished) == 45
    for at in finished:
        in_window = [t for t in finished if at - 10.0 < t <= at]
        assert len(in_window) <= 20


async def test_the_limit_and_window_are_configurable() -> None:
    clock = FakeClock()
    pacer = make_pacer(clock, max_calls=2, per_seconds=5.0)
    await pacer.wait("2001")
    await pacer.wait("2001")

    await pacer.wait("2001")

    assert clock.sleeps == [5.0]
