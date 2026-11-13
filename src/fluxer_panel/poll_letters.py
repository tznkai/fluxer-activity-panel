# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Option letters for polls. Pure: position 0 is `a`, 25 is `z`, 26 is `aa`, 701 is `zz`."""

from __future__ import annotations

import re

COUNT = 702
_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
_LABEL = re.compile(r"[a-z]{1,2}")


def letter(position: int) -> str:
    if not 0 <= position < COUNT:
        raise ValueError("position out of range")
    if position < 26:
        return _ALPHABET[position]
    first, second = divmod(position - 26, 26)
    return _ALPHABET[first] + _ALPHABET[second]


def position(label: str) -> int | None:
    """The position for a label, or None when it is not one or two lowercase letters."""
    if not _LABEL.fullmatch(label):
        return None
    if len(label) == 1:
        return _ALPHABET.index(label)
    return 26 + _ALPHABET.index(label[0]) * 26 + _ALPHABET.index(label[1])
