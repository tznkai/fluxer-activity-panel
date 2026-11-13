# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fluxer REST client."""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final, NamedTuple
from urllib.parse import quote

import aiohttp

from fluxer_client.ids import require_snowflake

log = logging.getLogger(__name__)

RETRY_AFTER_MIN = 0.001
RETRY_AFTER_MAX = 60.0
RETRY_AFTER_DEFAULT = 1.0
_FILENAME = re.compile(r"[A-Za-z0-9._-]{1,64}")


class Unset(enum.Enum):
    UNSET = enum.auto()

    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final = Unset.UNSET


def _finite_or_none(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


EMOJI_MAX = 64
REASON_MAX = 128
_CUSTOM_EMOJI = re.compile(r"[A-Za-z0-9_]{1,32}:[0-9]{1,25}")
_EMOJI_FORBIDDEN = ("/", "?", "#", "..", "%", "\\")


def encode_emoji(emoji: str) -> str:
    """Path segment for a reaction emoji: `name:id` for custom emoji, else the percent-encoded unicode."""
    if not isinstance(emoji, str) or not emoji or emoji.isspace() or len(emoji) > EMOJI_MAX:
        raise ValueError("emoji must be 1-64 non-blank characters")
    if any(bad in emoji for bad in _EMOJI_FORBIDDEN) or any(c.isspace() or ord(c) < 0x20 for c in emoji):
        raise ValueError("emoji contains a forbidden character")
    if ":" in emoji:
        if not _CUSTOM_EMOJI.fullmatch(emoji):
            raise ValueError("custom emoji must be name:id")
        return emoji
    return quote(emoji, safe="")


def _check_reason(reason: str | None) -> dict[str, str]:
    """Audit-log header for a fixed, caller-supplied reason; never pass user text."""
    if reason is None:
        return {}
    if not reason or len(reason) > REASON_MAX or not all(0x20 <= ord(c) <= 0x7E for c in reason):
        raise ValueError("audit reason must be 1-128 printable ASCII characters")
    return {"X-Audit-Log-Reason": reason}


class FluxerHTTPError(Exception):
    def __init__(self, status: int, code: str | None, message: str) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


async def discover_api(session: aiohttp.ClientSession, instance_url: str) -> str:
    async with session.get(instance_url.rstrip("/") + "/.well-known/fluxer") as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    return data["endpoints"]["api_public"]


class FluxerREST:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_base: str,
        token: str,
        *,
        max_retries: int = 5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.session = session
        self.api_base = api_base.rstrip("/")
        self._headers = {"Authorization": f"Bot {token}"}
        self.max_retries = max_retries
        self._sleep = sleep

    def _url(self, path: str) -> str:
        return f"{self.api_base}/v1{path}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        form: Callable[[], aiohttp.FormData] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        merged = {**self._headers, **(headers or {})}
        for attempt in range(self.max_retries + 1):
            kwargs: dict[str, Any] = {}
            if form is not None:
                kwargs["data"] = form()  # FormData is single-use: rebuild per attempt
            elif json_body is not None:
                kwargs["json"] = json_body
            async with self.session.request(
                method, self._url(path), headers=merged, **kwargs
            ) as resp:
                if resp.status == 429 and attempt < self.max_retries:
                    delay = await self._retry_after(resp)
                    log.warning("rate limited on %s %s; retrying in %.3fs", method, path, delay)
                    await self._sleep(delay)
                    continue
                if resp.status >= 400:
                    try:
                        body = await resp.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError):
                        body = {}
                    if not isinstance(body, dict):
                        body = {}
                    raise FluxerHTTPError(
                        resp.status,
                        body.get("code"),
                        body.get("message", resp.reason or ""),
                    )
                if resp.status == 204:
                    return None
                return await resp.json(content_type=None)
        raise AssertionError("unreachable")

    @staticmethod
    async def _retry_after(resp: aiohttp.ClientResponse) -> float:
        delay: float | None
        try:
            body = await resp.json(content_type=None)
            delay = _finite_or_none(body["retry_after"])
        except (ValueError, KeyError, TypeError, aiohttp.ContentTypeError):
            delay = None
        if delay is None:
            delay = _finite_or_none(resp.headers.get("Retry-After"))
        if delay is None:
            delay = RETRY_AFTER_DEFAULT
        return min(max(delay, RETRY_AFTER_MIN), RETRY_AFTER_MAX)

    async def get_gateway_url(self) -> str:
        return (await self.request("GET", "/gateway/bot"))["url"]

    async def send_message(
        self,
        channel_id: str,
        *,
        content: str | None = None,
        embeds: list[dict[str, Any]] | None = None,
        reply_to: str | None = None,
        file: tuple[str, bytes, str] | None = None,
        allowed_mentions: Mapping[str, Any] | None = None,
    ) -> Any:
        """POST a message. `file` is (filename, data, content_type) and is sent as multipart files[0]."""
        path = f"/channels/{require_snowflake(channel_id)}/messages"
        payload: dict[str, Any] = {}
        if content is not None:
            payload["content"] = content
        if embeds is not None:
            payload["embeds"] = embeds
        if reply_to is not None:
            # the reference lives in the JSON body only, never in the URL
            payload["message_reference"] = {"message_id": require_snowflake(reply_to)}
        if allowed_mentions is not None:
            payload["allowed_mentions"] = dict(allowed_mentions)
        if not payload.keys() - {"message_reference", "allowed_mentions"} and file is None:
            raise ValueError("send_message needs content, embeds or a file")
        if file is None:
            return await self.request("POST", path, json_body=payload)
        filename, data, content_type = file
        if not _FILENAME.fullmatch(filename):
            raise ValueError("attachment filename must match [A-Za-z0-9._-]{1,64}")

        def form() -> aiohttp.FormData:
            fd = aiohttp.FormData()
            fd.add_field("payload_json", json.dumps(payload), content_type="application/json")
            fd.add_field("files[0]", data, filename=filename, content_type=content_type)
            return fd

        return await self.request("POST", path, form=form)

    async def get_roles(self, guild_id: str) -> list[dict[str, Any]]:
        return await self.request("GET", f"/guilds/{require_snowflake(guild_id)}/roles")

    async def get_member(self, guild_id: str, user_id: str) -> dict[str, Any]:
        path = f"/guilds/{require_snowflake(guild_id)}/members/{require_snowflake(user_id)}"
        return await self.request("GET", path)

    async def list_members(self, guild_id: str, *, limit: int = 1000,
                           after: str | None = None) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1-1000")
        path = f"/guilds/{require_snowflake(guild_id)}/members?limit={limit}"
        if after is not None:
            path += f"&after={require_snowflake(after)}"
        result = await self.request("GET", path)
        return [m for m in result if isinstance(m, dict)] if isinstance(result, list) else []

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        *,
        content: str | None | Unset = UNSET,
        embeds: list[dict[str, Any]] | Unset = UNSET,
        allowed_mentions: Mapping[str, Any] | Unset = UNSET,
    ) -> Any:
        path = f"/channels/{require_snowflake(channel_id)}/messages/{require_snowflake(message_id)}"
        body: dict[str, Any] = {}
        if content is not UNSET:
            body["content"] = content  # None is sent as JSON null
        if embeds is not UNSET:
            body["embeds"] = embeds  # replaces the whole list
        if allowed_mentions is not UNSET:
            body["allowed_mentions"] = dict(allowed_mentions)
        if not body.keys() - {"allowed_mentions"}:
            raise ValueError("edit_message needs content or embeds")
        return await self.request("PATCH", path, json_body=body)

    def _reaction_path(self, channel_id: str, message_id: str, emoji: str, who: str) -> str:
        return (f"/channels/{require_snowflake(channel_id)}/messages/{require_snowflake(message_id)}"
                f"/reactions/{encode_emoji(emoji)}/{who}")

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        await self.request("PUT", self._reaction_path(channel_id, message_id, emoji, "@me"))

    async def remove_reaction(self, channel_id: str, message_id: str, emoji: str, user_id: str) -> None:
        path = self._reaction_path(channel_id, message_id, emoji, require_snowflake(user_id))
        await self.request("DELETE", path)

    def _role_path(self, guild_id: str, user_id: str, role_id: str) -> str:
        return (f"/guilds/{require_snowflake(guild_id)}/members/{require_snowflake(user_id)}"
                f"/roles/{require_snowflake(role_id)}")

    async def add_member_role(self, guild_id: str, user_id: str, role_id: str, *, reason: str | None = None) -> None:
        path, headers = self._role_path(guild_id, user_id, role_id), _check_reason(reason)
        await self.request("PUT", path, headers=headers)

    async def remove_member_role(self, guild_id: str, user_id: str, role_id: str, *,
                                 reason: str | None = None) -> None:
        path, headers = self._role_path(guild_id, user_id, role_id), _check_reason(reason)
        await self.request("DELETE", path, headers=headers)

    async def open_dm(self, user_id: str) -> str:
        data = await self.request("POST", "/users/@me/channels",
                                  json_body={"recipient_id": require_snowflake(user_id)})
        channel_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(channel_id, str):
            raise ValueError("DM channel response has no string id")
        return require_snowflake(channel_id)

    @staticmethod
    def _object(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise FluxerHTTPError(200, None, "unexpected response shape")
        return data

    async def get_channel(self, channel_id: str) -> dict[str, Any]:
        return self._object(await self.request("GET", f"/channels/{require_snowflake(channel_id)}"))

    async def get_guild(self, guild_id: str) -> dict[str, Any]:
        return self._object(await self.request("GET", f"/guilds/{require_snowflake(guild_id)}"))

    async def get_message(self, channel_id: str, message_id: str) -> dict[str, Any]:
        path = f"/channels/{require_snowflake(channel_id)}/messages/{require_snowflake(message_id)}"
        return self._object(await self.request("GET", path))

    async def delete_message(self, channel_id: str, message_id: str) -> None:
        path = f"/channels/{require_snowflake(channel_id)}/messages/{require_snowflake(message_id)}"
        await self.request("DELETE", path)

    REACTION_USERS_SUFFIX = "/users"   # the reactor-list path, confirmed by the live run (docs/fluxer-notes.md)

    async def create_role(self, guild_id: str, *, name: str, mentionable: bool,
                          reason: str | None = None) -> dict[str, Any]:
        """Create a role with no permissions (always "0"), not hoisted, colour 0. There is deliberately no
        permissions parameter: this client never creates a role that grants anything.

        Fluxer ignores mentionable and hoist on create (always false, position 1), so the returned role is
        not mentionable even when mentionable=True is passed. Callers must call set_role_mentionable next."""
        if not name or len(name) > 100 or not name.isprintable():
            raise ValueError("role name must be 1-100 printable characters")
        body = {"name": name, "permissions": "0", "color": 0, "hoist": False, "mentionable": bool(mentionable)}
        headers = _check_reason(reason)
        return await self.request("POST", f"/guilds/{require_snowflake(guild_id)}/roles",
                                  json_body=body, headers=headers)

    async def set_role_mentionable(self, guild_id: str, role_id: str, *, mentionable: bool,
                                   reason: str | None = None) -> dict[str, Any]:
        """Set only a role's mentionable flag (PATCH). Deliberately cannot change permissions or anything else,
        so this client never grants a role a permission through it."""
        path = f"/guilds/{require_snowflake(guild_id)}/roles/{require_snowflake(role_id)}"
        return await self.request("PATCH", path, json_body={"mentionable": bool(mentionable)},
                                  headers=_check_reason(reason))

    async def get_messages(self, channel_id: str, *, limit: int) -> list[dict[str, Any]]:
        """Newest-first messages of a channel (GET /channels/{id}/messages?limit=N, 1 <= N <= 100)."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1-100")
        result = await self.request("GET", f"/channels/{require_snowflake(channel_id)}/messages?limit={limit}")
        return result if isinstance(result, list) else []

    async def get_reaction_users(self, channel_id: str, message_id: str, emoji: str, *,
                                 limit: int = 100, after: str | None = None) -> list[dict[str, Any]]:
        return (await self.get_reaction_users_page(channel_id, message_id, emoji, limit=limit, after=after)).users

    async def get_reaction_users_page(self, channel_id: str, message_id: str, emoji: str, *,
                                      limit: int = 100, after: str | None = None) -> ReactionUsersPage:
        """One page of a reaction's users. The live /users path answers {"items", "has_more", "next_after"}; a plain
        list is read as the last page. A next_after that is not a string is treated as absent."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1-100")
        path = (f"/channels/{require_snowflake(channel_id)}/messages/{require_snowflake(message_id)}"
                f"/reactions/{encode_emoji(emoji)}{self.REACTION_USERS_SUFFIX}?limit={limit}")
        if after is not None:
            path += f"&after={require_snowflake(after)}"
        result = await self.request("GET", path)
        if isinstance(result, dict):
            items = result.get("items")
            next_after = result.get("next_after")
            return ReactionUsersPage(users=items if isinstance(items, list) else [],
                                     has_more=result.get("has_more") is True,
                                     next_after=next_after if isinstance(next_after, str) else None)
        return ReactionUsersPage(users=result if isinstance(result, list) else [], has_more=False, next_after=None)

    async def remove_emoji_reactions(self, channel_id: str, message_id: str, emoji: str) -> None:
        await self.request("DELETE", f"/channels/{require_snowflake(channel_id)}/messages/"
                                     f"{require_snowflake(message_id)}/reactions/{encode_emoji(emoji)}")

    async def remove_all_reactions(self, channel_id: str, message_id: str) -> None:
        await self.request("DELETE", f"/channels/{require_snowflake(channel_id)}/messages/"
                                     f"{require_snowflake(message_id)}/reactions")


async def resolve_endpoints(
    session: aiohttp.ClientSession,
    *,
    token: str,
    instance_url: str | None,
    api_url: str | None,
    gateway_url: str | None,
) -> tuple[str, str]:
    """(api_base, gateway_url): explicit overrides win over discovery."""
    if api_url is None and instance_url is None:
        raise ValueError("instance_url or api_url is required")
    api = api_url or await discover_api(session, instance_url or "")
    gateway = gateway_url or await FluxerREST(session, api, token).get_gateway_url()
    return api, gateway


class ReactionUsersPage(NamedTuple):
    """One page of a reaction's users. `has_more` and `next_after` are the paging cursor; a plain list has neither."""

    users: list[dict[str, Any]]
    has_more: bool
    next_after: str | None
