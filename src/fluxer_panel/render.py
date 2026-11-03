# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Card rendering: the inside-Fluxer embed, with every piece of card text escaped before it is placed.

The channel link is built only by `channel_link`, and `CHANNEL_LINK_RE` parses what it builds.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from fluxer_client.ids import require_snowflake
from fluxer_panel import text
from fluxer_panel.cards import Address, Card, ChannelLink
from fluxer_panel.resolver import CardStatus, StatusKind, status_label

ZWJ = "\u200d"  # zero-width joiner, written as an escape so no invisible character sits in the source

# Mention forms that Fluxer would expand: @everyone, @here, <@id>, <@!id>, <@&id> and <#id>.
_MENTION = re.compile(r"@(everyone|here)|<(@[!&]?|#)", re.IGNORECASE)
# Markdown control characters: each one is escaped with a backslash.
_MD = re.compile(r"([\\`*_~|>#\[\]()<:-])")

COLOURS = {
    StatusKind.READY: 0x2ECC71,
    StatusKind.WAKING: 0xF1C40F,
    StatusKind.GOING_TO_SLEEP: 0xE67E22,
    StatusKind.ASLEEP: 0x95A5A6,
    StatusKind.UNAVAILABLE: 0xE74C3C,
    StatusKind.UNKNOWN: 0x7F8C8D,
    StatusKind.NOT_RESPONDING: 0xE74C3C,
}
NEUTRAL = 0x5865F2
LIMITS = {
    "title": 256,
    "description": 1024,
    "join": 900,
    "first_time": 900,
    # Limits for the rendered (escaped) text. The worst-case growth is 2.5x, from `<#`: neutralise adds a
    # zero-width joiner, then both characters are escaped, so two characters become five. A validated location
    # (at most 200 characters, enforced by the database CHECK) and a validated detail (at most 48) therefore
    # render whole at 500 and 120.
    "location": 500,
    "detail": 120,
    # A channel name (voice or text), fitted after escaping. An entry is "[#" + name + "](" + link + ")", at most
    # 166 + the instance URL's length. Three entries and two newlines fit the 1024-character field at 174 characters.
    # The link is not fitted, so config caps FLUXER_INSTANCE_URL at 128 (config.INSTANCE_URL_MAX), well under 174.
    "channel_name": 100,
}


def neutralise(s: str) -> str:
    """Break @everyone/@here and <@…>/<@!…>/<@&…>/<#…> with a zero-width joiner."""
    return _MENTION.sub(lambda m: f"@{ZWJ}{m.group(1)}" if m.group(1) else f"<{ZWJ}{m.group(2)}", s)


def escape(s: str) -> str:
    """Untrusted text for an embed: mentions neutralised, then markdown control characters escaped."""
    return _MD.sub(r"\\\1", neutralise(s))


def fit(s: str, limit: int) -> str:
    """Cut escaped text to `limit` characters without leaving a dangling escape backslash."""
    if len(s) <= limit:
        return s
    cut = s[: limit - 1]
    trailing = len(cut) - len(cut.rstrip("\\"))
    if trailing % 2:  # an odd run of trailing backslashes would escape the ellipsis: drop the dangling one
        cut = cut[:-1]
    return cut + "…"


def code(s: str) -> str:
    """A code span. Backticks are removed, because they cannot be escaped inside one."""
    return "`" + s.replace("`", "") + "`"


def host_port(host: str, port: int) -> str:
    """`host:port`, with an IPv6 host in brackets so the port is unambiguous."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def channel_link(instance_url: str, guild_id: str, channel_id: str) -> str:
    """The only place a channel link is built. CHANNEL_LINK_RE parses it."""
    return f"{instance_url.rstrip('/')}/channels/{require_snowflake(guild_id)}/{require_snowflake(channel_id)}"


CHANNEL_LINK_RE = re.compile(r"https?://[^/\s]+/channels/([0-9]+)/([0-9]+)/?")  # parses what channel_link builds


def render_embed(card: Card, status: CardStatus | None, next_text: str | None, *,
                 instance_url: str | None) -> dict[str, Any]:
    """The inside-Fluxer embed. `next_text` is inserted as is: it is our own wording, not card text."""
    embed: dict[str, Any] = {
        "title": fit(escape(card.title), LIMITS["title"]),
        "color": COLOURS[status.kind] if status is not None else NEUTRAL,
    }
    if card.description is not None:
        embed["description"] = fit(escape(card.description), LIMITS["description"])

    fields: list[dict[str, Any]] = []

    def add(name: str, value: str) -> None:
        fields.append({"name": name, "value": value, "inline": False})

    if status is not None:
        add(text.FIELD_STATUS, status_label(status))
    if next_text is not None:
        add(text.FIELD_NEXT, next_text)
    if card.how_to_join is not None:
        add(text.FIELD_JOIN, fit(escape(card.how_to_join), LIMITS["join"]))
    if card.first_time is not None:
        add(text.FIELD_FIRST_TIME, fit(escape(card.first_time), LIMITS["first_time"]))
    if card.address is not None:
        add(text.FIELD_ADDRESS, code(host_port(card.address.host, card.address.port)))
    if card.voice is not None:
        add(text.FIELD_VOICE, _channel_value(card.voice, text.VOICE_UNNAMED, instance_url))
    linked = [link for link in card.text_channels if link is not None]
    if linked:
        add(text.FIELD_CHANNELS, "\n".join(_channel_value(link, text.TEXT_UNNAMED, instance_url) for link in linked))
    if card.location is not None:
        add(text.FIELD_LOCATION, fit(escape(card.location.text), LIMITS["location"]))
    if status is not None and status.details is not None:
        details = status.details
        for item, name, value in (
            ("map", text.FIELD_MAP, details.map),
            ("version", text.FIELD_VERSION, details.version),
            ("game_mode", text.FIELD_GAME_MODE, details.game_mode),
        ):
            if item in card.show_details and value is not None:
                add(name, fit(escape(value), LIMITS["detail"]))

    embed["fields"] = fields
    return embed


def _channel_value(channel: ChannelLink, unnamed: str, instance_url: str | None) -> str:
    """A channel's field entry: its escaped name when verified, else `unnamed`, as a link when the instance URL is known."""
    named = channel.verified and channel.channel_name is not None
    label = "#" + fit(escape(channel.channel_name), LIMITS["channel_name"]) if named else unnamed
    if instance_url is None:
        return label
    # The link is built here, and the label is escaped above, so no card text forms a link.
    return f"[{label}]({channel_link(instance_url, channel.guild_id, channel.channel_id)})"


def card_message(embed: dict[str, Any]) -> dict[str, Any]:
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def rendered_hash(embed: dict[str, Any]) -> str:
    canonical = json.dumps(embed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


# Outside Fluxer: plain text for a DM. It is mention-neutralised, not markdown-escaped.

def _outside_channel(name: str | None, unnamed: str) -> str:
    return text.OUTSIDE_CHANNEL_NAMED.format(name=neutralise(name)) if name else unnamed


def outside_voice(name: str | None) -> str:
    return _outside_channel(name, text.OUTSIDE_VOICE)


def outside_text_channel(name: str | None) -> str:
    return _outside_channel(name, text.OUTSIDE_TEXT)


def outside_address(address: Address, public: bool) -> str:
    return host_port(address.host, address.port) if public else text.OUTSIDE_ADDRESS_HIDDEN


def outside_location(has_location: bool) -> str | None:
    """Takes a bool only, so the location text cannot reach this line."""
    return text.OUTSIDE_LOCATION if has_location else None


def outside_text(card: Card, status: CardStatus | None, next_text: str | None) -> str:
    lines = [neutralise(card.title)]
    if status is not None:
        lines.append(status_label(status))
    if next_text is not None:
        lines.append(next_text)
    if card.how_to_join is not None:
        lines.append(neutralise(card.how_to_join))
    if card.address is not None:
        lines.append(outside_address(card.address, card.address.public))
    if card.voice is not None:
        lines.append(outside_voice(card.voice.channel_name if card.voice.verified else None))
    for link in card.text_channels:
        if link is not None:
            lines.append(outside_text_channel(link.channel_name if link.verified else None))
    location_line = outside_location(card.location is not None)
    if location_line is not None:
        lines.append(location_line)
    return "\n".join(lines)
