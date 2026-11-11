# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The calendar DM flow: a proposer presses the calendar on a card or a home copy, picks a time in a DM, confirms it
with the check, and the session is posted in the card's session channel.

One draft per user, in memory. A restart forgets every draft, and the user presses the calendar again. Each user has one
lock, held for every step that reads or changes that user's draft, so a second check or a late keycap finds the draft
already changed. A card's lock is taken only for the post and its store, and no DM is sent while it is held.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from fluxer_client import events
from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, notify, render, rsvp, session_time, sessions, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import Card
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.dm_router import NonAdminLimiter
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.interactions import card_target
from fluxer_panel.logs import log_event
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import Press
from fluxer_panel.session_post import SessionPost

log = logging.getLogger(__name__)

FLOW_TIMEOUT_SECONDS = 15 * 60
KEYCAPS: tuple[str, ...] = tuple(f"{n}️⃣" for n in range(1, 7))
_KEYCAP_INDEX = {k.replace("️", ""): i for i, k in enumerate(KEYCAPS)}


@dataclass
class Draft:
    card_id: int
    started_at: float
    dm_channel_id: str
    prompt_message_id: str
    slots: list[int]
    confirm_message_id: str | None = None
    candidate: int | None = None


def _local(now: float, zone: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(now, zone)


def _target_channel_id(card: Card, config: Config) -> str | None:
    """The channel a post goes to, read without any REST call: the card's verified channel, else PLAY_NOW_CHANNEL_ID."""
    own = card.play_now_channel
    if own is not None and own.verified:
        return own.channel_id
    return config.play_now_channel_id


class SessionFlow:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, locks: CardLocks,
                 session_post: SessionPost, games_guild: GamesGuild, pacer: ChannelPacer, identity: BotIdentity,
                 backoff: DmBackoff, limiter: NonAdminLimiter, clock: Callable[[], float] = time.time,
                 kick: Callable[[], None] = lambda: None) -> None:
        self._db = db
        self._rest = rest
        self._config = config
        self._locks = locks
        self._session_post = session_post
        self._games_guild = games_guild
        self._pacer = pacer
        self._identity = identity
        self._backoff = backoff
        self._limiter = limiter
        self._clock = clock
        self._kick = kick
        self._drafts: dict[str, Draft] = {}
        self._user_locks: dict[str, asyncio.Lock] = {}

    # Draft state -----------------------------------------------------------------------------------------

    def _lock(self, user_id: str) -> asyncio.Lock:
        return self._user_locks.setdefault(user_id, asyncio.Lock())

    def has_draft(self, user_id: str) -> bool:
        """Whether the user has an open draft. An expired draft counts until the expiry sweep takes it, and every
        action on it checks the expiry itself."""
        return user_id in self._drafts

    def next_expiry(self) -> float | None:
        """When the soonest open draft expires, or None when there is none. For the poller's next wake-up."""
        if not self._drafts:
            return None
        return min(draft.started_at for draft in self._drafts.values()) + FLOW_TIMEOUT_SECONDS

    def _expired(self, draft: Draft) -> bool:
        return self._clock() - draft.started_at >= FLOW_TIMEOUT_SECONDS

    async def _read_card(self, card_id: int) -> Card | None:
        return await self._db.run(lambda c: cards.get_by_id(c, card_id))

    async def _dm(self, user_id: str, content: str) -> None:
        await notify.dm_user(self._rest, user_id, content, backoff=self._backoff)

    # The calendar press -----------------------------------------------------------------------------------

    async def start(self, press: Press) -> None:
        """A calendar press: sends the prompt and opens a draft, or refuses with a DM. Replaces any draft the user has."""
        async with self._lock(press.user_id):
            opened = await self._open(press)
        if opened is None:
            return
        channel_id, message_id, count = opened
        await self._seed_keycaps(channel_id, message_id, count)
        log_event(log, "session_flow", outcome="started", card_id=press.card_id, count=count)

    async def _open(self, press: Press) -> tuple[str, str, int] | None:
        """Checks the press and sends the prompt, under the user's lock. Returns the prompt's DM channel, its message id
        and the slot count, or None after the refusal or the failed prompt has been handled."""
        user = press.user_id
        card = await self._read_card(press.card_id)
        if card is None or card.state != "live":
            return None
        if not press.surface.home_community:
            await self._elsewhere(user, card)
            return None
        if self._backoff.blocked(user):
            log_event(log, "session_flow", card_id=card.id, outcome="refused", reason="backoff")
            return None
        target, reason = await card_target(self._rest, self._config, card, failed_event="session_target_failed")
        if target is None:
            await self._dm(user, text.SESSION_NO_CHANNEL)
            log_event(log, "session_flow", card_id=card.id, outcome="refused", reason=reason)
            return None
        try:
            await self._db.run(lambda c: sessions.check_caps(c, card.id, user))
        except sessions.SessionCapReached as exc:
            await self._dm(user, text.SESSION_CAP[exc.kind].format(card=render.escape(card.title)))
            log_event(log, "session_flow", card_id=card.id, outcome="refused", reason=f"cap_{exc.kind}")
            return None
        now = self._clock()
        zone = self._config.session_time_zone
        slots = [int(when.timestamp()) for when in session_time.offered_slots(
            now=_local(now, zone), tz=zone, weekday=list(self._config.session_slots_weekday),
            weekend=list(self._config.session_slots_weekend))]
        self._drafts.pop(user, None)   # the old prompt's keycaps match nothing from now on (a second press replaces)
        lines = [text.SESSION_PROMPT.format(card=render.escape(card.title))]
        lines += [text.SESSION_PROMPT_SLOT.format(key=KEYCAPS[i], ts=ts) for i, ts in enumerate(slots)]
        sent = await notify.dm_user(self._rest, user, "\n".join(lines), backoff=self._backoff)
        if sent is None:
            return None   # the failed DM has started the backoff; nothing else happens
        channel_id, message_id = sent
        if not (is_snowflake(channel_id) and is_snowflake(message_id)):
            log_event(log, "session_flow", card_id=card.id, outcome="refused", reason="bad_message_id")
            return None
        self._drafts[user] = Draft(card_id=card.id, started_at=now, dm_channel_id=channel_id,
                                   prompt_message_id=message_id, slots=slots)
        return channel_id, message_id, len(slots)

    async def _elsewhere(self, user_id: str, card: Card) -> None:
        """A press on a copy outside the games community: no draft, only a pointer to the card in the games channel."""
        guild = await self._games_guild.get()
        if self._config.instance_url and guild and card.channel_id and card.message_id:
            link = render.jump_link(text.JUMP_LABEL_CARD, self._config.instance_url, guild, card.channel_id,
                                    card.message_id)
            content = text.SESSION_ELSEWHERE.format(link=link)
        else:
            content = text.SESSION_ELSEWHERE_NO_LINK
        await self._dm(user_id, content)
        log_event(log, "session_flow", card_id=card.id, outcome="refused", reason="elsewhere")

    async def _seed_keycaps(self, channel_id: str, message_id: str, count: int) -> None:
        """Adds the keycaps of the slots to the prompt, one per slot. Runs with no lock held. A failed add is logged and
        the next keycap is still added."""
        for key in KEYCAPS[:count]:
            await self._pacer.wait(channel_id)
            try:
                await self._rest.add_reaction(channel_id, message_id, key)
            except Exception as exc:  # noqa: BLE001 - a missing keycap is logged; the user can still type a time
                log_event(log, "session_flow_seed_failed", level=logging.WARNING, reason=type(exc).__name__)

    # Replies to the prompt and the confirmation ---------------------------------------------------------

    async def on_dm_reaction(self, event: events.ReactionAdd | events.ReactionRemove) -> None:
        """A keycap on the prompt picks a slot; the check on the newest confirmation posts it. Removals, the bot's own
        reactions, custom emoji and users without a draft do nothing. Keycaps and checks share the non-admin budget."""
        if not isinstance(event, events.ReactionAdd) or event.user_is_bot:
            return
        user = event.user_id
        if user == self._identity.user_id or event.emoji.id is not None or user not in self._drafts:
            return
        if not self._limiter.admit(user):
            return
        async with self._lock(user):
            draft = self._drafts.get(user)
            if draft is None or self._expired(draft) or event.channel_id != draft.dm_channel_id:
                return
            name = event.emoji.name.replace("️", "")
            if event.message_id == draft.prompt_message_id:
                index = _KEYCAP_INDEX.get(name)
                if index is None or index >= len(draft.slots):
                    return
                slot = draft.slots[index]
                if slot == draft.candidate:
                    return   # the same keycap again: nothing new to confirm
                await self._confirm(user, draft, slot)
                return
            if (draft.confirm_message_id is not None and event.message_id == draft.confirm_message_id
                    and rsvp.choice_for(name) == "going"):
                await self._post(user, draft)

    async def on_reply(self, event: events.MessageCreate) -> None:
        """A typed time in the DM, while the user has a draft. A refused time gets the refusal and the example, and the
        draft stays open. The typed text is never logged."""
        user = event.author_id
        async with self._lock(user):
            draft = self._drafts.get(user)
            if draft is None or self._expired(draft) or event.channel_id != draft.dm_channel_id:
                return
            zone = self._config.session_time_zone
            try:
                when = session_time.parse(event.content, now=_local(self._clock(), zone), tz=zone)
            except session_time.TimeRefused as exc:
                await self._dm(user, text.SESSION_TIME_REFUSED[exc.reason] + " " + text.SESSION_TIME_EXAMPLE)
                log_event(log, "session_flow", card_id=draft.card_id, outcome="refused", reason=exc.reason)
                return
            await self._confirm(user, draft, int(when.timestamp()))

    async def _confirm(self, user_id: str, draft: Draft, slot: int) -> None:
        """Sends the confirmation for one time, with a conflict line when the proposer is already going to an overlapping
        session. Nothing is posted here. The newest confirmation replaces the older one. Runs under the user's lock."""
        card = await self._read_card(draft.card_id)
        if card is None or card.state != "live":
            self._drafts.pop(user_id, None)
            await self._dm(user_id, text.SESSION_POST_FAILED)
            return
        length = self._config.session_length_seconds
        conflicts = await self._db.run(lambda c: sessions.conflicts_for(c, user_id, slot, slot + length))
        content = text.SESSION_CONFIRM.format(card=render.escape(card.title), ts=slot)
        if conflicts:
            content += "\n" + text.SESSION_CONFLICT.format(card=render.escape(conflicts[0][1]))
        sent = await notify.dm_user(self._rest, user_id, content, backoff=self._backoff)
        if sent is None:
            return   # keep the draft as it is: the old confirmation, if any, still stands
        channel_id, message_id = sent
        if not is_snowflake(message_id) or not is_snowflake(channel_id):
            log_event(log, "session_flow", card_id=card.id, outcome="refused", reason="bad_message_id")
            return
        draft.candidate = slot
        draft.confirm_message_id = message_id
        await self._pacer.wait(channel_id)
        try:
            await self._rest.add_reaction(channel_id, message_id, rsvp.GOING_EMOJI)
        except Exception as exc:  # noqa: BLE001 - a missing check is logged; the user can still react with it
            log_event(log, "session_flow_seed_failed", level=logging.WARNING, reason=type(exc).__name__)

    # The post ----------------------------------------------------------------------------------------------

    async def _post(self, user_id: str, draft: Draft) -> None:
        """Posts the session for the confirmed time. The draft is popped first, under the user's lock, so a second check
        finds nothing to post. The card is re-read under its lock, and the store runs there too."""
        self._drafts.pop(user_id, None)
        card = await self._read_card(draft.card_id)
        if card is None or card.state != "live":
            await self._dm(user_id, text.SESSION_POST_FAILED)
            return
        slot = draft.candidate
        now = self._clock()
        if slot is None or slot <= now:
            await self._dm(user_id, text.SESSION_TIME_REFUSED["past"])
            return
        target, _ = await card_target(self._rest, self._config, card, failed_event="session_target_failed")
        if target is None:
            await self._dm(user_id, text.SESSION_NO_CHANNEL)
            return
        channel_id, guild_id = target
        outcome = await self._publish(card.id, user_id, channel_id, guild_id, slot, now)
        if outcome.message_id is None or outcome.session_id is None:
            await self._dm(user_id, outcome.reply or text.SESSION_POST_FAILED)
            return
        await self._finish(user_id, card, guild_id, channel_id, outcome.message_id, outcome.session_id)

    async def _publish(self, card_id: int, user_id: str, channel_id: str, guild_id: str, slot: int,
                       now: float) -> _Published:
        """Sends the message and stores its row, under the card's lock. Returns the stored ids, or the reply to DM after the
        lock is released, in the failure cases."""
        async with self._locks.get(card_id):
            fresh = await self._read_card(card_id)
            if fresh is None or fresh.state != "live" or _target_channel_id(fresh, self._config) != channel_id:
                return _Published(reply=text.SESSION_POST_FAILED)   # the card changed since the target was read
            try:
                await self._db.run(lambda c: sessions.check_caps(c, card_id, user_id))
            except sessions.SessionCapReached as exc:
                return _Published(reply=text.SESSION_CAP[exc.kind].format(card=render.escape(fresh.title)))
            length = self._config.session_length_seconds
            provisional = sessions.provisional(card_id=card_id, guild_id=guild_id, channel_id=channel_id,
                                               starts_at=slot, ends_at=slot + length, proposer_id=user_id,
                                               now=int(now))
            payload = render.session_message(
                fresh, provisional, rsvp.Counts(going=1, maybe=0, waiting=0),
                effective_location=sessions.effective_location(provisional, fresh),
                effective_seats=sessions.effective_seats(provisional, fresh),
                instance_url=self._config.instance_url, provisional=True)
            try:
                sent = await self._session_post.send_session(channel_id, payload)
            except Exception as exc:  # noqa: BLE001 - a failed post is told to the proposer; the cause is logged by type
                log_event(log, "session_post_failed", level=logging.WARNING, card_id=card_id,
                          reason=type(exc).__name__)
                return _Published(reply=text.SESSION_POST_FAILED)
            message_id = sent.get("id") if isinstance(sent, dict) else None
            if not is_snowflake(message_id):
                log_event(log, "session_post_failed", level=logging.WARNING, card_id=card_id,
                          reason="bad_message_id")
                return _Published(reply=text.SESSION_POST_FAILED)
            try:
                session_id = await self._db.run(lambda c: sessions.add(
                    c, card_id=card_id, guild_id=guild_id, channel_id=channel_id, message_id=message_id,
                    starts_at=slot, ends_at=slot + length, proposer_id=user_id, now=int(now)))
            except Exception as exc:  # noqa: BLE001 - with no row the message would never end: it is deleted
                await self._session_post.delete_unstored(channel_id, message_id)
                log_event(log, "session_store_failed", level=logging.WARNING, card_id=card_id,
                          reason=type(exc).__name__)
                if isinstance(exc, sessions.SessionCapReached):
                    return _Published(reply=text.SESSION_CAP[exc.kind].format(card=render.escape(fresh.title)))
                return _Published(reply=text.SESSION_POST_FAILED)
        return _Published(message_id=message_id, session_id=session_id)

    async def _finish(self, user_id: str, card: Card, guild_id: str, channel_id: str, message_id: str,
                      session_id: int) -> None:
        """After the lock: the three RSVP seeds, the footer edit, the proposer's jump link, and a poller kick."""
        try:
            await self._session_post.seed_session(channel_id, message_id)
        except Exception as exc:  # noqa: BLE001 - a missing seed is logged; users can still react
            log_event(log, "session_seed_failed", level=logging.WARNING, session_id=session_id,
                      reason=type(exc).__name__)
        try:
            await self._session_post.edit_session(session_id)
        except Exception as exc:  # noqa: BLE001 - the message keeps its counts from the last good edit
            log_event(log, "session_edit_failed", level=logging.WARNING, session_id=session_id,
                      reason=type(exc).__name__)
        if self._config.instance_url:
            link = render.jump_link(text.SESSION_LINK_LABEL, self._config.instance_url, guild_id, channel_id,
                                    message_id)
            content = text.SESSION_POSTED.format(link=link)
        else:
            content = text.SESSION_POSTED_NO_LINK
        await self._dm(user_id, content)
        self._kick()
        log_event(log, "session_flow", outcome="posted", card_id=card.id, session_id=session_id)

    # Expiry ------------------------------------------------------------------------------------------------

    async def expire_drafts(self) -> None:
        """Ends each draft whose 15 minutes have passed: the draft is popped under its user's lock, then the prompt is
        edited to say so. A later check or reply finds nothing."""
        for user_id in [uid for uid, draft in self._drafts.items() if self._expired(draft)]:
            async with self._lock(user_id):
                draft = self._drafts.get(user_id)
                if draft is None or not self._expired(draft):
                    continue
                del self._drafts[user_id]
            try:
                await self._rest.edit_message(draft.dm_channel_id, draft.prompt_message_id,
                                              content=text.SESSION_DRAFT_EXPIRED)
            except Exception as exc:  # noqa: BLE001 - a failed edit leaves the old prompt; nothing else is affected
                log_event(log, "session_flow_expire_failed", level=logging.WARNING, reason=type(exc).__name__)
            log_event(log, "session_flow", outcome="expired", card_id=draft.card_id)


@dataclass
class _Published:
    """The result of the locked send and store: either the stored ids, or the reply to DM once the lock is released."""
    message_id: str | None = None
    session_id: int | None = None
    reply: str | None = None
