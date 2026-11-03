# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The `!card help` replies: every subcommand and field is named, the limits and kinds come from the code, and each
reply fits one message."""

from __future__ import annotations

import re

from fluxer_panel import card_fields, text
from fluxer_panel.card_commands import PREVIEW_LIMIT, SETTABLE, help_fields_reply
from fluxer_panel.sources.registry import PROBES

TEXT_FIELDS = ("title", "description", "join", "first_time", "location")


def _field_lines() -> dict[str, str]:
    """The help line of each field on the fields page, keyed by the field name that starts it."""
    lines = {}
    for line in help_fields_reply().splitlines():
        name, _, _ = line.partition(":")
        lines[name] = line
    return lines


def _listed_fields() -> set[str]:
    [line] = [each for each in text.HELP_COMMANDS.splitlines() if each.startswith("Fields: ")]
    return {name.strip() for name in line.removeprefix("Fields: ").split(",")}


def test_every_field_name_appears_in_the_help() -> None:
    combined = text.HELP_COMMANDS + "\n" + help_fields_reply()
    for name in card_fields.FIELD_NAMES:
        assert re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", combined), name


def test_the_help_names_no_field_outside_field_names() -> None:
    assert set(_field_lines()) <= card_fields.FIELD_NAMES
    assert _listed_fields() <= card_fields.FIELD_NAMES


def test_the_fields_page_and_the_field_listing_cover_every_settable_field() -> None:
    # slug is a command argument, not a field, so it is the one name in FIELD_NAMES without a field line.
    settable = card_fields.FIELD_NAMES - {"slug"}
    assert set(_field_lines()) == settable
    assert _listed_fields() == settable
    assert set(SETTABLE) == settable


def test_text_limits_in_the_help_are_the_limits_the_code_enforces() -> None:
    lines = _field_lines()
    for name in TEXT_FIELDS:
        assert f"1 to {card_fields.TEXT_LIMITS[name]} characters" in lines[name], name


def test_numeric_ranges_in_the_help_are_the_ranges_the_code_enforces() -> None:
    lines = _field_lines()
    lo, hi = card_fields.SORT_RANGE
    assert f"from {lo} to {hi}" in lines["sort"]
    lo, hi = card_fields.SEATS_RANGE
    assert f"from {lo} to {hi}" in lines["seats"]


def test_probe_kinds_and_show_items_in_the_help_are_the_ones_the_code_accepts() -> None:
    lines = _field_lines()
    for kind in PROBES:
        assert kind in lines["probe"]
    for item in ("map", "version", "game_mode"):
        assert item in lines["show"]


def test_each_help_reply_fits_one_message() -> None:
    assert len(text.HELP_COMMANDS) <= PREVIEW_LIMIT
    assert len(help_fields_reply()) <= PREVIEW_LIMIT


def test_the_usage_line_mentions_the_help_command() -> None:
    assert "!card help" in text.CARD_USAGE
    assert len(text.CARD_USAGE) <= PREVIEW_LIMIT
