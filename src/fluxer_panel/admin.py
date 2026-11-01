# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin commands. `authorize` is the single authorization check."""

from __future__ import annotations

import logging

from fluxer_client.events import MessageCreate
from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerREST
from fluxer_panel.config import Config
from fluxer_panel.logs import log_event

log = logging.getLogger(__name__)
SUBCOMMANDS = ("ping",)


def authorize(event: MessageCreate, config: Config) -> bool:
    """The only authorization check. DM only, human only, exact string match on a validated id."""
    return (
        event.guild_id is None
        and not event.author_is_bot
        and is_snowflake(event.author_id)
        and event.author_id in config.admin_user_ids
    )


def parse_command(content: str, prefix: str) -> str | None:
    """'<prefix>admin <sub> ...' -> sub ('' when absent); None when the message is not an admin command."""
    head = f"{prefix}admin"
    text = content.strip()
    if text != head and not text.startswith(head + " "):
        return None
    rest = text[len(head):].split()
    return rest[0].lower() if rest else ""


async def handle(event: MessageCreate, config: Config, rest: FluxerREST) -> None:
    sub = parse_command(event.content, config.prefix)
    if sub is None:
        return
    if not authorize(event, config):
        log_event(log, "admin_denied", level=logging.WARNING,
                  user_id=event.author_id, channel_id=event.channel_id)
        return
    if sub == "ping":
        await rest.send_message(event.channel_id, content="pong")
        log_event(log, "admin_command", command="ping", user_id=event.author_id)
        return
    await rest.send_message(event.channel_id,
                            content="Subcommands: " + ", ".join(f"{config.prefix}admin {s}" for s in SUBCOMMANDS))
    log_event(log, "admin_command", command="unknown", user_id=event.author_id)
