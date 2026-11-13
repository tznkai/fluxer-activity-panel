# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin commands. `authorize` is the single authorization check."""

from __future__ import annotations

import logging

from fluxer_client.events import MessageCreate
from fluxer_client.ids import is_snowflake
from fluxer_client.rest import FluxerREST
from fluxer_panel import card_commands, request_commands
from fluxer_panel.card_commands import CardContext
from fluxer_panel.config import Config
from fluxer_panel.logs import log_event

log = logging.getLogger(__name__)
SUBCOMMANDS = ("ping",)
HEADS = ("admin", "card", "request")


def authorize(event: MessageCreate, config: Config) -> bool:
    """The only authorization check. DM only, human only, exact string match on a validated id."""
    return (
        event.guild_id is None
        and not event.author_is_bot
        and is_snowflake(event.author_id)
        and event.author_id in config.admin_user_ids
    )


def is_admin_id(user_id: str, config: Config) -> bool:
    """For a guild reaction, where `authorize`'s DM-only rule cannot apply: exact match on a validated id."""
    return is_snowflake(user_id) and user_id in config.admin_user_ids


def parse_head(content: str, prefix: str) -> tuple[str, str] | None:
    """'<prefix><head> <args>' -> (head, args) for a known head; None when the message is not a command."""
    text = content.strip()
    for head in HEADS:
        full = f"{prefix}{head}"
        if text == full or text.startswith(full + " "):
            return head, text[len(full):].strip()
    return None


def parse_command(content: str, prefix: str) -> str | None:
    """'<prefix>admin <sub> ...' -> sub ('' when absent); None when the message is not an admin command."""
    parsed = parse_head(content, prefix)
    if parsed is None or parsed[0] != "admin":
        return None
    rest = parsed[1].split()
    return rest[0].lower() if rest else ""


async def handle(event: MessageCreate, config: Config, rest: FluxerREST, card_ctx: CardContext | None = None) -> None:
    parsed = parse_head(event.content, config.prefix)
    if parsed is None:
        return
    head, args = parsed
    if not authorize(event, config):
        log_event(log, "admin_denied", level=logging.WARNING,
                  user_id=event.author_id, channel_id=event.channel_id)
        return
    if head == "card":
        if card_ctx is None:
            return
        await card_commands.handle(args, event, card_ctx)
        return
    if head == "request":
        if card_ctx is None:
            return
        await request_commands.handle(args, event, card_ctx)
        return
    sub = args.split()[0].lower() if args else ""
    if sub == "ping":
        await rest.send_message(event.channel_id, content="pong")
        log_event(log, "admin_command", command="ping", user_id=event.author_id)
        return
    await rest.send_message(event.channel_id,
                            content="Subcommands: " + ", ".join(f"{config.prefix}admin {s}" for s in SUBCOMMANDS))
    log_event(log, "admin_command", command="unknown", user_id=event.author_id)
