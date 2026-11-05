# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The interaction settings store against a real SQLite file: the global switch, the per-card off switch, and the
degraded-channel state that drives the admin alert and the retry timer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fluxer_panel import cards, interaction_settings
from fluxer_panel.db import Database

CHANNEL = "2001"
OTHER_CHANNEL = "2002"
DAY = 86_400
NOW = 1_800_000_000


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
async def card_id(db: Database) -> int:
    return await db.run(lambda c: cards.create_card(c, slug="mc", title="Minecraft", author_id="1001", now=1))


async def test_reactions_are_enabled_and_no_card_is_off_by_default(db: Database, card_id: int) -> None:
    assert await db.run(lambda c: interaction_settings.enabled(c)) is True
    assert await db.run(lambda c: interaction_settings.card_off(c, card_id)) is False


async def test_set_enabled_reports_only_real_changes(db: Database) -> None:
    assert await db.run(lambda c: interaction_settings.set_enabled(c, False)) is True
    assert await db.run(lambda c: interaction_settings.enabled(c)) is False
    assert await db.run(lambda c: interaction_settings.set_enabled(c, False)) is False
    assert await db.run(lambda c: interaction_settings.set_enabled(c, True)) is True


async def test_set_card_off_reports_only_real_changes(db: Database, card_id: int) -> None:
    assert await db.run(lambda c: interaction_settings.set_card_off(c, card_id, True)) is True
    assert await db.run(lambda c: interaction_settings.card_off(c, card_id)) is True
    assert await db.run(lambda c: interaction_settings.set_card_off(c, card_id, True)) is False
    assert await db.run(lambda c: interaction_settings.set_card_off(c, card_id, False)) is True
    assert await db.run(lambda c: interaction_settings.card_off(c, card_id)) is False
    assert await db.run(lambda c: interaction_settings.set_card_off(c, card_id, False)) is False


async def test_a_channel_is_not_degraded_until_it_is_marked(db: Database) -> None:
    assert await db.run(lambda c: interaction_settings.is_degraded(c, CHANNEL)) is False


async def test_mark_degraded_is_true_only_for_the_first_call(db: Database) -> None:
    assert await db.run(lambda c: interaction_settings.mark_degraded(c, CHANNEL, NOW)) is True
    assert await db.run(lambda c: interaction_settings.mark_degraded(c, CHANNEL, NOW + 60)) is False
    assert await db.run(lambda c: interaction_settings.is_degraded(c, CHANNEL)) is True
    assert await db.run(lambda c: interaction_settings.is_degraded(c, OTHER_CHANNEL)) is False


async def test_claim_retry_waits_out_the_interval_and_advances_the_time(db: Database) -> None:
    await db.run(lambda c: interaction_settings.mark_degraded(c, CHANNEL, NOW))
    assert await db.run(lambda c: interaction_settings.claim_retry(c, CHANNEL, NOW + DAY - 1, DAY)) is False
    assert await db.run(lambda c: interaction_settings.claim_retry(c, CHANNEL, NOW + DAY, DAY)) is True
    # The claim moved last_retry_at to NOW + DAY, so the next claim waits a full interval from there.
    assert await db.run(lambda c: interaction_settings.claim_retry(c, CHANNEL, NOW + DAY + 1, DAY)) is False
    assert await db.run(lambda c: interaction_settings.claim_retry(c, CHANNEL, NOW + 2 * DAY, DAY)) is True


async def test_claim_retry_for_a_channel_that_is_not_degraded_is_false(db: Database) -> None:
    assert await db.run(lambda c: interaction_settings.claim_retry(c, CHANNEL, NOW + DAY, DAY)) is False


async def test_clear_degraded_removes_the_row_and_reports_it(db: Database) -> None:
    await db.run(lambda c: interaction_settings.mark_degraded(c, CHANNEL, NOW))
    assert await db.run(lambda c: interaction_settings.clear_degraded(c, CHANNEL)) is True
    assert await db.run(lambda c: interaction_settings.is_degraded(c, CHANNEL)) is False
    assert await db.run(lambda c: interaction_settings.clear_degraded(c, CHANNEL)) is False
