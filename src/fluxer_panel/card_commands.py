# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DM-only card commands behind `!card`. Every reply is a fixed `text` string or a rule text, never input echoed."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Container
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from fluxer_client.events import MessageCreate
from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_panel import card_fields, cards, render, text
from fluxer_panel.cards import SHOW_ITEMS, CardFieldError, ChannelLink
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.logs import log_event
from fluxer_panel.sources.registry import PROBES, Sources, known_manager_kinds, manager_ref_hints

log = logging.getLogger(__name__)

VOICE_CHANNEL_TYPE = 2
# Discord's text channel type. Not yet confirmed against Fluxer: `markup_live.py` prints the live type of the
# posting channel, so the operator can check this value on a real instance.
TEXT_CHANNEL_TYPE = 0
_CHANNEL_KINDS = {VOICE_CHANNEL_TYPE: text.CHANNEL_KIND_VOICE, TEXT_CHANNEL_TYPE: text.CHANNEL_KIND_TEXT}
CHANNEL_NAME_MAX = 100
LIST_CHUNK = 1900
PREVIEW_LIMIT = 2000  # Fluxer's message content limit
SUBCOMMANDS = ("new", "set", "clear", "show", "list", "publish", "archive", "help")
SETTABLE = ("title", "description", "join", "first_time", "sort", "seats", "location", "voice",
            "text_channel1", "text_channel2", "text_channel3", "address", "address_public", "manager", "probe", "show")
CLEARABLE = ("description", "join", "first_time", "sort", "seats", "location", "voice",
             "text_channel1", "text_channel2", "text_channel3", "address", "manager", "probe", "show")
# Text channel fields and the slot each one writes. The slots are the store's, so no input picks a table or a slot.
TEXT_CHANNEL_SLOTS = {"text_channel1": 1, "text_channel2": 2, "text_channel3": 3}
# Settable fields that are stored as base columns, and the column each one writes.
BASE_COLUMNS = {"title": "title", "description": "description", "join": "how_to_join", "first_time": "first_time",
                "sort": "sort_order", "show": "show_details"}
# Clearable fields stored as base columns: the column and the value a clear writes.
BASE_CLEARS = {"description": ("description", None), "join": ("how_to_join", None),
               "first_time": ("first_time", None), "sort": ("sort_order", 0), "show": ("show_details", ())}
SLOT_CLEARS = frozenset({"voice", "address", "location", "seats", "manager", "probe"})
_FIELD_NAMES = frozenset(SETTABLE) | {"slug"}


class CardLocks:
    """One asyncio.Lock per card id, shared by the commands and the poller."""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    def get(self, card_id: int) -> asyncio.Lock:
        return self._locks.setdefault(card_id, asyncio.Lock())


@dataclass
class CardContext:
    db: Database
    rest: FluxerREST
    config: Config
    sources: Sources
    kick: Callable[[], None] = lambda: None
    clock: Callable[[], float] = time.time
    locks: CardLocks = field(default_factory=CardLocks)


@dataclass(frozen=True)
class _Result:
    """What a handled subcommand touched, for the admin_command log line."""
    card_id: int | None = None
    field: str | None = None


def _known(value: str, allowed: Container[str]) -> str:
    """A name from user input, logged only when it is in the fixed vocabulary."""
    return value if value in allowed else "unknown"


async def _say(ctx: CardContext, event: MessageCreate, content: str) -> None:
    await ctx.rest.send_message(event.channel_id, content=content, allowed_mentions={"parse": []})


async def _refuse(ctx: CardContext, event: MessageCreate, err: CardFieldError) -> None:
    await _say(ctx, event, card_fields.refusal_reply(err))
    log_event(log, "card_field_refused", field=_known(err.field, _FIELD_NAMES),
              reason=_known(err.rule, text.RULES), user_id=event.author_id)


async def _find(ctx: CardContext, slug: str) -> cards.Card | None:
    return await ctx.db.run(lambda c: cards.get_by_slug(c, slug))


async def handle(args: str, event: MessageCreate, ctx: CardContext) -> None:
    parts = args.split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    handler = _HANDLERS.get(sub)
    if handler is None:
        await _say(ctx, event, text.CARD_USAGE)
        result = _Result()
    else:
        result = await handler(event, ctx, rest)
    command = f"card_{sub}" if sub in SUBCOMMANDS else "card_unknown"
    fields: dict[str, Any] = {"command": command, "user_id": event.author_id}
    if result.card_id is not None:
        fields["card_id"] = result.card_id
    if result.field is not None:
        fields["field"] = result.field
    log_event(log, "admin_command", **fields)


# new -------------------------------------------------------------------------------------------------

async def _new(event: MessageCreate, ctx: CardContext, rest: str) -> _Result:
    parts = rest.split(maxsplit=1)
    if len(parts) != 2:
        await _say(ctx, event, text.CARD_USAGE)
        return _Result()
    slug_raw, title_raw = parts
    try:
        slug_value = card_fields.slug(slug_raw)
        title_value = card_fields.text_value("title", title_raw)
    except CardFieldError as err:
        await _refuse(ctx, event, err)
        return _Result()
    now = int(ctx.clock())
    try:
        card_id = await ctx.db.run(lambda c: cards.create_card(
            c, slug=slug_value, title=title_value, author_id=event.author_id, now=now))
    except CardFieldError as err:
        await _refuse(ctx, event, err)
        return _Result()
    await _say(ctx, event, text.CARD_CREATED)
    return _Result(card_id=card_id)


# set -------------------------------------------------------------------------------------------------

async def _set(event: MessageCreate, ctx: CardContext, rest: str) -> _Result:
    parts = rest.split(maxsplit=2)
    if len(parts) != 3:
        await _say(ctx, event, text.CARD_USAGE)
        return _Result()
    slug_raw, field_raw, value = parts
    field_name = field_raw.lower()
    card = await _find(ctx, slug_raw)
    if card is None:
        await _say(ctx, event, text.NO_SUCH_CARD)
        return _Result()
    logged = _known(field_name, SETTABLE)
    if field_name not in SETTABLE:
        await _say(ctx, event, text.UNKNOWN_FIELD)
        return _Result(card.id, logged)
    try:
        write, reply = await _prepare_set(ctx, card, field_name, value)
    except CardFieldError as err:
        await _refuse(ctx, event, err)
        return _Result(card.id, logged)
    if write is not None:
        await ctx.db.run(write)
        ctx.kick()
    await _say(ctx, event, reply)
    return _Result(card.id, logged)


async def _prepare_set(ctx: CardContext, card: cards.Card, field_name: str,
                       value: str) -> tuple[Callable[[Any], None] | None, str]:
    """Validate one field value. Returns the database write (None when nothing is stored) and the reply."""
    now = int(ctx.clock())
    card_id = card.id
    if field_name in BASE_COLUMNS:
        column = BASE_COLUMNS[field_name]
        stored = _base_value(field_name, value)
        return (lambda c: cards.update_fields(c, card_id, now, **{column: stored})), text.CARD_UPDATED
    if field_name == "seats":
        seats = card_fields.int_range("seats", value, *card_fields.SEATS_RANGE)
        return (lambda c: cards.set_seats(c, card_id, seats)), text.CARD_UPDATED
    if field_name == "location":
        location = cards.Location(card_fields.text_value("location", value))
        return (lambda c: cards.set_location(c, card_id, location)), text.CARD_UPDATED
    if field_name == "address":
        host, port = card_fields.address(value)
        public = card.address.public if card.address is not None else False
        address = cards.Address(host, port, public)
        return (lambda c: cards.set_address(c, card_id, address)), text.CARD_UPDATED
    if field_name == "address_public":
        public = card_fields.yes_no("address_public", value)
        if card.address is None:
            raise CardFieldError("address_public", "no_address")
        return (lambda c: cards.set_address_public(c, card_id, public)), text.CARD_UPDATED
    if field_name == "manager":
        kind, ref = card_fields.manager(value, ctx.sources)
        manager = cards.ManagerRef(kind, ref)
        return (lambda c: cards.set_manager(c, card_id, manager)), text.CARD_UPDATED
    if field_name == "probe":
        kind, host, port = card_fields.probe(value, ctx.sources)
        probe = cards.ProbeRef(kind, host, port)
        return (lambda c: cards.set_probe(c, card_id, probe)), text.CARD_UPDATED
    if field_name in TEXT_CHANNEL_SLOTS:
        slot = TEXT_CHANNEL_SLOTS[field_name]
        guild_id, channel_id = card_fields.channel_target(field_name, value)
        link, reply = await _check_channel(ctx.rest, guild_id, channel_id, TEXT_CHANNEL_TYPE)
        if link is None:
            return None, reply
        return (lambda c: cards.set_text_channel(c, card_id, slot, link)), reply
    guild_id, channel_id = card_fields.channel_target("voice", value)
    voice, reply = await _check_channel(ctx.rest, guild_id, channel_id, VOICE_CHANNEL_TYPE)
    if voice is None:
        return None, reply
    return (lambda c: cards.set_voice(c, card_id, voice)), reply


def _base_value(field_name: str, value: str) -> Any:
    if field_name == "sort":
        return card_fields.int_range("sort", value, *card_fields.SORT_RANGE)
    if field_name == "show":
        return card_fields.show(value)
    return card_fields.text_value(field_name, value)


async def _check_channel(rest: FluxerREST, guild_id: str, channel_id: str,
                         channel_type: int) -> tuple[ChannelLink | None, str]:
    """Checks a channel link for a slot that takes `channel_type`. Returns the link to store (None when nothing should
    be stored) and the reply. The community is checked first, so a community the bot is not in is stored unverified."""
    try:
        await rest.get_guild(guild_id)
    except FluxerHTTPError as exc:
        if exc.status in (403, 404):
            return ChannelLink(guild_id, channel_id, None, False), text.CHANNEL_UNVERIFIED
        return None, text.CHANNEL_RETRY
    except (aiohttp.ClientError, TimeoutError):
        return None, text.CHANNEL_RETRY
    try:
        channel = await rest.get_channel(channel_id)
    except FluxerHTTPError as exc:
        if exc.status in (403, 404):
            return None, text.CHANNEL_MISSING
        return None, text.CHANNEL_RETRY
    except (aiohttp.ClientError, TimeoutError):
        return None, text.CHANNEL_RETRY
    if channel.get("type") != channel_type:
        return None, text.CHANNEL_WRONG_TYPE.format(kind=_CHANNEL_KINDS[channel_type])
    if str(channel.get("guild_id")) != guild_id:
        return None, text.CHANNEL_OTHER_COMMUNITY
    return ChannelLink(guild_id, channel_id, channel_name(channel.get("name")), True), text.CHANNEL_VERIFIED


def channel_name(value: object) -> str | None:
    """A channel name from Fluxer: printable characters only, whitespace collapsed, at most CHANNEL_NAME_MAX."""
    if not isinstance(value, str):
        return None
    printable = "".join(ch for ch in value if ch.isprintable())
    return " ".join(printable.split())[:CHANNEL_NAME_MAX] or None


# clear -----------------------------------------------------------------------------------------------

async def _clear(event: MessageCreate, ctx: CardContext, rest: str) -> _Result:
    parts = rest.split()
    if len(parts) != 2:
        await _say(ctx, event, text.CARD_USAGE)
        return _Result()
    slug_raw, field_raw = parts
    field_name = field_raw.lower()
    card = await _find(ctx, slug_raw)
    if card is None:
        await _say(ctx, event, text.NO_SUCH_CARD)
        return _Result()
    logged = _known(field_name, CLEARABLE)
    if field_name not in CLEARABLE:
        await _say(ctx, event, text.UNKNOWN_FIELD)
        return _Result(card.id, logged)
    now = int(ctx.clock())
    card_id = card.id
    if field_name in TEXT_CHANNEL_SLOTS:
        slot = TEXT_CHANNEL_SLOTS[field_name]
        await ctx.db.run(lambda c: cards.clear_text_channel(c, card_id, slot))
    elif field_name in SLOT_CLEARS:
        await ctx.db.run(lambda c: cards.clear_slot(c, card_id, field_name))
    else:
        column, empty = BASE_CLEARS[field_name]
        await ctx.db.run(lambda c: cards.update_fields(c, card_id, now, **{column: empty}))
    ctx.kick()
    await _say(ctx, event, text.CARD_CLEARED)
    return _Result(card_id, logged)


# show, list ------------------------------------------------------------------------------------------

async def _show(event: MessageCreate, ctx: CardContext, rest: str) -> _Result:
    slug = _one_slug(rest)
    if slug is None:
        await _say(ctx, event, text.CARD_USAGE)
        return _Result()
    card = await _find(ctx, slug)
    if card is None:
        await _say(ctx, event, text.NO_SUCH_CARD)
        return _Result()
    embed = render.render_embed(card, None, None, instance_url=ctx.config.instance_url)
    await ctx.rest.send_message(event.channel_id, content=text.SHOW_NOTE, embeds=[embed],
                                allowed_mentions={"parse": []})
    # The outside text is plain text everywhere else. In this DM it is escaped, so it shows as typed, and then
    # fitted so the label, newline and body stay within the message limit.
    label = text.SHOW_OUTSIDE_LABEL
    body = render.fit(render.escape(render.outside_text(card, None, None)), PREVIEW_LIMIT - len(label) - 1)
    await _say(ctx, event, label + "\n" + body)
    return _Result(card.id)


async def _list(event: MessageCreate, ctx: CardContext, rest: str) -> _Result:
    if rest.split():
        await _say(ctx, event, text.CARD_USAGE)
        return _Result()
    listed = await ctx.db.run(lambda c: cards.list_cards(c))
    if not listed:
        await _say(ctx, event, text.NO_CARDS)
        return _Result()
    lines = [f"{card.slug} · {card.state} · {render.neutralise(card.title)}" for card in listed]
    for chunk in _chunks(lines):
        await _say(ctx, event, chunk)
    return _Result()


def _chunks(lines: list[str]) -> list[str]:
    """Joins lines into messages of under LIST_CHUNK characters each, keeping order."""
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = line if not current else current + "\n" + line
        if current and len(candidate) >= LIST_CHUNK:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# publish, archive -------------------------------------------------------------------------------------

async def _publish(event: MessageCreate, ctx: CardContext, rest: str) -> _Result:
    slug = _one_slug(rest)
    if slug is None:
        await _say(ctx, event, text.CARD_USAGE)
        return _Result()
    found = await _find(ctx, slug)
    if found is None:
        await _say(ctx, event, text.NO_SUCH_CARD)
        return _Result()
    card_id = found.id
    now = int(ctx.clock())

    def publish_tx(conn: Any) -> str:
        """Re-reads the card under the lock. Returns "missing", "live" (nothing changed) or "published"."""
        current = cards.get_by_id(conn, card_id)
        if current is None:
            return "missing"
        if current.state == "live":
            return "live"
        cards.set_state(conn, card_id, "live", now)
        return "published"

    # The lock is shared with archive and the poller, so a publish cannot interleave with an archive's delete.
    async with ctx.locks.get(card_id):
        outcome = await ctx.db.run(publish_tx)
    if outcome == "missing":
        await _say(ctx, event, text.NO_SUCH_CARD)
        return _Result()
    if outcome == "live":
        await _say(ctx, event, text.ALREADY_LIVE)
        return _Result(card_id)
    ctx.kick()
    reply = text.CARD_PUBLISHED if ctx.config.games_channel_id is not None else text.PUBLISHED_NO_CHANNEL
    await _say(ctx, event, reply)
    return _Result(card_id)


async def _archive(event: MessageCreate, ctx: CardContext, rest: str) -> _Result:
    slug = _one_slug(rest)
    if slug is None:
        await _say(ctx, event, text.CARD_USAGE)
        return _Result()
    found = await _find(ctx, slug)
    if found is None:
        await _say(ctx, event, text.NO_SUCH_CARD)
        return _Result()
    card_id = found.id
    async with ctx.locks.get(card_id):
        current = await ctx.db.run(lambda c: cards.get_by_id(c, card_id))
        if current is None:  # deleted while this command waited for the lock
            await _say(ctx, event, text.NO_SUCH_CARD)
            return _Result(card_id)
        if current.state == "archived":
            await _say(ctx, event, text.ALREADY_ARCHIVED)
            return _Result(card_id)
        if current.channel_id and current.message_id:
            try:
                await ctx.rest.delete_message(current.channel_id, current.message_id)
            except FluxerHTTPError as exc:
                if exc.status != 404:
                    await _say(ctx, event, text.ARCHIVE_RETRY)
                    return _Result(card_id)
            except (aiohttp.ClientError, TimeoutError):
                await _say(ctx, event, text.ARCHIVE_RETRY)
                return _Result(card_id)
        now = int(ctx.clock())

        def archive_tx(conn: Any) -> None:
            cards.set_message(conn, card_id, None, None, None)
            cards.set_state(conn, card_id, "archived", now)

        await ctx.db.run(archive_tx)
    ctx.kick()
    await _say(ctx, event, text.CARD_ARCHIVED)
    return _Result(card_id)


# help ------------------------------------------------------------------------------------------------

def help_fields_reply() -> str:
    """The fields page, with each limit and kind taken from the code that enforces it."""
    return text.HELP_FIELDS.format(
        title=card_fields.TEXT_LIMITS["title"], description=card_fields.TEXT_LIMITS["description"],
        join=card_fields.TEXT_LIMITS["join"], first_time=card_fields.TEXT_LIMITS["first_time"],
        location=card_fields.TEXT_LIMITS["location"],
        sort_min=card_fields.SORT_RANGE[0], sort_max=card_fields.SORT_RANGE[1],
        seats_min=card_fields.SEATS_RANGE[0], seats_max=card_fields.SEATS_RANGE[1],
        manager_kinds="|".join(sorted(known_manager_kinds())),
        manager_refs="; ".join(f"{kind} {hint}" for kind, hint in sorted(manager_ref_hints().items())),
        probe_kinds="|".join(PROBES), show_items=", ".join(SHOW_ITEMS),
    )


async def _help(event: MessageCreate, ctx: CardContext, rest: str) -> _Result:
    topic = rest.split()
    if not topic:
        await _say(ctx, event, text.HELP_COMMANDS)
    elif [word.lower() for word in topic] == ["fields"]:
        await _say(ctx, event, help_fields_reply())
    else:
        await _say(ctx, event, text.CARD_USAGE)
    return _Result()


def _one_slug(rest: str) -> str | None:
    parts = rest.split()
    return parts[0] if len(parts) == 1 else None


_HANDLERS: dict[str, Callable[[MessageCreate, CardContext, str], Any]] = {
    "new": _new,
    "set": _set,
    "clear": _clear,
    "show": _show,
    "list": _list,
    "publish": _publish,
    "archive": _archive,
    "help": _help,
}
