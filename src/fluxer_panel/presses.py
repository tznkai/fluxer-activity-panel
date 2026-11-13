# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reactions as presses. This module is the only code that knows presses are reactions.

The emoji are written as escapes, never as literal characters: WAKE_EMOJI carries U+FE0F, which Fluxer stores on the
play reaction (docs/fluxer-notes.md, "Play emoji (U+25B6) stored name").

`Presses.on_reaction` takes a gateway reaction and decides whether it is a press or an RSVP. The order is fixed: the
filters, the RSVP route (state, never a press), the surface, the kill switch, the play-now me-too, the rate limits, the
handler, and last the removal of the user's reaction, through the channel's pacer. A removal is never a press.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol

from fluxer_client import events
from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_panel import board, cards, interaction_settings, notify, play_now, rsvp, sessions, subscriptions, text
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.logs import log_event
from fluxer_panel.pacer import ChannelPacer

log = logging.getLogger(__name__)

DAY = 86400

WAKE_EMOJI = "\u25b6\ufe0f"        # the play emoji, as the bot sends it and Fluxer stores it
FOLLOW_EMOJI = "\U0001F514"        # the bell
PLAY_NOW_EMOJI = "\U0001F64B"      # the person raising a hand
SESSION_EMOJI = "\U0001F4C5"       # the calendar: proposes a session
POLL_EMOJI = "\U0001F5F3\ufe0f"    # the ballot box: starts a time poll

SurfaceKind = Literal["card", "copy", "play_now", "session"]


class Action(Enum):
    WAKE = "wake"
    FOLLOW = "follow"
    PLAY_NOW = "play_now"
    SESSION = "session"
    POLL = "poll"


_BY_NAME = {"\u25b6": Action.WAKE, "\U0001F514": Action.FOLLOW, "\U0001F64B": Action.PLAY_NOW,
            "\U0001F4C5": Action.SESSION, "\U0001F5F3": Action.POLL}


def action_for(emoji: events.Emoji) -> Action | None:
    """The action a reaction's emoji stands for. Custom emoji never count, and U+FE0F is ignored."""
    if emoji.id is not None:
        return None
    return _BY_NAME.get(emoji.name.replace("\ufe0f", ""))


@dataclass(frozen=True)
class Surface:
    kind: SurfaceKind
    channel_id: str
    message_id: str
    guild_id: str | None
    home_community: bool   # the surface's community is the games channel's


@dataclass(frozen=True)
class Press:
    card_id: int
    action: Action
    user_id: str
    member_role_ids: frozenset[str] | None
    surface: Surface


class PressHandler(Protocol):
    async def handle(self, press: Press) -> None: ...
    async def me_too_changed(self, card_id: int, surface: Surface, user_id: str) -> None: ...
    async def rsvp_changed(self, session_id: int, user_id: str, choice: rsvp.Choice, added: bool) -> None: ...


@dataclass(frozen=True)
class _Lookup:
    card_id: int
    kind: SurfaceKind
    switched_on: bool   # the global switch is on and the card is not switched off
    degraded: bool      # the channel refuses the bot's removals


def _find_surface(conn, channel_id: str, message_id: str) -> tuple[int, SurfaceKind] | None:
    """The card and the kind of surface a message is, when it is a live card's, a live copy's or a live play-now post in
    this channel. The board's message is never a surface."""
    if board.get(conn).message_id == message_id:
        return None
    card = cards.get_by_message_id(conn, message_id)
    if card is not None:
        if card.state != "live" or card.channel_id != channel_id:
            return None
        return card.id, "card"
    sub = subscriptions.get_by_message_id(conn, message_id)
    if sub is not None:
        if sub.channel_id != channel_id or sub.paused:
            return None
        copied = cards.get_by_id(conn, sub.card_id)
        if copied is None or copied.state != "live":
            return None
        return copied.id, "copy"
    post = play_now.get_live_by_message(conn, message_id)
    if post is not None and post.channel_id == channel_id:
        owner = cards.get_by_id(conn, post.card_id)
        if owner is not None and owner.state == "live":
            return owner.id, "play_now"
    return None


def _lookup(conn, channel_id: str, message_id: str) -> _Lookup | None:
    found = _find_surface(conn, channel_id, message_id)
    if found is None:
        return None
    card_id, kind = found
    switched_on = interaction_settings.enabled(conn) and not interaction_settings.card_off(conn, card_id)
    return _Lookup(card_id=card_id, kind=kind, switched_on=switched_on,
                   degraded=interaction_settings.is_degraded(conn, channel_id))


def _find_session(conn, channel_id: str, message_id: str) -> int | None:
    """The id of the session whose message this is, when it is in this channel. The state is not checked here:
    `Interactions.rsvp_changed` reads it under the card's lock."""
    session = sessions.get_by_message(conn, message_id)
    if session is None or session.channel_id != channel_id:
        return None
    return session.id


class Presses:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, handler: PressHandler,
                 games_guild: GamesGuild, pacer: ChannelPacer, identity: BotIdentity,
                 clock: Callable[[], float] = time.time, backoff: DmBackoff | None = None) -> None:
        self._db = db
        self._rest = rest
        self._config = config
        self._handler = handler
        self._games_guild = games_guild
        self._pacer = pacer
        self._identity = identity   # identity.user_id is set by Bot on Ready
        self._clock = clock
        self._backoff = backoff     # the PRESS_FAILED reply obeys the DM backoff
        self._user_last: dict[tuple[str, int, Action], float] = {}   # when a user's press was last handled
        self._card_times: dict[int, deque[float]] = {}               # when each card's presses were last handled
        self._last_prune = float("-inf")                             # when the two maps above were last pruned

    async def on_reaction(self, event: events.ReactionAdd | events.ReactionRemove) -> None:
        # 1. The bot's own reactions, another bot's, and anything in a DM never reach the adapter.
        if event.guild_id is None or event.user_id == self._identity.user_id or event.user_is_bot:
            return
        # 1b. RSVP reactions on a session message are state, not presses: no kill switch, no rate limit, no removal.
        choice = rsvp.choice_for(event.emoji.name) if event.emoji.id is None else None
        if choice is not None:
            session_id = await self._db.run(lambda c: _find_session(c, event.channel_id, event.message_id))
            if session_id is not None:
                try:
                    await self._handler.rsvp_changed(session_id, event.user_id, choice,
                                                     isinstance(event, events.ReactionAdd))
                except Exception as exc:  # noqa: BLE001 - a failed RSVP is logged, never raised into the gateway
                    log_event(log, "rsvp_failed", level=logging.WARNING, session_id=session_id,
                              reason=type(exc).__name__)
            return
        # 2. Only the press emoji count.
        action = action_for(event.emoji)
        if action is None:
            return
        # 3 and 4. The surface, and the kill switch before any rate limit, in one read.
        found = await self._db.run(lambda c: _lookup(c, event.channel_id, event.message_id))
        if found is None or not found.switched_on:
            return
        # 5. Play-now's Me Too is a state change, not a press: it is never rate limited and never removed.
        me_too = found.kind == "play_now" and action is Action.PLAY_NOW
        if isinstance(event, events.ReactionRemove) and not me_too:
            return   # 6. Removals are never presses, in any channel
        surface = await self._surface(event, found.kind)
        if me_too:
            try:
                await self._handler.me_too_changed(found.card_id, surface, event.user_id)
            except Exception as exc:  # noqa: BLE001 - a failed me-too is logged, and never raised into the gateway
                log_event(log, "play_now_me_too_failed", level=logging.WARNING, card_id=found.card_id,
                          reason=type(exc).__name__)
            return
        # 7. Rate limits.
        now = self._clock()
        reason = self._admit(found.card_id, action, event.user_id, now)
        if reason is not None:
            log_event(log, "press_absorbed", card_id=found.card_id, action=action.value,
                      user_id=event.user_id, reason=reason)
        else:
            # 8. Handled. The log carries no user id.
            log_event(log, "press", event_type="MESSAGE_REACTION_ADD", card_id=found.card_id, action=action.value,
                      surface=surface.kind)
            press = Press(card_id=found.card_id, action=action, user_id=event.user_id,
                          member_role_ids=event.member_role_ids, surface=surface)
            try:
                await self._handler.handle(press)
            except Exception as exc:  # noqa: BLE001 - a failed press is told to the presser, and the removal still runs
                log_event(log, "press_failed", level=logging.WARNING, card_id=found.card_id, action=action.value,
                          kind=type(exc).__name__)
                await notify.dm_user(self._rest, event.user_id, text.PRESS_FAILED, backoff=self._backoff)
        # 9. Removal, so the button resets for the next tap.
        await self._remove(event, degraded=found.degraded, now=now)

    async def _surface(self, event: events.ReactionAdd | events.ReactionRemove, kind: SurfaceKind) -> Surface:
        games_guild_id = await self._games_guild.get()
        home = games_guild_id is not None and event.guild_id == games_guild_id
        return Surface(kind=kind, channel_id=event.channel_id, message_id=event.message_id,
                       guild_id=event.guild_id, home_community=home)

    def _admit(self, card_id: int, action: Action, user_id: str, now: float) -> str | None:
        """None when the press is handled, else the reason it is absorbed. Only a handled press moves the windows."""
        if now - self._last_prune >= self._config.press_user_window_seconds:
            self._prune(now)
        key = (user_id, card_id, action)
        last = self._user_last.get(key)
        if last is not None and now - last < self._config.press_user_window_seconds:
            return "user_window"
        times = self._card_times.get(card_id, deque())
        cutoff = now - self._config.press_card_window_seconds
        while times and times[0] <= cutoff:
            times.popleft()
        if not times:
            self._card_times.pop(card_id, None)
        if len(times) >= self._config.press_card_burst:
            return "card_burst"
        times.append(now)
        self._card_times[card_id] = times
        self._user_last[key] = now
        return None

    def _prune(self, now: float) -> None:
        """Drops every entry that can no longer change a decision: a user's last press outside the user window, and a
        card's presses outside the card window. A card left with none is dropped. Runs at most once per user window, so
        the cost is spread out and the maps hold only what the windows still need."""
        self._last_prune = now
        user_cutoff = now - self._config.press_user_window_seconds
        for key in [key for key, last in self._user_last.items() if last <= user_cutoff]:
            del self._user_last[key]
        card_cutoff = now - self._config.press_card_window_seconds
        for card_id in list(self._card_times):
            times = self._card_times[card_id]
            while times and times[0] <= card_cutoff:
                times.popleft()
            if not times:
                del self._card_times[card_id]

    async def _remove(self, event: events.ReactionAdd | events.ReactionRemove, *, degraded: bool, now: float) -> None:
        channel = event.channel_id
        if degraded:
            # A degraded channel is retried at most once a day. Until then no removal is attempted at all.
            claimed = await self._db.run(lambda c: interaction_settings.claim_retry(c, channel, int(now), DAY))
            if not claimed:
                return
            try:
                await self._delete_reaction(event)
            except FluxerHTTPError as exc:
                if exc.status == 404:
                    # A 404 means the reaction was already gone, which says nothing about permission. The retry is
                    # given back, so the next press may try again today.
                    await self._db.run(lambda c: interaction_settings.release_retry(c, channel, int(now), DAY))
                    log_event(log, "reaction_retry_inconclusive", channel_id=channel)
                    return
                self._log_removal_failure(channel, exc)
                return
            except Exception as exc:  # noqa: BLE001 - a failed retry leaves the channel degraded
                self._log_removal_failure(channel, exc)
                return
            await self._db.run(lambda c: interaction_settings.clear_degraded(c, channel))
            log_event(log, "reaction_degraded_cleared", channel_id=channel)
            return
        try:
            await self._delete_reaction(event)
        except FluxerHTTPError as exc:
            if exc.status == 403:
                await self._degrade(channel, now)
            else:
                self._log_removal_failure(channel, exc)
        except Exception as exc:  # noqa: BLE001 - one failed removal must not stop the next press
            self._log_removal_failure(channel, exc)

    async def _delete_reaction(self, event: events.ReactionAdd | events.ReactionRemove) -> None:
        await self._pacer.wait(event.channel_id)
        await self._rest.remove_reaction(event.channel_id, event.message_id, event.emoji.name, event.user_id)

    async def _degrade(self, channel: str, now: float) -> None:
        newly = await self._db.run(lambda c: interaction_settings.mark_degraded(c, channel, int(now)))
        if not newly:
            return   # already degraded: the admins heard once
        await notify.dm_admins(self._rest, self._config, text.REACTION_DEGRADED_ALERT.format(channel_id=channel),
                               failed_event="degraded_alert_failed")
        log_event(log, "reaction_degraded", level=logging.WARNING, channel_id=channel)

    @staticmethod
    def _log_removal_failure(channel: str, exc: Exception) -> None:
        if isinstance(exc, FluxerHTTPError):
            if exc.status == 404:
                return   # the reaction is already gone
            log_event(log, "reaction_remove_failed", level=logging.WARNING, channel_id=channel, status=exc.status)
            return
        log_event(log, "reaction_remove_failed", level=logging.WARNING, channel_id=channel,
                  reason=type(exc).__name__)
