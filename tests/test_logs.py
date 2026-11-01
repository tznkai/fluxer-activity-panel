# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Allowlisted JSON logging and token redaction."""

import json
import logging
import warnings
from collections.abc import Callable, Iterator
from io import StringIO

import pytest

from fluxer_panel.logs import log_event, setup_logging

TOKEN = "SENTINEL-TOKEN-7f3a9c"
CONTENT = "SENTINEL-CONTENT-b41e"
ESCAPY = 'SENT-"\'-\\-7f3a9c'   # quote, apostrophe and backslash: repr() and json.dumps() both escape it


@pytest.fixture
def install() -> Iterator[Callable[[str], StringIO]]:
    """setup_logging replaces every root handler and the root level, and captures warnings: undo all three."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level

    def _install(token: str) -> StringIO:
        stream = StringIO()
        setup_logging("DEBUG", token, stream)
        return stream

    try:
        yield _install
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging.captureWarnings(False)


@pytest.fixture
def captured(install) -> StringIO:
    return install(TOKEN)


@pytest.fixture
def captured_escapy(install) -> StringIO:
    return install(ESCAPY)


def lines(stream: StringIO) -> list[str]:
    return [line for line in stream.getvalue().splitlines() if line]


def parsed(stream: StringIO) -> list[dict]:
    return [json.loads(line) for line in lines(stream)]


def assert_no_token_forms(text: str, token: str) -> None:
    """The raw token, and its repr() and json.dumps() escaped forms, must all be absent."""
    for form in {token, repr(token)[1:-1], json.dumps(token)[1:-1]}:
        assert form not in text


def test_allowlist_keeps_known_keys_and_drops_content_and_guild_snowflake(captured):
    log = logging.getLogger("fluxer_panel.test")
    log_event(log, "x", user_id="1001", guild_id="12x", content=CONTENT, nickname="bob",
              command="ping", count=3)
    (record,) = parsed(captured)
    assert record["event"] == "x"
    assert record["user_id"] == "1001"
    assert record["command"] == "ping"
    assert record["count"] == 3
    assert "guild_id" not in record
    assert "content" not in record
    assert "nickname" not in record
    assert CONTENT not in captured.getvalue()


def test_non_identifier_string_and_non_finite_number_are_dropped(captured):
    log = logging.getLogger("fluxer_panel.test")
    log_event(log, "y", command="has space", status=float("nan"))
    (record,) = parsed(captured)
    assert "command" not in record
    assert "status" not in record


def test_plain_stdlib_record_is_valid_json_with_message_as_event(captured):
    logging.getLogger("fluxer_client.rest").warning("rate limited on %s", "GET /x")
    (record,) = parsed(captured)
    assert record["event"] == "rate limited on GET /x"
    assert record["logger"] == "fluxer_client.rest"
    assert record["level"] == "WARNING"


def test_every_line_is_valid_json(captured):
    log = logging.getLogger("fluxer_panel.test")
    log_event(log, "a", user_id="1001", content=CONTENT)
    logging.getLogger("fluxer_client.rest").warning("rate limited on %s", "GET /x")
    log_event(log, "b", command="has space", status=float("nan"))
    raw = lines(captured)
    assert len(raw) == 3
    for line in raw:
        json.loads(line)


def test_token_in_message_and_args_is_redacted(captured):
    logging.getLogger("x").error("boom %s", TOKEN)
    text = captured.getvalue()
    assert "[REDACTED]" in text
    assert TOKEN not in text


def test_token_in_exception_text_is_redacted(captured):
    log = logging.getLogger("x")
    try:
        raise RuntimeError(f"bad {TOKEN}")
    except RuntimeError:
        log.exception("failed")
    (record,) = parsed(captured)
    assert "[REDACTED]" in record["exc"]
    assert TOKEN not in captured.getvalue()


def test_token_in_chained_cause_is_redacted(captured):
    log = logging.getLogger("x")
    try:
        try:
            raise RuntimeError(f"inner {TOKEN}")
        except RuntimeError as inner:
            raise ValueError("outer") from inner
    except ValueError:
        log.exception("failed")
    (record,) = parsed(captured)
    assert "[REDACTED]" in record["exc"]
    assert TOKEN not in captured.getvalue()


def test_mismatched_format_args_do_not_raise_and_still_redact(captured):
    log = logging.getLogger("x")
    log.warning(f"bad {TOKEN} %s %s", 1)            # stdlib would raise TypeError from getMessage()
    log.warning("two args, one given %s %s", TOKEN)
    records = parsed(captured)
    assert len(records) == 2
    assert records[0]["event"] == "bad [REDACTED] %s %s (unformattable args)"
    assert records[1]["event"] == "two args, one given %s %s (unformattable args)"   # args are dropped, not printed
    assert_no_token_forms(captured.getvalue(), TOKEN)


def test_repr_escaped_token_in_args_is_redacted(captured_escapy):
    logging.getLogger("x").warning("headers %r and raw %s", ESCAPY, ESCAPY)
    (record,) = parsed(captured_escapy)
    assert record["event"] == "headers '[REDACTED]' and raw [REDACTED]"   # %r keeps its own quotes
    assert_no_token_forms(captured_escapy.getvalue(), ESCAPY)


def test_repr_escaped_token_in_exception_traceback_is_redacted(captured_escapy):
    log = logging.getLogger("x")
    try:
        raise RuntimeError(f"gateway connection failed: {ESCAPY!r}")
    except RuntimeError:
        log.exception("gateway connection failed: %r", ESCAPY)
    (record,) = parsed(captured_escapy)
    assert "[REDACTED]" in record["exc"]
    assert_no_token_forms(captured_escapy.getvalue(), ESCAPY)


def test_warning_text_with_token_is_json_and_redacted(captured):
    with warnings.catch_warnings():
        warnings.simplefilter("always")               # -W error would otherwise raise instead of showing it
        warnings.warn(f"leaky {TOKEN}", UserWarning, stacklevel=1)
    (record,) = parsed(captured)
    assert record["logger"] == "py.warnings"
    assert record["level"] == "WARNING"
    assert "[REDACTED]" in record["event"]
    assert_no_token_forms(captured.getvalue(), TOKEN)
