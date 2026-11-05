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
from fluxer_panel import cards, notify, render, text, wake_requests
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


def people(n: int) -> str:
    """A requester count in words: "1 person" or "2 people". It lives here, not in text.py, which holds constants only."""
    return "1 person" if n == 1 else f"{n} people"


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
        content = text.WAKE_SENT.format(card=render.escape(card.title))
        if not opens:
            # "no_manager" only when the bot cannot see the card's server and has no status to report instead.
            if not capable and (status is None or status.kind is StatusKind.ASLEEP):
                key = "no_manager"
            else:
                key = "none" if status is None else status.kind.value
            content = f"{content} {text.WAKE_NOTES[key]}"
        await notify.dm_user(self._rest, user_id, content)

    @staticmethod
    def _admin_content(card: Card, status: CardStatus | None, names: list[str], *, requested: bool) -> str:
        shown = "no status" if status is None else status_label(status)
        requesters = " ".join(f"<@{name}>" for name in names)
        content = text.WAKE_ADMIN.format(card=render.escape(card.title), slug=card.slug, status=shown,
                                         requesters=requesters)
        return (content + text.WAKE_ADMIN_OPEN) if requested else content

    async def _edit_admin_dms(self, card: Card, dms: list[AdminDm], content: str, kind: str, now: int) -> None:
        for dm in dms:
            outcome = await self._patch_admin_dm(card, dm, content)
            if outcome == "gone":
                await self._send_admin_dm(card, dm.admin_id, content, kind, now)
            elif outcome == "edited":
                await self._put_dm(AdminDm(card.id, dm.admin_id, kind, dm.channel_id, dm.message_id, dm.opened_at))

    async def _patch_admin_dm(self, card: Card, dm: AdminDm, content: str) -> str:
        """Edits one admin DM. Returns "edited", "gone" (a 404: the message was deleted) or "failed" (logged by type).
        One admin's failed edit never stops the other admins' DMs."""
        try:
            await self._rest.edit_message(dm.channel_id, dm.message_id, content=content, allowed_mentions=NO_MENTIONS)
        except Exception as exc:  # noqa: BLE001 - see above: any failure is logged by type and the others go on
            if isinstance(exc, FluxerHTTPError) and exc.status == 404:
                return "gone"
            log_event(log, "admin_dm_edit_failed", level=logging.WARNING, card_id=dm.card_id,
                      reason=type(exc).__name__)
            return "failed"
        return "edited"

    async def close(self, card_id: int, status: str) -> int:
        """Closes every open request of the card as `status`, DMs each requester once, and edits the admins' DMs to
        say so. Returns the number of requests closed. The rows close first, in one transaction, so a crash after
        that point never sends a second close DM."""
        async with self._locks.get(card_id):
            card = await self._db.run(lambda c: cards.get_by_id(c, card_id))
            if card is None:
                return 0
            now = int(self._clock())
            user_ids = await self._db.run(lambda c: wake_requests.close(c, card_id, status, now))
            if not user_ids:
                return 0
            title = render.escape(card.title)
            requester_text = text.WAKE_READY if status == "ready" else text.WAKE_GAVE_UP
            for user_id in user_ids:
                await notify.dm_user(self._rest, user_id, requester_text.format(card=title))
            admin_text = text.WAKE_ADMIN_CLOSED[status].format(card=title, slug=card.slug,
                                                               people=people(len(user_ids)))
            # A failed non-404 edit leaves that admin with the stale message, by design: it is logged, and nothing retries.
            for dm in await self._db.run(lambda c: wake_requests.get_dms(c, card_id)):
                await self._patch_admin_dm(card, dm, admin_text)
            await self._db.run(lambda c: wake_requests.drop_dms(c, card_id))
            self._signals.pop(card_id, None)
            self._kick()
            log_event(log, "wake_closed", card_id=card_id, outcome=status, count=len(user_ids))
            return len(user_ids)

    async def close_due(self) -> None:
        """Closes a card's requests as ready when it is Ready, or as expired when its oldest open row is older than the
        window. Runs on every poller tick."""
        now = int(self._clock())
        for summary in await self._db.run(wake_requests.open_summaries):
            status = self._status_of(summary.card_id)
            if status is not None and status.kind is StatusKind.READY:
                await self.close(summary.card_id, "ready")
            elif now - summary.oldest_at > self._config.wake_request_expiry_seconds:
                await self.close(summary.card_id, "expired")

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
