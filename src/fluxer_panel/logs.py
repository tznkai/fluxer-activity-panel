# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""JSON-lines logging: an event-name allowlist for panel code and a token-redaction backstop."""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TextIO

from fluxer_client.ids import is_snowflake

ID_KEYS = frozenset({"guild_id", "channel_id", "message_id", "user_id", "role_id"})
OTHER_KEYS = frozenset({
    "event_type", "command", "status", "code", "count", "duration_ms", "card_id", "field", "kind", "reason",
    "action", "surface", "outcome", "session_id", "poll_id", "sub_id", "mode", "scope_sub_id", "via_sub_id", "mirror_id",
})
# Third-party loggers that log more than warnings. a2s logs raw packets at DEBUG (python-a2s 1.4.2).
PINNED_LOGGERS = ("a2s", "mcstatus")
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


_ESCAPE_ROUNDS = 3   # a token can be escaped once per nested repr: an exception message repr'd inside a %r is two levels


def _escapes(text: str) -> set[str]:
    """One level of escaping: what `text` becomes when it is repr'd, json-dumped or quoted into another string."""
    return {
        repr(text)[1:-1],
        json.dumps(text)[1:-1],
        text.replace("\\", "\\\\").replace("'", "\\'"),
        text.replace("\\", "\\\\").replace('"', '\\"'),
    }


def token_variants(token: str) -> list[str]:
    """The token as it can appear in text, raw or escaped up to _ESCAPE_ROUNDS levels (e.g. inside nested %r).

    Each round escapes only the forms the previous round added, so a plain token stays just itself and the
    set is bounded by 4 + 4**2 + 4**3 escaped forms.
    """
    if not token:
        return []
    variants = {token}
    frontier = {token}
    for _ in range(_ESCAPE_ROUNDS):
        frontier = {escaped for text in frontier for escaped in _escapes(text)} - variants
        if not frontier:
            break
        variants |= frontier
    variants.discard("")
    return sorted(variants, key=len, reverse=True)   # longest first: an escaped form contains the raw one's pieces


class TokenRedactionFilter(logging.Filter):
    """Handler-level backstop: replaces the token in the message, args and traceback before formatting."""

    def __init__(self, secrets: Sequence[str]) -> None:
        super().__init__()
        if isinstance(secrets, str):   # a bare str would iterate character by character
            raise TypeError("secrets must be a sequence of strings")
        variants: set[str] = set()
        for secret in secrets:
            variants.update(token_variants(secret))
        self._variants = sorted(variants, key=len, reverse=True)   # longest first, as token_variants does

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


def setup_logging(level: str, secrets: Sequence[str], stream: TextIO | None = None) -> logging.Handler:
    """Replace root handlers with one JSON handler on stdout (or `stream`); returns it (tests remove it).

    Every non-empty secret is redacted. Also routes `warnings` through logging so warning text gets the same
    JSON format and redaction, and pins the probe libraries' loggers to WARNING.
    Tests that call this must restore it with `logging.captureWarnings(False)`.
    """
    redaction = TokenRedactionFilter(secrets)   # validates `secrets` before any logging state is changed
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(redaction)
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    for name in PINNED_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.captureWarnings(True)
    return handler
