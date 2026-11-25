# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The refusal for a message aimed at the games community's channel. It lives in its own module so the senders
(play-now, session, poll and mirror) can each import it without importing one another."""

from __future__ import annotations


class GamesChannelRefused(Exception):
    """A message was aimed at the games channel. Raised before any REST call (AC4.5)."""
