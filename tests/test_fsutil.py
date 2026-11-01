# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for fluxer_panel.fsutil: atomic replace and non-overwriting paths."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from fluxer_panel import fsutil
from fluxer_panel.fsutil import atomic_write, next_free_path, quarantine_target


def test_atomic_write_creates_parents_and_writes_exact_bytes(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "state.bin"
    payload = b"\x00\xffexact\n"

    atomic_write(target, payload)

    assert target.read_bytes() == payload


def test_atomic_write_replaces_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "state.bin"
    target.write_bytes(b"old")

    atomic_write(target, b"new")

    assert target.read_bytes() == b"new"


def test_atomic_write_default_mode_is_0644(tmp_path: Path) -> None:
    target = tmp_path / "state.bin"

    atomic_write(target, b"x")

    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_atomic_write_honours_explicit_mode(tmp_path: Path) -> None:
    target = tmp_path / "secret.bin"

    atomic_write(target, b"x", mode=0o600)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_failed_replace_keeps_original_and_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "state.bin"
    target.write_bytes(b"original")

    def boom(src: object, dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(fsutil.os, "replace", boom)

    with pytest.raises(OSError, match="simulated replace failure"):
        atomic_write(target, b"replacement")

    assert target.read_bytes() == b"original"
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".state.bin.") and p.name.endswith(".tmp")]
    assert leftovers == []


def test_quarantine_target_numbers_without_overwriting(tmp_path: Path) -> None:
    db = tmp_path / "x.db"

    first = quarantine_target(db)
    assert first == tmp_path / "x.db.corrupt"
    first.write_bytes(b"kept")

    second = quarantine_target(db)
    assert second == tmp_path / "x.db.corrupt.1"
    second.write_bytes(b"kept too")

    third = quarantine_target(db)
    assert third == tmp_path / "x.db.corrupt.2"


def test_next_free_path_follows_same_numbering(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    assert next_free_path(db, ".pre-v2") == tmp_path / "p.db.pre-v2"

    (tmp_path / "p.db.pre-v2").write_bytes(b"x")
    assert next_free_path(db, ".pre-v2") == tmp_path / "p.db.pre-v2.1"

    (tmp_path / "p.db.pre-v2.1").write_bytes(b"x")
    assert next_free_path(db, ".pre-v2") == tmp_path / "p.db.pre-v2.2"
