# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared probe helpers: every failure branch of the guard, and the count and detail limits."""

from __future__ import annotations

import pytest

from fluxer_panel.sources._probe_util import COUNT_MAX, RAW_MAX, _count, _detail, _guard
from fluxer_panel.sources.base import ProbeFailed

LIBRARY_MESSAGE = "SENTINEL-LIBRARY-MESSAGE"


async def _raise(exc: BaseException):
    raise exc


# AC3.5: each exception a library can raise maps to one fixed reason, and the library's message is dropped.
@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (TimeoutError(LIBRARY_MESSAGE), "timeout"),
        (ConnectionRefusedError(LIBRARY_MESSAGE), "refused"),
        (ValueError(LIBRARY_MESSAGE), "malformed"),
        (OSError(LIBRARY_MESSAGE), "error"),
        (RuntimeError(LIBRARY_MESSAGE), "error"),
    ],
)
async def test_guard_maps_each_exception_to_one_reason(exc: BaseException, reason: str) -> None:
    result = await _guard(_raise(exc), 0.1)
    assert result == ProbeFailed(reason)
    assert LIBRARY_MESSAGE not in repr(result)


# AC3.5: a caller-supplied malformed type counts as malformed, and any other OSError does not.
async def test_guard_treats_extra_malformed_types_as_malformed() -> None:
    class Broken(Exception):
        pass

    assert await _guard(_raise(Broken(LIBRARY_MESSAGE)), 0.1, malformed=(Broken,)) == ProbeFailed("malformed")
    assert await _guard(_raise(Broken(LIBRARY_MESSAGE)), 0.1) == ProbeFailed("error")


# AC3.3: counts must be plain non-negative integers no larger than COUNT_MAX.
@pytest.mark.parametrize("value", [True, False, -1, COUNT_MAX + 1, "3", 3.0, None])
def test_count_refuses_non_counts(value: object) -> None:
    with pytest.raises(ValueError):
        _count(value)


def test_count_accepts_the_limits() -> None:
    assert _count(0) == 0
    assert _count(COUNT_MAX) == COUNT_MAX


# AC3.3: a detail longer than RAW_MAX before sanitising is an oversized reply; a shorter one is capped.
def test_detail_refuses_oversized_raw_text() -> None:
    with pytest.raises(ValueError):
        _detail("x" * (RAW_MAX + 1))


def test_detail_caps_the_sanitised_text_and_passes_non_text_as_none() -> None:
    assert _detail("x" * RAW_MAX) == "x" * 48
    assert _detail(None) is None
    assert _detail(7) is None
