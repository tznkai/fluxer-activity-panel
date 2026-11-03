# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static checks on the Pterodactyl plugin: GET only, three read endpoints, no write or socket paths (AC2.9)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parent.parent / "src" / "fluxer_panel" / "sources" / "pterodactyl.py"
WRITE_METHODS = {"post", "put", "patch", "delete", "head", "options", "request", "ws_connect"}
FORBIDDEN_FRAGMENTS = (
    "/websocket",
    "websocket",
    "/power",
    "/command",
    "/files",
    "/schedules/",
    "/tasks",
    "/backups",
    "/startup",
    "/settings",
    "/users",
    "/network",
)


@pytest.fixture(scope="module")
def tree() -> ast.Module:
    return ast.parse(MODULE.read_text(encoding="utf-8"), filename=str(MODULE))


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _attr_name(func: ast.expr) -> str | None:
    return func.attr if isinstance(func, ast.Attribute) else None


def test_no_write_or_socket_method_is_called(tree) -> None:
    offenders = [call for call in _calls(tree) if _attr_name(call.func) in WRITE_METHODS]
    assert offenders == []


def test_exactly_one_session_get_call_and_it_is_get(tree) -> None:
    session_calls = [
        call
        for call in _calls(tree)
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Attribute)
        and isinstance(call.func.value.value, ast.Name)
        and call.func.value.value.id == "self"
        and call.func.value.attr == "_session"
    ]
    assert len(session_calls) == 1
    assert session_calls[0].func.attr == "get"


def test_no_module_level_request_or_new_session(tree) -> None:
    for call in _calls(tree):
        func = call.func
        if isinstance(func, ast.Name):
            assert func.id != "ClientSession"
        if isinstance(func, ast.Attribute):
            assert func.attr != "ClientSession"
            if isinstance(func.value, ast.Name) and func.value.id == "aiohttp":
                assert func.attr != "request"


def test_endpoints_is_the_three_read_paths(tree) -> None:
    assigned = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "_ENDPOINTS" for target in node.targets)
    ]
    assert len(assigned) == 1
    assert isinstance(assigned[0], ast.Tuple)
    assert ast.literal_eval(assigned[0]) == ("", "/resources", "/schedules")


def test_every_get_helper_call_uses_a_constant_allowed_endpoint(tree) -> None:
    allowed = ("", "/resources", "/schedules")
    helper_calls = [call for call in _calls(tree) if _attr_name(call.func) == "_get"]
    assert helper_calls, "expected at least one _get call"
    for call in helper_calls:
        endpoint = call.args[1] if len(call.args) > 1 else None
        assert isinstance(endpoint, ast.Constant), "endpoint must be a string literal"
        assert endpoint.value in allowed


def test_no_string_fragment_names_a_forbidden_path(tree) -> None:
    fragments: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            fragments.append(node.value)
        if isinstance(node, ast.JoinedStr):
            fragments.extend(
                part.value for part in node.values if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
    for text in fragments:
        lowered = text.lower()
        for forbidden in FORBIDDEN_FRAGMENTS:
            assert forbidden not in lowered, f"forbidden fragment {forbidden!r} in a string"


def test_url_is_built_under_the_client_servers_prefix(tree) -> None:
    fstrings = [node for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)]
    assert any(
        any(
            isinstance(part, ast.Constant) and "/api/client/servers/" in str(part.value)
            for part in node.values
        )
        for node in fstrings
    )
