# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared probe helpers: value limits and the guard that turns every library failure into a fixed reason.

This module imports no probe library, so plugins that use it stay independent of each other.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Awaitable

from fluxer_panel.sources.base import ProbeFailed, ProbeResult, sanitize_detail

RAW_MAX = 1024  # a detail longer than this before sanitising means an oversized reply
COUNT_MAX = 100_000

_MALFORMED = (ValueError, IndexError, KeyError, TypeError, AttributeError, struct.error, UnicodeError)


def _count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= COUNT_MAX:
        raise ValueError
    return value


def _detail(value: object) -> str | None:
    if isinstance(value, str) and len(value) > RAW_MAX:
        raise ValueError
    return sanitize_detail(value)


async def _guard(
    coro: Awaitable[ProbeResult], timeout: float, malformed: tuple[type[BaseException], ...] = ()
) -> ProbeResult | ProbeFailed:
    try:
        return await asyncio.wait_for(coro, timeout + 1.0)  # hard cap above the library's own timeout
    except TimeoutError:
        return ProbeFailed("timeout")
    except ConnectionRefusedError:
        return ProbeFailed("refused")
    except _MALFORMED + malformed:
        return ProbeFailed("malformed")
    except OSError:
        return ProbeFailed("error")
    except Exception:  # noqa: BLE001 - never let a library message escape
        return ProbeFailed("error")
