# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every REST call on a mirror message: a home session, poll or play-now post re-posted in a both subscription's
channel. It posts when no row is stored, edits only when the content hash changed, and forgets a mirror on a 404.
A 403 or another failure is logged and the row is kept, so the home object and the other mirrors are unaffected.
The poller never back-fills a mirror, so a dropped one stays gone until its object is fanned out again."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_panel import mirrors, render, scope, subscriptions
from fluxer_panel.cards import Card
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games_guard import GamesChannelRefused
from fluxer_panel.logs import log_event
from fluxer_panel.subscriptions import Subscription

log = logging.getLogger(__name__)

NO_MENTIONS: dict[str, Any] = {"parse": []}


def body_hash(payload: dict[str, Any]) -> str:
    """The hash of what a mirror shows: its content and embeds. The allowed mentions are left out, so a first post that
    pings a role and a later edit of the same content compare equal."""
    return render.rendered_hash({key: value for key, value in payload.items() if key != "allowed_mentions"})


def _reason(exc: Exception) -> str:
    """A reason key only: "forbidden" for a 403, else the exception's type name. Never its message."""
    if isinstance(exc, FluxerHTTPError) and exc.status == 403:
        return "forbidden"
    return type(exc).__name__


def _is_not_found(exc: Exception) -> bool:
    return isinstance(exc, FluxerHTTPError) and exc.status == 404


class MirrorPost:
    def __init__(self, db: Database, rest: FluxerREST, config: Config) -> None:
        self._db = db
        self._rest = rest
        self._config = config

    def targets(self, conn: sqlite3.Connection, card: Card) -> list[tuple[Subscription, str]]:
        """(sub, channel_id) for every active both subscription of the card, channel from scope.scope_target."""
        found: list[tuple[Subscription, str]] = []
        for sub in subscriptions.list_both(conn, card.id):
            channel_id = scope.scope_target(card, self._config, scope.scope_for_sub(conn, card, sub))
            if channel_id is not None:
                found.append((sub, channel_id))
        return found

    async def sync(self, *, sub: Subscription, channel_id: str, kind: mirrors.Kind, object_id: int,
                   payload: dict[str, Any], allowed_mentions: dict[str, Any] | None = None) -> str | None:
        """Posts the mirror if none is stored, else edits it when the payload hash changed. Returns the mirror's
        message id, or None when there is none (failed post, dropped on 404). Never raises FluxerHTTPError."""
        self._refuse(channel_id)
        digest = body_hash(payload)
        mirror = await self._db.run(lambda c: mirrors.get(c, kind, object_id, sub.id))
        if mirror is None:
            return await self._post(sub, channel_id, kind, object_id, payload, digest,
                                    allowed_mentions if allowed_mentions is not None else NO_MENTIONS)
        self._refuse(mirror.channel_id)
        if mirror.content_hash == digest:
            return mirror.message_id
        return await self._edit(mirror, payload, digest)

    async def edit_stored(self, mirror: mirrors.Mirror, payload: dict[str, Any]) -> str | None:
        """Edits a mirror that is already stored, whatever the subscription's mode now is (the purge scrub uses it).
        Same rules as `sync`: a 404 drops the row, a failure is logged and the row stays. Returns the message id, or
        None when the row was dropped."""
        self._refuse(mirror.channel_id)
        digest = body_hash(payload)
        if mirror.content_hash == digest:
            return mirror.message_id
        return await self._edit(mirror, payload, digest)

    async def delete(self, mirror: mirrors.Mirror) -> bool:
        """Deletes the message and the row. 404 counts as deleted. 403 logs and returns False (row deleted)."""
        self._refuse(mirror.channel_id)
        deleted = True
        try:
            await self._rest.delete_message(mirror.channel_id, mirror.message_id)
        except Exception as exc:  # noqa: BLE001 - a mirror that cannot be deleted is logged, and its row goes anyway
            if not _is_not_found(exc):
                deleted = False
                self._log_failed(sub_id=mirror.sub_id, channel_id=mirror.channel_id, kind=mirror.kind,
                                 reason=_reason(exc), mirror_id=mirror.id, message_id=mirror.message_id)
        await self._db.run(lambda c: mirrors.delete_if(c, mirror.id, mirror.message_id))
        return deleted

    async def _post(self, sub: Subscription, channel_id: str, kind: mirrors.Kind, object_id: int,
                    payload: dict[str, Any], digest: str, allowed_mentions: dict[str, Any]) -> str | None:
        try:
            sent = await self._rest.send_message(channel_id, embeds=payload["embeds"],
                                                 allowed_mentions=allowed_mentions)
        except Exception as exc:  # noqa: BLE001 - a refused post is logged; the home action goes on
            self._log_failed(sub_id=sub.id, channel_id=channel_id, kind=kind, reason=_reason(exc))
            return None
        message_id = sent["id"]
        try:
            await self._db.run(lambda c: mirrors.add(c, sub_id=sub.id, channel_id=channel_id, message_id=message_id,
                                                     kind=kind, object_id=object_id, content_hash=digest))
        except sqlite3.IntegrityError:
            # A row already holds this object in this subscription, or the object is gone. The message just sent is
            # removed, as the poller removes a message deleted before its id was stored.
            await self._remove_unstored(channel_id, message_id)
            self._log_failed(sub_id=sub.id, channel_id=channel_id, kind=kind, reason="row_refused",
                             message_id=message_id)
            return None
        return message_id

    async def _edit(self, mirror: mirrors.Mirror, payload: dict[str, Any], digest: str) -> str | None:
        # An edit never re-pings, so it always sends empty allowed_mentions.
        try:
            await self._rest.edit_message(mirror.channel_id, mirror.message_id, embeds=payload["embeds"],
                                          allowed_mentions=NO_MENTIONS)
        except Exception as exc:  # noqa: BLE001 - the row stays unless the message is gone
            if _is_not_found(exc):
                await self._db.run(lambda c: mirrors.delete_if(c, mirror.id, mirror.message_id))
                log_event(log, "mirror_dropped", level=logging.INFO, sub_id=mirror.sub_id, mirror_id=mirror.id,
                          channel_id=mirror.channel_id, message_id=mirror.message_id, kind=mirror.kind)
                return None
            self._log_failed(sub_id=mirror.sub_id, channel_id=mirror.channel_id, kind=mirror.kind,
                             reason=_reason(exc),
                             mirror_id=mirror.id, message_id=mirror.message_id)
            return mirror.message_id
        # Compare-and-set: a row deleted while the edit was in flight is not written back.
        await self._db.run(lambda c: mirrors.set_hash_if(c, mirror.id, mirror.message_id, digest))
        return mirror.message_id

    async def _remove_unstored(self, channel_id: str, message_id: str) -> None:
        try:
            await self._rest.delete_message(channel_id, message_id)
        except Exception as exc:  # noqa: BLE001 - the message is left behind and logged by reason only
            log_event(log, "mirror_failed", level=logging.WARNING, channel_id=channel_id, message_id=message_id,
                      reason=_reason(exc))

    def _refuse(self, channel_id: str) -> None:
        """The games channel is refused before any REST call. The far channel was refused at configuration too."""
        if channel_id == self._config.games_channel_id:
            raise GamesChannelRefused

    @staticmethod
    def _log_failed(*, sub_id: int, channel_id: str, kind: mirrors.Kind, reason: str, mirror_id: int | None = None,
                    message_id: str | None = None) -> None:
        log_event(log, "mirror_failed", level=logging.WARNING, sub_id=sub_id, mirror_id=mirror_id,
                  channel_id=channel_id, message_id=message_id, kind=kind, reason=reason)
