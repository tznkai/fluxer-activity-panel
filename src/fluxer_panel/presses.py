# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reactions as presses. This module is the only code that knows presses are reactions.

The emoji are written as escapes, never as literal characters: WAKE_EMOJI carries U+FE0F, which Fluxer stores on the
play reaction (docs/fluxer-notes.md, "Play emoji (U+25B6) stored name").
"""

from __future__ import annotations

WAKE_EMOJI = "\u25b6\ufe0f"        # the play emoji, as the bot sends it and Fluxer stores it
FOLLOW_EMOJI = "\U0001F514"        # the bell
PLAY_NOW_EMOJI = "\U0001F64B"      # the person raising a hand
