# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural check (AC3.10, AC3.11): only `follow_roles.py` changes roles, no role is ever deleted, and inside
`follow_roles.py` the role id of a member-role call comes only from `stored.role_id` in `toggle_follow`."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PANEL = SRC / "fluxer_panel"
ROLE_METHODS = {"add_member_role", "remove_member_role", "create_role", "set_role_mentionable"}
MEMBER_ROLE_METHODS = {"add_member_role", "remove_member_role"}
DELETE_NAMES = {"delete_role", "remove_role"}


def parse_tree(root: Path) -> dict[str, ast.Module]:
    """Every .py file under `root`, parsed, keyed by its path relative to `root`."""
    return {str(p.relative_to(root)): ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            for p in sorted(root.rglob("*.py"))}


def attribute_uses(tree: ast.AST, names: set[str]) -> list[ast.Attribute]:
    """Every attribute access (call or reference) whose attribute name is in `names`."""
    return [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in names]


def test_only_follow_roles_uses_the_role_methods() -> None:
    files = {rel for rel, tree in parse_tree(PANEL).items() if attribute_uses(tree, ROLE_METHODS)}

    assert files == {"follow_roles.py"}


def test_no_source_file_deletes_a_role() -> None:
    offenders = []
    for rel, tree in parse_tree(SRC).items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in DELETE_NAMES:
                offenders.append((rel, node.attr))
            if isinstance(node, ast.Name) and node.id in DELETE_NAMES:
                offenders.append((rel, node.id))
            if isinstance(node, ast.FunctionDef) and node.name in DELETE_NAMES:
                offenders.append((rel, node.name))
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and "/roles/" in node.value and "DELETE" in node.value):
                offenders.append((rel, "DELETE /roles/ literal"))

    assert offenders == []


def find_function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    found = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    assert len(found) == 1, f"expected one {name}"
    return found[0]


def role_id_argument(call: ast.Call) -> ast.expr | None:
    """The third argument of a member-role call (guild, user, role), positional or keyword."""
    if len(call.args) > 2:
        return call.args[2]
    return next((k.value for k in call.keywords if k.arg == "role_id"), None)


def test_member_role_calls_take_their_role_id_from_the_stored_role_in_toggle_follow() -> None:
    tree = parse_tree(PANEL)["follow_roles.py"]
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr in MEMBER_ROLE_METHODS]
    toggle = find_function(tree, "toggle_follow")
    inside = [n for n in ast.walk(toggle) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr in MEMBER_ROLE_METHODS]

    assert len(calls) == len(inside) == 2, "member-role calls must all be inside toggle_follow"
    for call in calls:
        arg = role_id_argument(call)
        assert isinstance(arg, ast.Name) and arg.id == "role_id"

    assert "role_id" not in {a.arg for a in toggle.args.args + toggle.args.kwonlyargs}
    assignments = []
    for node in ast.walk(toggle):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "role_id":
                assignments.append(value)

    assert assignments, "toggle_follow must assign role_id"
    for value in assignments:
        assert isinstance(value, ast.Attribute) and value.attr == "role_id"
        assert isinstance(value.value, ast.Name) and value.value.id == "stored"
