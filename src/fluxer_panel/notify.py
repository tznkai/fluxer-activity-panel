# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Direct messages: to every admin, and to one user (a presser or a requester). Neither logs message content. The
user DM never logs the user's id (AC6.3); the admin DM keeps the admin id it always logged."""

from __future__ import annotations

import logging

from fluxer_client.rest import FluxerREST
from fluxer_panel.config import Config
from fluxer_panel.dm_backoff import DmBackoff
from fluxer_panel.logs import log_event

log = logging.getLogger(__name__)


async def dm_admins(rest: FluxerREST, config: Config, content: str, *, failed_event: str) -> None:
    for user_id in sorted(config.admin_user_ids):
        try:
            dm = await rest.open_dm(user_id)
            await rest.send_message(dm, content=content, allowed_mentions={"parse": []})
        except Exception as exc:  # noqa: BLE001 - one admin's failed DM must not stop the other admins' DMs
            log_event(log, failed_event, level=logging.WARNING, user_id=user_id, reason=type(exc).__name__)


async def dm_user(rest: FluxerREST, user_id: str, content: str, *, backoff: DmBackoff | None = None,
                   bypass: bool = False) -> tuple[str, str] | None:
    """Opens a DM with `user_id` and sends `content`. Returns (channel_id, message_id), or None on failure, logged as
    `user_dm_failed` with the exception type only. It never logs user_id: the recipient is a presser or a requester.

    With a `backoff`, a user in backoff gets no request at all (`user_dm_skipped`, reason `backoff`), unless `bypass` is
    set. Every attempt moves the backoff: a failure starts it and a success clears it."""
    if backoff is not None and not bypass and backoff.blocked(user_id):
        log_event(log, "user_dm_skipped", reason="backoff")
        return None
    try:
        dm = await rest.open_dm(user_id)
        sent = await rest.send_message(dm, content=content, allowed_mentions={"parse": []})
    except Exception as exc:  # noqa: BLE001 - a failed DM must not break the press that asked for it
        log_event(log, "user_dm_failed", level=logging.WARNING, reason=type(exc).__name__)
        if backoff is not None:
            backoff.failed(user_id)
        return None
    if backoff is not None:
        backoff.succeeded(user_id)
    return dm, sent["id"]
