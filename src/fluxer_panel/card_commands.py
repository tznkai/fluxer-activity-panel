# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DM-only card commands behind `!card`. Every reply is a fixed `text` string or a rule text, never input echoed."""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections.abc import Callable, Container
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import aiohttp

from fluxer_client.events import MessageCreate
from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import card_fields, cards, follow_roles, interaction_settings, render, subscriptions, text
from fluxer_panel.cards import SHOW_ITEMS, CardFieldError, ChannelLink
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.logs import log_event
from fluxer_panel.presses import FOLLOW_EMOJI, PLAY_NOW_EMOJI, WAKE_EMOJI
from fluxer_panel.seeding import Seeder, has_play_now_target, wanted_emoji
from fluxer_panel.sources.registry import PROBES, Sources, known_manager_kinds, manager_ref_hints

if TYPE_CHECKING:
    # wake.py imports CardLocks from this module, so the wake requests are named for type checking only.
    from fluxer_panel.wake import WakeRequests

log = logging.getLogger(__name__)

VOICE_CHANNEL_TYPE = 2
# The text channel type, the same value as Discord's. Confirmed on a live instance: `markup_live.py` printed type 0
# for the posting channel.
TEXT_CHANNEL_TYPE = 0
_CHANNEL_KINDS = {VOICE_CHANNEL_TYPE: text.CHANNEL_KIND_VOICE, TEXT_CHANNEL_TYPE: text.CHANNEL_KIND_TEXT}
CHANNEL_NAME_MAX = 100
LIST_CHUNK = 1900  # UTF-16 units: a list message stays under Fluxer's 2000-unit message limit
PREVIEW_LIMIT = 2000  # Fluxer's message content limit
SUBCOMMANDS = ("new", "set", "clear", "show", "list", "publish", "archive", "subscribe", "unsubscribe", "interactions",
               "help")
SETTABLE = ("title", "description", "join", "first_time", "sort", "seats", "location", "voice",
            "text_channel1", "text_channel2", "text_channel3", "address", "address_public", "manager", "probe", "show",
            "follow_role", "play_now_channel")
CLEARABLE = ("description", "join", "first_time", "sort", "seats", "location", "voice",
             "text_channel1", "text_channel2", "text_channel3", "address", "manager", "probe", "show", "follow_role",
             "play_now_channel")
# Text channel fields and the slot each one writes. The slots are the store's, so no input picks a table or a slot.
TEXT_CHANNEL_SLOTS = {"text_channel1": 1, "text_channel2": 2, "text_channel3": 3}
# Settable fields that are stored as base columns, and the column each one writes.
BASE_COLUMNS = {"title": "title", "description": "description", "join": "how_to_join", "first_time": "first_time",
                "sort": "sort_order", "show": "show_details"}
# Clearable fields stored as base columns: the column and the value a clear writes.
BASE_CLEARS = {"description": ("description", None), "join": ("how_to_join", None),
               "first_time": ("first_time", None), "sort": ("sort_order", 0), "show": ("show_details", ())}
SLOT_CLEARS = frozenset({"voice", "address", "location", "seats", "manager", "probe"})
# Settable fields whose change shows on the posted messages' reactions, so a set re-seeds them.
RESEED_FIELDS = frozenset({"follow_role", "play_now_channel"})
# Role outcomes after which a card has a working follow role, so its bell is seeded and the reply says following is on.
_FOLLOWING = frozenset({follow_roles.Ensured.REUSED, follow_roles.Ensured.ADOPTED, follow_roles.Ensured.CREATED})
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
    roles: RoleCache | None = None
    games_guild: GamesGuild | None = None
    seeder: Seeder | None = None
    identity: BotIdentity | None = None
    wake: WakeRequests | None = None


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
    if field_name in RESEED_FIELDS:
        await _reseed(ctx, card.id)
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
    if field_name == "follow_role":
        return await _set_follow_role(ctx, card, value)
    if field_name in TEXT_CHANNEL_SLOTS:
        slot = TEXT_CHANNEL_SLOTS[field_name]
        guild_id, channel_id = card_fields.channel_target(field_name, value)
        link, reply = await _check_channel(ctx.rest, guild_id, channel_id, TEXT_CHANNEL_TYPE)
        if link is None:
            return None, reply
        return (lambda c: cards.set_text_channel(c, card_id, slot, link)), reply
    if field_name == "play_now_channel":
        return await _set_play_now_channel(ctx, card_id, value)
    guild_id, channel_id = card_fields.channel_target("voice", value)
    voice, reply = await _check_channel(ctx.rest, guild_id, channel_id, VOICE_CHANNEL_TYPE)
    if voice is None:
        return None, reply
    return (lambda c: cards.set_voice(c, card_id, voice)), reply


async def _set_follow_role(ctx: CardContext, card: cards.Card, value: str) -> tuple[None, str]:
    """Checks a role id and stores it when every check passes: `ensure_follow_role` does the store, so this returns no
    write and only the reply."""
    role_id = card_fields.snowflake("follow_role", value)
    guild = await ctx.games_guild.get() if ctx.games_guild is not None else None
    if guild is None:
        return None, text.FOLLOW_NO_GAMES
    deps = _follow_deps(ctx)
    if deps is None:
        return None, text.FOLLOW_REFUSED["no_bot_id"]
    res = await follow_roles.ensure_follow_role(deps, guild, card, role_id=role_id)
    if res.outcome is follow_roles.Ensured.SET:
        return None, text.FOLLOW_ROLE_SET.format(slug=card.slug)
    return None, text.FOLLOW_REFUSED[res.reason or "lookup_failed"]


async def _set_play_now_channel(ctx: CardContext, card_id: int, value: str) -> tuple[Callable[[Any], None] | None, str]:
    """A play-now channel is a text channel, checked as a subscription is. The games channel is refused before any
    Fluxer call, and an unverified community is refused too, so nothing unverified is stored."""
    guild_id, channel_id = card_fields.channel_target("play_now_channel", value)
    if channel_id == ctx.config.games_channel_id:
        return None, text.PLAY_NOW_IS_GAMES
    link, reply = await _check_channel(ctx.rest, guild_id, channel_id, TEXT_CHANNEL_TYPE)
    if link is None:
        return None, reply
    if not link.verified:
        return None, text.SUBSCRIPTION_NOT_IN_COMMUNITY
    return (lambda c: cards.set_play_now_channel(c, card_id, link)), reply


def _follow_deps(ctx: CardContext) -> follow_roles.FollowDeps | None:
    """The role dependencies, or None when following is off: a context built without them makes no role call."""
    if ctx.roles is None or ctx.identity is None:
        return None
    return follow_roles.FollowDeps(db=ctx.db, rest=ctx.rest, config=ctx.config, roles=ctx.roles, identity=ctx.identity)


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
    if field_name == "follow_role":
        # The bell already on the card stays. A press on it then gets "Following isn't available", which is correct.
        old = await ctx.db.run(lambda c: cards.clear_follow_role(c, card_id))
        if old is None:
            await _say(ctx, event, text.FOLLOW_ROLE_NONE.format(slug=card.slug))
        else:
            await _say(ctx, event, text.FOLLOW_ROLE_CLEARED.format(slug=card.slug, role=f"<@&{old.role_id}>"))
        return _Result(card_id, logged)
    if field_name in TEXT_CHANNEL_SLOTS:
        slot = TEXT_CHANNEL_SLOTS[field_name]
        await ctx.db.run(lambda c: cards.clear_text_channel(c, card_id, slot))
    elif field_name == "play_now_channel":
        await ctx.db.run(lambda c: cards.clear_play_now_channel(c, card_id))
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
    body = render.fit(render.escape(render.outside_text(card, None, None)), PREVIEW_LIMIT - render.units(label) - 1)
    await _say(ctx, event, label + "\n" + body)
    # Only a card with subscriptions gets this message, so a card without them shows exactly what it always did.
    subs = await ctx.db.run(lambda c: subscriptions.list_for_card(c, card.id))
    if subs:
        lines = [text.SUBSCRIPTIONS_HEADING]
        lines += [f"<#{sub.channel_id}>" + (text.SUBSCRIPTION_PAUSED_SUFFIX if sub.paused else "") for sub in subs]
        await _say(ctx, event, "\n".join(lines))
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
    for chunk in chunks(lines):
        await _say(ctx, event, chunk)
    return _Result()


def chunks(lines: list[str]) -> list[str]:
    """Joins lines into messages of under LIST_CHUNK UTF-16 units each, keeping order."""
    out: list[str] = []
    current = ""
    for line in lines:
        candidate = line if not current else current + "\n" + line
        if current and render.units(candidate) >= LIST_CHUNK:
            out.append(current)
            current = line
        else:
            current = candidate
    if current:
        out.append(current)
    return out


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
    # The role step runs after the lock, so its REST calls never hold the card's lock. It runs before the kick, so the
    # poller's first post already sees the stored role. A card that is already live gets the same step (a card published
    # before following existed has no role), but nothing is kicked for it: its posts are already up to date.
    follow = await _publish_follow(ctx, found)
    if outcome == "published":
        ctx.kick()
    if follow is not None and follow.outcome in _FOLLOWING:
        await _reseed(ctx, card_id)
    if outcome == "live":
        reply = text.ALREADY_LIVE
    else:
        # The reply says what the next check will post: the card when a games channel is set, otherwise only its active
        # copies (the poller posts those without a games channel), otherwise nothing.
        if ctx.config.games_channel_id is not None:
            reply = text.CARD_PUBLISHED
        elif await ctx.db.run(lambda c: subscriptions.list_active(c, card_id)):
            reply = text.PUBLISHED_COPIES_ONLY
        else:
            reply = text.PUBLISHED_NO_CHANNEL
    if follow is not None:
        reply += text.FOLLOW_ON if follow.outcome in _FOLLOWING else text.FOLLOW_OFF
    await _say(ctx, event, reply)
    return _Result(card_id)


async def _publish_follow(ctx: CardContext, card: cards.Card) -> follow_roles.EnsureResult | None:
    """The publish-time role step. None, with no role call and no reply text, when following is off: no games channel,
    no games community, or a context without the role dependencies."""
    deps = _follow_deps(ctx)
    if ctx.config.games_channel_id is None or ctx.games_guild is None or deps is None:
        return None
    guild = await ctx.games_guild.get()
    if guild is None:
        return None
    return await follow_roles.ensure_follow_role(deps, guild, card)


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
        # The copies go first and the card's own message last. A copy the bot is refused (403) stays where it is and is
        # named in the reply. Any other failure stops the archive while the card's message is still there.
        copies = await ctx.db.run(lambda c: subscriptions.list_for_card(c, card_id))
        deleted_copies: list[tuple[str, str]] = []
        left: list[str] = []
        for sub in copies:
            if sub.message_id is None:
                continue
            outcome = await _delete_posted(ctx.rest, sub.channel_id, sub.message_id)
            if outcome is _Delete.RETRY:
                await _stop_archive(ctx, event, deleted_copies)
                return _Result(card_id)
            if outcome is _Delete.LEFT:
                left.append(sub.channel_id)
            else:
                deleted_copies.append((sub.channel_id, sub.message_id))
        if current.channel_id and current.message_id:
            outcome = await _delete_posted(ctx.rest, current.channel_id, current.message_id)
            if outcome is _Delete.RETRY:
                await _stop_archive(ctx, event, deleted_copies)
                return _Result(card_id)
            if outcome is _Delete.LEFT:
                left.append(current.channel_id)
        now = int(ctx.clock())

        def archive_tx(conn: Any) -> None:
            cards.set_message(conn, card_id, None, None, None)
            subscriptions.clear_messages(conn, card_id)
            cards.set_state(conn, card_id, "archived", now)

        await ctx.db.run(archive_tx)
    ctx.kick()
    if left:
        reply = text.CARD_ARCHIVED_SOME_LEFT + "".join("\n" + text.ARCHIVE_LEFT_LINE.format(channel_id=channel_id)
                                                       for channel_id in left)
    else:
        reply = text.CARD_ARCHIVED
    await _say(ctx, event, reply)
    return _Result(card_id)


class _Delete(enum.Enum):
    """What came of one delete. GONE: deleted, or already a 404. LEFT: refused with a 403, so the message stays where it
    is. RETRY: any other status, or a transport error."""
    GONE = "gone"
    LEFT = "left"
    RETRY = "retry"


async def _stop_archive(ctx: CardContext, event: MessageCreate, deleted_copies: list[tuple[str, str]]) -> None:
    """Replies that the archive stopped. The card stays live and its own message stays in place, so the copies this
    archive already deleted are forgotten and the poller posts them again."""
    if deleted_copies:
        def forget(conn: Any) -> None:
            for channel_id, message_id in deleted_copies:
                subscriptions.clear_message_if(conn, channel_id, message_id)

        await ctx.db.run(forget)
        ctx.kick()
    await _say(ctx, event, text.ARCHIVE_RETRY)


async def _delete_posted(rest: FluxerREST, channel_id: str, message_id: str) -> _Delete:
    """Deletes one posted message and says what came of it. A failure is logged with the channel id and the exception
    type only."""
    try:
        await rest.delete_message(channel_id, message_id)
    except FluxerHTTPError as exc:
        if exc.status == 404:
            return _Delete.GONE
        log_event(log, "delete_failed", level=logging.WARNING, channel_id=channel_id, reason=type(exc).__name__)
        return _Delete.LEFT if exc.status == 403 else _Delete.RETRY
    except (aiohttp.ClientError, TimeoutError) as exc:
        log_event(log, "delete_failed", level=logging.WARNING, channel_id=channel_id, reason=type(exc).__name__)
        return _Delete.RETRY
    return _Delete.GONE


# subscribe, unsubscribe ------------------------------------------------------------------------------

async def _subscribe(event: MessageCreate, ctx: CardContext, rest: str) -> _Result:
    parts = rest.split(maxsplit=1)
    if len(parts) != 2:
        await _say(ctx, event, text.SUBSCRIPTION_USAGE)
        return _Result()
    slug_raw, target = parts
    try:
        guild_id, channel_id = card_fields.channel_target("channel", target)
    except CardFieldError:
        await _say(ctx, event, text.SUBSCRIPTION_USAGE)
        return _Result()
    card = await _find(ctx, slug_raw)
    if card is None:
        await _say(ctx, event, text.NO_SUCH_CARD)
        return _Result()
    if channel_id == ctx.config.games_channel_id:
        await _say(ctx, event, text.SUBSCRIPTION_IS_GAMES)
        return _Result(card.id)
    # The lock is shared with the poller and archive, so a subscription cannot change under a post or an archive.
    async with ctx.locks.get(card.id):
        reply = await _subscribe_locked(ctx, card.id, guild_id, channel_id)
    await _say(ctx, event, reply)
    return _Result(card.id)


async def _subscribe_locked(ctx: CardContext, card_id: int, guild_id: str, channel_id: str) -> str:
    """Adds or resumes one subscription, under the card lock. An active one is refused before any Fluxer request, and a
    full card too. A new or paused channel is checked with the same flow as a card's text channel slot: the bot must be
    in the community, and the channel must be a text channel of it."""
    existing = await ctx.db.run(lambda c: subscriptions.get(c, card_id, channel_id))
    if existing is not None and not existing.paused:
        return text.SUBSCRIPTION_ALREADY
    limit_reply = text.SUBSCRIPTION_LIMIT.format(limit=subscriptions.MAX_SUBSCRIPTIONS)
    if existing is None and await ctx.db.run(lambda c: subscriptions.count(c, card_id)) >= subscriptions.MAX_SUBSCRIPTIONS:
        return limit_reply
    link, reply = await _check_channel(ctx.rest, guild_id, channel_id, TEXT_CHANNEL_TYPE)
    if link is None:
        return reply
    if not link.verified:
        return text.SUBSCRIPTION_NOT_IN_COMMUNITY
    try:
        if existing is None:
            await ctx.db.run(lambda c: subscriptions.add(c, card_id, guild_id, channel_id))
            done = text.SUBSCRIBED
        else:
            await ctx.db.run(lambda c: subscriptions.resume(c, card_id, channel_id))
            done = text.SUBSCRIPTION_RESUMED
    except subscriptions.SubscriptionFull:  # defensive: the count above runs under this card's lock, so this cannot fire
        return limit_reply
    ctx.kick()
    return done


async def _unsubscribe(event: MessageCreate, ctx: CardContext, rest: str) -> _Result:
    """Deletes the copy and removes the row. A copy the bot cannot delete (any failure but a 404) still loses its row, is
    logged, and the reply says the copy is still there. Unsubscribing is final, unlike archive, which keeps the rows."""
    parts = rest.split(maxsplit=1)
    if len(parts) != 2:
        await _say(ctx, event, text.UNSUBSCRIBE_USAGE)
        return _Result()
    slug_raw, target = parts
    try:
        _, channel_id = card_fields.channel_target("channel", target)
    except CardFieldError:
        await _say(ctx, event, text.UNSUBSCRIBE_USAGE)
        return _Result()
    card = await _find(ctx, slug_raw)
    if card is None:
        await _say(ctx, event, text.NO_SUCH_CARD)
        return _Result()
    async with ctx.locks.get(card.id):
        sub = await ctx.db.run(lambda c: subscriptions.get(c, card.id, channel_id))
        if sub is None:
            reply = text.SUBSCRIPTION_NOT_FOUND
        else:
            copy_gone = (sub.message_id is None
                         or await _delete_posted(ctx.rest, channel_id, sub.message_id) is _Delete.GONE)
            await ctx.db.run(lambda c: subscriptions.remove(c, card.id, channel_id))
            reply = text.UNSUBSCRIBED if copy_gone else text.UNSUBSCRIBED_COPY_LEFT.format(channel_id=channel_id)
    await _say(ctx, event, reply)
    return _Result(card.id)


# interactions ----------------------------------------------------------------------------------------

REACTION_EMOJI = (WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI)


@dataclass(frozen=True)
class _Target:
    """One posted message that carries the bot's reactions: a card's own message, or a copy's."""
    card: cards.Card
    kind: Literal["card", "copy"]   # the surfaces that carry reactions
    channel_id: str
    message_id: str
    guild_id: str | None    # a copy's community; None for the card's own message, which is always at home


def _targets(conn: Any, listed: list[cards.Card]) -> list[_Target]:
    """The posted messages of each card and its active copies, each card before its own copies."""
    targets: list[_Target] = []
    for card in listed:
        if card.channel_id is not None and card.message_id is not None:
            targets.append(_Target(card, "card", card.channel_id, card.message_id, None))
        for sub in subscriptions.list_active(conn, card.id):
            if sub.message_id is not None:
                targets.append(_Target(card, "copy", sub.channel_id, sub.message_id, sub.guild_id))
    return targets


def _interaction_deps(ctx: CardContext) -> tuple[Seeder, GamesGuild]:
    if ctx.seeder is None or ctx.games_guild is None:
        raise RuntimeError("reactions are not wired into the command context")
    return ctx.seeder, ctx.games_guild


async def _apply_interactions(ctx: CardContext, targets: list[_Target], *, on: bool) -> None:
    """Removes the bot's reactions from each message, or adds the ones each surface should carry. Callers take the
    targets in a database read and hold no card lock here, so a slow reaction call never stalls a publish or archive."""
    seeder, games_guild = _interaction_deps(ctx)
    if not on:
        for target in targets:
            await seeder.unseed(target.channel_id, target.message_id, REACTION_EMOJI)
        return
    await _seed_targets(ctx, seeder, targets, await games_guild.get())


async def _seed_targets(ctx: CardContext, seeder: Seeder, targets: list[_Target], games_guild_id: str | None) -> None:
    """Adds the emoji each target should carry. A copy outside the games community is not home, so it gets no bell."""
    for target in targets:
        home = target.kind == "card" or target.guild_id == games_guild_id
        play_now = has_play_now_target(target.card, ctx.config, surface_kind=target.kind, home=home)
        await seeder.seed(target.channel_id, target.message_id,
                          wanted_emoji(target.card, home=home, play_now_target=play_now), card_id=target.card.id)


async def _reseed(ctx: CardContext, card_id: int) -> None:
    """Adds the reactions a live card's posted messages should carry: its own message and each active copy with one. It
    runs with no card lock held, and only adds the emoji a message lacks. Nothing is added while reactions are off."""
    if ctx.seeder is None or ctx.games_guild is None:
        return
    targets = await ctx.db.run(lambda c: _live_targets(c, card_id))
    if not targets or not await ctx.seeder.enabled_for(card_id):
        return
    await _seed_targets(ctx, ctx.seeder, targets, await ctx.games_guild.get())


def _live_targets(conn: Any, card_id: int) -> list[_Target]:
    card = cards.get_by_id(conn, card_id)
    return _targets(conn, [card] if card is not None and card.state == "live" else [])


async def _interactions(event: MessageCreate, ctx: CardContext, rest: str) -> _Result:
    # Checked before any write: the switch is stored before the reactions are touched, so missing wiring must stop here
    # rather than leave a setting changed with no reactions moved.
    _interaction_deps(ctx)
    parts = rest.split()
    if not 1 <= len(parts) <= 2 or parts[0].lower() not in ("on", "off"):
        await _say(ctx, event, text.INTERACTIONS_USAGE)
        return _Result()
    on = parts[0].lower() == "on"
    if len(parts) == 2:
        return await _interactions_card(event, ctx, parts[1], on=on)
    return await _interactions_all(event, ctx, on=on)


async def _interactions_all(event: MessageCreate, ctx: CardContext, *, on: bool) -> _Result:
    def switch(conn: Any) -> list[_Target]:
        interaction_settings.set_enabled(conn, on)
        listed = cards.list_cards(conn, state="live")
        if on:   # a card switched off on its own stays off when the global switch comes back on
            listed = [card for card in listed if not interaction_settings.card_off(conn, card.id)]
        return _targets(conn, listed)

    targets = await ctx.db.run(switch)
    await _apply_interactions(ctx, targets, on=on)
    await _say(ctx, event, text.INTERACTIONS_ON if on else text.INTERACTIONS_OFF)
    return _Result()


async def _interactions_card(event: MessageCreate, ctx: CardContext, slug: str, *, on: bool) -> _Result:
    card = await _find(ctx, slug)
    if card is None:
        await _say(ctx, event, text.NO_SUCH_CARD)
        return _Result()
    card_id = card.id

    def switch(conn: Any) -> list[_Target]:
        interaction_settings.set_card_off(conn, card_id, not on)
        current = cards.get_by_id(conn, card_id)
        # With the global switch off, a card turned on seeds nothing until the switch itself comes back on.
        if current is None or (on and not interaction_settings.enabled(conn)):
            return []
        return _targets(conn, [current])

    targets = await ctx.db.run(switch)
    await _apply_interactions(ctx, targets, on=on)
    reply = text.INTERACTIONS_CARD_ON if on else text.INTERACTIONS_CARD_OFF
    await _say(ctx, event, reply.format(slug=card.slug))
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
    "subscribe": _subscribe,
    "unsubscribe": _unsubscribe,
    "interactions": _interactions,
    "help": _help,
}
