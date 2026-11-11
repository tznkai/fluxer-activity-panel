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

    def blocked(self, user_id: str) -> bool:
        until = self._until.get(user_id)
        if until is None:
            return False
        if self._clock() >= until:
            del self._until[user_id]
            return False
        return True

    def failed(self, user_id: str) -> None:
        self._until[user_id] = self._clock() + self._seconds

    def succeeded(self, user_id: str) -> None:
        self._until.pop(user_id, None)
