# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The press handler used in production. `Presses` has already filtered, rate limited and logged each press, and
`Interactions` does what the press asks.

The play-now message goes out through `send_play_now`, the one place that sends it. It is refused before any REST call
when aimed at the games channel, and it mentions only the card's follow role, through `play_now_mentions`.
"""

from __future__ import annotations

import itertools
import logging
import time
from collections.abc import Callable
from typing import Any

from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import cards, follow_roles, notify, play_now, render, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import Card
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.follow_roles import Toggle
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.logs import log_event
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import PLAY_NOW_EMOJI, WAKE_EMOJI, Action, Press, Surface
from fluxer_panel.resolver import CardStatus, StatusKind
from fluxer_panel.seeding import Seeder
from fluxer_panel.wake import WakeRequests

log = logging.getLogger(__name__)

REACTOR_PAGE = 100   # the largest page the reactor list returns
MAX_REACTOR_PAGES = 20   # the most pages one count reads: at most 2000 reactors are counted


class GamesChannelRefused(Exception):
    """A play-now message was aimed at the games channel. Raised before any REST call (AC4.5)."""


def play_now_mentions(role_id: str | None) -> dict[str, Any]:
    if role_id is None:
        return {"parse": []}
    return {"parse": [], "roles": [role_id], "replied_user": False}   # ALLOWED_PLAY_NOW (see the Phase 1 note)


def _role_in(card: Card, guild_id: str) -> str | None:
    """The card's follow role when it belongs to `guild_id`. A role is never mentioned in another community."""
    follow = card.follow_role
    return follow.role_id if follow is not None and follow.guild_id == guild_id else None


def _failure_fields(exc: Exception) -> dict[str, Any]:
    """The loggable part of a failure: the HTTP status, or else the exception's type name. Never its message text."""
    if isinstance(exc, FluxerHTTPError):
        return {"status": exc.status}
    return {"reason": type(exc).__name__}


class Interactions:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, locks: CardLocks, games_guild: GamesGuild,
                 seeder: Seeder, pacer: ChannelPacer, identity: BotIdentity, roles: RoleCache,
                 clock: Callable[[], float] = time.time, kick: Callable[[], None] = lambda: None,
                 wake: WakeRequests | None = None,
                 status_of: Callable[[int], CardStatus | None] = lambda _id: None) -> None:
        self._db = db
        self._wake = wake
        self._rest = rest
        self._config = config
        self._locks = locks
        self._games_guild = games_guild
        self._seeder = seeder
        self._pacer = pacer   # the channel budget for reaction calls: shared with the seeder and the presses
        self._identity = identity
        self._roles = roles
        self._clock = clock
        self._kick = kick
        self._status_of = status_of
        self._follow_deps = follow_roles.FollowDeps(db=db, rest=rest, config=config, roles=roles, identity=identity)
        self._follow_alerted: set[tuple[int, str]] = set()   # (card id, reason) whose admin alert was sent
        self._recount_tickets = itertools.count(1)           # one ticket per Me Too recount, taken before its reactor reads
        self._recount_applied: dict[str, int] = {}           # play-now message id -> ticket of the newest recount shown

    async def handle(self, press: Press) -> None:
        match press.action:
            case Action.WAKE:
                await self.wake(press)
            case Action.FOLLOW:
                await self.follow(press)
            case Action.PLAY_NOW:
                await self.play_now(press)

    async def wake(self, press: Press) -> None:
        """A wake press on any surface (card, copy or play-now post) wakes the card (AC4.8)."""
        if self._wake is None:
            return
        await self._wake.press(press.card_id, press.user_id)

    async def follow(self, press: Press) -> None:
        card = await self._db.run(lambda c: cards.get_by_id(c, press.card_id))
        if card is None or card.state != "live":
            return
        guild = await self._games_guild.get()
        if not press.surface.home_community:
            await self._follow_elsewhere(press.user_id, card, guild)
            return
        if guild is None:   # no games community to toggle in: nothing is changed
            await self._dm_presser(press.user_id, text.FOLLOW_UNAVAILABLE, card)
            return
        async with self._locks.get(card.id):
            result = await follow_roles.toggle_follow(self._follow_deps, guild, press.user_id, card,
                                                      member_role_ids=press.member_role_ids)
        if result.outcome is Toggle.ADDED:
            await self._dm_presser(press.user_id, text.FOLLOW_ADDED, card)
        elif result.outcome is Toggle.REMOVED:
            await self._dm_presser(press.user_id, text.FOLLOW_REMOVED, card)
        else:
            await self._dm_presser(press.user_id, text.FOLLOW_UNAVAILABLE, card)
            # A card with no follow role was cleared on purpose: the press is unavailable, and nothing is wrong to report.
            cleared = result.reason == "not_allowlisted" and card.follow_role is None
            if result.reason is not None and result.reason != "no_bot_id" and not cleared:
                await self._alert_admins_once(card, result.reason)

    async def _follow_elsewhere(self, user_id: str, card: Card, guild: str | None) -> None:
        """A copy outside the games community: no role call, only a DM that points at the card in the games channel."""
        title = render.escape(card.title)
        if self._config.instance_url and guild and card.channel_id and card.message_id:
            link = render.jump_link(text.JUMP_LABEL_CARD, self._config.instance_url, guild, card.channel_id,
                                    card.message_id)
            content = text.FOLLOW_ELSEWHERE.format(card=title, link=link)
        else:
            content = text.FOLLOW_ELSEWHERE_NO_LINK.format(card=title)
        await notify.dm_user(self._rest, user_id, content)

    async def _dm_presser(self, user_id: str, template: str, card: Card) -> None:
        await notify.dm_user(self._rest, user_id, template.format(card=render.escape(card.title)))

    async def _alert_admins_once(self, card: Card, reason: str) -> None:
        """One admin DM per card per reason. The mark is set before the send, so a failed DM is not retried by a press."""
        key = (card.id, reason)
        if key in self._follow_alerted:
            return
        self._follow_alerted.add(key)
        content = text.FOLLOW_ADMIN_ALERT.format(slug=card.slug, reason=text.FOLLOW_REASON[reason])
        await notify.dm_admins(self._rest, self._config, content, failed_event="admin_alert_failed")

    async def play_now(self, press: Press) -> None:
        card = await self._db.run(lambda c: cards.get_by_id(c, press.card_id))
        if card is None or card.state != "live":
            return
        if not press.surface.home_community:
            await notify.dm_user(self._rest, press.user_id, text.PLAY_NOW_ELSEWHERE)
            return
        target = await self._play_now_target(press, card)
        if target is None:
            return
        channel_id, guild_id = target
        role_id = _role_in(card, guild_id)
        status = self._status_of(card.id)
        offers_wake = status is not None and status.kind is StatusKind.ASLEEP
        wanted = [PLAY_NOW_EMOJI] + ([WAKE_EMOJI] if offers_wake else [])
        now = int(self._clock())
        cooling = None
        message_id = None
        try:
            # The cooldown check and the post share the card's lock, so two presses cannot both post (AC4.7).
            async with self._locks.get(card.id):
                last = await self._db.run(lambda c: play_now.latest_for_card(c, card.id))
                if last is not None and now - last.created_at < self._config.play_now_cooldown_seconds:
                    cooling = last
                else:
                    message_id = await self._send_and_store(card, channel_id, guild_id, role_id, press.user_id,
                                                            offers_wake=offers_wake, now=now)
        except GamesChannelRefused:
            log_event(log, "play_now_refused", level=logging.WARNING, card_id=card.id, reason="games_channel")
            return
        except FluxerHTTPError as exc:
            log_event(log, "play_now_failed", level=logging.WARNING, card_id=card.id, channel_id=channel_id,
                      status=exc.status)
            await self._dm_presser(press.user_id, text.PLAY_NOW_UNAVAILABLE, card)
            return
        if cooling is not None:
            await self._dm_cooldown(press.user_id, card, cooling)
            return
        assert message_id is not None   # a cooled-down press returned above, and a post either stores its id or raises
        await self._seeder.seed(channel_id, message_id, wanted, card_id=card.id)   # outside the card lock
        log_event(log, "play_now_posted", card_id=card.id, channel_id=channel_id)

    async def _play_now_target(self, press: Press, card: Card) -> tuple[str, str] | None:
        """(channel id, community id) for the play-now message, or None after the presser has been told why not. The
        order is fixed: a copy at home, then the card's own verified channel, then PLAY_NOW_CHANNEL_ID."""
        if press.surface.kind == "copy" and press.surface.guild_id is not None:
            return press.surface.channel_id, press.surface.guild_id
        own = card.play_now_channel
        if own is not None and own.verified:
            return own.channel_id, own.guild_id
        configured = self._config.play_now_channel_id
        if configured is None:
            await self._dm_presser(press.user_id, text.PLAY_NOW_NO_CHANNEL, card)
            return None
        try:
            channel = await self._rest.get_channel(configured)
        except Exception as exc:  # noqa: BLE001 - an unreachable channel is refused by DM, never raised
            log_event(log, "play_now_failed", level=logging.WARNING, channel_id=configured,
                      reason=type(exc).__name__)
            await self._dm_presser(press.user_id, text.PLAY_NOW_UNAVAILABLE, card)
            return None
        guild_id = channel.get("guild_id")
        if not is_snowflake(guild_id):
            await self._dm_presser(press.user_id, text.PLAY_NOW_UNAVAILABLE, card)
            return None
        return configured, guild_id

    async def _send_and_store(self, card: Card, channel_id: str, guild_id: str, role_id: str | None, presser_id: str,
                              *, offers_wake: bool, now: int) -> str:
        """Sends the play-now message and stores its row. Runs under the card's lock. Returns the new message's id."""
        payload = render.play_now_message(card, role_id=role_id, playing=1, ended=False,
                                          instance_url=self._config.instance_url)
        sent = await self.send_play_now(channel_id, payload, role_id)
        message_id = str(sent["id"])
        expires_at = now + self._config.play_now_expiry_seconds
        try:
            await self._db.run(lambda c: play_now.add(c, card_id=card.id, guild_id=guild_id, channel_id=channel_id,
                                                      message_id=message_id, presser_id=presser_id,
                                                      offers_wake=offers_wake, now=now, expires_at=expires_at))
        except Exception:
            # With no row the message would never expire, so it is deleted; the press then fails as any other press does.
            log_event(log, "play_now_store_failed", level=logging.WARNING, card_id=card.id)
            await self._discard_unstored(channel_id, message_id, card.id)
            raise
        return message_id

    async def _discard_unstored(self, channel_id: str, message_id: str, card_id: int) -> None:
        """Deletes a play-now message whose row could not be stored. Its reactions and buttons would otherwise stay up
        on a message that nothing ever ends."""
        try:
            await self._rest.delete_message(channel_id, message_id)
        except Exception as exc:  # noqa: BLE001 - a failed delete is logged; the press is still told it failed
            log_event(log, "play_now_discard_failed", level=logging.WARNING, card_id=card_id, **_failure_fields(exc))

    async def send_play_now(self, channel_id: str, payload: dict[str, Any], role_id: str | None) -> dict[str, Any]:
        """The one send of a play-now message. A send aimed at the games channel is refused before the REST call."""
        if channel_id == self._config.games_channel_id:
            raise GamesChannelRefused
        return await self._rest.send_message(channel_id, content=payload["content"], embeds=payload["embeds"],
                                             allowed_mentions=play_now_mentions(role_id))

    async def _dm_cooldown(self, user_id: str, card: Card, last: play_now.PlayNow) -> None:
        title = render.escape(card.title)
        if self._config.instance_url is None:
            content = text.PLAY_NOW_COOLDOWN_NO_LINK.format(card=title)
        else:
            link = render.jump_link(text.JUMP_LABEL_MESSAGE, self._config.instance_url, last.guild_id,
                                    last.channel_id, last.message_id)
            content = text.PLAY_NOW_COOLDOWN.format(card=title, link=link)
        await notify.dm_user(self._rest, user_id, content)

    async def me_too_changed(self, card_id: int, surface: Surface, user_id: str) -> None:
        """A play-now reaction (PLAY_NOW_EMOJI) was added or removed on a live play-now message: the message's count is
        recounted and edited (AC4.9).
        The reactor pages are read outside the card lock, so a 429 retry never stalls the card. An edit never re-pings.
        Each recount takes a ticket before it reads, and an edit is skipped when a newer recount's count is already on the
        message. Overlapping recounts can therefore finish in any order, and the newest count still stands.
        `user_id` is not needed: the count is read from the message, not from the event."""
        pn = await self._db.run(lambda c: play_now.get_live_by_message(c, surface.message_id))
        if pn is None:
            return   # ended (AC4.10), or not a play-now message
        ticket = next(self._recount_tickets)
        try:
            count = await self._count_playing(pn)
        except Exception as exc:  # noqa: BLE001 - a failed count leaves the message as it is
            log_event(log, "play_now_count_failed", level=logging.WARNING, card_id=card_id,
                      **_failure_fields(exc))
            return
        async with self._locks.get(card_id):
            live = await self._db.run(lambda c: play_now.get_live_by_message(c, pn.message_id))
            card = await self._db.run(lambda c: cards.get_by_id(c, card_id))
            if live is None or card is None:
                return
            if ticket < self._recount_applied.get(pn.message_id, 0):
                return   # a newer recount has already been shown: this older count must not replace it
            try:
                await self._edit_play_now(card, pn, playing=count, ended=False)
            except Exception as exc:  # noqa: BLE001 - a failed edit leaves the message as it is, and is logged
                log_event(log, "play_now_edit_failed", level=logging.WARNING, card_id=card_id, **_failure_fields(exc))
                return
            self._recount_applied[pn.message_id] = ticket
        log_event(log, "play_now_count", card_id=card_id, count=count)

    async def _count_playing(self, pn: play_now.PlayNow) -> int:
        """The people on the message's play-now reaction: the distinct non-bot reactors plus the presser. Each page waits
        for the channel's budget. Pages follow `has_more` and `next_after` (the last id when `next_after` is absent). The
        count stops at the last page, when a page adds no new user, when the cursor does not advance, or after
        MAX_REACTOR_PAGES pages. The last two are logged, and a capped count is a floor. The reactor ids are read here and
        kept nowhere: not stored, not logged."""
        reactors: set[str] = set()
        seen: set[str] = set()
        after: str | None = None
        for _ in range(MAX_REACTOR_PAGES):
            await self._pacer.wait(pn.channel_id)
            page = await self._rest.get_reaction_users_page(pn.channel_id, pn.message_id, PLAY_NOW_EMOJI,
                                                            limit=REACTOR_PAGE, after=after)
            before = len(seen)
            for user in page.users:
                seen.add(user["id"])
                if user.get("bot") is not True and user["id"] != self._identity.user_id:
                    reactors.add(user["id"])
            count = len(reactors | {pn.presser_id})
            if not page.has_more:
                return count
            cursor = page.next_after or (page.users[-1]["id"] if page.users else None)
            if cursor is None or cursor == after or len(seen) == before:
                log_event(log, "play_now_count_stalled", level=logging.WARNING, card_id=pn.card_id)
                return count
            after = cursor
        log_event(log, "play_now_count_truncated", level=logging.WARNING, card_id=pn.card_id)
        return len(reactors | {pn.presser_id})

    async def _count_for_end(self, pn: play_now.PlayNow) -> int | None:
        """The count an ended message shows, or None when it cannot be read: the count line is then left out."""
        try:
            return await self._count_playing(pn)
        except Exception as exc:  # noqa: BLE001 - a failed recount omits the count rather than showing a guess
            log_event(log, "play_now_count_failed", level=logging.WARNING, card_id=pn.card_id,
                      **_failure_fields(exc))
            return None

    async def expire_play_now(self) -> None:
        """Ends each play-now message whose expiry has passed (AC4.10): one recount, one edit to the ended wording, then one
        bulk removal of its reactions. The recount runs before the card's lock, so the reactor reads never hold it. Each
        message is expired once, under its card's lock, and its removal runs after the lock is released. A failure on one
        message is logged, and the other due messages still end."""
        now = int(self._clock())
        for pn in await self._db.run(lambda c: play_now.due(c, now)):
            expired = False
            try:
                playing = await self._count_for_end(pn)
                async with self._locks.get(pn.card_id):
                    expired = await self._expire(pn.id)
                    if expired:
                        self._recount_applied.pop(pn.message_id, None)   # an ended message keeps no recount ticket
                        await self._end_message(pn, playing)
            except Exception as exc:  # noqa: BLE001 - one message's failure must not stop the others
                log_event(log, "play_now_expire_failed", level=logging.WARNING, card_id=pn.card_id,
                          **_failure_fields(exc))
            if expired:
                await self._remove_all_reactions(pn)

    async def _expire(self, play_now_id: int) -> bool:
        """Expires one row if it is still live. Returns whether this call expired it."""
        return await self._db.run(lambda c: play_now.expire_if(c, play_now_id))

    async def _end_message(self, pn: play_now.PlayNow, playing: int | None) -> None:
        """Edits an expiring message to the ended wording, with the count read just before the lock. Runs under the lock."""
        card = await self._db.run(lambda c: cards.get_by_id(c, pn.card_id))
        if card is None:
            return
        try:
            await self._edit_play_now(card, pn, playing=playing, ended=True)
        except Exception as exc:  # noqa: BLE001 - a failed edit is logged; the reactions are removed all the same
            log_event(log, "play_now_end_failed", level=logging.WARNING, card_id=pn.card_id, channel_id=pn.channel_id,
                      **_failure_fields(exc))

    async def _remove_all_reactions(self, pn: play_now.PlayNow) -> None:
        await self._pacer.wait(pn.channel_id)
        try:
            await self._rest.remove_all_reactions(pn.channel_id, pn.message_id)
        except FluxerHTTPError as exc:
            log_event(log, "play_now_reactions_not_removed", level=logging.WARNING, channel_id=pn.channel_id,
                      status=exc.status)
        except Exception as exc:  # noqa: BLE001 - one message's failed removal must not stop the others
            log_event(log, "play_now_reactions_not_removed", level=logging.WARNING, channel_id=pn.channel_id,
                      reason=type(exc).__name__)

    async def _edit_play_now(self, card: Card, pn: play_now.PlayNow, *, playing: int | None, ended: bool) -> None:
        """Rewrites the message from the card's current render. The edit allows no mention, so it never re-pings."""
        role_id = None if ended else _role_in(card, pn.guild_id)
        payload = render.play_now_message(card, role_id=role_id, playing=playing, ended=ended,
                                          instance_url=self._config.instance_url)
        await self._rest.edit_message(pn.channel_id, pn.message_id, content=payload["content"],
                                      embeds=payload["embeds"], allowed_mentions={"parse": []})
