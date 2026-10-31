# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Snowflake id validation for REST paths, events and config."""

from __future__ import annotations

import re

# fullmatch: "$" would accept a trailing newline
_SNOWFLAKE = re.compile(r"[0-9]{1,25}")


def is_snowflake(value: object) -> bool:
    return isinstance(value, str) and _SNOWFLAKE.fullmatch(value) is not None


def require_snowflake(value: str) -> str:
    """Return `value` unchanged, or raise ValueError: ids are interpolated into URL paths."""
    if not is_snowflake(value):
        raise ValueError("not a snowflake id")
    return value
