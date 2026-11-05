# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the pure helpers of tools/live/role_ping.py (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "live"))

import role_ping  # noqa: E402

GUILD = "1" * 19
ROLE = "7" * 19
OTHER = "8" * 19
MENTION_EVERYONE = 1 << 17
ADMINISTRATOR = 1 << 3


def test_mention_roles_returns_the_string_ids_of_the_array():
    assert role_ping.mention_roles({"mention_roles": [ROLE, OTHER]}) == [ROLE, OTHER]


def test_mention_roles_is_none_when_the_field_is_absent_or_not_a_list():
    assert role_ping.mention_roles({"id": "x"}) is None
    assert role_ping.mention_roles({"mention_roles": ROLE}) is None
    assert role_ping.mention_roles("not a message") is None


def test_mention_roles_keeps_only_string_entries():
    assert role_ping.mention_roles({"mention_roles": [ROLE, 5, None]}) == [ROLE]


def test_role_in_mentions_is_true_only_for_the_role_listed():
    assert role_ping.role_in_mentions({"mention_roles": [ROLE]}, ROLE) is True
    assert role_ping.role_in_mentions({"mention_roles": [OTHER]}, ROLE) is False
    assert role_ping.role_in_mentions({"mention_roles": []}, ROLE) is False


def test_role_in_mentions_is_false_when_the_field_is_missing():
    assert role_ping.role_in_mentions({"id": "x"}, ROLE) is False


def test_effective_permissions_ors_the_member_roles_and_everyone():
    roles = [
        {"id": GUILD, "permissions": "0"},
        {"id": ROLE, "permissions": str(MENTION_EVERYONE)},
        {"id": OTHER, "permissions": str(ADMINISTRATOR)},
    ]
    assert role_ping.effective_permissions(roles, {ROLE}, GUILD) == MENTION_EVERYONE
    assert role_ping.effective_permissions(roles, {ROLE, OTHER}, GUILD) == MENTION_EVERYONE | ADMINISTRATOR


def test_effective_permissions_ignores_roles_the_member_does_not_hold():
    roles = [{"id": GUILD, "permissions": "0"}, {"id": OTHER, "permissions": str(ADMINISTRATOR)}]
    assert role_ping.effective_permissions(roles, set(), GUILD) == 0


def test_effective_permissions_is_none_when_a_relevant_role_has_no_readable_permissions():
    roles = [{"id": GUILD, "permissions": "0"}, {"id": ROLE}]
    assert role_ping.effective_permissions(roles, {ROLE}, GUILD) is None


def test_effective_permissions_is_none_when_everyone_is_missing():
    roles = [{"id": ROLE, "permissions": "1"}]
    assert role_ping.effective_permissions(roles, {ROLE}, GUILD) is None


def test_flag_reports_yes_no_or_unknown():
    assert role_ping.flag(MENTION_EVERYONE, MENTION_EVERYONE) == "yes"
    assert role_ping.flag(0, MENTION_EVERYONE) == "no"
    assert role_ping.flag(None, MENTION_EVERYONE) == "unknown"


def test_bit_constants_match_the_documented_values():
    assert role_ping.MENTION_EVERYONE == 1 << 17
    assert role_ping.ADMINISTRATOR == 1 << 3
