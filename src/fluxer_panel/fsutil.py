# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Filesystem helpers: atomic replace, durable renames and non-overwriting quarantine paths."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from fluxer_panel.logs import log_event

log = logging.getLogger(__name__)


def fsync_dir(directory: Path) -> None:
    """Make a rename durable. Best effort: the replace already happened, so a failure only logs."""
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        log_event(log, "fsync_dir_failed", level=logging.WARNING)
        return
    try:
        os.fsync(fd)
    except OSError:
        log_event(log, "fsync_dir_failed", level=logging.WARNING)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    """Replace `path` with `data` atomically: temp file in the same directory, fsync, os.replace, dir fsync.

    mkstemp creates 0600 files; `mode` is applied before the replace so e.g. a monitor user can read status.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), mode)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


def next_free_path(path: Path, suffix: str) -> Path:
    """`<name><suffix>`, then `<name><suffix>.1`, `.2`, ... — the first that doesn't exist (never overwrites)."""
    target = path.with_name(path.name + suffix)
    n = 0
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}{suffix}.{n}")
    return target


def quarantine_target(path: Path) -> Path:
    return next_free_path(path, ".corrupt")
