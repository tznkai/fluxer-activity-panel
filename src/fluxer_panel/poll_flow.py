# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The poll flow: 🗳️ starts a time poll for a card; typed DM replies vote in it.

All poll state is in the database. The card lock covers only the post and the insert; ballots and other DMs are sent
after it. Each refusal is logged as `poll_flow` with `outcome="refused"` and a reason, and names no user.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import partial

from fluxer_client.events import MessageCreate
from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, notify, poll_letters, poll_members, poll_reply, polls, render, session_flow, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import Card
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.games import GamesGuild
from fluxer_panel.interactions import card_target, target_channel_id
from fluxer_panel.logs import log_event
from fluxer_panel.poll_post import PollPost
from fluxer_panel.presses import Press

log = logging.getLogger(__name__)


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


class PollFlow:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, locks: CardLocks, poll_post: PollPost,
                 backoff: DmBackoff, games_guild: GamesGuild, clock: Callable[[], float] = time.time,
                 kick: Callable[[], None] = lambda: None) -> None:
        self._db = db
        self._rest = rest
        self._config = config
        self._locks = locks
        self._poll_post = poll_post
        self._backoff = backoff
        self._games_guild = games_guild
        self._clock = clock
        self._kick = kick

    # The 🗳️ press ----------------------------------------------------------------------------------------------

    async def start(self, press: Press) -> None:
        """A 🗳️ press: posts a poll message in the card's session channel and DMs a ballot to each recipient, or refuses
        with a DM. The poll is stored under the card's lock; the ballots are sent after the lock is released."""
        user = press.user_id
        card = await self._db.run(lambda c: cards.get_by_id(c, press.card_id))
        if card is None or card.state != "live":
            return self._refused(press.card_id, "card")
        if not press.surface.home_community:
            await self._elsewhere(user, card)
            return self._refused(card.id, "elsewhere")
        if self._backoff.blocked(user):
            return self._refused(card.id, "backoff")
        target, reason = await card_target(self._rest, self._config, card, failed_event="poll_target_failed")
        if target is None:
            await self._dm(user, text.POLL_NO_CHANNEL)
            return self._refused(card.id, reason or "no_channel")
        channel_id, guild_id = target
        existing = await self._db.run(lambda c: polls.open_for_card(c, card.id))
        if existing is not None:
            await self._already_open(user, existing)
            return self._refused(card.id, "open")

        now = int(self._clock())
        starts = session_flow.offered_slot_times(self._config, now)
        found = await self._recipients(card, user)
        if found is None:
            await self._dm(user, text.POLL_TRY_LATER)
            return self._refused(card.id, "members")
        recipients, has_role = found

        async with self._locks.get(card.id):
            fresh = await self._db.run(lambda c: cards.get_by_id(c, card.id))
            if fresh is None or fresh.state != "live" or target_channel_id(fresh, self._config) != channel_id:
                await self._dm(user, text.POLL_POST_FAILED)
                return self._refused(card.id, "card_changed")
            again = await self._db.run(lambda c: polls.open_for_card(c, card.id))
            if again is not None:
                await self._already_open(user, again)
                return self._refused(card.id, "open")
            view = polls.provisional_view(
                card_id=card.id, card_title=fresh.title, guild_id=guild_id, channel_id=channel_id, starter_id=user,
                now=now, closes_at=now + self._config.poll_length_seconds, ballot_size=self._config.poll_ballot_size,
                starts=starts, recipients=len(recipients.user_ids))
            payload = render.render_poll_message(view, instance_url=self._config.instance_url, provisional=True)
            try:
                sent = await self._poll_post.send_poll(channel_id, payload)
            except Exception as exc:  # noqa: BLE001 - a failed post is told to the presser; the cause is logged by type
                log_event(log, "poll_post_failed", level=logging.WARNING, card_id=card.id,
                          reason=type(exc).__name__)
                await self._dm(user, text.POLL_POST_FAILED)
                return self._refused(card.id, "post")
            message_id = sent.get("id") if isinstance(sent, dict) else None
            if not is_snowflake(message_id):
                log_event(log, "poll_post_failed", level=logging.WARNING, card_id=card.id, reason="bad_message_id")
                await self._dm(user, text.POLL_POST_FAILED)
                return self._refused(card.id, "post")
            try:
                poll_id = await self._db.run(partial(
                    polls.create, card_id=card.id, guild_id=guild_id, channel_id=channel_id, message_id=message_id,
                    starter_id=user, kind="approval", parent_poll_id=None, now=now,
                    closes_at=now + self._config.poll_length_seconds, ballot_size=self._config.poll_ballot_size,
                    options=[(starts_at, "offered") for starts_at in starts], recipients=recipients.user_ids))
            except Exception as exc:  # noqa: BLE001 - with no row the message would never close: it is deleted
                await self._poll_post.delete_unstored(channel_id, message_id)
                log_event(log, "poll_store_failed", level=logging.WARNING, card_id=card.id,
                          reason=type(exc).__name__)
                await self._dm(user, text.POLL_POST_FAILED)
                return self._refused(card.id, "store")
            if poll_id is None:   # another poll won the race after the check: the provisional message goes
                await self._poll_post.delete_unstored(channel_id, message_id)
                stored = await self._db.run(lambda c: polls.open_for_card(c, card.id))
                if stored is not None:
                    await self._already_open(user, stored)
                return self._refused(card.id, "open")

        for uid in recipients.user_ids:
            await self._poll_post.send_ballot(poll_id, uid)
        self._poll_post.changed(poll_id)
        notes = []
        if recipients.left_out:
            notes.append(text.POLL_LEFT_OUT.format(n=recipients.left_out))
        if not has_role:
            notes.append(text.POLL_NO_ROLE)
        if notes:
            await self._dm(user, "\n".join(notes))
        self._kick()
        log_event(log, "poll_started", poll_id=poll_id, card_id=card.id, count=len(recipients.user_ids))

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

    async def _recipients(self, card: Card, starter: str) -> tuple[poll_members.Recipients, bool] | None:
        """The starter and the follow-role holders, capped; with a flag for whether the card has a follow role. None when
        the member list could not be read, so nothing is posted."""
        role = await self._db.run(lambda c: cards.get_follow_role(c, card.id))
        if role is None:
            return poll_members.Recipients(user_ids=(starter,), left_out=0), False
        try:
            holders = await poll_members.role_holders(self._rest, role.guild_id, role.role_id)
        except poll_members.MembersUnavailable:
            return None
        return poll_members.recipients(holders, starter_id=starter, cap=self._config.poll_max_recipients), True

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


def _refusal_text(refused: _Refused) -> str:
    """The answer to a refused reply: the item's position and reason, then the format reminder for an item refusal."""
    if refused.index is None:
        return _REFUSAL_TEXT[refused.reason]
    reason_text = text.POLL_REPLY_REFUSED.get(refused.reason) or text.SESSION_TIME_REFUSED[refused.reason]
    return (text.POLL_REPLY_ITEM.format(index=refused.index, reason=reason_text) + "\n" + text.POLL_FORMAT + " "
            + text.SESSION_TIME_EXAMPLE)
