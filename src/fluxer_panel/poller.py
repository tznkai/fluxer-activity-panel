# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The card poller: one task that keeps each live card's message in the games channel up to date.

Every send, edit and store for a card runs under that card's lock from `CardLocks`, which `!card archive` also takes,
so a card cannot be posted after it is archived. The poller logs event names and allowlisted fields only, never card
text, probe detail or channel names.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fluxer_client.events import MessageDelete
from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_panel import cards, render, text
from fluxer_panel.card_commands import VOICE_CHANNEL_TYPE, CardLocks, channel_name
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.logs import log_event
from fluxer_panel.resolver import next_action_text, resolve
from fluxer_panel.sources.base import HostManager, PowerAction, PowerState, ProbeFailed, ProbeResult
from fluxer_panel.sources.registry import Sources

log = logging.getLogger(__name__)


@dataclass
class _ScheduleCache:
    fetched_at: float
    action: PowerAction | None  # None also when the fetch failed: the Next line is then hidden


class Poller:
    def __init__(self, *, db: Database, rest: FluxerREST, sources: Sources, config: Config, locks: CardLocks,
                 clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._rest = rest
        self._sources = sources
        self._config = config
        self._locks = locks
        self._clock = clock
        self._kick = asyncio.Event()
        self._schedules: dict[int, _ScheduleCache] = {}
        self._voice_checked: dict[int, float] = {}
        self._warned_no_channel = False

    def kick(self) -> None:
        self._kick.set()

    async def on_message_delete(self, event: MessageDelete) -> None:
        # One compare-and-clear transaction: a message the poller has just recreated is never wiped.
        card_id = await self._db.run(lambda c: cards.clear_message_if(c, event.channel_id, event.message_id))
        if card_id is not None:
            self.kick()

    async def run(self) -> None:
        log_event(log, "poller_started")
        try:
            await self.reconcile()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must still start when the startup pass fails
            log_event(log, "poller_reconcile_failed", level=logging.ERROR, reason=type(exc).__name__)
        while True:
            self._kick.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - nothing watches this task: it must never end on its own
                log_event(log, "poller_tick_failed", level=logging.ERROR, reason=type(exc).__name__)
            try:
                await asyncio.wait_for(self._kick.wait(), self._config.poll_interval_seconds)
            except TimeoutError:
                pass

    async def reconcile(self) -> None:
        """Checks each stored message once. A message that is gone is forgotten, so the next tick posts a new one."""
        live = await self._db.run(lambda c: cards.list_cards(c, "live"))
        for card in live:
            if card.channel_id is None or card.message_id is None:
                continue
            try:
                await self._rest.get_message(card.channel_id, card.message_id)
            except FluxerHTTPError as exc:
                if exc.status == 404:
                    await self._forget_message(card.channel_id, card.message_id)
                else:
                    _reconcile_failed(card.id, exc)
            except Exception as exc:  # noqa: BLE001 - a failed lookup leaves this card alone; the others still run
                _reconcile_failed(card.id, exc)

    async def tick(self) -> None:
        live = await self._db.run(lambda c: cards.list_cards(c, "live"))
        sem = asyncio.Semaphore(self._config.poll_concurrency)
        await asyncio.gather(*(self._guarded(card, sem) for card in live))

    async def _guarded(self, card: cards.Card, sem: asyncio.Semaphore) -> None:
        async with sem:
            try:
                await self._process(card)
            except Exception as exc:  # noqa: BLE001 - one card's failure must not stop the other cards
                log_event(log, "card_poll_failed", level=logging.WARNING, card_id=card.id,
                          reason=type(exc).__name__)

    async def _process(self, card: cards.Card) -> None:
        now = int(self._clock())
        now_dt = datetime.fromtimestamp(now, UTC)
        card = await self._refresh_voice(card)
        power: PowerState | None = None
        action: PowerAction | None = None
        if card.manager is not None:
            power, action = await self._manager_state(card.id, card.manager, now_dt)
        probe_result: ProbeResult | ProbeFailed | None = None
        if card.probe is not None and (card.manager is None or power is PowerState.RUNNING):
            probe_result = await self._probe(card.probe)
        res = resolve(
            has_manager=card.manager is not None,
            power=power,
            has_probe=card.probe is not None,
            probe=probe_result,
            failing_since=card.runtime.probe_failing_since,
            alert_sent=card.runtime.probe_alert_sent,
            now=now,
            grace=self._config.probe_grace_seconds,
        )
        if (res.failing_since, res.alert_sent) != (card.runtime.probe_failing_since, card.runtime.probe_alert_sent):
            # Saved before the DM goes out: a crash or a failed DM can never send the alert a second time.
            await self._db.run(lambda c: cards.set_probe_tracking(c, card.id, res.failing_since, res.alert_sent))
        if res.alert_now:
            await self._alert(card)
        next_text = next_action_text(action, now_dt) if action is not None else None
        embed = render.render_embed(card, res.status, next_text, instance_url=self._config.instance_url)
        await self._publish(card.id, embed)

    async def _refresh_voice(self, card: cards.Card) -> cards.Card:
        """Re-reads a verified voice channel's name, at most once per refresh interval. Returns the card to render."""
        voice = card.voice
        if voice is None or not voice.verified:
            return card
        now = self._clock()
        last = self._voice_checked.get(card.id)
        if last is not None and now - last < self._config.schedule_refresh_seconds:
            return card
        self._voice_checked[card.id] = now
        try:
            channel = await self._rest.get_channel(voice.channel_id)
        except Exception as exc:  # noqa: BLE001 - a failed name check keeps the stored name
            log_event(log, "voice_refresh_failed", level=logging.WARNING, card_id=card.id,
                      reason=type(exc).__name__)
            return card
        name = channel_name(channel.get("name"))
        if name is None or name == voice.channel_name:
            return card
        if channel.get("type") != VOICE_CHANNEL_TYPE or str(channel.get("guild_id")) != voice.guild_id:
            return card
        refreshed = cards.Voice(voice.guild_id, voice.channel_id, name, True)
        await self._db.run(lambda c: cards.set_voice(c, card.id, refreshed))
        return await self._db.run(lambda c: cards.get_by_id(c, card.id)) or card

    async def _manager_state(self, card_id: int, ref: cards.ManagerRef,
                             now_dt: datetime) -> tuple[PowerState, PowerAction | None]:
        manager = self._sources.managers.get(ref.kind)
        if manager is None:
            # A kind that is no longer configured: no request, and no schedule is cached for it.
            return PowerState.UNKNOWN, None
        power = await manager.power_state(ref.ref)
        return power, await self._next_action(card_id, manager, ref.ref, now_dt)

    async def _next_action(self, card_id: int, manager: HostManager, server_ref: str,
                           now_dt: datetime) -> PowerAction | None:
        cached = self._schedules.get(card_id)
        if cached is None or self._clock() - cached.fetched_at >= self._config.schedule_refresh_seconds:
            try:
                action = await manager.next_power_action(server_ref, now=now_dt)
            except Exception:  # noqa: BLE001 - the Next line is optional: a failed fetch only hides it
                action = None
            cached = _ScheduleCache(self._clock(), action)
            self._schedules[card_id] = cached
        if cached.action is not None and cached.action.at < now_dt:
            return None  # a cached action that has already passed is hidden until the next refresh
        return cached.action

    async def _probe(self, probe: cards.ProbeRef) -> ProbeResult | ProbeFailed:
        plugin = self._sources.probes.get(probe.kind)
        if plugin is None:
            return ProbeFailed("error")  # a probe kind with no plugin reads as a failed probe
        return await plugin.probe(probe.host, probe.port, self._config.probe_timeout_seconds)

    async def _alert(self, card: cards.Card) -> None:
        content = text.PROBE_ALERT.format(slug=card.slug)
        for user_id in sorted(self._config.admin_user_ids):
            try:
                dm = await self._rest.open_dm(user_id)
                await self._rest.send_message(dm, content=content, allowed_mentions={"parse": []})
            except Exception as exc:  # noqa: BLE001 - one admin's failed DM must not stop the other admins' DMs
                log_event(log, "probe_alert_failed", level=logging.WARNING, user_id=user_id,
                          reason=type(exc).__name__)

    async def _publish(self, card_id: int, embed: dict[str, Any]) -> None:
        channel_id = self._config.games_channel_id
        if channel_id is None:
            if not self._warned_no_channel:
                self._warned_no_channel = True
                log_event(log, "games_channel_unset", level=logging.WARNING)
            return
        rendered = render.rendered_hash(embed)
        payload = render.card_message(embed)
        async with self._locks.get(card_id):
            fresh = await self._db.run(lambda c: cards.get_by_id(c, card_id))
            if fresh is None or fresh.state != "live":
                return
            if fresh.message_id is None or fresh.channel_id is None:
                await self._post(card_id, channel_id, rendered, payload)
            elif rendered != fresh.runtime.rendered_hash:
                await self._edit(card_id, fresh.channel_id, fresh.message_id, rendered, payload)

    async def _post(self, card_id: int, channel_id: str, rendered: str, payload: dict[str, Any]) -> None:
        sent = await self._rest.send_message(channel_id, embeds=payload["embeds"],
                                             allowed_mentions=payload["allowed_mentions"])
        message_id = sent["id"]
        await self._db.run(lambda c: cards.set_message(c, card_id, channel_id, message_id, rendered))

    async def _edit(self, card_id: int, channel_id: str, message_id: str, rendered: str,
                    payload: dict[str, Any]) -> None:
        try:
            await self._rest.edit_message(channel_id, message_id, embeds=payload["embeds"],
                                          allowed_mentions=payload["allowed_mentions"])
        except FluxerHTTPError as exc:
            if exc.status != 404:
                raise
            log_event(log, "card_message_recreated", card_id=card_id)
            await self._post(card_id, channel_id, rendered, payload)
            return
        await self._db.run(lambda c: cards.set_message(c, card_id, channel_id, message_id, rendered))

    async def _forget_message(self, channel_id: str, message_id: str) -> None:
        await self._db.run(lambda c: cards.clear_message_if(c, channel_id, message_id))


def _reconcile_failed(card_id: int, exc: Exception) -> None:
    log_event(log, "card_reconcile_failed", level=logging.WARNING, card_id=card_id, reason=type(exc).__name__)
