# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ending and deleting a subscription's scope: the sessions, the poll and the play-now posts that carry its
scope_sub_id, and its mirrors. A mode change away from local/both and an unsubscribe both use it.

The card's lock is held by the caller for end_scope_locked and delete_scope_rows_locked. A play-now post's reactions are
cleared by end_scope_after_lock, after the lock is released, so no reactor read or removal holds it."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fluxer_client.rest import FluxerHTTPError
from fluxer_panel import mirrors, play_now, polls, sessions
from fluxer_panel.logs import log_event
from fluxer_panel.play_now import PlayNow

if TYPE_CHECKING:   # type-only: card_commands imports this module to end a scope, so it is not imported at run time
    from fluxer_panel.card_commands import CardContext

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScopeEnded:
    sessions: int                       # cancelled
    poll_closed: bool
    play_now: tuple[PlayNow, ...]       # ended; their reactions are cleared after the lock is released
    mirrors_left: tuple[str, ...]       # channel ids where a mirror delete was refused with a 403


def _require_wiring(ctx: CardContext) -> None:
    if ctx.session_commands is None or ctx.play_now_ender is None:
        raise RuntimeError("scope cleanup needs session_commands and play_now_ender on the card context")


async def end_scope_locked(ctx: CardContext, card_id: int, sub_id: int, actor: str, *,
                           counts: Mapping[int, int]) -> ScopeEnded:
    """Ends everything the subscription scope owns, plus its mirrors. The caller holds ctx.locks.get(card_id).
    `counts` maps the scope's live play-now ids to the reactor counts the caller read before taking the lock."""
    _require_wiring(ctx)
    cancelled = await ctx.session_commands.cancel_card_sessions_locked(card_id, actor, scope=sub_id)
    poll_closed = await _close_scope_poll_locked(ctx, card_id, sub_id)
    live = await ctx.db.run(lambda c: play_now.live_for_scope(c, card_id, sub_id))
    ended = await ctx.play_now_ender.end_play_now_locked(live, counts)
    mirrors_left = await delete_mirrors_locked(ctx, sub_id)
    log_event(log, "scope_ended", card_id=card_id, sub_id=sub_id, count=cancelled)
    return ScopeEnded(sessions=cancelled, poll_closed=poll_closed, play_now=tuple(ended),
                      mirrors_left=mirrors_left)


async def end_scope_after_lock(ctx: CardContext, ended: ScopeEnded) -> None:
    """Clears the reactions of the ended play-now posts. Call after the card's lock is released."""
    _require_wiring(ctx)
    await ctx.play_now_ender.clear_play_now_reactions(ended.play_now)


async def _close_scope_poll_locked(ctx: CardContext, card_id: int, sub_id: int) -> bool:
    """Closes the scope's open poll with no winner. A failure is logged and the scope's end goes on."""
    poll = await ctx.db.run(lambda c: polls.open_for_scope(c, card_id, sub_id))
    if poll is None or ctx.poll_closer is None:
        return False
    try:
        closed = await ctx.poll_closer.close_locked(poll.id, outcome_override="none")
    except Exception as exc:  # noqa: BLE001 - a poll that cannot close must not stop the scope's end
        log_event(log, "scope_poll_close_failed", level=logging.WARNING, card_id=card_id, sub_id=sub_id,
                  reason=type(exc).__name__)
        return False
    return closed is not None


async def delete_mirrors_locked(ctx: CardContext, sub_id: int) -> tuple[str, ...]:
    """Deletes every mirror message of the subscription, then its rows. A 404 means the message is already gone. A 403
    or any other failure is logged and the channel is returned, and the row is deleted all the same. The caller holds
    the card's lock."""
    rows = await ctx.db.run(lambda c: mirrors.for_sub(c, sub_id))
    left: list[str] = []
    for mirror in rows:
        if not await _delete_mirror_message(ctx, sub_id, mirror.channel_id, mirror.message_id):
            left.append(mirror.channel_id)
    await ctx.db.run(lambda c: mirrors.delete_for_sub(c, sub_id))
    return tuple(dict.fromkeys(left))


async def _delete_mirror_message(ctx: CardContext, sub_id: int, channel_id: str, message_id: str) -> bool:
    """True when the message is deleted or already gone. False when it is left behind."""
    try:
        await ctx.rest.delete_message(channel_id, message_id)
    except FluxerHTTPError as exc:
        if exc.status == 404:
            return True
        log_event(log, "mirror_delete_failed", level=logging.WARNING, channel_id=channel_id, sub_id=sub_id,
                  reason=type(exc).__name__)
        return False
    except Exception as exc:  # noqa: BLE001 - a failed delete is logged and the message counts as left
        log_event(log, "mirror_delete_failed", level=logging.WARNING, channel_id=channel_id, sub_id=sub_id,
                  reason=type(exc).__name__)
        return False
    return True


async def delete_scope_rows_locked(ctx: CardContext, sub_id: int) -> None:
    """Unsubscribe only. Scrubs each of the scope's session messages, as purge does, so no location stays in Fluxer,
    then deletes the scope's play-now, poll and session rows in one transaction. RSVPs, options, ballots, votes and
    mirrors cascade. The caller deletes the subscription row after this. The caller holds the card's lock."""
    _require_wiring(ctx)
    scoped = await ctx.db.run(lambda c: sessions.list_for_scope(c, sub_id))
    for session in scoped:
        try:
            await ctx.session_commands.scrub(session)
        except Exception as exc:  # noqa: BLE001 - a failed scrub is logged; the delete still runs, as in purge
            log_event(log, "session_scrub_failed", level=logging.WARNING, session_id=session.id,
                      reason=type(exc).__name__)
    await ctx.db.run(lambda c: _delete_scope_rows_tx(c, sub_id))


def _delete_scope_rows_tx(conn: Any, sub_id: int) -> None:
    conn.execute("DELETE FROM play_now WHERE scope_sub_id = ?", (sub_id,))
    conn.execute("DELETE FROM polls WHERE scope_sub_id = ?", (sub_id,))
    conn.execute("DELETE FROM sessions WHERE scope_sub_id = ?", (sub_id,))
