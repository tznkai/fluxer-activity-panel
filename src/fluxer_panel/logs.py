# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""JSON-lines logging: an event-name allowlist for panel code and a token-redaction backstop."""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TextIO

from fluxer_client.ids import is_snowflake

ID_KEYS = frozenset({"guild_id", "channel_id", "message_id", "user_id", "role_id"})
OTHER_KEYS = frozenset({"event_type", "command", "status", "code", "count", "duration_ms"})
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")   # str values of OTHER_KEYS must look like identifiers
FIELDS_ATTR = "panel_fields"


def clean_fields(fields: Mapping[str, object]) -> dict[str, object]:
    kept: dict[str, object] = {}
    for key, value in fields.items():
        if key in ID_KEYS:
            if is_snowflake(value):
                kept[key] = value
        elif key in OTHER_KEYS:
            if isinstance(value, bool) or (isinstance(value, (int, float)) and math.isfinite(value)):
                kept[key] = value
            elif isinstance(value, str) and _SAFE_TOKEN.fullmatch(value):
                kept[key] = value
    return kept


def log_event(logger: logging.Logger, name: str, *, level: int = logging.INFO, **fields: object) -> None:
    """The only way panel code logs: an event name plus allowlisted fields."""
    logger.log(level, "%s", name, extra={FIELDS_ATTR: clean_fields(fields)})


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        data.update(getattr(record, FIELDS_ATTR, {}))
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            data["exc"] = record.exc_text
        if record.stack_info:
            data["stack"] = self.formatStack(record.stack_info)
        return json.dumps(data, ensure_ascii=True, default=str)


def token_variants(token: str) -> list[str]:
    """The token as it can appear in text: raw, and escaped by repr() or json.dumps() (e.g. inside a %r)."""
    if not token:
        return []
    variants = {
        token,
        repr(token)[1:-1],
        json.dumps(token)[1:-1],
        token.replace("\\", "\\\\").replace("'", "\\'"),
        token.replace("\\", "\\\\").replace('"', '\\"'),
    }
    variants.discard("")
    return sorted(variants, key=len, reverse=True)   # longest first: an escaped form contains the raw one's pieces


class TokenRedactionFilter(logging.Filter):
    """Handler-level backstop: replaces the token in the message, args and traceback before formatting."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._variants = token_variants(token)

    def _redact(self, text: str) -> str:
        for variant in self._variants:
            text = text.replace(variant, "[REDACTED]")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed call must not raise into the caller (stdlib would swallow it)
            message = f"{record.msg!s} (unformattable args)"
        record.msg = self._redact(message)
        record.args = None
        if record.exc_info:
            record.exc_text = self._redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redact(record.stack_info)
        return True


def setup_logging(level: str, token: str, stream: TextIO | None = None) -> logging.Handler:
    """Replace root handlers with one JSON handler on stdout (or `stream`); returns it (tests remove it).

    Also routes `warnings` through logging so warning text gets the same JSON format and redaction.
    Tests that call this must restore it with `logging.captureWarnings(False)`.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(TokenRedactionFilter(token))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    logging.captureWarnings(True)
    return handler
