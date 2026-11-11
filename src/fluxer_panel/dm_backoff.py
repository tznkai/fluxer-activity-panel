# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-user DM-failure backoff, in memory. A failed DM usually means the user has DMs closed, so press replies to that
user are skipped for a while. A restart forgets every block, by design."""

from __future__ import annotations

import time
from collections.abc import Callable


class DmBackoff:
    def __init__(self, seconds: int, *, clock: Callable[[], float] = time.time) -> None:
        self._seconds = seconds
        self._clock = clock
        self._until: dict[str, float] = {}
        self._last_sweep: float | None = None

    def blocked(self, user_id: str) -> bool:
        until = self._until.get(user_id)
        if until is None:
            return False
        if self._clock() >= until:
            del self._until[user_id]
            return False
        return True

    def failed(self, user_id: str) -> None:
        now = self._clock()
        self._sweep(now)
        self._until[user_id] = now + self._seconds

    def _sweep(self, now: float) -> None:
        """Forgets every block that has run out, at most once per window. A user who is never read again would otherwise
        keep an entry for the life of the process. An expired block and a forgotten one behave the same."""
        if self._last_sweep is not None and now - self._last_sweep < self._seconds:
            return
        self._last_sweep = now
        for user_id in [uid for uid, until in self._until.items() if now >= until]:
            del self._until[user_id]

    def succeeded(self, user_id: str) -> None:
        self._until.pop(user_id, None)
