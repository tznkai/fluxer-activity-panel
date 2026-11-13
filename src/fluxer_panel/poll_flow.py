# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The poll flow: 🗳️ starts a time poll for a card; typed DM replies vote in it.

All poll state is in the database. The card lock covers only the post and the insert; ballots and other DMs are sent
after it. Each refusal is logged as `poll_flow` with `outcome="refused"` and a reason, and names no user.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from functools import partial

from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerREST
from fluxer_panel import cards, notify, poll_members, polls, render, session_flow, text
from fluxer_panel.card_commands import CardLocks
from fluxer_panel.cards import Card
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.games import GamesGuild
from fluxer_panel.interactions import card_target
from fluxer_panel.logs import log_event
from fluxer_panel.poll_post import PollPost
from fluxer_panel.presses import Press

log = logging.getLogger(__name__)


def _channel_of(card: Card, config: Config) -> str | None:
    """The channel a poll posts to, read from the card with no REST call: its verified channel, else the configured one.
    The same rule as the session flow's, so a card that changed since the target was read is noticed under the lock."""
    own = card.play_now_channel
    if own is not None and own.verified:
        return own.channel_id
    return config.play_now_channel_id


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
            if fresh is None or fresh.state != "live" or _channel_of(fresh, self._config) != channel_id:
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
