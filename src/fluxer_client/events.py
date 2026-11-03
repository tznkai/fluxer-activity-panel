# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gateway dispatches parsed into frozen dataclasses. Unknown or malformed payloads become None."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fluxer_client.ids import is_snowflake

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Emoji:
    name: str
    id: str | None


@dataclass(frozen=True)
class Ready:
    user_id: str
    session_id: str = field(repr=False)


@dataclass(frozen=True)
class Resumed:
    pass


@dataclass(frozen=True)
class MessageCreate:
    message_id: str
    channel_id: str
    guild_id: str | None
    author_id: str
    author_is_bot: bool
    member_role_ids: frozenset[str] | None
    content: str = field(repr=False)


@dataclass(frozen=True)
class ReactionAdd:
    user_id: str
    channel_id: str
    message_id: str
    guild_id: str | None
    emoji: Emoji
    member_role_ids: frozenset[str] | None


@dataclass(frozen=True)
class ReactionRemove:
    user_id: str
    channel_id: str
    message_id: str
    guild_id: str | None
    emoji: Emoji
    member_role_ids: frozenset[str] | None


@dataclass(frozen=True)
class ReactionRemoveAll:
    channel_id: str
    message_id: str
    guild_id: str | None


@dataclass(frozen=True)
class ReactionRemoveEmoji:
    channel_id: str
    message_id: str
    guild_id: str | None
    emoji: Emoji


@dataclass(frozen=True)
class GuildRolesChanged:
    guild_id: str


@dataclass(frozen=True)
class MessageDelete:
    message_id: str
    channel_id: str
    guild_id: str | None


Event = (Ready | Resumed | MessageCreate | ReactionAdd | ReactionRemove | ReactionRemoveAll
         | ReactionRemoveEmoji | GuildRolesChanged | MessageDelete)


class _Malformed(Exception):
    def __init__(self, field_name: str) -> None:
        super().__init__(field_name)
        self.field_name = field_name


def _obj(d: Any, name: str) -> dict[str, Any]:
    if not isinstance(d, dict):
        raise _Malformed(name)
    return d


def _id(d: dict[str, Any], key: str, name: str | None = None) -> str:
    value = d.get(key)
    if not is_snowflake(value):
        raise _Malformed(name or key)
    return value


def _opt_id(d: dict[str, Any], key: str) -> str | None:
    return None if d.get(key) is None else _id(d, key)


def _roles(d: dict[str, Any]) -> frozenset[str] | None:
    member = d.get("member")
    if member is None:
        return None
    roles = _obj(member, "member").get("roles")
    if roles is None:
        return None
    if not isinstance(roles, list) or not all(is_snowflake(r) for r in roles):
        raise _Malformed("member.roles")
    return frozenset(roles)


def _emoji(d: dict[str, Any]) -> Emoji:
    e = _obj(d.get("emoji"), "emoji")
    name = e.get("name")
    if not isinstance(name, str):
        raise _Malformed("emoji.name")
    return Emoji(name=name, id=None if e.get("id") is None else _id(e, "id", "emoji.id"))


def _parse_ready(raw: Any) -> Ready:
    d = _obj(raw, "payload")
    user = _obj(d.get("user"), "user")
    session_id = d.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise _Malformed("session_id")
    return Ready(user_id=_id(user, "id", "user.id"), session_id=session_id)


def _parse_resumed(raw: Any) -> Resumed:
    return Resumed()


def _parse_message_create(raw: Any) -> MessageCreate:
    d = _obj(raw, "payload")
    author = _obj(d.get("author"), "author")
    content = d.get("content", "")
    if not isinstance(content, str):
        raise _Malformed("content")
    return MessageCreate(
        message_id=_id(d, "id"),
        channel_id=_id(d, "channel_id"),
        guild_id=_opt_id(d, "guild_id"),
        author_id=_id(author, "id", "author.id"),
        author_is_bot=author.get("bot") is True,
        member_role_ids=_roles(d),
        content=content,
    )


def _parse_reaction_add(raw: Any) -> ReactionAdd:
    d = _obj(raw, "payload")
    return ReactionAdd(
        user_id=_id(d, "user_id"),
        channel_id=_id(d, "channel_id"),
        message_id=_id(d, "message_id"),
        guild_id=_opt_id(d, "guild_id"),
        emoji=_emoji(d),
        member_role_ids=_roles(d),
    )


def _parse_reaction_remove(raw: Any) -> ReactionRemove:
    d = _obj(raw, "payload")
    return ReactionRemove(
        user_id=_id(d, "user_id"),
        channel_id=_id(d, "channel_id"),
        message_id=_id(d, "message_id"),
        guild_id=_opt_id(d, "guild_id"),
        emoji=_emoji(d),
        member_role_ids=_roles(d),
    )


def _parse_reaction_remove_all(raw: Any) -> ReactionRemoveAll:
    d = _obj(raw, "payload")
    return ReactionRemoveAll(
        channel_id=_id(d, "channel_id"),
        message_id=_id(d, "message_id"),
        guild_id=_opt_id(d, "guild_id"),
    )


def _parse_reaction_remove_emoji(raw: Any) -> ReactionRemoveEmoji:
    d = _obj(raw, "payload")
    return ReactionRemoveEmoji(
        channel_id=_id(d, "channel_id"),
        message_id=_id(d, "message_id"),
        guild_id=_opt_id(d, "guild_id"),
        emoji=_emoji(d),
    )


def _parse_message_delete(raw: Any) -> MessageDelete:
    # Only these three ids are read. Any content or author fields in the payload are never touched.
    d = _obj(raw, "payload")
    return MessageDelete(message_id=_id(d, "id"), channel_id=_id(d, "channel_id"), guild_id=_opt_id(d, "guild_id"))


def _parse_guild_roles(raw: Any) -> GuildRolesChanged:
    d = _obj(raw, "payload")
    return GuildRolesChanged(guild_id=_id(d, "guild_id"))


_PARSERS: dict[str, Callable[[Any], Event]] = {
    "READY": _parse_ready,
    "RESUMED": _parse_resumed,
    "MESSAGE_CREATE": _parse_message_create,
    "MESSAGE_DELETE": _parse_message_delete,
    "MESSAGE_REACTION_ADD": _parse_reaction_add,
    "MESSAGE_REACTION_REMOVE": _parse_reaction_remove,
    "MESSAGE_REACTION_REMOVE_ALL": _parse_reaction_remove_all,
    "MESSAGE_REACTION_REMOVE_EMOJI": _parse_reaction_remove_emoji,
}


def parse(t: str, d: Any) -> Event | None:
    """Typed event for gateway dispatch `t`, or None for unknown types and malformed payloads.

    A malformed payload is logged with the field name only, never the payload or any value.
    """
    if t.startswith("GUILD_ROLE_"):
        parser: Callable[[Any], Event] | None = _parse_guild_roles
    else:
        # MESSAGE_REACTION_ADD_MANY and other types are deliberately not parsed yet
        parser = _PARSERS.get(t)
    if parser is None:
        return None
    try:
        return parser(d)
    except _Malformed as exc:
        log.warning("malformed %s payload: field %s", t, exc.field_name)
    except (TypeError, AttributeError):
        # Defensive catch-all: the parsers check types before use, so no current payload reaches
        # this. It logs a placeholder, never the exception text, which may carry the value.
        log.warning('malformed %s payload: field "?"', t)
    return None
