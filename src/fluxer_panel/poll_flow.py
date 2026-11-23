# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The poll flow: 🗳️ starts a time poll for a card; typed DM replies vote in it.

All poll state is in the database. The card lock covers only the post and the insert; ballots and other DMs are sent
after it. Each refusal is logged as `poll_flow` with `outcome="refused"` and a reason, and names no user.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any

from fluxer_client.events import MessageCreate
from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerREST
from fluxer_panel import (
    admin, cards, notify, poll_count, poll_letters, poll_members, poll_reply, polls, render, scope, session_flow,
    subscriptions, text)
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import Card
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.games import GamesGuild
from fluxer_panel.logs import log_event
from fluxer_panel.mirror_post import MirrorPost
from fluxer_panel.poll_post import PollPost, mirror_payload, session_jump
from fluxer_panel.presses import RUNOFF_EMOJI, SESSION_EMOJI, Press

log = logging.getLogger(__name__)

MIN_RUNOFF_SECONDS = 900   # a runoff must have at least this long to run, or it is refused (it would close too soon)
# A runoff closes this much before its earliest candidate's usability boundary (one hour before it). The poller closes a
# due poll on the first tick at or after closes_at, which can be late, and PollCloser ranks usable options at the time
# it runs. The margin keeps the earliest candidate usable when a late tick closes the runoff.
RUNOFF_CLOSE_MARGIN = 600


@dataclass(frozen=True)
class _Voted:
    letters: tuple[str, ...]      # the voter's picks after the write, in pick order, as letters
    added: int                    # options this reply added


@dataclass(frozen=True)
class _Refused:
    reason: str                   # "closed", "not_recipient", "full", or a poll_reply / session_time refusal key
    index: int | None = None      # the 1-based item of a poll_reply refusal


class _Full(Exception):
    """Raised inside the vote transaction when the poll has no room for a typed time. db.run rolls back every option
    this reply had already added, and vote() answers POLL_FULL."""


_REFUSAL_TEXT = {"closed": text.POLL_REPLY_CLOSED, "not_recipient": text.POLL_NOT_RECIPIENT, "full": text.POLL_FULL}


def _vote_tx(conn, *, poll_id: int, user_id: str, content: str, now: datetime, tz) -> _Voted | _Refused:
    """One reply, parsed and written in the caller's transaction: the options it adds and the replaced picks commit
    together or not at all. The write is compare-and-set on the poll being open."""
    poll = polls.get(conn, poll_id)
    if poll is None or poll.state != "open":
        return _Refused("closed")
    if polls.ballot(conn, poll_id, user_id) is None:
        return _Refused("not_recipient")
    options = polls.options(conn, poll_id)
    letters = {poll_letters.letter(o.position): o.id for o in options}
    reply = poll_reply.parse(content, runoff=poll.is_runoff, now=now, tz=tz, letters=letters)
    if isinstance(reply, poll_reply.Refusal):
        return _Refused(reply.reason, reply.index)
    added = 0
    if isinstance(reply, poll_reply.Clear):
        ids: list[int] = []
    elif isinstance(reply, poll_reply.All):
        ids = [o.id for o in options[: poll.ballot_size]]
    else:
        ids = []
        for item in reply.items:
            if isinstance(item, poll_reply.Letter):
                option_id = item.option_id
            else:
                try:
                    option_id, created = polls.add_option(conn, poll_id, starts_at=item.starts_at, origin="typed")
                except polls.TooManyOptions:
                    raise _Full from None
                added += int(created)
            if option_id not in ids:
                ids.append(option_id)
    if not polls.replace_picks_if_open(conn, poll_id, user_id, ids, ranked=poll.is_runoff):
        return _Refused("closed")
    positions = {o.id: o.position for o in polls.options(conn, poll_id)}
    return _Voted(tuple(poll_letters.letter(positions[option_id]) for option_id in ids), added)


@dataclass
class _PollLock:
    """One poll's lock, and how many coroutines hold or wait for it. The entry goes when the count returns to 0."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class PollFlow:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, locks: CardLocks, poll_post: PollPost,
                 backoff: DmBackoff, games_guild: GamesGuild, clock: Callable[[], float] = time.time,
                 kick: Callable[[], None] = lambda: None,
                 session_flow: session_flow.SessionFlow | None = None,
                 mirror_post: MirrorPost | None = None) -> None:
        self._db = db
        self._rest = rest
        self._config = config
        self._locks = locks
        self._poll_post = poll_post
        self._backoff = backoff
        self._games_guild = games_guild
        self._clock = clock
        self._kick = kick
        self._session_flow = session_flow   # the session publish for 📅; without it 📅 on a poll does nothing
        self._mirror_post = mirror_post   # posts the poll mirrors of both copies; None leaves them out
        self._poll_locks: dict[int, _PollLock] = {}

    # The 🗳️ press ----------------------------------------------------------------------------------------------

    async def start(self, press: Press) -> None:
        """A 🗳️ press: posts a poll message in the card's session channel and DMs a ballot to each recipient, or refuses
        with a DM. The checks that depend on the press (the surface and the backoff) are here; the rest is `_open_poll`."""
        user = press.user_id
        card = await self._db.run(lambda c: cards.get_by_id(c, press.card_id))
        if card is None or card.state != "live":
            return self._refused(press.card_id, "card")
        resolved = await self._db.run(lambda c: scope.resolve_scope(c, card, press.surface, None))
        if resolved is scope.ELSEWHERE:
            await self._elsewhere(user, card)
            return self._refused(card.id, "elsewhere")
        if self._backoff.blocked(user):
            return self._refused(card.id, "backoff")
        now = int(self._clock())
        starts = session_flow.offered_slot_times(self._config, now)
        await self._open_poll(card.id, starter_id=user, presser_id=user, kind="approval", parent_poll_id=None,
                              options=[(starts_at, "offered") for starts_at in starts], scope_sub_id=resolved.sub_id)

    async def _open_poll(self, card_id: int, *, starter_id: str, presser_id: str, kind: str,
                         parent_poll_id: int | None, options: Sequence[tuple[int, str]],
                         closes_at: int | None = None, scope_sub_id: int | None = None) -> int | None:
        """Posts the poll message and stores the poll with its options and one ballot per recipient, under the card's
        lock. The ballots, the notes and the poller's kick come after it. Every refusal is DMed to the presser and
        logged. `closes_at` is the close time, or None for now plus the poll length. Returns the new poll's id, or None
        when nothing started. The caller has checked the press."""
        card = await self._db.run(lambda c: cards.get_by_id(c, card_id))
        if card is None or card.state != "live":
            return self._refused(card_id, "card")
        resolved = await self._db.run(lambda c: scope.scope_of(c, card, scope_sub_id))
        if resolved is None:
            await self._dm(presser_id, text.POLL_SCOPE_GONE)
            return self._refused(card.id, "scope")
        target, reason = await scope.checked_target(self._rest, self._config, card, resolved,
                                                    failed_event="poll_target_failed")
        if target is None:
            await self._dm(presser_id, text.POLL_NO_CHANNEL)
            return self._refused(card.id, reason or "no_channel")
        channel_id, guild_id = target
        existing = await self._db.run(lambda c: polls.open_for_scope(c, card.id, scope_sub_id))
        if existing is not None:
            await self._already_open(presser_id, existing)
            return self._refused(card.id, "open")

        now = int(self._clock())
        closes = now + self._config.poll_length_seconds if closes_at is None else closes_at
        found = await self._recipients(card, starter_id, resolved)
        if found is None:
            await self._dm(presser_id, text.POLL_TRY_LATER)
            return self._refused(card.id, "members")
        recipients, has_role = found

        async with self._locks.get(card.id):
            fresh = await self._db.run(lambda c: cards.get_by_id(c, card.id))
            if fresh is None or fresh.state != "live":
                await self._dm(presser_id, text.POLL_POST_FAILED)
                return self._refused(card.id, "card_changed")
            fresh_scope = await self._db.run(lambda c: scope.scope_of(c, fresh, scope_sub_id))
            if fresh_scope is None:
                await self._dm(presser_id, text.POLL_SCOPE_GONE)
                return self._refused(card.id, "scope")
            if scope.scope_target(fresh, self._config, fresh_scope) != channel_id:
                await self._dm(presser_id, text.POLL_POST_FAILED)
                return self._refused(card.id, "card_changed")
            again = await self._db.run(lambda c: polls.open_for_scope(c, card.id, scope_sub_id))
            if again is not None:
                await self._already_open(presser_id, again)
                return self._refused(card.id, "open")
            view = polls.provisional_view(
                card_id=card.id, card_title=fresh.title, guild_id=guild_id, channel_id=channel_id,
                starter_id=starter_id, now=now, closes_at=closes,
                ballot_size=self._config.poll_ballot_size, starts=[starts_at for starts_at, _ in options],
                recipients=len(recipients.user_ids), kind=kind, parent_poll_id=parent_poll_id)
            payload = render.render_poll_message(view, instance_url=self._config.instance_url, provisional=True)
            try:
                sent = await self._poll_post.send_poll(channel_id, payload)
            except Exception as exc:  # noqa: BLE001 - a failed post is told to the presser; the cause is logged by type
                log_event(log, "poll_post_failed", level=logging.WARNING, card_id=card.id,
                          reason=type(exc).__name__)
                await self._dm(presser_id, text.POLL_POST_FAILED)
                return self._refused(card.id, "post")
            message_id = sent.get("id") if isinstance(sent, dict) else None
            if not is_snowflake(message_id):
                log_event(log, "poll_post_failed", level=logging.WARNING, card_id=card.id, reason="bad_message_id")
                await self._dm(presser_id, text.POLL_POST_FAILED)
                return self._refused(card.id, "post")
            try:
                poll_id = await self._db.run(partial(
                    polls.create, card_id=card.id, guild_id=guild_id, channel_id=channel_id, message_id=message_id,
                    starter_id=starter_id, kind=kind, parent_poll_id=parent_poll_id, now=now,
                    closes_at=closes, ballot_size=self._config.poll_ballot_size,
                    options=list(options), recipients=recipients.user_ids, scope_sub_id=scope_sub_id,
                    via=recipients.via))
            except Exception as exc:  # noqa: BLE001 - with no row the message would never close: it is deleted
                await self._poll_post.delete_unstored(channel_id, message_id)
                log_event(log, "poll_store_failed", level=logging.WARNING, card_id=card.id,
                          reason=type(exc).__name__)
                await self._dm(presser_id, text.POLL_POST_FAILED)
                return self._refused(card.id, "store")
            if poll_id is None:   # another poll won the race after the check: the provisional message goes
                await self._poll_post.delete_unstored(channel_id, message_id)
                stored = await self._db.run(lambda c: polls.open_for_scope(c, card.id, scope_sub_id))
                if stored is not None:
                    await self._already_open(presser_id, stored)
                return self._refused(card.id, "open")

        if scope_sub_id is None:
            await self._mirror_new_poll(card, poll_id)
        for uid in recipients.user_ids:
            await self._poll_post.send_ballot(poll_id, uid)
        self._poll_post.changed(poll_id)
        notes = []
        if recipients.left_out:
            notes.append(text.POLL_LEFT_OUT.format(n=recipients.left_out))
        if not has_role:
            notes.append(text.POLL_NO_ROLE)
        if notes:
            await self._dm(presser_id, "\n".join(notes))
        self._kick()
        log_event(log, "poll_started", poll_id=poll_id, card_id=card.id, count=len(recipients.user_ids))
        return poll_id

    # The 📅 and 🔁 on a poll message ----------------------------------------------------------------------------

    async def on_poll_reaction(self, poll_id: int, user_id: str, emoji: str, channel_id: str, message_id: str) -> None:
        """A 📅 or 🔁 added to a poll message. Presses has already checked that the message is this poll's, so the poll's
        own stored ids are used for every call. The presser's reaction is removed first, whatever happens. Only the
        starter or an admin acts, and the action runs under the poll's own lock."""
        # The reaction is removed under the spelling the gateway reported (Fluxer stores U+FE0F when a client sends it).
        # The stripped name is only for comparing the action.
        name = emoji.replace("\ufe0f", "")
        await self._poll_post.remove_reaction(poll_id, emoji, user_id)
        poll = await self._db.run(lambda c: polls.get(c, poll_id))
        if poll is None or poll.state != "closed":
            return
        if user_id != poll.starter_id and not admin.is_admin_id(user_id, self._config):
            log_event(log, "poll_reaction", outcome="not_allowed", poll_id=poll_id)
            return
        async with self._poll_lock(poll_id):
            if name == SESSION_EMOJI:
                await self._schedule(poll_id, presser=user_id)
            elif name == RUNOFF_EMOJI:
                await self._runoff(poll_id, presser=user_id)

    @asynccontextmanager
    async def _poll_lock(self, poll_id: int) -> AsyncIterator[None]:
        """Holds the poll's lock for the block. The entry lives only while some coroutine holds or waits for it, so an
        idle poll has no entry."""
        entry = self._poll_locks.get(poll_id)
        if entry is None:
            entry = self._poll_locks[poll_id] = _PollLock()
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0:
                del self._poll_locks[poll_id]

    async def _schedule(self, poll_id: int, *, presser: str) -> None:
        """📅: posts the winning time as a session in the card's session channel, with the starter as proposer and each
        voter for the winner as an implicit maybe. Nothing is created when the poll has no winner, already has a session,
        a runoff was started from it, or its winning time is under an hour away (refused with a DM to the presser). Runs
        under the poll's lock. 📅 and 🔁 on one poll exclude each other."""
        if self._session_flow is None:
            return
        view = await self._db.run(lambda c: polls.view(c, poll_id))
        if view is None or view.poll.outcome != "winner" or view.poll.session_id is not None:
            return self._refused_poll(poll_id, "schedule", "not_schedulable")
        # A runoff started from this poll decides the time, so a session must not be made beside it. This is read under
        # the poll's lock, after the 🔁 path has had its chance to write its runoff, so the two cannot both succeed.
        runoff = await self._db.run(lambda c: polls.child_of(c, poll_id))
        if runoff is not None:
            await self._dm(presser, self._linked_text(text.POLL_SCHEDULE_RUNOFF_EXISTS,
                                                      text.POLL_SCHEDULE_RUNOFF_EXISTS_NO_LINK, runoff))
            return self._refused_poll(poll_id, "schedule", "runoff_exists")
        winner = next(option for option in view.options if option.id == view.poll.winner_option_id)
        if winner.starts_at - self._clock() < poll_count.LEAD_SECONDS:
            await self._dm(presser, text.POLL_TOO_LATE)
            return self._refused_poll(poll_id, "schedule", "too_late")
        voters = sorted({vote.user_id for vote in view.votes if vote.option_id == winner.id}, key=int)
        published = await self._session_flow.publish(view.poll.card_id, view.poll.starter_id, winner.starts_at,
                                                     maybe=voters, notify_proposer=False,
                                                     scope_sub_id=view.poll.scope_sub_id)
        if published.reply is not None:
            reply = published.reply
            if published.cap == "proposer" and presser != view.poll.starter_id:
                # The cap is the starter's, not the presser's: an admin is not told that they have sessions.
                reply = text.POLL_STARTER_AT_CAP.format(card=render.escape(view.card_title))
            await self._dm(presser, reply)
            return self._refused_poll(poll_id, "schedule", "publish")
        linked = await self._db.run(lambda c: polls.set_session_if_unset(c, poll_id, published.session_id))
        if not linked:   # cannot happen under the poll's lock; logged so that a stray session is visible
            log_event(log, "poll_schedule", level=logging.WARNING, poll_id=poll_id, outcome="unlinked")
            return
        self._poll_post.changed(poll_id)
        await self._poll_post.unseed_closed(poll_id)
        via = await self._db.run(lambda c: {b.user_id: b.via_sub_id for b in polls.ballots(c, poll_id)})
        links = await self._db.run(lambda c: {sub_id: _scheduled_link(c, published, sub_id)
                                              for sub_id in set(via.values())})
        for uid in sorted({*voters, view.poll.starter_id}, key=int):
            await self._dm(uid, self._scheduled_text(view.card_title, links.get(via.get(uid))))
        log_event(log, "poll_scheduled", poll_id=poll_id, session_id=published.session_id,
                  count=len(voters))

    async def _runoff(self, poll_id: int, *, presser: str) -> None:
        """🔁: starts a runoff of the top three usable times with votes, in rank order as options a to c. The runoff keeps
        the parent's starter, reads the role again, and is opened by the same pipeline as a 🗳️ poll. It closes after the
        poll length or RUNOFF_CLOSE_MARGIN plus an hour before its earliest time, whichever is sooner. Refused with a DM
        when the winner is already scheduled, when there is already a runoff, when fewer than two usable times have votes,
        or when less than MIN_RUNOFF_SECONDS would remain before its capped close. Runs under the poll's lock."""
        view = await self._db.run(lambda c: polls.view(c, poll_id))
        if view is None or view.poll.outcome != "winner" or view.poll.is_runoff:
            return self._refused_poll(poll_id, "runoff", "not_runoffable")
        # A scheduled winner is decided, so no runoff is started beside its session. Read under the poll's lock, as the
        # 📅 path's check is, so the two cannot both succeed.
        if view.poll.session_id is not None:
            await self._dm(presser, self._session_text(text.POLL_RUNOFF_SCHEDULED, text.POLL_RUNOFF_SCHEDULED_NO_LINK,
                                                       view.session_link))
            return self._refused_poll(poll_id, "runoff", "scheduled")
        existing = await self._db.run(lambda c: polls.child_of(c, poll_id))
        if existing is not None:
            await self._dm(presser, self._linked_text(text.POLL_RUNOFF_EXISTS, text.POLL_RUNOFF_EXISTS_NO_LINK, existing))
            return self._refused_poll(poll_id, "runoff", "exists")
        now = int(self._clock())
        by_id = {option.id: option for option in view.options}
        ranked = poll_count.ranked_with_votes(poll_count.usable(view.options, now=now),
                                              [(vote.user_id, vote.option_id) for vote in view.votes])
        top = [by_id[option_id] for option_id, _count in ranked[:3]]
        if len(top) < 2:
            await self._dm(presser, text.POLL_RUNOFF_NEEDS_TWO)
            return self._refused_poll(poll_id, "runoff", "needs_two")
        # The runoff closes an hour and the close margin before its earliest time at the latest, so every candidate is
        # still usable when a late tick closes it.
        earliest = min(option.starts_at for option in top)
        closes_at = min(now + self._config.poll_length_seconds,
                        earliest - poll_count.LEAD_SECONDS - RUNOFF_CLOSE_MARGIN)
        if closes_at - now < MIN_RUNOFF_SECONDS:
            await self._dm(presser, text.POLL_RUNOFF_TOO_SOON.format(ts=earliest))
            return self._refused_poll(poll_id, "runoff", "too_soon")
        runoff_id = await self._open_poll(view.poll.card_id, starter_id=view.poll.starter_id, presser_id=presser,
                                          kind="runoff", parent_poll_id=poll_id,
                                          options=[(option.starts_at, "offered") for option in top],
                                          closes_at=closes_at, scope_sub_id=view.poll.scope_sub_id)
        if runoff_id is None:
            return
        await self._poll_post.unseed_closed(poll_id)
        if presser != view.poll.starter_id:
            runoff = await self._db.run(lambda c: polls.get(c, runoff_id))
            if runoff is not None:
                await self._dm(presser, self._linked_text(text.POLL_RUNOFF_STARTED, text.POLL_RUNOFF_STARTED_NO_LINK,
                                                          runoff))
        log_event(log, "poll_runoff_started", poll_id=poll_id, count=len(top))

    def _scheduled_text(self, card_title: str, link: tuple[str, str, str] | None) -> str:
        """The scheduled DM to one voter or the starter: the link to the session that voter sees (the home session, or
        their mirror), or the no-link text when there is no link to give."""
        card = render.escape(card_title)
        if not self._config.instance_url or link is None:
            return text.POLL_SCHEDULED_DM_NO_LINK.format(card=card)
        jump = render.jump_link(text.SESSION_LINK_LABEL, self._config.instance_url, *link)
        return text.POLL_SCHEDULED_DM.format(card=card, link=jump)

    def _linked_text(self, with_link: str, without_link: str, poll: polls.Poll) -> str:
        """A text that names a poll by its message link, or the no-link text when the instance URL is not set."""
        if not self._config.instance_url:
            return without_link
        link = render.jump_link(text.POLL_LINK_LABEL, self._config.instance_url, poll.guild_id, poll.channel_id,
                                poll.message_id)
        return with_link.format(link=link)

    def _session_text(self, with_link: str, without_link: str, session: tuple[str, str, str] | None) -> str:
        """A text that names the scheduled session by its message link (guild, channel, message), or the no-link text
        when the instance URL is not set or the session is not found."""
        if not self._config.instance_url or session is None:
            return without_link
        guild_id, channel_id, message_id = session
        link = render.jump_link(text.SESSION_LINK_LABEL, self._config.instance_url, guild_id, channel_id, message_id)
        return with_link.format(link=link)

    def _refused_poll(self, poll_id: int, action: str, reason: str) -> None:
        log_event(log, "poll_" + action, outcome="refused", poll_id=poll_id, reason=reason)


    # Voting by DM reply -----------------------------------------------------------------------------------------

    async def open_ballots(self, user_id: str) -> list[polls.Poll]:
        """The open polls where this user has a ballot. The router runs this once per DM and passes the result to
        `on_reply`, so the lookup is not repeated."""
        return await self._db.run(lambda c: polls.open_ballot_polls(c, user_id))

    async def on_reply(self, event: MessageCreate, open_polls: Sequence[polls.Poll]) -> None:
        """A DM reply from a user with open ballots (`open_polls`, from `open_ballots`). One open ballot takes the reply;
        several cannot be told apart in a DM, so the reply changes nothing and names the open polls."""
        if not open_polls:
            return
        if len(open_polls) > 1:
            log_event(log, "poll_vote", outcome="refused", reason="several_open", count=0)
            await self._answer(event.channel_id,
                               text.POLL_WHICH.format(polls=", ".join(str(p.id) for p in open_polls)))
            return
        await self.vote(event.author_id, open_polls[0].id, event.content, reply_channel_id=event.channel_id)

    async def vote(self, user_id: str, poll_id: int, content: str, *, reply_channel_id: str) -> None:
        """Parse and store one reply, then answer it in the DM it came from and refresh the poll and its ballots.
        Logs the poll, the outcome and a reason key or a count, never the content or the user."""
        tz = self._config.session_time_zone
        now = datetime.fromtimestamp(self._clock(), tz)
        try:
            result = await self._db.run(partial(_vote_tx, poll_id=poll_id, user_id=user_id, content=content,
                                                now=now, tz=tz))
        except _Full:
            result = _Refused("full")
        if isinstance(result, _Refused):
            outcome = "closed" if result.reason == "closed" else "refused"
            log_event(log, "poll_vote", outcome=outcome, poll_id=poll_id, reason=result.reason, count=0)
            await self._answer(reply_channel_id, _refusal_text(result))
            return
        log_event(log, "poll_vote", outcome="voted", poll_id=poll_id, count=result.added)
        # The vote is committed, so the poll and its ballots are marked before the answer is sent: a failed answer
        # must not leave the messages stale.
        self._poll_post.changed(poll_id)
        await self._answer(reply_channel_id, text.POLL_YOUR_PICKS.format(letters=", ".join(result.letters))
                           if result.letters else text.POLL_NO_PICKS)

    async def _answer(self, channel_id: str, content: str) -> None:
        """Answers a reply in the DM it came from. A failed send is logged by its exception type and not raised: the
        reply's effect (if any) is already stored and refreshed."""
        try:
            await self._rest.send_message(channel_id, content=content, allowed_mentions={"parse": []})
        except Exception as exc:  # noqa: BLE001 - the answer is best effort; the vote it reports is already stored
            log_event(log, "poll_answer_failed", level=logging.WARNING, reason=type(exc).__name__)

    # Helpers ---------------------------------------------------------------------------------------------------

    async def _recipients(self, card: Card, starter: str, resolved: scope.Scope
                          ) -> tuple[poll_members.Recipients, bool] | None:
        """The starter, then the holders of the scope's follow role, then (for a home scope) the holders of each both
        copy's role, capped. The flag says whether the scope has a follow role. The home role's holders are read from
        its own community, and a failure there refuses the start. A far community that cannot be read is skipped and
        logged per subscription, so it never fails the home poll. None when the home member list could not be read."""
        role = resolved.role
        home: list[str] = []
        if role is not None:
            try:
                home = await poll_members.role_holders(self._rest, role.guild_id, role.role_id)
            except poll_members.MembersUnavailable:
                return None
        far = await self._far_holders(card) if resolved.sub_id is None else []
        found = poll_members.combined([(None, home), *far], starter_id=starter, cap=self._config.poll_max_recipients)
        return found, role is not None

    async def _far_holders(self, card: Card) -> list[tuple[int, list[str]]]:
        """(subscription id, holders of its follow role) for each both copy that has one, in subscription order. The
        member list is read once per community, for all of that community's roles. A failed read skips that community's
        subscriptions and logs each one by reason."""
        pairs = await self._db.run(lambda c: [(sub.id, role) for sub in subscriptions.list_both(c, card.id)
                                              if (role := cards.get_subscription_role(c, sub.id)) is not None])
        by_guild: dict[str, list[tuple[int, str]]] = {}
        for sub_id, role in pairs:
            by_guild.setdefault(role.guild_id, []).append((sub_id, role.role_id))
        holders: dict[int, list[str]] = {}
        for guild_id, members in by_guild.items():
            try:
                found = await poll_members.guild_role_holders(self._rest, guild_id, [r for _s, r in members])
            except poll_members.MembersUnavailable as exc:
                for sub_id, _role_id in members:
                    log_event(log, "poll_members_failed", level=logging.WARNING, sub_id=sub_id,
                              reason=poll_members.failure_reason(exc))
                continue
            for sub_id, role_id in members:
                holders[sub_id] = found[role_id]
        return sorted(holders.items())

    async def _mirror_new_poll(self, card: Card, poll_id: int) -> None:
        """Posts the new home poll's mirror in each both copy's channel. The mirror has no session link yet and no 📅
        or 🔁 seed. It runs after the card lock is released and before the ballots. A failure is logged and never
        fails the poll."""
        if self._mirror_post is None:
            return
        try:
            targets = await self._db.run(lambda c: self._mirror_post.targets(c, card))
            view = await self._db.run(lambda c: polls.view(c, poll_id))
            if view is None:
                return
            payload = mirror_payload(view, instance_url=self._config.instance_url, session_jump=None)
            for sub, channel_id in targets:
                await self._mirror_post.sync(sub=sub, channel_id=channel_id, kind="poll", object_id=poll_id,
                                             payload=payload)
        except Exception as exc:  # noqa: BLE001 - the home poll is already open; a mirror that fails is logged by type
            log_event(log, "poll_mirror_failed", level=logging.WARNING, poll_id=poll_id, reason=type(exc).__name__)

    async def _elsewhere(self, user_id: str, card: Card) -> None:
        """A press on a copy outside the games community: no poll, only a pointer to the card in the games channel."""
        guild = await self._games_guild.get()
        if self._config.instance_url and guild and card.channel_id and card.message_id:
            link = render.jump_link(text.JUMP_LABEL_CARD, self._config.instance_url, guild, card.channel_id,
                                    card.message_id)
            content = text.POLL_ELSEWHERE.format(link=link)
        else:
            content = text.POLL_ELSEWHERE_NO_LINK
        await self._dm(user_id, content)

    async def _already_open(self, user_id: str, poll: polls.Poll) -> None:
        """Links the open poll, which is already in the card's channel."""
        if self._config.instance_url:
            link = render.jump_link(text.POLL_LINK_LABEL, self._config.instance_url, poll.guild_id, poll.channel_id,
                                    poll.message_id)
            content = text.POLL_ALREADY_OPEN.format(link=link)
        else:
            content = text.POLL_ALREADY_OPEN_NO_LINK
        await self._dm(user_id, content)

    async def _dm(self, user_id: str, content: str) -> None:
        await notify.dm_user(self._rest, user_id, content, backoff=self._backoff)

    @staticmethod
    def _refused(card_id: int, reason: str) -> None:
        log_event(log, "poll_flow", outcome="refused", card_id=card_id, reason=reason)


def _scheduled_link(conn: Any, published: session_flow.Published, sub_id: int | None) -> tuple[str, str, str] | None:
    """The session link a voter reached through `sub_id` gets: the home session for a home voter (None), else the
    session's mirror in that subscription, or None when there is no mirror. A far voter never gets the home link."""
    if sub_id is None:
        return published.guild_id, published.channel_id, published.message_id
    return session_jump(conn, published.session_id, sub_id)


def _refusal_text(refused: _Refused) -> str:
    """The answer to a refused reply: the item's position and reason, then the format reminder for an item refusal."""
    if refused.index is None:
        return _REFUSAL_TEXT[refused.reason]
    reason_text = text.POLL_REPLY_REFUSED.get(refused.reason) or text.SESSION_TIME_REFUSED[refused.reason]
    return (text.POLL_REPLY_ITEM.format(index=refused.index, reason=reason_text) + "\n" + text.POLL_FORMAT + " "
            + text.SESSION_TIME_EXAMPLE)
