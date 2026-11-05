# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wake presses. A press on an asleep card with a Pterodactyl manager opens a request, a row in `requests`. Any other
press is a signal: no row, and the admins hear about it in the same DM. While a request or a signal is current, the
admins' DMs are edited rather than re-sent.

Every press runs under the card's lock. The signal list lives in memory only, so a restart forgets it, by design.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_panel import cards, notify, text, wake_requests
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import Card
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.logs import log_event
from fluxer_panel.resolver import CardStatus, StatusKind, status_label
from fluxer_panel.sources import pterodactyl
from fluxer_panel.sources.registry import Sources
from fluxer_panel.wake_requests import AdminDm

log = logging.getLogger(__name__)
NO_MENTIONS = {"parse": []}


def wake_capable(card: Card, sources: Sources) -> bool:
    """A card can be woken only through a Pterodactyl manager, and only when that manager is configured."""
    return (card.manager is not None and card.manager.kind == pterodactyl.KIND
            and pterodactyl.KIND in sources.managers)


@dataclass
class _Signal:
    opened_at: int
    requesters: list[str]


class WakeRequests:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, sources: Sources, locks: CardLocks,
                 status_of: Callable[[int], CardStatus | None], clock: Callable[[], float] = time.time,
                 kick: Callable[[], None] = lambda: None) -> None:
        self._db = db
        self._rest = rest
        self._config = config
        self._sources = sources
        self._locks = locks
        self._status_of = status_of
        self._clock = clock
        self._kick = kick
        self._signals: dict[int, _Signal] = {}     # lost on restart, by design

    async def press(self, card_id: int, user_id: str) -> None:
        async with self._locks.get(card_id):
            card = await self._db.run(lambda c: cards.get_by_id(c, card_id))
            if card is None or card.state != "live":
                return
            status = self._status_of(card.id)
            now = int(self._clock())
            capable = wake_capable(card, self._sources)
            opens = status is not None and status.kind is StatusKind.ASLEEP and capable
            outcome = "signal"
            if opens:
                added = await self._db.run(lambda c: wake_requests.add_open(c, card.id, user_id, now))
                outcome = "opened" if added else "repeat"
            await self._dm_requester(card, user_id, opens, status, capable)

            sig = self._signals.get(card.id)
            if sig is not None and now - sig.opened_at >= self._config.wake_request_expiry_seconds:
                del self._signals[card.id]
                sig = None
            had_sig = sig is not None
            if not opens:
                if sig is None:
                    sig = _Signal(now, [])
                    self._signals[card.id] = sig
                if user_id not in sig.requesters:
                    sig.requesters.append(user_id)

            open_ids, dms = await self._db.run(
                lambda c: (wake_requests.open_requesters(c, card.id), wake_requests.get_dms(c, card.id)))
            names = list(open_ids) + [r for r in (sig.requesters if sig is not None else []) if r not in open_ids]
            kind = "request" if open_ids else "signal"
            content = self._admin_content(card, status, names, requested=bool(open_ids))
            reuse = bool(dms) and (bool(open_ids) or (had_sig and all(dm.kind == "signal" for dm in dms)))
            if reuse:
                await self._edit_admin_dms(card, dms, content, kind, now)
            else:
                await self._send_admin_dms(card, content, kind, now)
            if opens:
                self._kick()
            log_event(log, "wake_press", card_id=card.id, outcome=outcome)

    async def list_open(self) -> list[tuple[str, int, int]]:
        """(slug, distinct requesters, age in seconds of the oldest open row), one per card, ordered by slug."""
        now = int(self._clock())

        def read(c) -> list[tuple[str, int, int]]:
            rows: list[tuple[str, int, int]] = []
            for summary in wake_requests.open_summaries(c):
                card = cards.get_by_id(c, summary.card_id)
                if card is not None:
                    rows.append((card.slug, summary.count, now - summary.oldest_at))
            return sorted(rows)

        return await self._db.run(read)

    async def _dm_requester(self, card: Card, user_id: str, opens: bool, status: CardStatus | None,
                            capable: bool) -> None:
        content = text.WAKE_SENT.format(card=card.title)
        if not opens:
            key = "no_manager" if not capable else ("none" if status is None else status.kind.value)
            content = f"{content} {text.WAKE_NOTES[key]}"
        await notify.dm_user(self._rest, user_id, content)

    @staticmethod
    def _admin_content(card: Card, status: CardStatus | None, names: list[str], *, requested: bool) -> str:
        shown = "no status" if status is None else status_label(status)
        requesters = " ".join(f"<@{name}>" for name in names)
        content = text.WAKE_ADMIN.format(card=card.title, slug=card.slug, status=shown, requesters=requesters)
        return (content + text.WAKE_ADMIN_OPEN) if requested else content

    async def _edit_admin_dms(self, card: Card, dms: list[AdminDm], content: str, kind: str, now: int) -> None:
        for dm in dms:
            try:
                await self._rest.edit_message(dm.channel_id, dm.message_id, content=content,
                                              allowed_mentions=NO_MENTIONS)
            except Exception as exc:  # noqa: BLE001 - one admin's failed edit must not stop the other admins' DMs
                if isinstance(exc, FluxerHTTPError) and exc.status == 404:
                    await self._send_admin_dm(card, dm.admin_id, content, kind, now)
                else:
                    log_event(log, "admin_dm_edit_failed", level=logging.WARNING, card_id=card.id,
                              reason=type(exc).__name__)
                continue
            await self._put_dm(AdminDm(card.id, dm.admin_id, kind, dm.channel_id, dm.message_id, dm.opened_at))

    async def _send_admin_dms(self, card: Card, content: str, kind: str, now: int) -> None:
        await self._db.run(lambda c: wake_requests.drop_dms(c, card.id))
        for admin in sorted(self._config.admin_user_ids):
            await self._send_admin_dm(card, admin, content, kind, now)

    async def _send_admin_dm(self, card: Card, admin_id: str, content: str, kind: str, now: int) -> None:
        sent = await notify.dm_user(self._rest, admin_id, content)
        if sent is not None:
            channel_id, message_id = sent
            await self._put_dm(AdminDm(card.id, admin_id, kind, channel_id, message_id, now))

    async def _put_dm(self, row: AdminDm) -> None:
        await self._db.run(lambda c: wake_requests.put_dm(c, row))
