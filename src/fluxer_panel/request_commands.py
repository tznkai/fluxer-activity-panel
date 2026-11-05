# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""`!request list` and `!request clear <slug>`: the admin's view of wake requests. Routed by `admin.handle` after
`authorize`. Every reply is a fixed `text` string, or a line built from a card's own slug and counts, never input echoed."""

from __future__ import annotations

import logging
from typing import Any

from fluxer_client.events import MessageCreate
from fluxer_panel import cards, text
from fluxer_panel.card_commands import CardContext, chunks
from fluxer_panel.logs import log_event
from fluxer_panel.wake import WakeRequests, people

log = logging.getLogger(__name__)
NO_MENTIONS = {"parse": []}
MINUTE = 60
HOUR = 60 * MINUTE


async def handle(args: str, event: MessageCreate, ctx: CardContext) -> None:
    wake = ctx.wake
    if wake is None:
        return
    parts = args.split()
    sub = parts[0].lower() if parts else ""
    card_id: int | None = None
    if sub == "list" and len(parts) == 1:
        replies = chunks((await _list(wake)).split("\n"))
    elif sub == "clear" and len(parts) == 2:
        reply, card_id = await _clear(ctx, wake, parts[1])
        replies = [reply]
    else:
        sub = "unknown"
        replies = [text.REQUEST_USAGE]
    for reply in replies:
        await ctx.rest.send_message(event.channel_id, content=reply, allowed_mentions=NO_MENTIONS)
    fields: dict[str, Any] = {"command": "request", "action": sub, "user_id": event.author_id}
    if card_id is not None:
        fields["card_id"] = card_id
    log_event(log, "admin_command", **fields)


async def _list(wake: WakeRequests) -> str:
    rows = await wake.list_open()
    if not rows:
        return text.REQUEST_LIST_EMPTY
    return "\n".join(text.REQUEST_LIST_LINE.format(slug=slug, n=count, age=_age(seconds))
                     for slug, count, seconds in rows)


async def _clear(ctx: CardContext, wake: WakeRequests, slug: str) -> tuple[str, int | None]:
    """Closes the card's open requests as cleared. Returns the reply and the card id (None for an unknown slug)."""
    card = await ctx.db.run(lambda c: cards.get_by_slug(c, slug))
    if card is None:
        return text.NO_SUCH_CARD, None
    closed = await wake.close(card.id, "cleared")
    if closed == 0:
        return text.REQUEST_NONE_OPEN.format(slug=card.slug), card.id
    return text.REQUEST_CLEARED.format(slug=card.slug, people=people(closed)), card.id


def _age(seconds: int) -> str:
    """An open request's age as a short phrase: under a minute, whole minutes, or hours and minutes."""
    if seconds < MINUTE:
        return "<1 min"
    if seconds < HOUR:
        return f"{seconds // MINUTE} min"
    return f"{seconds // HOUR} h {seconds % HOUR // MINUTE} min"
