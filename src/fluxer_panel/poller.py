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
from typing import TYPE_CHECKING, Any

import aiohttp

from fluxer_client.events import MessageDelete
from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_panel import board, cards, notify, render, sessions, subscriptions, text, wake_requests
from fluxer_panel.card_commands import TEXT_CHANNEL_TYPE, VOICE_CHANNEL_TYPE, CardLocks, channel_name
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.games import GamesGuild
from fluxer_panel.logs import log_event
from fluxer_panel.resolver import CardStatus, next_action_text, resolve
from fluxer_panel.seeding import Seeder, has_play_now_target, has_session_channel, wanted_emoji
from fluxer_panel.sources.base import HostManager, PowerAction, PowerState, ProbeFailed, ProbeResult
from fluxer_panel.sources.registry import Sources
from fluxer_panel.wake import WakeRequests

if TYPE_CHECKING:
    from fluxer_panel.interactions import Interactions
    from fluxer_panel.session_jobs import SessionJobs

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

# The replaced boards whose delete failed and is still retried by each check. The oldest is dropped past this count.
_STALE_BOARD_CAP = 10


@dataclass(frozen=True)
class _Evaluated:
    """One card's outcome for this tick. `embed` is None when the evaluation failed: such a card is not published, and
    the board lists it with the dash, as a card with no status. `far_embed` is the same card with the next session's
    link left out, for a copy outside the games community."""
    card: cards.Card
    status: CardStatus | None
    embed: dict[str, Any] | None
    far_embed: dict[str, Any] | None


class Poller:
    def __init__(self, *, db: Database, rest: FluxerREST, sources: Sources, config: Config, locks: CardLocks,
                 clock: Callable[[], float] = time.time, games_guild: GamesGuild | None = None,
                 seeder: Seeder | None = None) -> None:
        self._db = db
        self._rest = rest
        self._sources = sources
        self._config = config
        self._locks = locks
        self._clock = clock
        self._seeder = seeder                                # None: no reactions are seeded
        # Each card's surfaces posted in the current publish: (channel, message, "card" or "copy", copy's community).
        # Filled under the card's lock, and taken by `_publish` once the lock is released, so seeding never holds it.
        self._to_seed: dict[int, list[tuple[str, str, str, str | None]]] = {}
        self._kick = asyncio.Event()
        self._schedules: dict[int, _ScheduleCache] = {}
        self._names_checked: dict[int, float] = {}
        self._statuses: dict[int, CardStatus | None] = {}   # each card's latest evaluated status, for wake requests
        self._requested: dict[int, int] = {}                 # each card's open wake request count, read per tick
        self._wake: WakeRequests | None = None               # set by set_wake, before the loop starts
        self._interactions: Interactions | None = None       # set by set_interactions, before the loop starts
        self._session_jobs: SessionJobs | None = None        # set by set_session_jobs, before the loop starts
        self._deleted: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._warned_no_channel = False
        self._games_guild = games_guild if games_guild is not None else GamesGuild(rest, config)
        # The board check. Startup arms it, so the first tick checks the board once.
        self._board_armed = True
        self._board_channel: str | None = None   # the stored board's channel, once read: a moved GAMES_CHANNEL_ID never moves it
        self._repost_due: float | None = None
        self._last_repost = float("-inf")
        self._stale_boards: list[tuple[str, str]] = []   # replaced boards whose delete failed, retried by each check

    def kick(self) -> None:
        self._kick.set()

    def set_wake(self, wake: WakeRequests) -> None:
        """Given the wake requests once, after both are built: the requests read this poller's statuses, so it comes first."""
        self._wake = wake

    def set_interactions(self, interactions: Interactions) -> None:
        """Given the interactions once, after both are built: each tick ends the play-now messages that are due."""
        self._interactions = interactions

    def set_session_jobs(self, jobs: SessionJobs) -> None:
        """Given the timed session jobs once, after both are built: each tick runs them, and their `due` shortens the wait."""
        self._session_jobs = jobs

    def on_games_message(self, channel_id: str) -> None:
        """Arms the board check when a message lands in the board's channel. It does not kick the loop: a kick runs a
        whole tick, and a busy channel would probe every server on every message. The check runs on the next scheduled
        tick, which `_wait_timeout` brings forward once a re-post is due."""
        if self._is_board_channel(channel_id):
            self._board_armed = True

    def status_of(self, card_id: int) -> CardStatus | None:
        """The card's status at its last evaluation, or None when it has none (never evaluated, or its evaluation failed)."""
        return self._statuses.get(card_id)

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
                await asyncio.wait_for(self._kick.wait(), self._wait_timeout())
            except TimeoutError:
                pass

    def _wait_timeout(self) -> float:
        """How long the run loop waits before its next tick: the poll interval, or less when a re-post is due sooner."""
        timeout = float(self._config.poll_interval_seconds)
        if self._repost_due is not None:
            timeout = min(timeout, max(0.0, self._repost_due - self._clock()))
        if self._session_jobs is not None and self._session_jobs.due is not None:
            timeout = min(timeout, max(0.0, self._session_jobs.due - self._clock()))
        return timeout

    async def reconcile(self) -> None:
        """Checks each stored message once. A message that is gone is forgotten, so the next tick posts a new one."""
        live = await self._db.run(lambda c: cards.list_cards(c, "live"))
        for card in live:
            if card.channel_id is None or card.message_id is None:
                continue
            try:
                message = await self._rest.get_message(card.channel_id, card.message_id)
            except FluxerHTTPError as exc:
                if exc.status == 404:
                    await self._forget_message(card.channel_id, card.message_id)
                else:
                    _reconcile_failed(card.id, exc)
            except Exception as exc:  # noqa: BLE001 - a failed lookup leaves this card alone; the others still run
                _reconcile_failed(card.id, exc)
            else:
                await self._seed_surface(card, card.channel_id, card.message_id, "card", None, message)
        for card in live:
            for sub in await self._subscriptions_of(card.id):
                if sub.message_id is not None:
                    await self._reconcile_copy(card, sub, sub.message_id)
        await self._reconcile_board()
        if self._interactions is not None:
            try:
                await self._interactions.reconcile_sessions()
            except Exception as exc:  # noqa: BLE001 - a session repair never stops the poller; the next start retries
                log_event(log, "session_reconcile_failed", level=logging.WARNING, reason=type(exc).__name__)

    async def _reconcile_copy(self, card: cards.Card, sub: subscriptions.Subscription, message_id: str) -> None:
        """Checks one stored copy, the way the card's message is checked, and seeds the reactions it is missing. A copy
        that is gone is forgotten."""
        try:
            message = await self._rest.get_message(sub.channel_id, message_id)
        except FluxerHTTPError as exc:
            if exc.status == 404:
                await self._forget_message(sub.channel_id, message_id)
            else:
                _reconcile_copy_failed(card.id, sub.channel_id, exc)
        except Exception as exc:  # noqa: BLE001 - a failed lookup keeps the stored copy
            _reconcile_copy_failed(card.id, sub.channel_id, exc)
        else:
            await self._seed_surface(card, sub.channel_id, message_id, "copy", sub.guild_id, message)

    async def _subscriptions_of(self, card_id: int) -> list[subscriptions.Subscription]:
        return await self._db.run(lambda c: subscriptions.list_for_card(c, card_id))

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
        """Keeps the cards and the board up to date. Each card is published as soon as its own evaluation ends, so one slow
        card does not hold back the others. The board is brought up to date after the cards, so a board posted here sits
        below them. When this tick's read of the stored board fails, the cards publish as they would with a board stored,
        and no new board is posted in this tick."""
        self._requested = await self._db.run(wake_requests.open_counts)
        await self._tick_cards()
        await self._after_tick()

    async def _tick_cards(self) -> None:
        """The card and board passes. Without a games channel there is no board, so only the cards are published."""
        live = await self._db.run(lambda c: cards.list_cards(c, "live"))
        self._forget_cards_not_in({card.id for card in live})
        sem = asyncio.Semaphore(self._config.poll_concurrency)
        channel_id = self._config.games_channel_id
        if channel_id is None:
            await asyncio.gather(*(self._evaluate_and_publish(card, sem) for card in live))
            return
        stored = await self._read_board()
        results = await asyncio.gather(*(self._evaluate_and_publish(card, sem) for card in live))
        await self._board_after_cards({result.card.id: result.status for result in results}, channel_id,
                                      may_post=stored is not None)
        await self._board_check()

    async def _after_tick(self) -> None:
        """The passes that run on every tick, with or without a games channel. A request that is ready or expired closes
        here. Closing kicks the poller, so the next loop renders the card without the suffix. Play-now messages that are
        due end here too (AC4.10), so they expire without a games channel as well."""
        if self._wake is not None:
            await self._wake.close_due()
        if self._interactions is not None:
            await self._interactions.expire_play_now()
        if self._session_jobs is not None:
            await self._session_jobs.run()

    def _forget_cards_not_in(self, live_ids: set[int]) -> None:
        """Drops the cached schedule, channel name check and status of every card not in `live_ids`, the live set of this
        tick. A card republished after such a tick fetches them again. A card republished before any tick saw it as
        non-live keeps its cache. A manager change is handled by the ManagerRef comparison in `_next_action`, not by
        this drop."""
        for entries in (self._schedules, self._names_checked, self._statuses):
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
                self._statuses[card.id] = None
                return _Evaluated(card, None, None, None)

    async def _publish_guarded(self, result: _Evaluated, sem: asyncio.Semaphore) -> None:
        async with sem:
            # A failed evaluation has no embed and is never published. This check does not rely on asserts.
            if result.embed is None or result.far_embed is None:
                return
            try:
                await self._publish(result.card.id, result.embed, result.far_embed)
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
        self._statuses[card.id] = res.status
        if (res.failing_since, res.alert_sent) != (card.runtime.probe_failing_since, card.runtime.probe_alert_sent):
            # Saved before the DM goes out: a crash or a failed DM can never send the alert a second time.
            await self._db.run(lambda c: cards.set_probe_tracking(c, card.id, res.failing_since, res.alert_sent))
        if res.alert_now:
            await self._alert(card)
        next_text = next_action_text(action, now_dt) if action is not None else None
        near, far = await self._db.run(lambda c: sessions.next_session_views(c, card, now))
        requested = self._requested.get(card.id, 0)
        embed = render.render_embed(card, res.status, next_text, instance_url=self._config.instance_url,
                                    requested=requested, next_session=near)
        # Without a next session the far rendering is the same embed, so no second one is built.
        far_embed = embed if near is None else render.render_embed(
            card, res.status, next_text, instance_url=self._config.instance_url, requested=requested, next_session=far)
        return _Evaluated(card, res.status, embed, far_embed)

    async def _refresh_names(self, card: cards.Card) -> cards.Card:
        """Re-reads the name of each verified channel link (every voice slot and every text channel slot), at most once
        per refresh interval. Returns the card to render."""
        links = _verified_links(card)
        if not links:
            return card
        now = self._clock()
        last = self._names_checked.get(card.id)
        if last is not None and now - last < self._config.schedule_refresh_seconds:
            return card
        self._names_checked[card.id] = now
        attempted = False
        for kind, slot, link, channel_type in links:
            attempted = await self._refresh_name(card.id, kind, slot, link, channel_type) or attempted
        if not attempted:
            return card
        # Reloaded after any write attempt, so this tick renders what the store holds now, not the links as read.
        return await self._db.run(lambda c: cards.get_by_id(c, card.id)) or card

    async def _refresh_name(self, card_id: int, kind: str, slot: int, link: cards.ChannelLink,
                            channel_type: int) -> bool:
        """Re-reads one link's channel and writes a changed name. `kind` is "voice" or "text_channel", and `slot` is
        that kind's slot. Returns whether a write was attempted."""
        try:
            channel = await self._rest.get_channel(link.channel_id)
        except Exception as exc:  # noqa: BLE001 - a failed name check keeps the stored name
            event = "voice_refresh_failed" if kind == "voice" else "text_channel_refresh_failed"
            log_event(log, event, level=logging.WARNING, card_id=card_id, reason=type(exc).__name__)
            return False
        name = channel_name(channel.get("name"))
        if name is None or name == link.channel_name:
            return False
        if channel.get("type") != channel_type or str(channel.get("guild_id")) != link.guild_id:
            return False
        # Only the name is written, and only while the link still matches the one read: an admin's clear or set that
        # landed during the channel lookup stands.
        if kind == "voice":
            await self._db.run(lambda c: cards.set_voice_name_if(c, card_id, link, name, slot=slot))
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
        await notify.dm_admins(self._rest, self._config, content, failed_event=failed_event)

    async def _read_board(self) -> board.BoardState | None:
        """The stored board state, read at the start of a tick. A failed read is logged and gives None: the cards then
        publish as they would with a board stored, and no new board is posted in this tick."""
        try:
            state = await self._db.run(board.get)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed read must not stop the cards of this tick
            log_event(log, "board_read_failed", level=logging.WARNING, reason=type(exc).__name__)
            return None
        self._board_channel = state.channel_id
        return state

    def _is_board_channel(self, channel_id: str) -> bool:
        """Whether a message in `channel_id` concerns the board: the stored board's channel, or the games channel when no
        board is stored (or none has been read yet)."""
        board_channel = self._board_channel if self._board_channel is not None else self._config.games_channel_id
        return channel_id == board_channel

    async def _board_check(self) -> None:
        """Runs at the end of each games-channel tick. A check is armed by a post in the board's channel, by a message
        there, and by startup. An armed check reads the newest message; when that isn't the board, a re-post is due
        `BOARD_REPOST_MIN_INTERVAL_SECONDS` after the last one, and a re-post already due is not moved. Once due, the
        re-post goes ahead. Each check also retries the deletes of replaced boards that failed."""
        now = self._clock()
        await self._retry_stale_deletes()
        if self._board_armed:
            self._board_armed = False
            state = await self._read_board()
            if state is None:
                self._board_armed = True          # the read failed: the next tick checks again
                return
            if state.message_id is None or state.channel_id is None:
                return                             # no board stored: the board post arms the next check
            try:
                newest = await self._rest.get_messages(state.channel_id, limit=1)
            except (FluxerHTTPError, aiohttp.ClientError, TimeoutError) as exc:
                log_event(log, "board_check_failed", level=logging.WARNING, channel_id=state.channel_id,
                          reason=type(exc).__name__)
                self._board_armed = True
                return
            if newest and str(newest[0].get("id")) != state.message_id and self._repost_due is None:
                self._repost_due = max(now, self._last_repost + self._config.board_repost_min_interval_seconds)
        if self._repost_due is not None and now >= self._repost_due:
            self._repost_due = None
            self._last_repost = now
            try:
                await self._board_repost()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the re-post is retried by the next check, never lost
                log_event(log, "board_repost_failed", level=logging.WARNING, reason=type(exc).__name__)
                self._board_armed = True          # the next tick checks again; the debounce still holds

    async def _board_repost(self) -> None:
        """Posts the board again in its channel, then deletes the one it replaces. The new board is stored before the old
        one goes, so a failed delete leaves the old message to be retried, never a second stored board."""
        state = await self._read_board()
        if state is None or state.message_id is None or state.channel_id is None:
            return
        live = await self._db.run(lambda c: cards.list_cards(c, "live"))
        entries = [(card, self._statuses[card.id]) for card in live if card.id in self._statuses]
        payload = await self._board_payload(entries, state.channel_id)
        old_id = state.message_id
        await self._board_post(state.channel_id, payload)
        if not await self._delete_old_board(state.channel_id, old_id):
            self._keep_stale_board(state.channel_id, old_id)

    def _keep_stale_board(self, channel_id: str, message_id: str) -> None:
        """Keeps a replaced board whose delete failed for the next check to retry. The list is capped: past
        `_STALE_BOARD_CAP` the oldest entry is dropped and logged."""
        self._stale_boards.append((channel_id, message_id))
        while len(self._stale_boards) > _STALE_BOARD_CAP:
            dropped_channel, _ = self._stale_boards.pop(0)
            log_event(log, "board_stale_dropped", level=logging.WARNING, channel_id=dropped_channel)

    async def _retry_stale_deletes(self) -> None:
        for channel_id, message_id in list(self._stale_boards):
            if await self._delete_old_board(channel_id, message_id):
                self._stale_boards.remove((channel_id, message_id))

    async def _delete_old_board(self, channel_id: str, message_id: str) -> bool:
        """Deletes a board message the poller replaced. The replacement is stored before this runs, so the old id matches
        no stored row and its delete event clears nothing and causes no post. Remembering the id before the delete is a
        backstop: it applies only if the event lands while the old id is still stored. A 404 means it is gone already. A
        403 is refused by Fluxer, and a retry gets the same answer, so it is logged once here and not kept. Returns
        whether no further attempt is needed: the message is gone, or it was refused."""
        self._remember_deleted(channel_id, message_id)
        try:
            await self._rest.delete_message(channel_id, message_id)
        except asyncio.CancelledError:
            raise
        except FluxerHTTPError as exc:
            if exc.status == 404:
                return True
            if exc.status == 403:
                log_event(log, "board_old_delete_refused", level=logging.WARNING, channel_id=channel_id,
                          reason=type(exc).__name__)
                return True
            log_event(log, "board_old_delete_failed", level=logging.WARNING, reason=type(exc).__name__)
            return False
        except Exception as exc:  # noqa: BLE001 - kept for the next check, which retries it
            log_event(log, "board_old_delete_failed", level=logging.WARNING, reason=type(exc).__name__)
            return False
        return True

    async def _board_after_cards(self, statuses: dict[int, CardStatus | None], games_channel_id: str, *,
                                 may_post: bool) -> None:
        """Brings the board up to date with the cards as they are now: posts it if none is stored, and edits it when its
        content changed. A stored board stays in the channel it was posted in, so its edits go there, whatever
        GAMES_CHANNEL_ID is now. Cards that were not evaluated this tick are left off until the next tick. `may_post` is
        False when this tick's read of the stored board failed: then no new board is posted, though an edit of a stored
        board still goes ahead."""
        try:
            live = await self._db.run(lambda c: cards.list_cards(c, "live"))
            state = await self._db.run(board.get)
            if state.message_id is None and not may_post:
                # This tick's first read failed, so this tick posts no board. The next tick reads again and posts one.
                log_event(log, "board_post_deferred", level=logging.WARNING)
                return
            entries = [(card, statuses[card.id]) for card in live if card.id in statuses]
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
        # Until a lookup answers, the board's links are plain text.
        guild_id = await self._games_guild.get(channel_id)
        return render.board_message(entries, games_channel_id=channel_id, games_guild_id=guild_id,
                                    instance_url=self._config.instance_url, requested=self._requested)

    async def _board_post(self, channel_id: str, payload: dict[str, Any]) -> None:
        sent = await self._rest.send_message(channel_id, embeds=payload["embeds"],
                                             allowed_mentions=payload["allowed_mentions"])
        message_id = sent["id"]
        self._board_channel = channel_id
        self._board_armed = True                # the new board is checked too: a message below it is noticed
        rendered = render.rendered_hash(payload)
        await self._db.run(lambda c: board.set_message(c, channel_id, message_id, rendered))
        if await self._undo_if_deleted(channel_id, message_id):
            return  # deleted before its id was stored: forgotten, so it is posted again
        log_event(log, "board_posted")

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

    async def _publish(self, card_id: int, embed: dict[str, Any], far_embed: dict[str, Any]) -> None:
        """Brings the card's own message up to date, then each active copy, all under the card's lock. A failure of the
        card's own message is logged and the copies still run, and a failure in one copy never stops the others. The card's
        message and a home copy get `embed`; a far copy gets `far_embed`."""
        rendered = render.rendered_hash(embed)
        payload = render.card_message(embed)
        far_rendered = render.rendered_hash(far_embed)
        far_payload = render.card_message(far_embed)
        fresh: cards.Card | None = None
        try:
            async with self._locks.get(card_id):
                fresh = await self._db.run(lambda c: cards.get_by_id(c, card_id))
                if fresh is not None and fresh.state == "live":
                    await self._publish_live(fresh, rendered, payload, far_rendered, far_payload)
        except asyncio.CancelledError:
            self._to_seed.pop(card_id, None)
            raise
        except Exception:
            # The surfaces posted before this failure still need their reactions. They are seeded here, and then the
            # failure goes on to `_publish_guarded`, which logs it.
            await self._seed_posted(card_id, fresh)
            raise
        await self._seed_posted(card_id, fresh)

    async def _publish_live(self, fresh: cards.Card, rendered: str, payload: dict[str, Any], far_rendered: str,
                            far_payload: dict[str, Any]) -> None:
        """Under the card's lock: the card's own message, then its copies. A failure of the card's own message is logged
        and the copies still run."""
        channel_id = self._config.games_channel_id
        if channel_id is None:
            if not self._warned_no_channel:
                self._warned_no_channel = True
                log_event(log, "games_channel_unset", level=logging.WARNING)
        else:
            try:
                await self._sync(fresh.id, post_channel=channel_id, edit_channel=fresh.channel_id,
                                 message_id=fresh.message_id, stored_hash=fresh.runtime.rendered_hash,
                                 rendered=rendered, payload=payload, subscription=False)
            except Exception as exc:  # noqa: BLE001 - logged here, and the copies below still run
                log_event(log, "card_poll_failed", level=logging.WARNING, card_id=fresh.id,
                          reason=type(exc).__name__)
        await self._publish_subscriptions(fresh.id, fresh.slug, rendered, payload, far_rendered, far_payload)

    async def _seed_posted(self, card_id: int, fresh: cards.Card | None) -> None:
        """Seeds the reactions of the surfaces this publish posted. It runs after the card's lock is released, so a 429
        retry in the seeding never stalls the card's next publish."""
        jobs = self._to_seed.pop(card_id, [])
        if fresh is None:
            return
        for channel_id, message_id, kind, guild_id in jobs:
            await self._seed_surface(fresh, channel_id, message_id, kind, guild_id)

    async def _seed_surface(self, card: cards.Card, channel_id: str, message_id: str, kind: str,
                            guild_id: str | None, message: dict[str, Any] | None = None) -> None:
        """Seeds the bot's reactions on one surface of a card: its card message ("card") or a copy ("copy"). Callers hold
        no card lock. It does nothing while seeding is off for the card, and `message` is a payload already fetched. It
        never raises: a failure, a database error included, is logged with the card id and the exception type, and the
        caller goes on to the next surface."""
        if self._seeder is None:
            return
        try:
            if not await self._seeder.enabled_for(card.id):
                return
            home = await self._is_home(kind, guild_id)
            play_now = has_play_now_target(card, self._config, surface_kind=kind, home=home)
            session = has_session_channel(card, self._config, surface_kind=kind, home=home)
            wanted = wanted_emoji(card, play_now_target=play_now, session_target=session)
            await self._seeder.seed(channel_id, message_id, wanted, message, card_id=card.id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one surface's failed seeding must not stop the others
            log_event(log, "seed_failed", level=logging.WARNING, card_id=card.id, reason=type(exc).__name__)

    async def _is_home(self, kind: str, guild_id: str | None) -> bool:
        """Whether the surface sits in the games community: every card message does, and a copy does when its community is
        the games channel's. The lookup runs only for a copy."""
        if kind == "card":
            return True
        return guild_id is not None and guild_id == await self._games_guild.get()

    async def _publish_subscriptions(self, card_id: int, slug: str, rendered: str, payload: dict[str, Any],
                                     far_rendered: str, far_payload: dict[str, Any]) -> None:
        """Brings each active copy up to date, in the order the channels were added. A home copy (in the games channel's
        community) gets the linked rendering and a far copy the unlinked one. A copy that fails is logged by ids and
        exception type and retried on the next tick. A 403 on a copy's post or edit pauses it, and so does a 404 on its
        post, which means the channel is gone. Each pause tells the admins once. A 404 on an edit reposts the copy."""
        subs = await self._db.run(lambda c: subscriptions.list_active(c, card_id))
        if not subs:
            return
        games_guild = await self._games_guild.get()
        for sub in subs:
            home = games_guild is not None and sub.guild_id == games_guild
            copy_rendered, copy_payload = (rendered, payload) if home else (far_rendered, far_payload)
            try:
                await self._sync(card_id, post_channel=sub.channel_id, edit_channel=sub.channel_id,
                                 message_id=sub.message_id, stored_hash=sub.content_hash, rendered=copy_rendered,
                                 payload=copy_payload, subscription=True, guild_id=sub.guild_id)
            except FluxerHTTPError as exc:
                if exc.status == 403:
                    await self._pause_subscription(card_id, slug, sub.channel_id, text.SUBSCRIPTION_PAUSED_ALERT)
                elif exc.status == 404:
                    await self._pause_subscription(card_id, slug, sub.channel_id,
                                                   text.SUBSCRIPTION_CHANNEL_GONE_ALERT)
                else:
                    _subscription_failed(card_id, sub.channel_id, exc)
            except Exception as exc:  # noqa: BLE001 - one copy's failure must not stop the other copies
                _subscription_failed(card_id, sub.channel_id, exc)

    async def _pause_subscription(self, card_id: int, slug: str, channel_id: str, alert: str) -> None:
        """Pauses a copy Fluxer refused, or whose channel is gone, and DMs the admins. The card lock is what makes the alert
        go out once: the copies are read and paused under it, so one tick pauses a copy at a time. The guard below is a
        backstop: the DM goes out only when this call changed the row. The DM names the card by its slug and the channel
        by its mention, nothing else."""
        # defensive: copies are read and paused under the card's lock, so a tick never sees this False
        if not await self._db.run(lambda c: subscriptions.pause(c, card_id, channel_id)):
            return
        log_event(log, "subscription_paused", level=logging.WARNING, card_id=card_id, channel_id=channel_id)
        await self._dm_admins(alert.format(slug=slug, channel_id=channel_id),
                              failed_event="subscription_alert_failed")

    async def _sync(self, card_id: int, *, post_channel: str, edit_channel: str | None, message_id: str | None,
                    stored_hash: str | None, rendered: str, payload: dict[str, Any], subscription: bool,
                    guild_id: str | None = None) -> None:
        """Posts the message when none is stored, and edits it when its rendering changed. The card's own message and each
        copy both go through here, so they share the post, the edit, the repost after a 404 and the conditional store.
        `guild_id` is a copy's community, and None for the card's own message."""
        if message_id is None or edit_channel is None:
            await self._post(card_id, post_channel, rendered, payload, subscription=subscription, guild_id=guild_id)
        elif rendered != stored_hash:
            await self._edit(card_id, edit_channel, message_id, rendered, payload, subscription=subscription,
                             guild_id=guild_id)

    async def _post(self, card_id: int, channel_id: str, rendered: str, payload: dict[str, Any], *,
                    subscription: bool = False, guild_id: str | None = None) -> None:
        sent = await self._rest.send_message(channel_id, embeds=payload["embeds"],
                                             allowed_mentions=payload["allowed_mentions"])
        message_id = sent["id"]
        if self._is_board_channel(channel_id):
            self._board_armed = True            # a card posted below the board: the next check looks at the newest message
        if subscription:
            await self._db.run(lambda c: subscriptions.set_message(c, card_id, channel_id, message_id, rendered))
        else:
            await self._db.run(lambda c: cards.set_message(c, card_id, channel_id, message_id, rendered))
        if await self._undo_if_deleted(channel_id, message_id):
            return                              # deleted before its id was stored: forgotten, so nothing to seed
        # Seeded by `_publish` after the card's lock is released. Posting makes no REST call for this.
        kind = "copy" if subscription else "card"
        self._to_seed.setdefault(card_id, []).append((channel_id, message_id, kind, guild_id))

    async def _edit(self, card_id: int, channel_id: str, message_id: str, rendered: str,
                    payload: dict[str, Any], *, subscription: bool = False, guild_id: str | None = None) -> None:
        try:
            await self._rest.edit_message(channel_id, message_id, embeds=payload["embeds"],
                                          allowed_mentions=payload["allowed_mentions"])
        except FluxerHTTPError as exc:
            if exc.status != 404:
                raise
            if subscription:
                log_event(log, "subscription_message_recreated", card_id=card_id, channel_id=channel_id)
            else:
                log_event(log, "card_message_recreated", card_id=card_id)
            await self._post(card_id, channel_id, rendered, payload, subscription=subscription, guild_id=guild_id)
            return
        # Conditional: a message deleted while the edit was in flight is not written back. A delete that commits
        # before this write leaves the card without the message, and one that commits after it clears the message,
        # so no undo is needed here.
        if subscription:
            await self._db.run(lambda c: subscriptions.set_hash_if(c, card_id, channel_id, message_id, rendered))
        else:
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


def _verified_links(card: cards.Card) -> list[tuple[str, int, cards.ChannelLink, int]]:
    """Each verified channel link in every voice slot and every text channel slot, with its kind, its slot and the
    channel type it must still have."""
    links: list[tuple[str, int, cards.ChannelLink, int]] = []
    for kind, slots, channel_type in (("voice", card.voices, VOICE_CHANNEL_TYPE),
                                      ("text_channel", card.text_channels, TEXT_CHANNEL_TYPE)):
        for slot, link in enumerate(slots, start=1):
            if link is not None and link.verified:
                links.append((kind, slot, link, channel_type))
    return links


def _clear_posted(conn: Any, channel_id: str, message_id: str) -> bool:
    """Forgets a deleted or missing message wherever it is stored: a card's message, a card's copy, or the board's.
    Message ids are unique, so at most one of them matches. Returns whether one was forgotten."""
    if cards.clear_message_if(conn, channel_id, message_id) is not None:
        return True
    if subscriptions.clear_message_if(conn, channel_id, message_id) is not None:
        return True
    return board.clear_message_if(conn, channel_id, message_id)


def _reconcile_failed(card_id: int, exc: Exception) -> None:
    log_event(log, "card_reconcile_failed", level=logging.WARNING, card_id=card_id, reason=type(exc).__name__)


def _reconcile_copy_failed(card_id: int, channel_id: str, exc: Exception) -> None:
    log_event(log, "subscription_reconcile_failed", level=logging.WARNING, card_id=card_id, channel_id=channel_id,
              reason=type(exc).__name__)


def _subscription_failed(card_id: int, channel_id: str, exc: Exception) -> None:
    log_event(log, "subscription_failed", level=logging.WARNING, card_id=card_id, channel_id=channel_id,
              reason=type(exc).__name__)
