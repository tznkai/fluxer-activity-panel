# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The timed poll work, run on every poller tick, next to the session jobs. A poll whose close time has come is closed
through `PollCloser`, which takes the card lock, ranks and edits. When reminders are on, each recipient who has not
answered gets one reminder DM at the halfway time.

Each step lists its work without a lock, and acts on each item in turn. A reminder is marked in one compare-and-set
transaction before its DM goes out, so it is never sent twice, and a reminder never follows a close. A recipient who is
in DM backoff is skipped without being marked, so the one reminder can still go out if the backoff ends before the close.
`due` is the soonest close or reminder still to come, and the poller's wait ends there and never passes it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from functools import partial

from fluxer_client.rest import FluxerREST
from fluxer_panel import notify, polls, render, text
from fluxer_panel.config import Config
from fluxer_panel.db import Database
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.logs import log_event
from fluxer_panel.poll_close import PollCloser

log = logging.getLogger(__name__)


class PollJobs:
    def __init__(self, *, db: Database, rest: FluxerREST, config: Config, closer: PollCloser, backoff: DmBackoff,
                 clock: Callable[[], float] = time.time, kick: Callable[[], None] = lambda: None) -> None:
        self._db = db
        self._rest = rest
        self._config = config
        self._closer = closer
        self._backoff = backoff
        self._clock = clock
        self._kick = kick
        self.due: float | None = None   # the soonest close or reminder still to come: the poller's wait ends there

    async def run(self) -> None:
        """One pass of both jobs. A failing step is logged and never stops the other step or the poller. `due` is read
        last, whatever the steps did."""
        for step in (self._close_due, self._remind):
            try:
                await step()
            except Exception as exc:  # noqa: BLE001 - one failing job must not stop the others
                log_event(log, "poll_job_failed", level=logging.WARNING, kind=step.__name__.lstrip("_"),
                          reason=type(exc).__name__)
        try:
            self.due = await self._next_due()
        except Exception as exc:  # noqa: BLE001 - no wake-up is kept when none can be read: the poll interval still runs
            self.due = None
            log_event(log, "poll_job_failed", level=logging.WARNING, kind="due", reason=type(exc).__name__)

    async def _close_due(self) -> None:
        """Closes each open poll whose close time has come. One poll's failed close is logged and the other due polls
        still close. The poller is kicked once when any poll closed."""
        now = int(self._clock())
        due = await self._db.run(lambda c: polls.due(c, now))
        closed = 0
        try:
            for poll in due:
                try:
                    if await self._closer.close(poll.id) is not None:
                        closed += 1
                except Exception as exc:  # noqa: BLE001 - one poll's failure must not stop the others
                    log_event(log, "poll_job_failed", level=logging.WARNING, kind="close", poll_id=poll.id,
                              reason=type(exc).__name__)
        finally:
            if closed:
                self._kick()

    async def _remind(self) -> None:
        """Sends each unanswered recipient their one halfway reminder, when reminders are on. A recipient in backoff is
        skipped and left unmarked. Any other recipient is marked first, and then DMed through `notify.dm_user` with the
        backoff, so a failed DM is never retried. Logs the poll id and the number sent, never a user id."""
        if not self._config.poll_reminder:
            return
        now = int(self._clock())
        pending = await self._db.run(lambda c: polls.reminder_due(c, now))
        views: dict[int, polls.PollView] = {}
        sent: dict[int, int] = {}
        for poll, user_id in pending:
            if self._backoff.blocked(user_id):
                continue
            if poll.id not in views:
                view = await self._db.run(partial(polls.view, poll_id=poll.id))
                if view is None:
                    continue
                views[poll.id] = view
            if not await self._db.run(partial(polls.mark_reminded_if, poll_id=poll.id, user_id=user_id)):
                continue
            content = self._reminder_content(views[poll.id])
            if await notify.dm_user(self._rest, user_id, content, backoff=self._backoff) is not None:
                sent[poll.id] = sent.get(poll.id, 0) + 1
        for poll_id, count in sent.items():
            log_event(log, "poll_reminded", poll_id=poll_id, count=count)

    def _reminder_content(self, view: polls.PollView) -> str:
        """The reminder's text. The card title is escaped, and the link goes to the poll message, or is plain when no
        instance URL is configured."""
        poll = view.poll
        instance_url = self._config.instance_url
        link = (render.jump_link(text.POLL_LINK_LABEL, instance_url, poll.guild_id, poll.channel_id, poll.message_id)
                if instance_url is not None else text.POLL_LINK_LABEL)
        return text.POLL_REMINDER_DM.format(card=render.escape(view.card_title), link=link, n=poll.id,
                                            ts=poll.closes_at)

    async def _next_due(self) -> float | None:
        """The soonest time still to come: a close, or a reminder while reminders are on. Only times after the clock now
        count, so work that is due but still pending gives no wake-up, and the poller cannot spin on it."""
        clock_now = self._clock()
        now = int(clock_now)
        close = await self._db.run(polls.next_close)
        reminder = await self._db.run(partial(polls.next_reminder, now=now)) if self._config.poll_reminder else None
        pending = [when for when in (close, reminder) if when is not None and when > clock_now]
        return float(min(pending)) if pending else None
