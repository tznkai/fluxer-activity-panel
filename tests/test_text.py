# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The user-facing strings module: static shape only, and no mention of guilds."""

import ast
from pathlib import Path

import pytest

TEXT_PATH = Path(__file__).resolve().parents[1] / "src" / "fluxer_panel" / "text.py"


def text_module_violations(source: str) -> list[str]:
    """Shape rules for a strings module. Returns the rule broken by each offending statement."""
    problems: list[str] = []
    tree = ast.parse(source)
    for index, node in enumerate(tree.body):
        if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # the module docstring
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            problems.append(f"line {node.lineno}: only module-level name assignments are allowed")
            continue
        if not _is_string_or_string_dict(node.value):
            problems.append(f"line {node.lineno}: value must be a str or a dict of str to str")
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "guild" in node.value.lower():
            problems.append(f"line {node.lineno}: string mentions guild")
    return problems


def _is_string_or_string_dict(value: ast.expr) -> bool:
    if isinstance(value, ast.Constant):
        return isinstance(value.value, str)
    if isinstance(value, ast.Dict):
        return all(_is_str_constant(key) and _is_str_constant(item) for key, item in zip(value.keys, value.values, strict=True))
    return False


def _is_str_constant(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def test_text_module_obeys_the_shape_rules() -> None:
    assert text_module_violations(TEXT_PATH.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "source",
    [
        'GUILD_NAME = "server"\nSERVER = "a Guild"\n',
        'X = "ok"\ndef helper():\n    return "x"\n',
        'X = "ok"\nY = 1 + 2\n',
        'X = "ok"\nLIST = ["a"]\n',
        'X = "ok"\nDICT = {"a": 1}\n',
        'X = "ok"\nA, B = "a", "b"\n',
        'X = "ok"\nif True:\n    Y = "no"\n',
        'X = "ok"\nDICT = {**{}}\n',
    ],
)
def test_shape_rule_refuses_logic_and_guild_text(source: str) -> None:
    assert text_module_violations(source) != []


def test_shape_rule_accepts_docstring_strings_and_str_dicts() -> None:
    source = '"""Doc."""\nA = "one"\nB = {"k": "v", "k2": "v2"}\n'
    assert text_module_violations(source) == []
