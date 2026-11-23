# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DM-only card commands behind `!card`. Every reply is a fixed `text` string or a rule text, never input echoed."""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import logging
import time
from collections.abc import Callable, Container, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import aiohttp

from fluxer_client.events import MessageCreate
from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import (
    card_fields, cards, follow_roles, interaction_settings, play_now, polls, render, scope_cleanup, sessions,
    subscriptions, text)
from fluxer_panel.cards import SHOW_ITEMS, CardFieldError, ChannelLink
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.logs import log_event
from fluxer_panel.presses import FOLLOW_EMOJI, PLAY_NOW_EMOJI, POLL_EMOJI, SESSION_EMOJI, WAKE_EMOJI
from fluxer_panel.seeding import Seeder, follow_for, has_play_now_target, has_session_channel, wanted_emoji
from fluxer_panel.sources.registry import PROBES, Sources, known_manager_kinds, manager_ref_hints

if TYPE_CHECKING:
    # wake.py imports CardLocks from this module, so the wake requests are named for type checking only.
    # session_commands imports CardLocks and chunks from this module too, so it is named for type checking only.
    from fluxer_panel.interactions import Interactions
    from fluxer_panel.poll_close import PollCloser
    from fluxer_panel.session_commands import SessionCommands
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
SETTABLE = ("title", "description", "join", "first_time", "sort", "seats", "location",
            *card_fields.SLOT_FIELDS, *card_fields.SUFFIX_FIELDS, *card_fields.PUBLIC_FIELDS,
            "manager", "probe", "show", "follow_role", "play_now_channel")
# A public flag is set to no, never cleared. Clearing a slot clears its suffix and its public flag with it.
CLEARABLE = ("description", "join", "first_time", "sort", "seats", "location",
             *card_fields.SLOT_FIELDS, *card_fields.SUFFIX_FIELDS,
             "manager", "probe", "show", "follow_role", "play_now_channel")
# Each slot kind's channel type. The address kind has none: it takes host:port.
_SLOT_CHANNEL_TYPES = {"voice": VOICE_CHANNEL_TYPE, "text_channel": TEXT_CHANNEL_TYPE}
# Settable fields that are stored as base columns, and the column each one writes.
BASE_COLUMNS = {"title": "title", "description": "description", "join": "how_to_join", "first_time": "first_time",
                "sort": "sort_order", "show": "show_details"}
# Clearable fields stored as base columns: the column and the value a clear writes.
BASE_CLEARS = {"description": ("description", None), "join": ("how_to_join", None),
               "first_time": ("first_time", None), "sort": ("sort_order", 0), "show": ("show_details", ())}
SLOT_CLEARS = frozenset({"location", "seats", "manager", "probe"})
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
    follow_locks: follow_roles.FollowLocks = field(default_factory=follow_roles.FollowLocks)
    roles: RoleCache | None = None
    games_guild: GamesGuild | None = None
    seeder: Seeder | None = None
    identity: BotIdentity | None = None
    wake: WakeRequests | None = None
    session_commands: SessionCommands | None = None
    poll_closer: PollCloser | None = None     # `!card archive` closes the card's open poll through it
    play_now_ender: Interactions | None = None   # ends a subscription's play-now posts (scope_cleanup)


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
    if field_name in card_fields.RENAMED_FIELDS:
        await _say_renamed(ctx, event, field_name)
        return _Result(card.id, logged)
    if field_name not in SETTABLE:
        await _say(ctx, event, text.UNKNOWN_FIELD)
        return _Result(card.id, logged)
    try:
        write, reply = await _prepare_set(ctx, card, field_name, value)
    except CardFieldError as err:
        await _refuse(ctx, event, err)
        return _Result(card.id, logged)
    if write is not None:
        try:
            await ctx.db.run(write)
        except CardFieldError as err:
            await _refuse(ctx, event, err)
            return _Result(card.id, logged)
        ctx.kick()
    await _say(ctx, event, reply)
    if field_name in RESEED_FIELDS:
        await _reseed(ctx, card.id)
    return _Result(card.id, logged)


def _or_refuse(write: Callable[[Any], bool], field_name: str, rule: str) -> Callable[[Any], None]:
    """A write that reports whether it found its row. A miss, from a clear that landed after the card was read, raises
    the refusal, so the reply never says updated for a write that stored nothing."""
    def run(conn: Any) -> None:
        if not write(conn):
            raise CardFieldError(field_name, rule)
    return run


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
    if field_name in card_fields.SLOT_FIELDS:
        return await _set_slot(ctx, card, field_name, value)
    if field_name in card_fields.SUFFIX_FIELDS:
        kind, slot = card_fields.SUFFIX_FIELDS[field_name]
        suffix = card_fields.suffix(field_name, value)
        if _slot_value(card, kind, slot) is None:
            raise CardFieldError(field_name, "no_slot")
        # The store writes a suffix only on a slot that still has a value, so a clear that lands first leaves no suffix
        # and the reply says so.
        return _or_refuse(lambda c: cards.set_suffix(c, card_id, kind, slot, suffix), field_name, "no_slot"), \
            text.CARD_UPDATED
    if field_name in card_fields.PUBLIC_FIELDS:
        slot = card_fields.PUBLIC_FIELDS[field_name]
        public = card_fields.yes_no(field_name, value)
        if card.addresses[slot - 1] is None:
            raise CardFieldError(field_name, "no_address")
        return _or_refuse(lambda c: cards.set_address_public(c, card_id, public, slot=slot), field_name,
                          "no_address"), text.CARD_UPDATED
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
    return await _set_play_now_channel(ctx, card_id, value)


async def _set_slot(ctx: CardContext, card: cards.Card, field_name: str,
                    value: str) -> tuple[Callable[[Any], None] | None, str]:
    """A voice, text channel or address slot. A slot that already has a value keeps its suffix, and an address its
    public flag; only `!card clear` of the slot drops them. A refusal names the slot's own field."""
    kind, slot = card_fields.SLOT_FIELDS[field_name]
    card_id = card.id
    existing = _slot_value(card, kind, slot)
    suffix = existing.suffix if existing is not None else None
    if kind == "address":
        host, port = card_fields.address(value, field_name)
        public = existing.public if isinstance(existing, cards.Address) else False
        address = cards.Address(host, port, public, suffix)
        return (lambda c: cards.set_address(c, card_id, address, slot=slot)), text.CARD_UPDATED
    guild_id, channel_id = card_fields.channel_target(field_name, value)
    checked, reply = await _check_channel(ctx.rest, guild_id, channel_id, _SLOT_CHANNEL_TYPES[kind])
    if checked is None:
        return None, reply
    link = dataclasses.replace(checked, suffix=suffix)
    if kind == "voice":
        return (lambda c: cards.set_voice(c, card_id, link, slot=slot)), reply
    return (lambda c: cards.set_text_channel(c, card_id, slot, link)), reply


def _slot_value(card: cards.Card, kind: str, slot: int) -> ChannelLink | cards.Address | None:
    slots = {"voice": card.voices, "text_channel": card.text_channels, "address": card.addresses}[kind]
    return slots[slot - 1]


async def _say_renamed(ctx: CardContext, event: MessageCreate, field_name: str) -> None:
    await _say(ctx, event, text.FIELD_RENAMED.format(old=field_name, new=card_fields.RENAMED_FIELDS[field_name]))


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
    return follow_roles.FollowDeps(db=ctx.db, rest=ctx.rest, config=ctx.config, roles=ctx.roles,
                                   identity=ctx.identity, locks=ctx.follow_locks)


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
    if field_name in card_fields.RENAMED_FIELDS:
        await _say_renamed(ctx, event, field_name)
        return _Result(card.id, logged)
    if field_name not in CLEARABLE:
        await _say(ctx, event, text.UNKNOWN_FIELD)
        return _Result(card.id, logged)
    now = int(ctx.clock())
    card_id = card.id
    if field_name == "follow_role":
        # The bell already on the card stays. A press on it then gets "Following isn't available", which is correct.
        # The clear takes the card's role lock, so it can't land in the middle of a publish's role step.
        async with ctx.follow_locks.get(card_id):
            old = await ctx.db.run(lambda c: cards.clear_follow_role(c, card_id))
        if old is None:
            await _say(ctx, event, text.FOLLOW_ROLE_NONE.format(slug=card.slug))
        else:
            await _say(ctx, event, text.FOLLOW_ROLE_CLEARED.format(slug=card.slug, role=f"<@&{old.role_id}>"))
        return _Result(card_id, logged)
    if field_name in card_fields.SLOT_FIELDS:
        kind, slot = card_fields.SLOT_FIELDS[field_name]
        clear = {"voice": cards.clear_voice, "text_channel": cards.clear_text_channel, "address": cards.clear_address}
        await ctx.db.run(lambda c: clear[kind](c, card_id, slot))   # the slot goes with its suffix and public flag
    elif field_name in card_fields.SUFFIX_FIELDS:
        kind, slot = card_fields.SUFFIX_FIELDS[field_name]
        await ctx.db.run(lambda c: cards.set_suffix(c, card_id, kind, slot, None))
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
    # The embed preview shows the next session as a card does, with its link. The outside preview shows it without one,
    # the way a far copy does, and never the location text.
    near, far = await ctx.db.run(lambda c: sessions.next_session_views(c, card, int(time.time())))
    embed = render.render_embed(card, None, None, instance_url=ctx.config.instance_url, next_session=near)
    await ctx.rest.send_message(event.channel_id, content=text.SHOW_NOTE, embeds=[embed],
                                allowed_mentions={"parse": []})
    # The outside text is plain text everywhere else. In this DM it is escaped, so it shows as typed, and then
    # fitted so the label, newline and body stay within the message limit.
    label = text.SHOW_OUTSIDE_LABEL
    body = render.fit(render.escape(render.outside_text(card, None, None, far)),
                      PREVIEW_LIMIT - render.units(label) - 1)
    await _say(ctx, event, label + "\n" + body)
    # Only a card with subscriptions gets this message, so a card without them shows exactly what it always did.
    subs, roles = await ctx.db.run(lambda c: _subscriptions_with_roles(c, card.id))
    if subs:
        lines = [text.SUBSCRIPTIONS_HEADING] + [_subscription_line(sub, roles.get(sub.id)) for sub in subs]
        for chunk in chunks(lines):   # a card with many copies can pass one message's limit
            await _say(ctx, event, chunk)
    return _Result(card.id)


def _subscriptions_with_roles(conn: Any, card_id: int) -> tuple[list[subscriptions.Subscription],
                                                              dict[int, cards.FollowRole | None]]:
    subs = subscriptions.list_for_card(conn, card_id)
    return subs, {sub.id: cards.get_subscription_role(conn, sub.id) for sub in subs}


def _subscription_line(sub: subscriptions.Subscription, role: cards.FollowRole | None) -> str:
    """One channel of `!card show`: its mode, and for a local or both copy the channel it posts in and its role."""
    line = f"<#{sub.channel_id}>" + (text.SUBSCRIPTION_PAUSED_SUFFIX if sub.paused else "")
    line += text.SUBSCRIPTION_LINE_MODE.format(mode=sub.mode)
    if sub.is_scope:
        role_text = (text.SUBSCRIPTION_LINE_ROLE.format(role_id=role.role_id) if role is not None
                     else text.SUBSCRIPTION_LINE_NO_ROLE)
        line += text.SUBSCRIPTION_LINE_SCOPE.format(channel_id=sub.local_channel_id or sub.channel_id, role=role_text)
    return line


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
        # The sessions go first, while the lock is held: the cancel edits their messages and DMs their attendees.
        # If a later step stops the archive (`_stop_archive`), the sessions stay cancelled while the card stays live.
        # That is accepted, because an admin who meant to archive does not want those sessions either.
        if ctx.session_commands is not None:
            await ctx.session_commands.cancel_card_sessions_locked(card_id, event.author_id)
        # The open poll closes with no winner, under the same lock, before the messages are deleted.
        await _close_open_poll_locked(ctx, card_id)
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


async def _close_open_poll_locked(ctx: CardContext, card_id: int) -> None:
    """Closes the card's open poll with no winner. The caller holds the card lock. A failure is logged and the archive
    goes on: the card's messages are still deleted, and the poll stays open only if the close itself failed."""
    if ctx.poll_closer is None:
        return
    poll = await ctx.db.run(lambda c: polls.open_for_card(c, card_id))
    if poll is None:
        return
    try:
        await ctx.poll_closer.close_locked(poll.id, outcome_override="none")
    except Exception as exc:  # noqa: BLE001 - a poll that cannot close must not stop the archive
        log_event(log, "poll_archive_failed", level=logging.WARNING, card_id=card_id, reason=type(exc).__name__)


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
    tokens = rest.split()
    parsed = card_fields.subscription_options(tokens[1:]) if tokens else None
    if parsed is None:
        await _say(ctx, event, text.SUBSCRIPTION_USAGE)
        return _Result()
    target_tokens, opts = parsed
    try:
        guild_id, channel_id = card_fields.channel_target("channel", " ".join(target_tokens))
    except CardFieldError:
        await _say(ctx, event, text.SUBSCRIPTION_USAGE)
        return _Result()
    card = await _find(ctx, tokens[0])
    if card is None:
        await _say(ctx, event, text.NO_SUCH_CARD)
        return _Result()
    if channel_id == ctx.config.games_channel_id:
        await _say(ctx, event, text.SUBSCRIPTION_IS_GAMES)
        return _Result(card.id)
    # A change that leaves a scope needs the reactor counts of the scope's live play-now posts, read before the lock.
    before = await ctx.db.run(lambda c: subscriptions.get(c, card.id, channel_id))
    counts = await _scope_end_counts(ctx, card.id, before) if opts.mode == "redirect" else {}
    # The lock is shared with the poller and archive, so a subscription cannot change under a post or an archive.
    async with ctx.locks.get(card.id):
        reply, follow = await _subscribe_locked(ctx, card, event.author_id, guild_id, channel_id, opts, counts)
    lines = [reply] if reply else []
    if follow is not None:
        lines += await _finish_change(ctx, card, follow)
    await _say(ctx, event, "\n".join(lines))
    return _Result(card.id)


@dataclass(frozen=True)
class _ModeChange:
    """What a mode change left to do after the card lock: the ended scope, the copy message whose calendar and ballot
    box reactions come off, and the channels where a mirror could not be deleted."""
    ended: scope_cleanup.ScopeEnded | None = None
    unseed: tuple[str, str] | None = None
    mirrors_left: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Followup:
    """What runs after the card lock is released for a subscribe that wrote a row."""
    card_id: int
    sub_id: int
    guild_id: str
    role_id: str | None
    role_step: bool
    reseed: bool
    change: _ModeChange


def _has_options(opts: card_fields.SubOptions) -> bool:
    return opts.mode is not None or opts.channel is not None or opts.role is not None


def _is_scoped(mode: str) -> bool:
    return mode in subscriptions.LOCAL_MODES


async def _in_games_community(ctx: CardContext, guild_id: str) -> bool | None:
    """Whether a copy in `guild_id` is in the games community: True, False, or None when a games channel is set but its
    community can't be read (no lookup, or the lookup failed), so it is unknown. With no games channel, no copy is."""
    if ctx.config.games_channel_id is None:
        return False
    games_guild_id = await ctx.games_guild.get() if ctx.games_guild is not None else None
    if games_guild_id is None:
        return None
    return games_guild_id == guild_id


async def _scope_end_counts(ctx: CardContext, card_id: int, sub: subscriptions.Subscription | None) -> dict[int, int]:
    """The reactor counts of a scope's live play-now posts, read before the card lock. Empty when there is no scope."""
    if sub is None or not _is_scoped(sub.mode) or ctx.play_now_ender is None:
        return {}
    rows = await ctx.db.run(lambda c: play_now.live_for_scope(c, card_id, sub.id))
    return await ctx.play_now_ender.count_play_now(rows) if rows else {}


async def _local_channel(ctx: CardContext, guild_id: str, value: str) -> tuple[str | None, str]:
    """The local channel a `channel=` value names. It must be a verified text channel of the copy's community and not
    the games channel. Returns the channel id to store (None when refused) and the reply. Nothing is written here."""
    try:
        link_guild, channel_id = card_fields.local_channel_value(value)
    except CardFieldError:
        return None, text.SUBSCRIPTION_USAGE
    if link_guild is not None and link_guild != guild_id:
        return None, text.CHANNEL_OTHER_COMMUNITY
    if channel_id == ctx.config.games_channel_id:
        return None, text.SUBSCRIPTION_CHANNEL_IS_GAMES
    link, reply = await _check_channel(ctx.rest, guild_id, channel_id, TEXT_CHANNEL_TYPE)
    if link is None or not link.verified:
        return None, reply
    return channel_id, reply


async def _subscribe_locked(ctx: CardContext, card: cards.Card, actor: str, guild_id: str, channel_id: str,
                            opts: card_fields.SubOptions, counts: Mapping[int, int]
                            ) -> tuple[str, _Followup | None]:
    """Adds or resumes one subscription and applies its options, under the card lock. Every refusal is decided before
    the first write, so a refused command changes nothing. An active one with no options is refused before any Fluxer
    request, and a full card too. A new or paused channel is checked with the same flow as a card's text channel slot:
    the bot must be in the community, and the channel must be a text channel of it. Returns the reply and, when a row
    was written, what runs after the lock."""
    existing = await ctx.db.run(lambda c: subscriptions.get(c, card.id, channel_id))
    if existing is not None and not existing.paused and not _has_options(opts):
        return text.SUBSCRIPTION_ALREADY, None
    current_mode = existing.mode if existing is not None else "redirect"
    mode = opts.mode or current_mode
    if _is_scoped(mode):
        in_games = await _in_games_community(ctx, guild_id)
        if in_games is None:
            return text.SUBSCRIPTION_GAMES_LOOKUP_FAILED, None
        if in_games:
            return text.SUBSCRIPTION_MODE_IN_GAMES, None
    if mode == "redirect" and (opts.channel is not None or opts.role is not None):
        return text.SUBSCRIPTION_NEEDS_SCOPE, None
    local_channel = None
    if opts.channel is not None:
        local_channel, refusal = await _local_channel(ctx, guild_id, opts.channel)
        if local_channel is None:
            return refusal, None
    limit_reply = text.SUBSCRIPTION_LIMIT.format(limit=subscriptions.MAX_SUBSCRIPTIONS)
    count = await ctx.db.run(lambda c: subscriptions.count(c, card.id)) if existing is None else 0
    if existing is None and count >= subscriptions.MAX_SUBSCRIPTIONS:
        return limit_reply, None
    if existing is None or existing.paused:
        link, reply = await _check_channel(ctx.rest, guild_id, channel_id, TEXT_CHANNEL_TYPE)
        if link is None:
            return reply, None
        if not link.verified:
            return text.SUBSCRIPTION_NOT_IN_COMMUNITY, None
    lines: list[str] = []
    try:
        if existing is None:
            sub_id = await ctx.db.run(lambda c: subscriptions.add(c, card.id, guild_id, channel_id))
            lines.append(text.SUBSCRIBED)
        else:
            sub_id = existing.id
            if existing.paused:
                await ctx.db.run(lambda c: subscriptions.resume(c, card.id, channel_id))
                lines.append(text.SUBSCRIPTION_RESUMED)
    except subscriptions.SubscriptionFull:  # defensive: the count above runs under the card's lock, so: cannot fire
        return limit_reply, None
    change: _ModeChange | None = None
    if opts.mode is not None and opts.mode != current_mode:
        sub = await ctx.db.run(lambda c: subscriptions.get_by_id(c, sub_id))
        assert sub is not None
        change = await _change_mode_locked(ctx, card, sub, opts.mode, actor, counts=counts)
        if change is None:
            return text.SUBSCRIPTION_RETRY, None
        lines.append(text.SUBSCRIPTION_MODE_SET.format(channel_id=channel_id, mode=opts.mode))
    if local_channel is not None:
        await ctx.db.run(lambda c: subscriptions.set_local_channel(c, sub_id, local_channel))
        lines.append(text.SUBSCRIPTION_LOCAL_CHANNEL_SET.format(channel_id=local_channel))
    final = await ctx.db.run(lambda c: subscriptions.get_by_id(c, sub_id))
    assert final is not None
    stored_role = await ctx.db.run(lambda c: cards.get_subscription_role(c, sub_id))
    # The role step runs after the lock when the copy just left redirect, when a role was given, or when none is stored.
    role_step = _is_scoped(final.mode) and (
        (change is not None and current_mode == "redirect") or opts.role is not None or stored_role is None)
    if not lines and not role_step:
        return text.SUBSCRIPTION_ALREADY, None
    ctx.kick()
    follow = _Followup(card_id=card.id, sub_id=sub_id, guild_id=guild_id, role_id=opts.role, role_step=role_step,
                       reseed=role_step or change is not None, change=change or _ModeChange())
    return "\n".join(lines), follow


async def _change_mode_locked(ctx: CardContext, card: cards.Card, sub: subscriptions.Subscription, new_mode: str,
                              actor: str, *, counts: Mapping[int, int]) -> _ModeChange | None:
    """Moves a subscription to `new_mode`. The caller holds the card lock. Leaving a scope ends it (its sessions, poll,
    play-now posts and mirrors) and forgets its local channel; the copy's calendar and ballot box come off after the
    lock. Both to local deletes the mirrors only. Returns None when the row changed under the caller."""
    old_mode = sub.mode
    ended: scope_cleanup.ScopeEnded | None = None
    mirrors_left: tuple[str, ...] = ()
    if new_mode == "redirect" and _is_scoped(old_mode):
        ended = await scope_cleanup.end_scope_locked(ctx, card.id, sub.id, actor, counts=counts)
        mirrors_left = ended.mirrors_left
    elif old_mode == "both" and new_mode == "local":
        mirrors_left = await scope_cleanup.delete_mirrors_locked(ctx, sub.id)
    if not await ctx.db.run(lambda c: subscriptions.set_mode_if(c, sub.id, old_mode, new_mode)):
        return None
    if new_mode == "redirect":
        await ctx.db.run(lambda c: subscriptions.set_local_channel(c, sub.id, None))
    unseed = (sub.channel_id, sub.message_id) if ended is not None and sub.message_id is not None else None
    return _ModeChange(ended=ended, unseed=unseed, mirrors_left=mirrors_left)


async def _role_step_for_sub(ctx: CardContext, card: cards.Card, guild_id: str, sub_id: int,
                             role_id: str | None) -> str:
    """The follow role of one subscription, in its own community. Runs after the card lock. A refusal keeps the mode."""
    deps = _follow_deps(ctx)
    if deps is None:
        return text.SUBSCRIPTION_ROLE_UNAVAILABLE
    res = await follow_roles.ensure_follow_role(deps, guild_id, card, role_id=role_id, sub_id=sub_id)
    if res.outcome in (follow_roles.Ensured.REUSED, follow_roles.Ensured.ADOPTED, follow_roles.Ensured.CREATED,
                       follow_roles.Ensured.SET):
        return text.SUBSCRIPTION_ROLE_OK.format(role_id=res.role_id)
    if res.reason == "missing_subscription":
        return text.SUBSCRIPTION_ROLE_GONE
    if res.reason in ("taken", "duplicate_name"):
        return text.SUBSCRIPTION_ROLE_ASK
    reasons = text.SUBSCRIPTION_ROLE_REASON
    reason = reasons.get(res.reason or "lookup_failed", reasons["lookup_failed"])
    return text.SUBSCRIPTION_ROLE_FAILED.format(reason=reason)


def _mirrors_left_line(channels: tuple[str, ...]) -> str:
    return text.SUBSCRIPTION_MIRRORS_LEFT.format(channels=", ".join(f"<#{c}>" for c in dict.fromkeys(channels)))


async def _finish_change(ctx: CardContext, card: cards.Card, follow: _Followup) -> list[str]:
    """What runs after the card lock is released: the play-now reactions of an ended scope, the calendar and ballot box
    of a copy that left its scope, the role step, the copy's reactions, and the names of any mirror left behind."""
    if follow.change.ended is not None:
        await scope_cleanup.end_scope_after_lock(ctx, follow.change.ended)
    if follow.change.unseed is not None and ctx.seeder is not None:
        channel_id, message_id = follow.change.unseed
        await ctx.seeder.unseed(channel_id, message_id, (SESSION_EMOJI, POLL_EMOJI))
    lines: list[str] = []
    if follow.role_step:
        lines.append(await _role_step_for_sub(ctx, card, follow.guild_id, follow.sub_id, follow.role_id))
    if follow.reseed:
        await _reseed(ctx, follow.card_id)
    if follow.change.mirrors_left:
        lines.append(_mirrors_left_line(follow.change.mirrors_left))
    return lines


async def _unsubscribe(event: MessageCreate, ctx: CardContext, rest: str) -> _Result:
    """Deletes the copy and removes the row. A scope the copy owns ends first (its sessions cancelled with DMs, its poll
    closed, its play-now posts expired, its mirrors deleted), and then its history is deleted with the row, whatever the
    mode now is. A copy the bot cannot delete (any failure but a 404) still loses its row, is logged, and the reply says
    the copy is still there. Unsubscribing is final, unlike archive, which keeps the rows."""
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
    before = await ctx.db.run(lambda c: subscriptions.get(c, card.id, channel_id))
    counts = await _scope_end_counts(ctx, card.id, before)
    ended: scope_cleanup.ScopeEnded | None = None
    async with ctx.locks.get(card.id):
        sub = await ctx.db.run(lambda c: subscriptions.get(c, card.id, channel_id))
        if sub is None:
            reply = text.SUBSCRIPTION_NOT_FOUND
        else:
            if _is_scoped(sub.mode):
                ended = await scope_cleanup.end_scope_locked(ctx, card.id, sub.id, event.author_id, counts=counts)
            copy_gone = (sub.message_id is None
                         or await _delete_posted(ctx.rest, channel_id, sub.message_id) is _Delete.GONE)
            if await ctx.db.run(lambda c: scope_cleanup.has_scope_rows(c, sub.id)):
                await scope_cleanup.delete_scope_rows_locked(ctx, sub.id)
            await ctx.db.run(lambda c: subscriptions.remove_by_id(c, sub.id))
            reply = text.UNSUBSCRIBED if copy_gone else text.UNSUBSCRIBED_COPY_LEFT.format(channel_id=channel_id)
    if ended is not None:
        await scope_cleanup.end_scope_after_lock(ctx, ended)
        if ended.mirrors_left:
            reply += "\n" + _mirrors_left_line(ended.mirrors_left)
    await _say(ctx, event, reply)
    return _Result(card.id)


# interactions ----------------------------------------------------------------------------------------

REACTION_EMOJI = (WAKE_EMOJI, FOLLOW_EMOJI, PLAY_NOW_EMOJI, SESSION_EMOJI, POLL_EMOJI)


@dataclass(frozen=True)
class _Target:
    """One posted message that carries the bot's reactions: a card's own message, or a copy's."""
    card: cards.Card
    kind: Literal["card", "copy"]   # the surfaces that carry reactions
    channel_id: str
    message_id: str
    guild_id: str | None    # a copy's community; None for the card's own message, which is always at home
    sub: subscriptions.Subscription | None = None   # a copy's subscription; None for the card's own message
    has_role: bool = False                          # whether that subscription has a stored follow role


def _targets(conn: Any, listed: list[cards.Card]) -> list[_Target]:
    """The posted messages of each card and its active copies, each card before its own copies."""
    targets: list[_Target] = []
    for card in listed:
        if card.channel_id is not None and card.message_id is not None:
            targets.append(_Target(card, "card", card.channel_id, card.message_id, None))
        for sub in subscriptions.list_active(conn, card.id):
            if sub.message_id is not None:
                has_role = cards.get_subscription_role(conn, sub.id) is not None
                targets.append(_Target(card, "copy", sub.channel_id, sub.message_id, sub.guild_id, sub, has_role))
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
    """Adds the emoji each target should carry. Being home decides only a copy's play-now target. A local or both copy
    with no stored role loses a bell its card's rule seeded before the mode changed (AC2.8), so its bell is removed
    first. Callers hold no card lock, and the removal is a command-path action only: the poller never removes."""
    for target in targets:
        home = target.kind == "card" or target.guild_id == games_guild_id
        play_now = has_play_now_target(target.card, ctx.config, surface_kind=target.kind, home=home, sub=target.sub)
        session = has_session_channel(target.card, ctx.config, surface_kind=target.kind, home=home, sub=target.sub)
        follow = follow_for(target.sub, has_role=target.has_role)
        if follow is False and target.card.follow_role is not None:
            await seeder.unseed(target.channel_id, target.message_id, (FOLLOW_EMOJI,))
        await seeder.seed(target.channel_id, target.message_id,
                          wanted_emoji(target.card, play_now_target=play_now, session_target=session,
                                       poll_target=session, follow=follow),
                          card_id=target.card.id)


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
        location=card_fields.TEXT_LIMITS["location"], suffix_max=card_fields.SUFFIX_MAX,
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
