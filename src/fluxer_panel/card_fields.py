# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validators for admin card fields. Each returns the normalised value or raises CardFieldError(field, rule).

No validator puts the value it was given into its error, so a refusal names the field and the rule only.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata

from fluxer_panel import text
from fluxer_panel.cards import SHOW_ITEMS, CardFieldError
from fluxer_panel.render import CHANNEL_LINK_RE
from fluxer_panel.sources.registry import Sources, known_manager_kinds

TEXT_LIMITS = {"title": 200, "description": 1000, "join": 1000, "first_time": 1000, "location": 200}
SORT_RANGE = (0, 9999)
SEATS_RANGE = (1, 999)
# Every field name a validator may name in a refusal. A refusal for any other name uses a fixed label instead.
FIELD_NAMES = frozenset({"title", "description", "join", "first_time", "sort", "seats", "location", "voice",
                         "text_channel1", "text_channel2", "text_channel3", "address", "address_public", "manager",
                         "probe", "show", "follow_role", "play_now_channel", "slug"})
_SLUG = re.compile(r"[a-z0-9-]{1,32}")
_SNOWFLAKE = re.compile(r"[1-9][0-9]{0,24}")          # same shape as config._ADMIN_ID
_LABEL = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)")
_PORT = re.compile(r"[0-9]{1,5}")
_INT = re.compile(r"[0-9]{1,9}")
_BRACKETED = re.compile(r"\[([^\]\s]+)\]:([0-9]{1,5})")
# Bidirectional overrides and isolates, written as escapes so no invisible character sits in the source.
_BIDI = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def text_value(field: str, value: str) -> str:
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= TEXT_LIMITS[field]:
        raise CardFieldError(field, "length")
    if any(unicodedata.category(ch) == "Cc" or ch in _BIDI for ch in cleaned):
        raise CardFieldError(field, "control")
    return cleaned


def slug(value: str) -> str:
    if not _SLUG.fullmatch(value):
        raise CardFieldError("slug", "slug")
    return value


def snowflake(field: str, value: str) -> str:
    if not _SNOWFLAKE.fullmatch(value):
        raise CardFieldError(field, "snowflake")
    return value


def _plain_ip(value: str) -> bool:
    """An IP address with no zone id. A zone id is free text that a reply could carry, so `%` is never accepted."""
    if "%" in value:
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def host(field: str, value: str) -> str:
    if _plain_ip(value):
        return value
    labels = value.split(".")
    # A name whose last label is all digits can be read as a dotted IPv4 address, so it is refused.
    if len(value) > 253 or not all(_LABEL.fullmatch(label) for label in labels) or labels[-1].isdigit():
        raise CardFieldError(field, "host")
    return value


def port(field: str, value: str) -> int:
    if not _PORT.fullmatch(value) or not 1 <= int(value) <= 65535:
        raise CardFieldError(field, "port")
    return int(value)


def address(value: str) -> tuple[str, int]:
    stripped = value.strip()
    if stripped.startswith("["):
        match = _BRACKETED.fullmatch(stripped)
        if match is None:
            raise CardFieldError("address", "address")
        if not _plain_ip(match.group(1)):
            raise CardFieldError("address", "host")
        host_part, port_part = match.group(1), match.group(2)
    else:
        if stripped.count(":") != 1:
            raise CardFieldError("address", "address")
        host_part, port_part = stripped.split(":")
    return host("address", host_part), port("address", port_part)


def int_range(field: str, value: str, lo: int, hi: int) -> int:
    if not _INT.fullmatch(value) or not lo <= int(value) <= hi:
        raise CardFieldError(field, "range")
    return int(value)


def yes_no(field: str, value: str) -> bool:
    lowered = value.strip().lower()
    if lowered not in ("yes", "no"):
        raise CardFieldError(field, "yes_no")
    return lowered == "yes"


def show(value: str) -> tuple[str, ...]:
    lowered = value.strip().lower()
    if lowered == "none":
        return ()
    items = [item.strip() for item in lowered.split(",")]
    if any(item not in SHOW_ITEMS for item in items):
        raise CardFieldError("show", "show_item")
    return tuple(item for item in SHOW_ITEMS if item in items)


def channel_target(field: str, value: str) -> tuple[str, str]:
    """A channel target for `field`: `<community id> <channel id>`, or a pasted channel link. A refusal names `field`."""
    stripped = value.strip()
    link = CHANNEL_LINK_RE.fullmatch(stripped)
    if link is not None:
        guild_id, channel_id = link.group(1), link.group(2)
    else:
        parts = stripped.split()
        if len(parts) != 2:
            raise CardFieldError(field, "channel_target")
        guild_id, channel_id = parts
    return snowflake(field, guild_id), snowflake(field, channel_id)


def manager(value: str, sources: Sources) -> tuple[str, str]:
    parts = value.split(maxsplit=1)
    if len(parts) != 2:
        raise CardFieldError("manager", "manager_format")
    kind, ref = parts
    if kind not in known_manager_kinds():
        raise CardFieldError("manager", "unknown_kind")
    configured = sources.managers.get(kind)
    if configured is None:
        raise CardFieldError("manager", "not_configured")
    return kind, configured.validate_ref(ref)


def probe(value: str, sources: Sources) -> tuple[str, str, int]:
    parts = value.split()
    if len(parts) != 3:
        raise CardFieldError("probe", "probe_format")
    kind, host_part, port_part = parts
    if kind not in sources.probes:
        raise CardFieldError("probe", "unknown_kind")
    return kind, host("probe", host_part), port("probe", port_part)


def refusal_reply(err: CardFieldError) -> str:
    """The reply for a refused field: the field name and the rule text, never the value. A field name outside
    FIELD_NAMES is replaced by a fixed label, so no text from the input can reach the reply through it."""
    field = err.field if err.field in FIELD_NAMES else text.FIELD_FALLBACK
    rule = text.RULES[err.rule].format(limit=TEXT_LIMITS.get(field, 0))
    return text.FIELD_ERROR.format(field=field, rule=rule)
