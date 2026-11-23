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
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_client.roles import RoleCache
from fluxer_panel import cards, follow_roles, notify, play_now, render, rsvp, scope, sessions, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import Card
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.follow_roles import Toggle
from fluxer_panel.games import BotIdentity, GamesGuild
from fluxer_panel.logs import log_event
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.presses import PLAY_NOW_EMOJI, WAKE_EMOJI, Action, Press, Surface
from fluxer_panel.resolver import CardStatus, StatusKind
from fluxer_panel.seeding import Seeder
from fluxer_panel.wake import WakeRequests

if TYPE_CHECKING:   # type-only: session_post imports GamesChannelRefused from this module
    from fluxer_panel.poll_flow import PollFlow
    from fluxer_panel.session_flow import SessionFlow
    from fluxer_panel.session_post import SessionPost
    from fluxer_panel.sessions import Session

log = logging.getLogger(__name__)

REACTOR_PAGE = 100   # the largest page the reactor list returns
MAX_REACTOR_PAGES = 20   # the most pages one count reads: at most 2000 reactors are counted
# The RSVP reactions the startup reconcile reads, with the choice each one stands for.
_RSVP_READINGS: tuple[tuple[str, rsvp.Choice], ...] = (
    (rsvp.GOING_EMOJI, "going"), (rsvp.MAYBE_EMOJI, "maybe"), (rsvp.NO_EMOJI, "no"))


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


async def card_target(rest: FluxerREST, config: Config, card: Card, *,
                      failed_event: str = "play_now_failed") -> tuple[tuple[str, str] | None, str | None]:
    """The card's home target channel, as scope.checked_target gives it. Kept for the session and poll flows, which move
    to scopes in a later phase."""
    return await scope.checked_target(rest, config, card, scope.home_scope(card, None), failed_event=failed_event)


def target_channel_id(card: Card, config: Config) -> str | None:
    """The home target channel, read with no REST call (see scope.scope_target)."""
    return scope.scope_target(card, config, scope.home_scope(card, None))


class Interactions:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, locks: CardLocks, games_guild: GamesGuild,
                 seeder: Seeder, pacer: ChannelPacer, identity: BotIdentity, roles: RoleCache,
                 clock: Callable[[], float] = time.time, kick: Callable[[], None] = lambda: None,
                 wake: WakeRequests | None = None,
                 status_of: Callable[[int], CardStatus | None] = lambda _id: None,
                 session_post: SessionPost | None = None, backoff: DmBackoff | None = None,
                 session_flow: SessionFlow | None = None, poll_flow: PollFlow | None = None) -> None:
        self._db = db
        self._wake = wake
        self._session_flow = session_flow   # the calendar's DM flow: a 📅 press starts a draft there
        self._poll_flow = poll_flow         # the poll flow: a 🗳️ press starts a poll there
        self._backoff = backoff   # the press replies' DM backoff (dm_user checks it)
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
        self._session_post = session_post
        self._session_tickets = itertools.count(1)           # one ticket per committed RSVP change
        self._session_latest: dict[int, int] = {}            # session id -> ticket of its newest committed RSVP change

    async def handle(self, press: Press) -> None:
        match press.action:
            case Action.WAKE:
                await self.wake(press)
            case Action.FOLLOW:
                await self.follow(press)
            case Action.PLAY_NOW:
                await self.play_now(press)
            case Action.SESSION:
                await self.session(press)
            case Action.POLL:
                await self.poll(press)

    async def session(self, press: Press) -> None:
        """A calendar press starts a session draft on a card or a home copy. A play-now post never plans a session, and
        without a flow nothing happens."""
        if press.surface.kind == "play_now" or self._session_flow is None:
            return
        await self._session_flow.start(press)

    async def poll(self, press: Press) -> None:
        """A 🗳️ press starts a poll on a card or a home copy. A play-now post never starts one, and without a flow nothing
        happens."""
        if press.surface.kind == "play_now" or self._poll_flow is None:
            return
        await self._poll_flow.start(press)

    async def poll_reaction(self, poll_id: int, user_id: str, emoji: str, channel_id: str, message_id: str) -> None:
        """A 📅 or 🔁 on a poll message. Not a press: the poll flow checks the presser and removes the reaction. Without a
        flow nothing happens."""
        if self._poll_flow is None:
            return
        await self._poll_flow.on_poll_reaction(poll_id, user_id, emoji, channel_id, message_id)

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
        resolved = await self._db.run(lambda c: scope.resolve_scope(c, card, press.surface, guild))
        if resolved is scope.ELSEWHERE:
            await self._follow_elsewhere(press.user_id, card, guild)
            return
        if resolved.sub is not None:
            await self._follow_scope(press, card, resolved)
            return
        if guild is None:   # no games community to toggle in: nothing is changed
            await self._dm_presser(press.user_id, text.FOLLOW_UNAVAILABLE, card)
            return
        async with self._locks.get(card.id):
            result = await follow_roles.toggle_follow(self._follow_deps, guild, press.user_id, card,
                                                      member_role_ids=press.member_role_ids)
        await self._follow_reply(press.user_id, card, result, sub_id=None)

    async def _follow_scope(self, press: Press, card: Card, resolved: scope.Scope) -> None:
        """A bell on a local or both copy: its subscription's role, in the subscription's community. With no stored role
        the press does nothing, and nothing is said."""
        if resolved.role is None or resolved.guild_id is None:
            log_event(log, "press_ignored", card_id=card.id, sub_id=resolved.sub_id, reason="no_role")
            return
        async with self._locks.get(card.id):
            result = await follow_roles.toggle_follow(self._follow_deps, resolved.guild_id, press.user_id, card,
                                                      member_role_ids=press.member_role_ids, sub_id=resolved.sub_id)
        await self._follow_reply(press.user_id, card, result, sub_id=resolved.sub_id)

    async def _follow_reply(self, user_id: str, card: Card, result: follow_roles.ToggleResult,
                            *, sub_id: int | None) -> None:
        if result.outcome is Toggle.ADDED:
            await self._dm_presser(user_id, text.FOLLOW_ADDED, card)
        elif result.outcome is Toggle.REMOVED:
            await self._dm_presser(user_id, text.FOLLOW_REMOVED, card)
        else:
            await self._dm_presser(user_id, text.FOLLOW_UNAVAILABLE, card)
            # A card with no follow role was cleared on purpose: the press is unavailable, and nothing is wrong to report.
            cleared = result.reason == "not_allowlisted" and card.follow_role is None and sub_id is None
            if result.reason is not None and result.reason != "no_bot_id" and not cleared:
                await self._alert_admins_once(card, result.reason, sub_id)

    async def _follow_elsewhere(self, user_id: str, card: Card, guild: str | None) -> None:
        """A copy outside the games community: no role call, only a DM that points at the card in the games channel."""
        title = render.escape(card.title)
        if self._config.instance_url and guild and card.channel_id and card.message_id:
            link = render.jump_link(text.JUMP_LABEL_CARD, self._config.instance_url, guild, card.channel_id,
                                    card.message_id)
            content = text.FOLLOW_ELSEWHERE.format(card=title, link=link)
        else:
            content = text.FOLLOW_ELSEWHERE_NO_LINK.format(card=title)
        await notify.dm_user(self._rest, user_id, content, backoff=self._backoff)

    async def _dm_presser(self, user_id: str, template: str, card: Card) -> None:
        await notify.dm_user(self._rest, user_id, template.format(card=render.escape(card.title)),
                             backoff=self._backoff)

    async def _alert_admins_once(self, card: Card, reason: str, sub_id: int | None) -> None:
        """One admin DM per card, subscription and reason, so a scope's failure never hides the home alert or the
        reverse. The mark is set before the send, so a failed DM is not retried by a press."""
        key = (card.id, sub_id, reason)
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
            await notify.dm_user(self._rest, press.user_id, text.PLAY_NOW_ELSEWHERE, backoff=self._backoff)
            return
        targets = await self._play_now_targets(press, card)
        if targets is None:
            return
        status = self._status_of(card.id)
        offers_wake = status is not None and status.kind is StatusKind.ASLEEP
        wanted = [PLAY_NOW_EMOJI] + ([WAKE_EMOJI] if offers_wake else [])
        now = int(self._clock())
        cooling = None
        posted: list[tuple[str, str]] = []   # (channel id, message id) of each message this press posted
        failures: list[Exception] = []
        # The cooldown check and the posts share the card's lock, so two presses cannot both post (AC4.7). One press
        # counts once for the cooldown, however many messages it posts.
        async with self._locks.get(card.id):
            last = await self._db.run(lambda c: play_now.latest_for_card(c, card.id))
            if last is not None and now - last.created_at < self._config.play_now_cooldown_seconds:
                cooling = last
            else:
                for channel_id, guild_id in targets:
                    try:
                        message_id = await self._send_and_store(card, channel_id, guild_id,
                                                                _role_in(card, guild_id), press.user_id,
                                                                offers_wake=offers_wake, now=now)
                    except Exception as exc:  # noqa: BLE001 - a failed target is logged; another target can still post
                        failures.append(exc)
                        self._log_target_failure(card.id, channel_id, exc)
                        continue
                    posted.append((channel_id, message_id))
        if cooling is not None:
            await self._dm_cooldown(press.user_id, card, cooling)
            return
        if not posted:
            await self._press_failed(press, card, failures)
            return
        for channel_id, message_id in posted:
            await self._seeder.seed(channel_id, message_id, wanted, card_id=card.id)   # outside the card lock
            log_event(log, "play_now_posted", card_id=card.id, channel_id=channel_id)

    def _log_target_failure(self, card_id: int, channel_id: str, exc: Exception) -> None:
        """Logs one target's failed send with ids and status or reason only. The other target is not affected."""
        if isinstance(exc, GamesChannelRefused):
            log_event(log, "play_now_refused", level=logging.WARNING, card_id=card_id, reason="games_channel")
        else:
            log_event(log, "play_now_failed", level=logging.WARNING, card_id=card_id, channel_id=channel_id,
                      **_failure_fields(exc))

    async def _press_failed(self, press: Press, card: Card, failures: list[Exception]) -> None:
        """A press that posted nothing. A refusal is silent, a failed send is told by DM, and any other failure is raised
        as it was before there were two targets."""
        if any(isinstance(exc, FluxerHTTPError) for exc in failures):
            await self._dm_presser(press.user_id, text.PLAY_NOW_UNAVAILABLE, card)
            return
        for exc in failures:
            if not isinstance(exc, GamesChannelRefused):
                raise exc

    async def _play_now_targets(self, press: Press, card: Card) -> list[tuple[str, str]] | None:
        """The (channel id, community id) pairs a press posts to, or None after the presser has been told why not.
        A press on the card posts once, in the card's target: its verified play-now channel, else PLAY_NOW_CHANNEL_ID.
        A press on a home copy posts in the copy's channel, and also in the card's target when that is another channel."""
        if press.surface.kind == "copy" and press.surface.guild_id is not None:
            copy_target = (press.surface.channel_id, press.surface.guild_id)
            # The target is looked up before the cooldown check, by design: a cooled-down copy press still costs one
            # channel lookup. Moving the lookup under the card lock would hold the lock across a REST call.
            card_target, _ = await self._card_target(card)
            if card_target is None or card_target[0] == copy_target[0]:
                return [copy_target]
            return [copy_target, card_target]
        card_target, reason = await self._card_target(card)
        if card_target is None:
            template = text.PLAY_NOW_NO_CHANNEL if reason == "no_channel" else text.PLAY_NOW_UNAVAILABLE
            await self._dm_presser(press.user_id, template, card)
            return None
        return [card_target]

    async def _card_target(self, card: Card) -> tuple[tuple[str, str] | None, str | None]:
        """The play-now target of a card (see `card_target`), logged as a play-now failure."""
        return await card_target(self._rest, self._config, card)

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
        await notify.dm_user(self._rest, user_id, content, backoff=self._backoff)

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
        """Ends each play-now message whose expiry has passed (AC4.10). The reactor counts are read before the card's
        lock, so no reactor read holds it. Each message is expired once, under its card's lock, and its reactions are
        removed after the lock is released. A failure on one message is logged, and the other due messages still end."""
        now = int(self._clock())
        for pn in await self._db.run(lambda c: play_now.due(c, now)):
            try:
                counts = await self.count_play_now([pn])
                async with self._locks.get(pn.card_id):
                    ended = await self.end_play_now_locked([pn], counts)
            except Exception as exc:  # noqa: BLE001 - one message's failure must not stop the others
                log_event(log, "play_now_expire_failed", level=logging.WARNING, card_id=pn.card_id,
                          **_failure_fields(exc))
                ended = []
            await self.clear_play_now_reactions(ended)

    async def count_play_now(self, rows: Iterable[play_now.PlayNow]) -> dict[int, int]:
        """The reactor counts that ended messages will show, keyed by play-now id. Call before taking the card lock. A
        row whose count cannot be read has no entry, and its ended message leaves the count line out."""
        counts: dict[int, int] = {}
        for pn in rows:
            playing = await self._count_for_end(pn)
            if playing is not None:
                counts[pn.id] = playing
        return counts

    async def end_play_now_locked(self, rows: Iterable[play_now.PlayNow],
                                  counts: Mapping[int, int]) -> list[play_now.PlayNow]:
        """Expires each row that is still live and edits its message to the ended wording, with the count from `counts`
        (no REST read here, so it is safe under the lock). Returns the rows this call expired: their reactions are
        removed by clear_play_now_reactions after the lock is released. The caller holds the card's lock."""
        ended: list[play_now.PlayNow] = []
        for pn in rows:
            try:
                if not await self._expire(pn.id):
                    continue
                ended.append(pn)
                self._recount_applied.pop(pn.message_id, None)   # an ended message keeps no recount ticket
                await self._end_message(pn, counts.get(pn.id))
            except Exception as exc:  # noqa: BLE001 - one row's failure must not stop the others
                log_event(log, "play_now_expire_failed", level=logging.WARNING, card_id=pn.card_id,
                          **_failure_fields(exc))
        return ended

    async def clear_play_now_reactions(self, rows: Iterable[play_now.PlayNow]) -> None:
        """Removes every reaction from each ended message. Call after the card's lock is released."""
        for pn in rows:
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

    async def rsvp_changed(self, session_id: int, user_id: str, choice: rsvp.Choice, added: bool) -> None:
        """An RSVP reaction was added or removed on a session message. The presence row and the states change in one
        transaction under the card's lock, and only while the session is upcoming (AC4.10). The message is then edited,
        and each promoted user is DMed. RSVPs bypass the kill switch and the rate limits, and never kick the poller: the
        card's count updates on its next scheduled tick. `user_id` is stored and never logged."""
        if self._session_post is None:
            return
        found = await self._db.run(lambda c: sessions.get(c, session_id))
        if found is None:
            return
        now = int(self._clock())

        def tx(c: Any) -> tuple[Session, Card, rsvp.Outcome] | None:
            s = sessions.get(c, session_id)
            if s is None or s.state != "upcoming":
                return None                                   # AC4.10: frozen once ended or cancelled
            card = cards.get_by_id(c, s.card_id)
            if card is None:
                return None                                   # checked before any write, so no half-change commits
            changed = (sessions.add_presence(c, s, user_id, choice, now) if added
                       else sessions.remove_presence(c, session_id, user_id, choice))
            if not changed:
                return None
            return s, card, sessions.recompute(c, s, sessions.effective_seats(s, card), now)

        async with self._locks.get(found.card_id):
            result = await self._db.run(tx)
            if result is None:
                return
            ticket = next(self._session_tickets)
            self._session_latest[session_id] = ticket
        session, card, outcome = result
        log_event(log, "rsvp_changed", session_id=session_id, card_id=session.card_id, action=choice,
                  outcome="added" if added else "removed", count=len(outcome.promoted))
        await self._edit_session_coalesced(session, ticket)
        await self._session_post.dm_promoted(session, card, outcome.promoted)

    async def _edit_session_coalesced(self, session: Session, ticket: int) -> None:
        """Edits the session message unless a newer RSVP change has already committed: that change's edit renders after
        this one and covers it. The ticket check only skips redundant edits. `edit_session` re-reads the session, its card
        and the counts under the edit lock, so the last PATCH always shows the newest state, a cancel included. It edits
        only the session's stored channel (AC10.3)."""
        if self._session_post is None or self._session_latest.get(session.id) != ticket:
            return
        try:
            await self._session_post.edit_session(session.id)
        except Exception as exc:  # noqa: BLE001 - a failed count edit is logged; the next change edits again
            log_event(log, "session_edit_failed", level=logging.WARNING, session_id=session.id,
                      **_failure_fields(exc))

    async def reconcile_sessions(self) -> None:
        """At startup, each upcoming session's stored RSVP rows are corrected from the reactions on its message, then
        recomputed. The reactor lists are read under the card lock, so a live RSVP change either lands before the reads
        or waits until the reconcile has committed. A session whose lists cannot all be read is skipped whole, so a
        partial read never deletes a row. An edit and the promotion DMs follow only when a row changed. A session that
        raises is logged, and the sessions after it are still reconciled. There is no kick: the poller's first tick
        follows its reconcile anyway."""
        if self._session_post is None:
            return
        upcoming = await self._db.run(sessions.all_upcoming)
        for listed in upcoming:
            try:
                await self._reconcile_session(listed)
            except Exception as exc:  # noqa: BLE001 - one session's failure must not stop the sessions after it
                log_event(log, "session_reconcile_failed", level=logging.WARNING, session_id=listed.id,
                          **_failure_fields(exc))

    async def _reconcile_session(self, listed: Session) -> None:
        async with self._locks.get(listed.card_id):
            try:
                readings = await self._read_reactions(listed)
            except _ReactorsUnreadable as exc:
                log_event(log, "session_reconcile_skipped", level=logging.WARNING, session_id=listed.id, **exc.fields)
                return
            now = int(self._clock())

            def tx(c: Any) -> tuple[Session, Card, rsvp.Outcome | None] | None:
                s = sessions.get(c, listed.id)
                if s is None or s.state != "upcoming":
                    return None                                   # only upcoming sessions are repaired
                card = cards.get_by_id(c, s.card_id)
                if card is None:
                    return None
                if not sessions.reconcile_presence(c, s, readings, now):
                    return s, card, None
                return s, card, sessions.recompute(c, s, sessions.effective_seats(s, card), now)

            result = await self._db.run(tx)
            if result is None:
                return
            session, card, outcome = result
            ticket = 0   # the real tickets start at 1, so 0 never matches a newer change
            if outcome is not None:
                ticket = next(self._session_tickets)
                self._session_latest[session.id] = ticket
        log_event(log, "session_reconciled", session_id=session.id, card_id=session.card_id,
                  outcome="changed" if outcome is not None else "unchanged")
        if outcome is None:
            return
        await self._edit_session_coalesced(session, ticket)
        await self._session_post.dm_promoted(session, card, outcome.promoted)

    async def _read_reactions(self, listed: Session) -> dict[rsvp.Choice, set[str]]:
        """The reactors on each RSVP reaction of the session's message. Raises _ReactorsUnreadable if any list is not read in
        full, so the caller can leave every row alone."""
        readings: dict[rsvp.Choice, set[str]] = {}
        for emoji, choice in _RSVP_READINGS:
            readings[choice] = await self._reactor_ids(listed.channel_id, listed.message_id, emoji)
        return readings

    async def _reactor_ids(self, channel_id: str, message_id: str, emoji: str) -> set[str]:
        """The non-bot users on one reaction of a message, paged the way `_count_playing` pages them: each page waits for
        the channel's budget, pages follow `has_more` and `next_after`, and the bot's own id is skipped. Raises
        _ReactorsUnreadable, with the reason for the skip log, when a page fails, an item is not a user with a snowflake
        id, the paging stalls, or it runs past MAX_REACTOR_PAGES, so the caller leaves the rows alone."""
        reactors: set[str] = set()
        seen: set[str] = set()
        after: str | None = None
        for _ in range(MAX_REACTOR_PAGES):
            await self._pacer.wait(channel_id)
            try:
                page = await self._rest.get_reaction_users_page(channel_id, message_id, emoji,
                                                                limit=REACTOR_PAGE, after=after)
            except Exception as exc:  # noqa: BLE001 - a failed page skips this session; the caller logs why
                raise _ReactorsUnreadable(_failure_fields(exc)) from None
            if any(not isinstance(user, dict) or not is_snowflake(user.get("id")) for user in page.users):
                raise _ReactorsUnreadable({"reason": "reactors_malformed"})
            before = len(seen)
            for user in page.users:
                seen.add(user["id"])
                if user.get("bot") is not True and user["id"] != self._identity.user_id:
                    reactors.add(user["id"])
            if not page.has_more:
                return reactors
            cursor = page.next_after or (page.users[-1]["id"] if page.users else None)
            if cursor is None or cursor == after or len(seen) == before:
                raise _ReactorsUnreadable({"reason": "reactors_stalled"})
            after = cursor
        raise _ReactorsUnreadable({"reason": "reactors_truncated"})


class _ReactorsUnreadable(Exception):
    """A reactor list that cannot be trusted, so its session is skipped whole. `fields` is the allowlisted reason for the
    skip log: the HTTP status, or the exception's type name, or a reactors_* reason key."""

    def __init__(self, fields: dict[str, Any]) -> None:
        super().__init__()
        self.fields = fields
