# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-channel reaction pacer. Fluxer's reaction bucket is per channel and shared across the reaction routes, so
every reaction REST call the panel makes in a channel goes through one pacer."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable


class ChannelPacer:
    """At most `max_calls` reaction calls per channel in any `per_seconds` window. Every reaction REST call in
    fluxer_panel goes through wait()."""

    def __init__(self, *, max_calls: int = 20, per_seconds: float = 10.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._max_calls = max_calls
        self._per_seconds = per_seconds
        self._clock = clock
        self._sleep = sleep
        self._calls: dict[str, deque[float]] = {}   # per channel: the times of its recent calls, oldest first
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, channel_id: str) -> None:
        """Returns once a call in `channel_id` may go out, and records it. Waiters in one channel queue behind one
        another, so the window is never over-filled."""
        lock = self._locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            calls = self._calls.setdefault(channel_id, deque())
            while True:
                now = self._clock()
                while calls and calls[0] <= now - self._per_seconds:
                    calls.popleft()
                if len(calls) < self._max_calls:
                    break
                await self._sleep(calls[0] + self._per_seconds - now)
            calls.append(now)
