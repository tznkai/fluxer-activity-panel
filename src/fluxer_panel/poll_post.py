# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every REST call on poll content: the poll message, ballot DMs and the reactions on a closed poll.

The games channel is refused before any REST call. Edits are debounced per poll: a change marks the poll message and the
affected ballots dirty, and one task per poll waits `edit_delay` and then edits each dirty message once, skipping a
message whose text has not changed since the bot last sent it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from fluxer_client.rest import FluxerHTTPError, FluxerREST
from fluxer_panel import mirrors, notify, poll_count, polls, render, subscriptions
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.games import BotIdentity
from fluxer_panel.games_guard import GamesChannelRefused
from fluxer_panel.logs import log_event
from fluxer_panel.mirror_post import MirrorPost
from fluxer_panel.pacer import ChannelPacer
from fluxer_panel.polls import Ballot, PollView
from fluxer_panel.presses import SESSION_EMOJI

log = logging.getLogger(__name__)

POLL_SCHEDULE_EMOJI = SESSION_EMOJI          # the calendar, from presses
POLL_RUNOFF_EMOJI = "\U0001F501"             # the repeat button: starts a runoff


@dataclass
class _Dirty:
    """What a flush must edit: the poll message, and the ballots of `ballots` (or of every ballot when `everyone`)."""
    message: bool = False
    everyone: bool = False
    ballots: set[str] = field(default_factory=set)

    def empty(self) -> bool:
        return not (self.message or self.everyone or self.ballots)


def _canonical(payload: dict[str, Any]) -> str:
    """The text of a payload as the bot compares it: the same content gives the same string."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _reason(exc: Exception) -> str:
    return f"http_{exc.status}" if isinstance(exc, FluxerHTTPError) else type(exc).__name__


def _read_poll(conn: sqlite3.Connection, poll_id: int) -> tuple[PollView, list[Ballot]] | None:
    view = polls.view(conn, poll_id)
    if view is None:
        return None
    return view, polls.ballots(conn, poll_id)


def poll_jump(conn: sqlite3.Connection, poll: polls.Poll, via_sub_id: int | None) -> tuple[str, str, str] | None:
    """The (guild, channel, message) a ballot or reminder links to. A home ballot (no via) links the home poll. A far
    ballot links its subscription's mirror, in that subscription's community, or nothing when the mirror is gone. The
    home poll is never returned for a far ballot (decision 2)."""
    if via_sub_id is None:
        return poll.guild_id, poll.channel_id, poll.message_id
    mirror = mirrors.get(conn, "poll", poll.id, via_sub_id)
    sub = subscriptions.get_by_id(conn, via_sub_id)
    if mirror is None or sub is None:
        return None
    return sub.guild_id, mirror.channel_id, mirror.message_id


def session_jump(conn: sqlite3.Connection, session_id: int, sub_id: int) -> tuple[str, str, str] | None:
    """The (guild, channel, message) of a session's mirror in one both subscription, or None when it has none."""
    mirror = mirrors.get(conn, "session", session_id, sub_id)
    sub = subscriptions.get_by_id(conn, sub_id)
    if mirror is None or sub is None:
        return None
    return sub.guild_id, mirror.channel_id, mirror.message_id


def mirror_payload(view: PollView, *, instance_url: str | None,
                   session_jump: tuple[str, str, str] | None) -> dict[str, Any]:
    """The poll message as a both copy shows it: the same render as home, with the session link given by the caller (its
    mirror, or None for plain text). A mirror never gets the home session's jump."""
    return render.render_poll_message(view, instance_url=instance_url, rounds=_rounds(view),
                                      session_jump=session_jump)


def _mirror_state(conn: sqlite3.Connection, poll: polls.Poll) -> tuple[list[mirrors.Mirror],
                                                                       dict[int, tuple[str, str, str] | None]]:
    """The poll's mirrors, and for each subscription the jump to its mirror of the poll's session (None without one)."""
    rows = mirrors.for_object(conn, "poll", poll.id)
    jumps = {row.sub_id: None if poll.session_id is None else session_jump(conn, poll.session_id, row.sub_id)
             for row in rows}
    return rows, jumps


def _rounds(view: PollView) -> poll_count.Rounds | None:
    """A closed runoff's rounds: the instant runoff over the options usable at close, each ballot in its rank order."""
    poll = view.poll
    if not poll.is_runoff or poll.state != "closed" or poll.closed_at is None:
        return None
    usable = poll_count.usable(view.options, now=poll.closed_at)
    ballots: dict[str, list[int]] = {}
    for vote in view.votes:    # the stored order is by rank within each user
        ballots.setdefault(vote.user_id, []).append(vote.option_id)
    return poll_count.instant_runoff(usable, list(ballots.values()))


class PollPost:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, pacer: ChannelPacer,
                 identity: BotIdentity, backoff: DmBackoff | None = None, edit_delay: float = 5.0,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 mirror_post: MirrorPost | None = None) -> None:
        """`identity` is required: unseeding removes the bot's own reactions, so wiring must pass the shared
        BotIdentity. Its user id is None until READY; an unseed before then is logged and skipped."""
        self._db = db
        self._rest = rest
        self._config = config
        self._pacer = pacer
        self._backoff = backoff
        self._edit_delay = edit_delay
        self._sleep = sleep
        self._mirror_post = mirror_post   # the both copies' poll mirrors; None leaves them unedited
        self._identity = identity
        self._on_gone: Callable[[int], Awaitable[None]] | None = None
        self._dirty: dict[int, _Dirty] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._users: dict[int, int] = {}                     # flushes holding or waiting for a poll's lock
        self._last: dict[int, dict[tuple[str, str], str]] = {}   # per poll: the text last sent for each message

    def set_on_gone(self, hook: Callable[[int], Awaitable[None]]) -> None:
        """Called with the poll id when the poll message answers 404 to an edit. Runs after the poll's lock is released."""
        self._on_gone = hook

    async def send_poll(self, channel_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Send the first poll message. GamesChannelRefused before any REST call."""
        if channel_id == self._config.games_channel_id:
            raise GamesChannelRefused
        return await self._rest.send_message(channel_id, embeds=payload["embeds"],
                                             allowed_mentions=payload["allowed_mentions"])

    async def delete_unstored(self, channel_id: str, message_id: str) -> None:
        """Deletes a provisional poll message whose row was not stored (a lost race, or a failed insert). Best effort:
        the games channel is refused before any REST call, and a failure is logged, never raised."""
        if channel_id == self._config.games_channel_id:
            log_event(log, "poll_discard_failed", level=logging.WARNING, channel_id=channel_id,
                      reason="games_channel")
            return
        try:
            await self._rest.delete_message(channel_id, message_id)
        except Exception as exc:  # noqa: BLE001 - a failed delete is logged; the caller is not told
            log_event(log, "poll_discard_failed", level=logging.WARNING, channel_id=channel_id, reason=_reason(exc))

    async def send_ballot(self, poll_id: int, user_id: str) -> bool:
        """Render and DM one ballot through notify.dm_user (backoff honoured); store the DM ids or mark dm_failed."""
        loaded = await self._db.run(lambda conn: _read_poll(conn, poll_id))
        if loaded is None:
            return False
        view, ballots = loaded
        found = next((ballot for ballot in ballots if ballot.user_id == user_id), None)
        if found is None:
            return False
        jump = await self._db.run(lambda conn: poll_jump(conn, view.poll, found.via_sub_id))
        content = self._ballot_content(view, user_id, jump)
        sent = await notify.dm_user(self._rest, user_id, content, backoff=self._backoff)
        if sent is None:
            await self._db.run(lambda conn: polls.mark_dm_failed(conn, poll_id, user_id))
            return False
        channel_id, message_id = sent
        await self._db.run(lambda conn: polls.set_ballot_message(conn, poll_id, user_id, channel_id=channel_id,
                                                                 message_id=message_id))
        self._remember(poll_id, channel_id, message_id, _canonical({"content": content}))
        return True

    def changed(self, poll_id: int, *, ballots: Iterable[str] | None = None, message: bool = True) -> None:
        """Mark the poll message and ballots dirty (None = every ballot with a DM) and start the poll's task if idle."""
        dirty = self._dirty.setdefault(poll_id, _Dirty())
        if message:
            dirty.message = True
        if ballots is None:
            dirty.everyone = True
        else:
            dirty.ballots.update(ballots)
        task = self._tasks.get(poll_id)
        if task is None or task.done():
            self._tasks[poll_id] = asyncio.create_task(self._run(poll_id))

    async def flush(self, poll_id: int) -> None:
        """Edit the dirty messages now. Holds the poll's lock while editing, and calls the on_gone hook after releasing it."""
        self._users[poll_id] = self._users.get(poll_id, 0) + 1
        gone = False
        try:
            lock = self._locks.setdefault(poll_id, asyncio.Lock())
            async with lock:
                gone = await self._flush_locked(poll_id)
        finally:
            self._users[poll_id] -= 1
            if self._users[poll_id] == 0:
                del self._users[poll_id]
            self._forget_if_idle(poll_id)
        if gone and self._on_gone is not None:
            try:
                await self._on_gone(poll_id)
            except Exception as exc:  # noqa: BLE001 - a failing hook is logged; the flush is already done
                log_event(log, "poll_gone_hook_failed", level=logging.WARNING, poll_id=poll_id,
                          reason=type(exc).__name__)

    async def seed_closed(self, poll_id: int) -> None:
        """Add 📅, and 🔁 for an approval poll, to a closed poll message with a winner (pacer before each)."""
        loaded = await self._db.run(lambda conn: _read_poll(conn, poll_id))
        if loaded is None:
            return
        view = loaded[0]
        if self._refused(view):
            return
        if view.poll.outcome != "winner":
            return
        for emoji in self._emojis(view):
            await self._pacer.wait(view.poll.channel_id)
            try:
                await self._rest.add_reaction(view.poll.channel_id, view.poll.message_id, emoji)
            except Exception as exc:  # noqa: BLE001 - a missing seed is logged; the poll stays usable
                log_event(log, "poll_seed_failed", level=logging.WARNING, poll_id=poll_id, reason=_reason(exc))
                return

    async def unseed_closed(self, poll_id: int) -> None:
        """Remove the bot's own 📅/🔁 (used once a closed poll is acted on)."""
        loaded = await self._db.run(lambda conn: _read_poll(conn, poll_id))
        if loaded is None:
            return
        view = loaded[0]
        if self._refused(view):
            return
        bot_id = self._identity.user_id
        if bot_id is None:
            log_event(log, "poll_unseed_skipped", level=logging.WARNING, poll_id=poll_id, reason="no_bot_id")
            return
        for emoji in self._emojis(view):
            await self._pacer.wait(view.poll.channel_id)
            try:
                await self._rest.remove_reaction(view.poll.channel_id, view.poll.message_id, emoji, bot_id)
            except Exception as exc:  # noqa: BLE001 - a failed removal is logged; the next emoji is still removed
                if not (isinstance(exc, FluxerHTTPError) and exc.status == 404):
                    log_event(log, "poll_unseed_failed", level=logging.WARNING, poll_id=poll_id, reason=_reason(exc))

    async def remove_reaction(self, poll_id: int, emoji: str, user_id: str) -> None:
        """Removes one user's reaction from a poll message: the presser's own calendar or repeat reaction. The games channel
        is refused before any REST call, the channel's pacer runs first, and a failure is logged and not raised. A 404 means
        the reaction is already gone."""
        loaded = await self._db.run(lambda conn: _read_poll(conn, poll_id))
        if loaded is None:
            return
        view = loaded[0]
        if self._refused(view):
            return
        await self._pacer.wait(view.poll.channel_id)
        try:
            await self._rest.remove_reaction(view.poll.channel_id, view.poll.message_id, emoji, user_id)
        except Exception as exc:  # noqa: BLE001 - a failed removal is logged; the poll action is not undone by it
            if not (isinstance(exc, FluxerHTTPError) and exc.status == 404):
                log_event(log, "poll_reaction_remove_failed", level=logging.WARNING, poll_id=poll_id,
                          reason=_reason(exc))

    async def drain(self) -> None:
        """Wait for every pending task (tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def aclose(self, *, timeout: float = 5.0) -> None:
        """Shutdown: let pending edits finish for up to `timeout` seconds, then cancel what is left."""
        pending = {task for task in self._tasks.values() if not task.done()}
        if pending:
            _done, still = await asyncio.wait(pending, timeout=timeout)
            for task in still:
                task.cancel()
            if still:
                await asyncio.gather(*still, return_exceptions=True)
        self._dirty.clear()

    # Internals ---------------------------------------------------------------------------------------

    def _refused(self, view: PollView) -> bool:
        """The stored channel is the games channel: refused, logged, and no call is made."""
        if view.poll.channel_id == self._config.games_channel_id:
            log_event(log, "poll_post_refused", level=logging.WARNING, poll_id=view.poll.id, reason="games_channel")
            return True
        return False

    @staticmethod
    def _emojis(view: PollView) -> tuple[str, ...]:
        return (POLL_SCHEDULE_EMOJI,) if view.poll.is_runoff else (POLL_SCHEDULE_EMOJI, POLL_RUNOFF_EMOJI)

    def _ballot_content(self, view: PollView, user_id: str, jump: tuple[str, str, str] | None) -> str:
        """The ballot's text. `jump` is the link the ballot gives: the home poll, the recipient's mirror, or None."""
        zone = self._config.session_time_zone.key
        payload = render.render_poll_ballot(view, user_id=user_id, zone_name=zone,
                                            instance_url=self._config.instance_url, poll_jump=jump)
        return payload["content"]

    def _remember(self, poll_id: int, channel_id: str, message_id: str, canonical: str) -> None:
        self._last.setdefault(poll_id, {})[(channel_id, message_id)] = canonical

    def _unchanged(self, poll_id: int, channel_id: str, message_id: str, canonical: str) -> bool:
        return self._last.get(poll_id, {}).get((channel_id, message_id)) == canonical

    async def _run(self, poll_id: int) -> None:
        """One task per poll: wait the debounce window, flush, and go again while changes keep arriving. A flush that
        raises is logged and ends the task without raising; its marks are restored, so the next change flushes them."""
        try:
            while True:
                await self._sleep(self._edit_delay)
                try:
                    await self.flush(poll_id)
                except Exception as exc:  # noqa: BLE001 - a background task must not raise; the marks are restored
                    log_event(log, "poll_flush_failed", level=logging.WARNING, poll_id=poll_id,
                              reason=type(exc).__name__)
                    return
                dirty = self._dirty.get(poll_id)
                if dirty is None or dirty.empty():
                    return
        finally:
            self._tasks.pop(poll_id, None)
            self._forget_if_idle(poll_id)

    def _forget_if_idle(self, poll_id: int) -> None:
        """Drops a poll's dirty marks and lock only when nothing will use them: no live task, no pending marks, and no
        flush holding or waiting for the lock. A change made during a flush keeps both alive."""
        task = self._tasks.get(poll_id)
        if task is not None and not task.done():
            return
        if self._users.get(poll_id, 0):
            return
        dirty = self._dirty.get(poll_id)
        if dirty is not None:
            if not dirty.empty():
                return
            del self._dirty[poll_id]
        lock = self._locks.get(poll_id)
        if lock is not None and lock.locked():
            return
        self._locks.pop(poll_id, None)

    async def _flush_locked(self, poll_id: int) -> bool:
        """The flush body, run under the poll's lock. Returns True when the poll message answered 404. The marks are
        taken before any I/O, so a flush that raises hands them back (`_restore`) for the next change to flush."""
        dirty = self._dirty.pop(poll_id, None)
        if dirty is None or dirty.empty():
            return False
        try:
            return await self._edit_marked(poll_id, dirty)
        except Exception:
            self._restore(poll_id, dirty)
            raise

    def _restore(self, poll_id: int, dirty: _Dirty) -> None:
        """Merges the marks of a flush that raised back into the poll's pending marks."""
        pending = self._dirty.setdefault(poll_id, _Dirty())
        pending.message = pending.message or dirty.message
        pending.everyone = pending.everyone or dirty.everyone
        pending.ballots |= dirty.ballots

    async def _edit_marked(self, poll_id: int, dirty: _Dirty) -> bool:
        loaded = await self._db.run(lambda conn: _read_poll(conn, poll_id))
        if loaded is None:
            return False
        view, ballots = loaded
        if self._refused(view):
            return False
        gone = False
        if dirty.message and view.poll.outcome != "removed":
            gone = await self._edit_poll_message(view)
        if dirty.message:
            await self._edit_mirrors(view)
        jumps = await self._db.run(lambda conn: {ballot.via_sub_id: poll_jump(conn, view.poll, ballot.via_sub_id)
                                                 for ballot in ballots})
        for ballot in ballots:
            if ballot.dm_channel_id is None or ballot.dm_message_id is None:
                continue
            if dirty.everyone or ballot.user_id in dirty.ballots:
                await self._edit_ballot(view, ballot, jumps[ballot.via_sub_id])
        if view.poll.state == "closed":
            self._last.pop(poll_id, None)   # a later edit re-renders and sends; only the cache is dropped
        return gone

    async def _edit_poll_message(self, view: PollView) -> bool:
        poll = view.poll
        payload = render.render_poll_message(view, instance_url=self._config.instance_url, rounds=_rounds(view),
                                             session_jump=view.session_link)
        canonical = _canonical(payload)
        if self._unchanged(poll.id, poll.channel_id, poll.message_id, canonical):
            return False
        try:
            await self._rest.edit_message(poll.channel_id, poll.message_id, embeds=payload["embeds"],
                                          allowed_mentions=payload["allowed_mentions"])
        except FluxerHTTPError as exc:
            if exc.status == 404:
                log_event(log, "poll_message_gone", level=logging.WARNING, poll_id=poll.id, reason="http_404")
                return True
            log_event(log, "poll_edit_failed", level=logging.WARNING, poll_id=poll.id, reason=_reason(exc))
            return False
        except Exception as exc:  # noqa: BLE001 - a failed edit is logged; the next change edits it again
            log_event(log, "poll_edit_failed", level=logging.WARNING, poll_id=poll.id, reason=_reason(exc))
            return False
        self._remember(poll.id, poll.channel_id, poll.message_id, canonical)
        return False

    async def _edit_mirrors(self, view: PollView) -> None:
        """Edits each stored poll mirror to the current poll. Only stored mirrors are edited, never posted: a mirror
        that answers 404 is dropped by MirrorPost, and the home poll is not closed by it (AC3.8)."""
        if self._mirror_post is None:
            return
        rows, jumps = await self._db.run(lambda conn: _mirror_state(conn, view.poll))
        for row in rows:
            payload = mirror_payload(view, instance_url=self._config.instance_url, session_jump=jumps[row.sub_id])
            try:
                await self._mirror_post.edit_stored(row, payload)
            except Exception as exc:  # noqa: BLE001 - one mirror's failure must not stop the home poll or the others
                log_event(log, "poll_mirror_failed", level=logging.WARNING, poll_id=view.poll.id, sub_id=row.sub_id,
                          reason=type(exc).__name__)

    async def _edit_ballot(self, view: PollView, ballot: Ballot, jump: tuple[str, str, str] | None) -> None:
        assert ballot.dm_channel_id is not None and ballot.dm_message_id is not None
        content = self._ballot_content(view, ballot.user_id, jump)
        canonical = _canonical({"content": content})
        channel_id, message_id = ballot.dm_channel_id, ballot.dm_message_id
        if self._unchanged(view.poll.id, channel_id, message_id, canonical):
            return
        try:
            await self._rest.edit_message(channel_id, message_id, content=content,
                                          allowed_mentions={"parse": []})
        except Exception as exc:  # noqa: BLE001 - one ballot's failed edit must not stop the others
            log_event(log, "poll_ballot_edit_failed", level=logging.WARNING, poll_id=view.poll.id, reason=_reason(exc))
            return
        self._remember(view.poll.id, channel_id, message_id, canonical)
