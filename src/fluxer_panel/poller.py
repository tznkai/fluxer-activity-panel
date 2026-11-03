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
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fluxer_client.events import MessageDelete
from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_panel import board, cards, render, text
from fluxer_panel.card_commands import TEXT_CHANNEL_TYPE, VOICE_CHANNEL_TYPE, CardLocks, channel_name
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.logs import log_event
from fluxer_panel.resolver import CardStatus, next_action_text, resolve
from fluxer_panel.sources.base import HostManager, PowerAction, PowerState, ProbeFailed, ProbeResult
from fluxer_panel.sources.registry import Sources

log = logging.getLogger(__name__)


@dataclass
class _ScheduleCache:
    ref: cards.ManagerRef       # the manager the action was fetched for: a changed manager is a cache miss
    fetched_at: float
    action: PowerAction | None  # None also when the fetch failed: the Next line is then hidden


# Every MESSAGE_DELETE the bot sees is recorded, from any channel, and only the most recent _DELETED_CAP are kept.
# A record only matters for a store still in flight, which needs a delete from the window between its send and its
# commit. That window is short, so the most recent few hundred deletes cover it.
_DELETED_CAP = 256


@dataclass(frozen=True)
class _Evaluated:
    """One card's outcome for this tick. `embed` is None when the evaluation failed: such a card is not published, and
    the board lists it with the dash, as a card with no status."""
    card: cards.Card
    status: CardStatus | None
    embed: dict[str, Any] | None


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
        self._names_checked: dict[int, float] = {}
        self._deleted: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._warned_no_channel = False
        self._games_guilds: dict[str, str] = {}   # each board channel's community, once a lookup has answered

    def kick(self) -> None:
        self._kick.set()

    async def on_message_delete(self, event: MessageDelete) -> None:
        # Remembered before the transaction, so a store that commits after this point still sees the deletion.
        self._remember_deleted(event.channel_id, event.message_id)
        # One compare-and-clear transaction: a message the poller has just recreated is never wiped.
        if await self._db.run(lambda c: _clear_posted(c, event.channel_id, event.message_id)):
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
        await self._reconcile_board()

    async def _reconcile_board(self) -> None:
        """Checks the stored board message once, the way cards are checked. A message that is gone is forgotten."""
        state = await self._db.run(board.get)
        if state.channel_id is None or state.message_id is None:
            return
        try:
            await self._rest.get_message(state.channel_id, state.message_id)
        except FluxerHTTPError as exc:
            if exc.status == 404:
                await self._forget_message(state.channel_id, state.message_id)
            else:
                log_event(log, "board_reconcile_failed", level=logging.WARNING, reason=type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - a failed lookup keeps the stored message
            log_event(log, "board_reconcile_failed", level=logging.WARNING, reason=type(exc).__name__)

    async def tick(self) -> None:
        """Keeps the cards and the board up to date. When no board message is stored, every card is evaluated first, then
        the board is posted, then the cards are published, so a new board sits above them. When a board is stored, each
        card is published as soon as its own evaluation ends, so one slow card does not hold back the others. Either way
        the board is brought up to date after the cards."""
        live = await self._db.run(lambda c: cards.list_cards(c, "live"))
        self._forget_cards_not_in({card.id for card in live})
        sem = asyncio.Semaphore(self._config.poll_concurrency)
        channel_id = self._config.games_channel_id
        if channel_id is not None and await self._board_is_missing():
            evaluated = await asyncio.gather(*(self._evaluate_guarded(card, sem) for card in live))
            await self._board_before_cards(evaluated, channel_id)
            await asyncio.gather(*(self._publish_guarded(result, sem) for result in evaluated
                                   if result.embed is not None))
            results = evaluated
        else:
            results = await asyncio.gather(*(self._evaluate_and_publish(card, sem) for card in live))
        if channel_id is not None:
            await self._board_after_cards({result.card.id: result.status for result in results}, channel_id)

    def _forget_cards_not_in(self, live_ids: set[int]) -> None:
        """Drops the cached schedule and channel name check of every card not in `live_ids`, the live set of this tick.
        A card republished after such a tick fetches them again. A card republished before any tick saw it as non-live
        keeps its cache. A manager change is handled by the ManagerRef comparison in `_next_action`, not by this drop."""
        for entries in (self._schedules, self._names_checked):
            for card_id in [card_id for card_id in entries if card_id not in live_ids]:
                del entries[card_id]

    async def _evaluate_and_publish(self, card: cards.Card, sem: asyncio.Semaphore) -> _Evaluated:
        result = await self._evaluate_guarded(card, sem)
        if result.embed is not None:
            await self._publish_guarded(result, sem)
        return result

    async def _evaluate_guarded(self, card: cards.Card, sem: asyncio.Semaphore) -> _Evaluated:
        """The card's evaluation. A failure is logged and gives an outcome with no embed, so the card is not published
        but still appears on the board."""
        async with sem:
            try:
                return await self._evaluate(card)
            except Exception as exc:  # noqa: BLE001 - one card's failure must not stop the other cards
                log_event(log, "card_poll_failed", level=logging.WARNING, card_id=card.id,
                          reason=type(exc).__name__)
                return _Evaluated(card, None, None)

    async def _publish_guarded(self, result: _Evaluated, sem: asyncio.Semaphore) -> None:
        async with sem:
            assert result.embed is not None
            try:
                await self._publish(result.card.id, result.embed)
            except Exception as exc:  # noqa: BLE001 - one card's failure must not stop the other cards
                log_event(log, "card_poll_failed", level=logging.WARNING, card_id=result.card.id,
                          reason=type(exc).__name__)

    async def _evaluate(self, card: cards.Card) -> _Evaluated:
        now = int(self._clock())
        now_dt = datetime.fromtimestamp(now, UTC)
        card = await self._refresh_names(card)
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
        return _Evaluated(card, res.status, embed)

    async def _refresh_names(self, card: cards.Card) -> cards.Card:
        """Re-reads the name of each verified channel link (the voice channel and the text channels), at most once per
        refresh interval. Returns the card to render."""
        links = _verified_links(card)
        if not links:
            return card
        now = self._clock()
        last = self._names_checked.get(card.id)
        if last is not None and now - last < self._config.schedule_refresh_seconds:
            return card
        self._names_checked[card.id] = now
        attempted = False
        for slot, link, channel_type in links:
            attempted = await self._refresh_name(card.id, slot, link, channel_type) or attempted
        if not attempted:
            return card
        # Reloaded after any write attempt, so this tick renders what the store holds now, not the links as read.
        return await self._db.run(lambda c: cards.get_by_id(c, card.id)) or card

    async def _refresh_name(self, card_id: int, slot: int | None, link: cards.ChannelLink,
                            channel_type: int) -> bool:
        """Re-reads one link's channel and writes a changed name. `slot` is None for the voice link. Returns whether a
        write was attempted."""
        try:
            channel = await self._rest.get_channel(link.channel_id)
        except Exception as exc:  # noqa: BLE001 - a failed name check keeps the stored name
            event = "voice_refresh_failed" if slot is None else "text_channel_refresh_failed"
            log_event(log, event, level=logging.WARNING, card_id=card_id, reason=type(exc).__name__)
            return False
        name = channel_name(channel.get("name"))
        if name is None or name == link.channel_name:
            return False
        if channel.get("type") != channel_type or str(channel.get("guild_id")) != link.guild_id:
            return False
        # Only the name is written, and only while the link still matches the one read: an admin's clear or set that
        # landed during the channel lookup stands.
        if slot is None:
            await self._db.run(lambda c: cards.set_voice_name_if(c, card_id, link, name))
        else:
            await self._db.run(lambda c: cards.set_text_channel_name_if(c, card_id, slot, link, name))
        return True

    async def _manager_state(self, card_id: int, ref: cards.ManagerRef,
                             now_dt: datetime) -> tuple[PowerState, PowerAction | None]:
        manager = self._sources.managers.get(ref.kind)
        if manager is None:
            # A kind that is no longer configured: no request, and no schedule is cached for it.
            return PowerState.UNKNOWN, None
        power = await manager.power_state(ref.ref)
        return power, await self._next_action(card_id, manager, ref, now_dt)

    async def _next_action(self, card_id: int, manager: HostManager, ref: cards.ManagerRef,
                           now_dt: datetime) -> PowerAction | None:
        cached = self._schedules.get(card_id)
        stale = (cached is None or cached.ref != ref
                 or self._clock() - cached.fetched_at >= self._config.schedule_refresh_seconds)
        if stale:
            try:
                action = await manager.next_power_action(ref.ref, now=now_dt)
            except Exception:  # noqa: BLE001 - the Next line is optional: a failed fetch only hides it
                action = None
            cached = _ScheduleCache(ref, self._clock(), action)
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
        await self._dm_admins(text.PROBE_ALERT.format(slug=card.slug), failed_event="probe_alert_failed")

    async def _dm_admins(self, content: str, *, failed_event: str) -> None:
        for user_id in sorted(self._config.admin_user_ids):
            try:
                dm = await self._rest.open_dm(user_id)
                await self._rest.send_message(dm, content=content, allowed_mentions={"parse": []})
            except Exception as exc:  # noqa: BLE001 - one admin's failed DM must not stop the other admins' DMs
                log_event(log, failed_event, level=logging.WARNING, user_id=user_id, reason=type(exc).__name__)

    async def _board_is_missing(self) -> bool:
        """Whether no board message is stored, so this tick posts the board before the cards. A failed read is logged and
        answers False: the cards then publish as they would with no board, and no board is posted while the stored state
        is unknown."""
        try:
            state = await self._db.run(board.get)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed read must not stop the cards of this tick
            log_event(log, "board_read_failed", level=logging.WARNING, reason=type(exc).__name__)
            return False
        return state.message_id is None

    async def _board_before_cards(self, evaluated: list[_Evaluated], channel_id: str) -> None:
        """Posts the board to the games channel before this tick's card messages. It runs only when no board is stored.
        A failure is logged, and the board is posted again on the next tick."""
        try:
            payload = await self._board_payload([(result.card, result.status) for result in evaluated], channel_id)
            await self._board_post(channel_id, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed board post must not stop the cards of this tick
            log_event(log, "board_post_failed", level=logging.WARNING, channel_id=channel_id,
                      reason=type(exc).__name__)

    async def _board_after_cards(self, statuses: dict[int, CardStatus | None], games_channel_id: str) -> None:
        """Brings the board up to date with the cards as they are now: posts it if none is stored, and edits it when its
        content changed. A stored board stays in the channel it was posted in, so its edits go there, whatever
        GAMES_CHANNEL_ID is now. Cards that were not evaluated this tick are left off until the next tick."""
        try:
            live = await self._db.run(lambda c: cards.list_cards(c, "live"))
            entries = [(card, statuses[card.id]) for card in live if card.id in statuses]
            state = await self._db.run(board.get)
            channel_id = state.channel_id if state.channel_id is not None else games_channel_id
            payload = await self._board_payload(entries, channel_id)
            rendered = render.rendered_hash(payload)
            if state.message_id is None:
                await self._board_post(channel_id, payload)
            elif rendered != state.content_hash:
                assert state.message_id is not None
                await self._board_edit(channel_id, state.message_id, payload, rendered)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed board edit must not stop the poller; the next tick retries
            log_event(log, "board_sync_failed", level=logging.WARNING, reason=type(exc).__name__)

    async def _board_payload(self, entries: list[tuple[cards.Card, CardStatus | None]],
                             channel_id: str) -> dict[str, Any]:
        guild_id = await self._games_guild_id(channel_id)
        return render.board_message(entries, games_channel_id=channel_id, games_guild_id=guild_id,
                                    instance_url=self._config.instance_url)

    async def _games_guild_id(self, channel_id: str) -> str | None:
        """The community of a board channel, looked up once per channel and kept. Until a lookup answers, the board's
        links are plain text."""
        known = self._games_guilds.get(channel_id)
        if known is not None:
            return known
        try:
            channel = await self._rest.get_channel(channel_id)
        except Exception as exc:  # noqa: BLE001 - without the community the board shows plain text, and the lookup retries
            log_event(log, "games_channel_lookup_failed", level=logging.WARNING, reason=type(exc).__name__)
            return None
        guild_id = channel.get("guild_id")
        if not is_snowflake(guild_id):
            log_event(log, "games_channel_lookup_failed", level=logging.WARNING, reason="no_community")
            return None
        self._games_guilds[channel_id] = guild_id
        return guild_id

    async def _board_post(self, channel_id: str, payload: dict[str, Any]) -> None:
        sent = await self._rest.send_message(channel_id, embeds=payload["embeds"],
                                             allowed_mentions=payload["allowed_mentions"])
        message_id = sent["id"]
        rendered = render.rendered_hash(payload)
        await self._db.run(lambda c: board.set_message(c, channel_id, message_id, rendered))
        if await self._undo_if_deleted(channel_id, message_id):
            return  # deleted before its id was stored: forgotten, so no pin request for it
        log_event(log, "board_posted")
        await self._dm_admins(text.BOARD_PIN_ALERT, failed_event="board_alert_failed")

    async def _board_edit(self, channel_id: str, message_id: str, payload: dict[str, Any], rendered: str) -> None:
        try:
            await self._rest.edit_message(channel_id, message_id, embeds=payload["embeds"],
                                          allowed_mentions=payload["allowed_mentions"])
        except FluxerHTTPError as exc:
            if exc.status != 404:
                raise
            log_event(log, "board_message_recreated")
            await self._board_post(channel_id, payload)
            return
        # Conditional, as for cards: a message deleted during the edit is not written back.
        await self._db.run(lambda c: board.set_hash_if(c, channel_id, message_id, rendered))

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
        await self._undo_if_deleted(channel_id, message_id)

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
        # Conditional: a message deleted while the edit was in flight is not written back. A delete that commits
        # before this write leaves the card without the message, and one that commits after it clears the message,
        # so no undo is needed here.
        await self._db.run(lambda c: cards.set_hash_if(c, card_id, channel_id, message_id, rendered))

    async def _undo_if_deleted(self, channel_id: str, message_id: str) -> bool:
        """A message that was deleted before its id was stored is forgotten, and it is posted again. Returns whether it
        was forgotten."""
        if (channel_id, message_id) not in self._deleted:
            return False
        if await self._db.run(lambda c: _clear_posted(c, channel_id, message_id)):
            self.kick()
            return True
        return False

    def _remember_deleted(self, channel_id: str, message_id: str) -> None:
        # Insertion order decides eviction. A repeated delete does not move its entry, since a message is deleted once.
        self._deleted[(channel_id, message_id)] = None
        while len(self._deleted) > _DELETED_CAP:
            self._deleted.popitem(last=False)

    async def _forget_message(self, channel_id: str, message_id: str) -> None:
        await self._db.run(lambda c: _clear_posted(c, channel_id, message_id))


def _verified_links(card: cards.Card) -> list[tuple[int | None, cards.ChannelLink, int]]:
    """Each verified channel link, with its slot (None for voice) and the channel type it must still have."""
    links: list[tuple[int | None, cards.ChannelLink, int]] = []
    if card.voice is not None and card.voice.verified:
        links.append((None, card.voice, VOICE_CHANNEL_TYPE))
    for slot, link in enumerate(card.text_channels, start=1):
        if link is not None and link.verified:
            links.append((slot, link, TEXT_CHANNEL_TYPE))
    return links


def _clear_posted(conn: Any, channel_id: str, message_id: str) -> bool:
    """Forgets a deleted or missing message wherever it is stored, as a card's message or the board's. Message ids are
    unique, so at most one of the two matches. Returns whether one was forgotten."""
    if cards.clear_message_if(conn, channel_id, message_id) is not None:
        return True
    return board.clear_message_if(conn, channel_id, message_id)


def _reconcile_failed(card_id: int, exc: Exception) -> None:
    log_event(log, "card_reconcile_failed", level=logging.WARNING, card_id=card_id, reason=type(exc).__name__)
