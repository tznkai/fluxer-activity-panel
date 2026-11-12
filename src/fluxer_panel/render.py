# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Card rendering: the inside-Fluxer embed, with every piece of card text escaped before it is placed.

The channel link is built only by `channel_link`, and `CHANNEL_LINK_RE` parses what it builds.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fluxer_client.ids import require_snowflake
from fluxer_panel import text
from fluxer_panel.cards import Address, Card, ChannelLink
from fluxer_panel.resolver import CardStatus, StatusKind, status_label

if TYPE_CHECKING:  # type-only: sessions imports render at runtime in a later phase, so these must not be runtime imports
    from fluxer_panel.rsvp import Counts
    from fluxer_panel.sessions import Session

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
    # Limits for the rendered (escaped) text, in UTF-16 code units (`units`). The worst-case growth is 2.5x, from `<#`:
    # neutralise adds a zero-width joiner, then both characters are escaped, so two characters become five. A validated
    # location (at most 200 characters, enforced by the database CHECK) and a validated detail (at most 48) therefore
    # render whole at 500 and 120. The growth is ASCII, and an astral character is two units against one character,
    # so the same bounds hold in units.
    "location": 500,
    "detail": 120,
    # A channel name (voice or text), fitted after escaping. Each slot is its own field, whose value is
    # "[#" + name + "](" + link + ")": at most 105 + the link, and the link is at most the instance URL (config caps
    # FLUXER_INSTANCE_URL at 128 UTF-16 units, INSTANCE_URL_MAX) + 61, so a value stays far under the 1024-unit field.
    "channel_name": 100,
    # A board cell's name: the card's title, escaped and fitted. A cell's value is a status, at most about 250 characters.
    "board_title": 50,
}

# The summary board. Fields come in rows of three, so a full embed holds a multiple of three. Fluxer caps a message
# at ten embeds, and the text of every embed in one message together at 6000 UTF-16 code units (BOARD_CHAR_LIMIT),
# counted with `units`. The budget is applied to the whole message, which is the stricter reading.
BOARD_FIELDS_PER_EMBED = 24
BOARD_MAX_EMBEDS = 10
BOARD_MAX_CARDS = BOARD_FIELDS_PER_EMBED * BOARD_MAX_EMBEDS
BOARD_CHAR_LIMIT = 6000

# One card embed: at most 25 fields, and its title, description and field names and values together at most 6000
# UTF-16 code units (`embed_units`). A card has at most 17 fields (status, next, join, first time, nine slots, location
# and three details). Only every slot and every other field at its limit at once passes 6000, and then the budget pass
# (`_fit_budget`) shortens first_time, then description, then join, each only as far as needed.
EMBED_MAX_FIELDS = 25
EMBED_CHAR_LIMIT = 6000


def neutralise(s: str) -> str:
    """Break @everyone/@here and <@…>/<@!…>/<@&…>/<#…> with a zero-width joiner."""
    return _MENTION.sub(lambda m: f"@{ZWJ}{m.group(1)}" if m.group(1) else f"<{ZWJ}{m.group(2)}", s)


def escape(s: str) -> str:
    """Untrusted text for an embed: mentions neutralised, then markdown control characters escaped."""
    return _MD.sub(r"\\\1", neutralise(s))


def units(s: str) -> int:
    """The length of `s` in UTF-16 code units, the unit Fluxer counts message text in. A character outside the Basic
    Multilingual Plane, an emoji for one, counts two. A lone surrogate counts one."""
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def fit(s: str, limit: int) -> str:
    """Cut escaped text to `limit` UTF-16 code units, the ellipsis included, without leaving a dangling escape backslash."""
    if units(s) <= limit:
        return s
    kept: list[str] = []
    used = 0
    for char in s:
        size = units(char)
        if used + size > limit - 1:
            break
        kept.append(char)
        used += size
    cut = "".join(kept)
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


def jump_link(label: str, instance_url: str, guild_id: str, channel_id: str, message_id: str) -> str:
    """A masked link to one message. The only place a jump link is built. `label` is our own wording, never card text.
    JUMP_LINK_RE parses the URL it builds."""
    url = (f"{instance_url.rstrip('/')}/channels/{require_snowflake(guild_id)}/{require_snowflake(channel_id)}"
           f"/{require_snowflake(message_id)}")
    return f"[{label}]({url})"


JUMP_LINK_RE = re.compile(r"\[([^\]]*)\]\(https?://[^/\s]+/channels/([0-9]+)/([0-9]+)/([0-9]+)\)")  # parses jump_link


def status_text(status: CardStatus, requested: int) -> str:
    """The status line, with the open wake request count after an Asleep label when there are requests."""
    label = status_label(status)
    if status.kind is StatusKind.ASLEEP and requested > 0:
        return label + text.REQUESTED_SUFFIX.format(n=requested)
    return label


@dataclass(frozen=True)
class NextSession:
    """What a card shows of its next session. `jump` is (guild_id, channel_id, message_id) of the session's message; None
    renders the line without its link, as a far copy does. `has_location` says only whether a location is set: the text
    never reaches a rendering."""
    starts_at: int
    going: int
    jump: tuple[str, str, str] | None
    has_location: bool


def _next_session_value(session: NextSession, instance_url: str | None) -> str:
    """The Next session field's value: the time, its relative time and the going count, then a jump link when the view has
    one and the instance URL is known. The label is our own wording."""
    value = text.NEXT_SESSION.format(ts=session.starts_at, going=session.going)
    if session.jump is not None and instance_url is not None:
        value += " · " + jump_link(text.NEXT_SESSION_OPEN, instance_url, *session.jump)
    return value


def render_embed(card: Card, status: CardStatus | None, next_text: str | None, *,
                 instance_url: str | None, requested: int = 0, next_session: NextSession | None = None) -> dict[str, Any]:
    """The inside-Fluxer embed. `next_text` is inserted as is: it is our own wording, not card text. `requested` is
    the card's count of open wake requests. `next_session` adds the Next session field; every card carries the DM hint
    footer."""
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
        add(text.FIELD_STATUS, status_text(status, requested))
    if next_text is not None:
        add(text.FIELD_NEXT, next_text)
    if next_session is not None:
        add(text.FIELD_NEXT_SESSION, _next_session_value(next_session, instance_url))
    if card.how_to_join is not None:
        add(text.FIELD_JOIN, fit(escape(card.how_to_join), LIMITS["join"]))
    if card.first_time is not None:
        add(text.FIELD_FIRST_TIME, fit(escape(card.first_time), LIMITS["first_time"]))
    # One field per slot: the voice slots, then the text channel slots, then the address slots, each in slot order.
    # An empty slot is skipped, and no slot moves up into its place.
    for link in card.voices:
        if link is not None:
            add(slot_label(text.FIELD_VOICE, link.suffix), _channel_value(link, text.VOICE_UNNAMED, instance_url))
    for link in card.text_channels:
        if link is not None:
            add(slot_label(text.FIELD_TEXT_CHANNEL, link.suffix), _channel_value(link, text.TEXT_UNNAMED, instance_url))
    for address in card.addresses:
        if address is not None:
            add(slot_label(text.FIELD_ADDRESS, address.suffix), code(host_port(address.host, address.port)))
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
    embed["footer"] = {"text": text.CARD_DM_HINT}
    _fit_budget(embed, fields)
    return embed


def slot_label(kind: str, suffix: str | None) -> str:
    """A slot field's name: the kind, with the escaped suffix in parentheses when the slot has one. The suffix is
    validated card text (`card_fields.suffix`), and it is escaped here too, as a second layer."""
    return kind if suffix is None else text.SLOT_LABEL.format(kind=kind, suffix=escape(suffix))


def embed_units(embed: Mapping[str, Any]) -> int:
    """The embed's length as Fluxer counts it: the title, the description, every field name and value and the footer
    text, in UTF-16 code units."""
    total = units(embed.get("title", "")) + units(embed.get("description", ""))
    total += sum(units(field["name"]) + units(field["value"]) for field in embed.get("fields", []))
    return total + units(embed.get("footer", {}).get("text", ""))


def _fit_budget(embed: dict[str, Any], fields: list[dict[str, Any]]) -> None:
    """Keeps the embed within EMBED_CHAR_LIMIT by shortening first_time, then the description, then join, each only as
    far as needed and never below the ellipsis. Nothing else is shortened. Without those three, the worst case is
    about 4580 units, so the limit always holds."""
    over = embed_units(embed) - EMBED_CHAR_LIMIT
    by_name = {field["name"]: field for field in fields}
    for holder, key in ((by_name.get(text.FIELD_FIRST_TIME), "value"), (embed, "description"),
                        (by_name.get(text.FIELD_JOIN), "value")):
        if over <= 0:
            return
        if holder is None or key not in holder:
            continue
        current = holder[key]
        shortened = fit(current, max(units(current) - over, 1))
        holder[key] = shortened
        over -= units(current) - units(shortened)


def _channel_value(channel: ChannelLink, unnamed: str, instance_url: str | None) -> str:
    """A channel's field entry: its escaped name when verified, else `unnamed`, as a link when the instance URL is known."""
    named = channel.verified and channel.channel_name is not None
    label = "#" + fit(escape(channel.channel_name), LIMITS["channel_name"]) if named else unnamed
    if instance_url is None:
        return label
    # The link is built here, and the label is escaped above, so no card text forms a link.
    return f"[{label}]({channel_link(instance_url, channel.guild_id, channel.channel_id)})"


def board_message(entries: Sequence[tuple[Card, CardStatus | None]], *, games_channel_id: str | None,
                  games_guild_id: str | None, instance_url: str | None,
                  requested: Mapping[int, int] | None = None) -> dict[str, Any]:
    """The whole summary board, as a message payload. One cell per card, in the order given: the title as the name and
    the status line as the value. `games_channel_id` is the board's channel: the one it is stored in, or the games
    channel when none is stored. `games_guild_id` is that channel's community. A status is a jump link only when the
    card's message is in the board's channel, the community is known and the instance URL is known, so a link never
    leaves the community the lookup named. A card with no live status shows a dash."""
    counts = requested or {}
    cells = [(fit(escape(card.title), LIMITS["board_title"]),
              _board_value(card, status, games_channel_id, games_guild_id, instance_url,
                           counts.get(card.id, 0)))
             for card, status in entries]
    shown = _fit_cells(cells)
    chunks = [shown[i:i + BOARD_FIELDS_PER_EMBED] for i in range(0, len(shown), BOARD_FIELDS_PER_EMBED)] or [[]]
    embeds: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        embed: dict[str, Any] = {"color": NEUTRAL}
        if index == 0:
            embed["title"] = text.BOARD_TITLE
        embed["fields"] = [{"name": name, "value": value, "inline": True} for name, value in chunk]
        embeds.append(embed)
    if not cells:
        embeds[0]["description"] = text.BOARD_EMPTY
    elif len(shown) < len(cells):
        embeds[-1]["description"] = text.BOARD_TRUNCATED
    return {"embeds": embeds, "allowed_mentions": {"parse": []}}


def _board_value(card: Card, status: CardStatus | None, games_channel_id: str | None,
                 games_guild_id: str | None, instance_url: str | None, requested: int) -> str:
    label = text.BOARD_NO_STATUS if status is None else status_text(status, requested)
    linkable = (instance_url is not None and games_guild_id is not None and games_channel_id is not None
                and card.message_id is not None and card.channel_id == games_channel_id)
    if not linkable:
        return label
    assert card.message_id is not None and games_channel_id is not None and games_guild_id is not None
    return jump_link(label, instance_url, games_guild_id, games_channel_id, card.message_id)


def _fit_cells(cells: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The leading cells that fit the card cap and the message character budget. When any cell is dropped, the
    budget also reserves room for the truncation note."""
    count = _count_within(cells, BOARD_CHAR_LIMIT)
    if count < len(cells):
        count = _count_within(cells, BOARD_CHAR_LIMIT - units(text.BOARD_TRUNCATED))
    return cells[:count]


def _count_within(cells: list[tuple[str, str]], budget: int) -> int:
    """How many leading cells fit `budget` UTF-16 units together with the board's title, which is counted too."""
    used = units(text.BOARD_TITLE)
    count = 0
    for name, value in cells:
        cost = units(name) + units(value)
        if count == BOARD_MAX_CARDS or used + cost > budget:
            break
        used += cost
        count += 1
    return count


def card_message(embed: dict[str, Any]) -> dict[str, Any]:
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def rendered_hash(embed: dict[str, Any]) -> str:
    canonical = json.dumps(embed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


# Outside Fluxer: plain text for a DM. It is mention-neutralised, not markdown-escaped.

def _outside_channel(name: str | None, unnamed: str, suffix: str | None) -> str:
    line = text.OUTSIDE_CHANNEL_NAMED.format(name=neutralise(name)) if name else unnamed
    return line if suffix is None else line + text.OUTSIDE_SUFFIX.format(suffix=neutralise(suffix))


def outside_voice(name: str | None, suffix: str | None = None) -> str:
    return _outside_channel(name, text.OUTSIDE_VOICE, suffix)


def outside_text_channel(name: str | None, suffix: str | None = None) -> str:
    return _outside_channel(name, text.OUTSIDE_TEXT, suffix)


def outside_address(address: Address, public: bool) -> str:
    """The single place that decides, for every address slot, whether the outside line shows the host or only says
    the address is in Fluxer. The host is shown only when `public` is true."""
    if address.suffix is None:
        return host_port(address.host, address.port) if public else text.OUTSIDE_ADDRESS_HIDDEN
    suffix = neutralise(address.suffix)
    if public:
        return text.OUTSIDE_ADDRESS_PUBLIC_SUFFIX.format(suffix=suffix, address=host_port(address.host, address.port))
    return text.OUTSIDE_ADDRESS_HIDDEN_SUFFIX.format(suffix=suffix)


def outside_location(has_location: bool) -> str | None:
    """Takes a bool only, so the location text cannot reach this line."""
    return text.OUTSIDE_LOCATION if has_location else None


def _outside_next_session(session: NextSession) -> str:
    """The outside session line: the time and the going count, then the placeholder when a location is set. Never a link,
    and never the location text (`outside_location` takes a bool)."""
    line = text.OUTSIDE_NEXT_SESSION.format(ts=session.starts_at, going=session.going)
    where = outside_location(session.has_location)
    return line if where is None else line + " · " + where


def outside_text(card: Card, status: CardStatus | None, next_text: str | None,
                 next_session: NextSession | None = None) -> str:
    lines = [neutralise(card.title)]
    if status is not None:
        lines.append(status_label(status))
    if next_text is not None:
        lines.append(next_text)
    if next_session is not None:
        lines.append(_outside_next_session(next_session))
    if card.how_to_join is not None:
        lines.append(neutralise(card.how_to_join))
    # The slots in the embed's order: voice, text channel, address.
    for link in card.voices:
        if link is not None:
            lines.append(outside_voice(link.channel_name if link.verified else None, link.suffix))
    for link in card.text_channels:
        if link is not None:
            lines.append(outside_text_channel(link.channel_name if link.verified else None, link.suffix))
    for address in card.addresses:
        if address is not None:
            lines.append(outside_address(address, address.public))
    location_line = outside_location(card.location is not None)
    if location_line is not None:
        lines.append(location_line)
    return "\n".join(lines)


# The play-now message. The content is our own wording with the card title escaped, and its one mention is the card's
# follow role. The sender adds allowed_mentions, so nothing here can ping a user.

def _join_fields(card: Card, instance_url: str | None) -> list[dict[str, Any]]:
    """The join part shared by the play-now and session messages: each public address slot, then each voice slot,
    labelled as on the card, then the join text. A private address is never shown."""
    fields: list[dict[str, Any]] = []
    for address in card.addresses:
        if address is not None and address.public:
            fields.append({"name": slot_label(text.FIELD_ADDRESS, address.suffix),
                           "value": code(host_port(address.host, address.port)), "inline": False})
    for link in card.voices:
        if link is not None:
            fields.append({"name": slot_label(text.FIELD_VOICE, link.suffix),
                           "value": _channel_value(link, text.VOICE_UNNAMED, instance_url), "inline": False})
    if card.how_to_join is not None:
        fields.append({"name": text.FIELD_JOIN, "value": fit(escape(card.how_to_join), LIMITS["join"]),
                       "inline": False})
    return fields


def play_now_message(card: Card, *, role_id: str | None, playing: int | None, ended: bool,
                     instance_url: str | None) -> dict[str, Any]:
    """{"content": ..., "embeds": [embed]}; no allowed_mentions here (the sender adds it). `playing` None leaves the
    count line out: it is used when the count could not be read, so the message never shows a guess."""
    title = escape(card.title)
    if ended:
        content = text.PLAY_NOW_ENDED_CONTENT.format(card=title)
    else:
        content = text.PLAY_NOW_CONTENT.format(card=title)
        if role_id is not None:
            content += f" <@&{require_snowflake(role_id)}>"
    fields: list[dict[str, Any]] = []
    if playing is not None:
        fields.append({"name": text.FIELD_STATUS, "value": text.PLAY_NOW_PLAYING.format(n=playing), "inline": False})
    fields.extend(_join_fields(card, instance_url))
    embed: dict[str, Any] = {"title": fit(title, LIMITS["title"]), "color": NEUTRAL, "fields": fields}
    return {"content": content, "embeds": [embed]}


# The session message: an embed with no mention in it. The sender adds allowed_mentions, and the text is escaped here.

def session_message(card: Card, session: Session, counts: Counts, *, effective_location: str | None,
                    effective_seats: int | None, instance_url: str | None, scrubbed: bool = False,
                    provisional: bool = False) -> dict[str, Any]:
    """The session message. `effective_location` and `effective_seats` are the values the session resolves to; the
    caller passes them, so this renderer reads nothing else. A cancelled or scrubbed session shows no location. A
    provisional session has no stored id yet, so its message has no footer; the real footer comes with the first edit."""
    fields: list[dict[str, Any]] = []
    if session.state != "upcoming" or session.moved:
        fields.append({"name": text.SESSION_FIELD_STATUS, "value": _session_status(session), "inline": False})
    fields.append({"name": text.SESSION_FIELD_WHEN, "value": text.SESSION_WHEN.format(ts=session.starts_at),
                   "inline": False})
    fields.extend(_join_fields(card, instance_url))
    location: dict[str, Any] | None = None
    if effective_location is not None and session.state != "cancelled" and not scrubbed:
        location = {"name": text.SESSION_FIELD_LOCATION, "value": fit(escape(effective_location), LIMITS["location"]),
                    "inline": False}
        fields.append(location)
    if effective_seats is not None:
        fields.append({"name": text.SESSION_FIELD_SEATS, "value": str(effective_seats), "inline": False})
    rsvp_value = text.SESSION_COUNTS.format(going=counts.going, maybe=counts.maybe)
    if counts.waiting > 0:
        rsvp_value += text.SESSION_COUNTS_WAITING.format(waiting=counts.waiting)
    fields.append({"name": text.SESSION_FIELD_RSVPS, "value": rsvp_value, "inline": False})
    embed: dict[str, Any] = {
        "title": fit(escape(card.title), LIMITS["title"]),
        "color": NEUTRAL,
        "fields": fields,
    }
    if not provisional:
        embed["footer"] = {"text": text.SESSION_FOOTER.format(n=session.id)}
    join = [field for field in fields if field["name"] == text.FIELD_JOIN]
    _fit_session_budget(embed, [*join, *([location] if location is not None else [])])
    return {"embeds": [embed], "allowed_mentions": {"parse": []}}


def _session_status(session: Session) -> str:
    if session.state == "ended":
        return text.SESSION_ENDED
    if session.state == "cancelled":
        return text.SESSION_CANCELLED
    return text.SESSION_MOVED


def _fit_session_budget(embed: dict[str, Any], holders: list[dict[str, Any]]) -> None:
    """Shortens the given field values, in order, only as far as the embed limit needs. With the current limits the
    worst case already fits (the AC3.4 test), so this is the backstop the plan asks for, and it never shortens more."""
    over = embed_units(embed) - EMBED_CHAR_LIMIT
    for holder in holders:
        if over <= 0:
            return
        current = holder["value"]
        shortened = fit(current, max(units(current) - over, 1))
        holder["value"] = shortened
        over -= units(current) - units(shortened)
