# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repo hygiene: no tracked secrets, no real ids in published files, REUSE-clean, no source-bot imports.

Secret files are matched by name only and are never opened by the scan.
"""

from __future__ import annotations

import ast
import builtins
import fnmatch
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]

SECRET_NAMES = {".env"}
SECRET_GLOBS = (".env.*", "*.pem", "*.key", "id_rsa*")
SECRET_EXEMPT_NAMES = {".env.example"}


def is_secret(path: Path) -> bool:
    name = path.name
    if name in SECRET_EXEMPT_NAMES:
        return False
    return name in SECRET_NAMES or any(fnmatch.fnmatchcase(name, glob) for glob in SECRET_GLOBS)


def tracked_files(root: Path) -> list[Path]:
    """Paths git tracks under `root`. Lists names only; opens no file."""
    try:
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True, check=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("no git index")
    return [root / name for name in listing.decode("utf-8", "surrogateescape").split("\0") if name]


def test_no_secret_file_is_tracked():
    secrets = [str(p.relative_to(PROJECT)) for p in tracked_files(PROJECT) if is_secret(p)]
    assert secrets == [], f"secret files must not be tracked by git: {secrets}"


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.production", "server.pem", "deploy.key",
                                  "id_rsa", "id_rsa.pub"])
def test_secret_denylist_names(name):
    assert is_secret(Path(name))


@pytest.mark.parametrize("name", [".env.example", "README.md", "pyproject.toml"])
def test_templates_and_ordinary_files_are_not_secret(name):
    assert not is_secret(Path(name))


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_hygiene_check_never_opens_secret_files(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=60)
    names = [".env", ".env.local", "key.pem", ".env.example"]
    for name in names:
        (tmp_path / name).write_text("TOKEN=placeholder\n", "utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", *names], check=True, timeout=60)

    opened: list[str] = []

    def recording(real):
        def wrapper(self, *args, **kwargs):
            opened.append(self.name)
            return real(self, *args, **kwargs)
        return wrapper

    for method in ("open", "read_text", "read_bytes"):
        monkeypatch.setattr(Path, method, recording(getattr(Path, method)))

    real_open = builtins.open

    def recording_open(file, *args, **kwargs):
        opened.append(Path(file).name)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording_open)

    found = sorted(p.name for p in tracked_files(tmp_path) if is_secret(p))
    assert found == [".env", ".env.local", "key.pem"]
    secret_names = {".env", ".env.local", "key.pem"}
    opened_secrets = sorted(set(opened) & secret_names)
    assert not opened_secrets, f"secret files were opened: {opened_secrets}"


def long_digit_offenders(root: Path) -> list[str]:
    """Tracked files under `root` that contain a 15+ digit run. Secrets are skipped before any read.

    A tracked path that is missing on disk (deleted but still tracked) or is no longer a regular
    file (e.g. now a directory) is skipped rather than read.
    """
    offenders = []
    for path in tracked_files(root):
        if is_secret(path):
            continue
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", "ignore")
        if re.search(r"[0-9]{15,}", text) is not None:
            offenders.append(str(path.relative_to(root)))
    return offenders


def test_no_long_digit_runs_in_tracked_files():
    offenders = long_digit_offenders(PROJECT)
    assert offenders == [], f"tracked files contain 15+ digit runs (possible real ids): {offenders}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_digit_scan_skips_tracked_file_deleted_from_disk(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=60)
    (tmp_path / "gone.txt").write_text("deleted after add\n", "utf-8")
    (tmp_path / "live.txt").write_text("1" * 15 + "\n", "utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "gone.txt", "live.txt"], check=True, timeout=60)
    (tmp_path / "gone.txt").unlink()

    assert sorted(p.name for p in tracked_files(tmp_path)) == ["gone.txt", "live.txt"]
    assert long_digit_offenders(tmp_path) == ["live.txt"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_digit_scan_skips_tracked_path_that_became_directory(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=60)
    (tmp_path / "item").write_text("plain\n", "utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "item"], check=True, timeout=60)
    (tmp_path / "item").unlink()
    (tmp_path / "item").mkdir()
    (tmp_path / "item" / "inner.txt").write_text("plain\n", "utf-8")

    assert long_digit_offenders(tmp_path) == []


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


# Built from parts so the source bot's package name never appears as a literal in published files.
SOURCE_BOT_PACKAGE = "_".join(["green", "oaks", "bot"])


def test_no_source_bot_imports():
    offenders = []
    for top in ("src", "tests"):
        for path in sorted((PROJECT / top).rglob("*.py")):
            tree = ast.parse(path.read_text("utf-8"), filename=str(path))
            for module in _imported_modules(tree):
                if module == SOURCE_BOT_PACKAGE or module.startswith(SOURCE_BOT_PACKAGE + "."):
                    offenders.append(f"{path.relative_to(PROJECT)}: {module}")
    assert offenders == [], f"source bot imports found: {offenders}"


def test_fluxer_client_never_imports_fluxer_panel():
    offenders = []
    for path in sorted((PROJECT / "src" / "fluxer_client").rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            if module == "fluxer_panel" or module.startswith("fluxer_panel."):
                offenders.append(f"{path.relative_to(PROJECT)}: {module}")
    assert offenders == [], f"fluxer_client imports fluxer_panel: {offenders}"


def test_reuse_lint_passes():
    result = subprocess.run(
        [sys.executable, "-m", "reuse", "--root", str(PROJECT), "lint"],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


CI_WORKFLOW = PROJECT / ".github" / "workflows" / "ci.yml"
CI_REQUIRED_STEPS = (
    "run: python -m pytest -q -W error",
    "run: ruff check src tests tools",
    "run: reuse --root . lint",
)


def test_ci_runs_the_three_checks():
    lines = {line.strip().removeprefix("- ") for line in CI_WORKFLOW.read_text("utf-8").splitlines()}
    missing = [step for step in CI_REQUIRED_STEPS if step not in lines]
    assert missing == [], f"ci.yml is missing these steps: {missing}"
