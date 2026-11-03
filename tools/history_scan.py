# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run scan of the subtree split's history for private details.

Run from the project directory: .venv/bin/python tools/history_scan.py [--prefix fluxer-activity-panel]

The prefix is split from HEAD of the monorepo, and the split's history is scanned. Commit messages, the file paths
on each diff header, and every added or removed patch line are checked against the private denylist and the network
and long-digit rules of tests/test_repo_hygiene.py. A hit prints the short commit sha and the kind, never the matched
text. The denylist is read from the main checkout, and a scan without it is refused.

Exit codes: 0 clean, 1 hits, 2 refused (no denylist, a bad denylist entry, or a split that git cannot make).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from tests.test_repo_hygiene import denylist_path, load_denylist, network_kinds  # noqa: E402

DEFAULT_PREFIX = "fluxer-activity-panel"
SHORT_SHA = 12
COMMIT_MARK = "\x00"  # `%x00` in the log format starts each commit
DIFF_START = "diff --git "
HUNK_START = "@@"
LONG_DIGITS = re.compile(r"[0-9]{15,}")
EXIT_CLEAN, EXIT_HITS, EXIT_REFUSED = 0, 1, 2

Hit = tuple[str, str]  # (full commit sha, kind such as "patch:denylist")


def _kinds(text: str, literals: Sequence[str]) -> set[str]:
    """The kinds of hit on one piece of text. The text is never returned, only the kinds."""
    kinds = set(network_kinds(text))
    if any(literal in text for literal in literals):
        kinds.add("denylist")
    if LONG_DIGITS.search(text):
        kinds.add("long-digits")
    return kinds


def scan_message_lines(lines: Iterable[str], literals: Sequence[str]) -> Iterator[Hit]:
    """Hits in commit messages, from a stream of `--format=%x00%H%n%B` lines. The sha line is not message text."""
    sha = ""
    for raw in lines:
        line = raw.rstrip("\n")
        if line.startswith(COMMIT_MARK):
            sha = line[len(COMMIT_MARK):]
            continue
        for kind in _kinds(line, literals):
            yield sha, f"message:{kind}"


def scan_patch_lines(lines: Iterable[str], literals: Sequence[str]) -> Iterator[Hit]:
    """Hits in diffs, from a stream of `-p --format=%x00%H` lines.

    A diff's header runs from its `diff --git` line to its first `@@`. Only the `diff --git` line is checked there, as a
    path, and the `---` and `+++` lines are skipped. Inside a hunk, every line that starts with `+` or `-` is checked,
    including a removed line whose content starts with `--`.
    """
    sha = ""
    in_hunk = False
    for raw in lines:
        line = raw.rstrip("\n")
        if line.startswith(COMMIT_MARK):
            sha = line[len(COMMIT_MARK):]
            in_hunk = False
            continue
        if line.startswith(DIFF_START):
            in_hunk = False
            for kind in _kinds(line[len(DIFF_START):], literals):
                yield sha, f"path:{kind}"
            continue
        if line.startswith(HUNK_START):
            in_hunk = True
            continue
        if in_hunk and line[:1] in ("+", "-"):
            for kind in _kinds(line[1:], literals):
                yield sha, f"patch:{kind}"


def _split(repo: Path, prefix: str) -> str | None:
    """The sha of the split commit for `prefix` at HEAD, or None when git cannot make it."""
    try:
        done = subprocess.run(["git", "-C", str(repo), "subtree", "split", f"--prefix={prefix}", "HEAD"],
                              capture_output=True, text=True, check=True, timeout=900)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = done.stdout.strip().splitlines()
    return lines[-1] if lines else None


def _stream(repo: Path, args: list[str], scan: Callable[[Iterable[str], Sequence[str]], Iterator[Hit]],
            literals: Sequence[str]) -> set[Hit] | None:
    """Scans one `git log` stream. None when git exits with an error."""
    process = subprocess.Popen(["git", "-C", str(repo), "log", *args], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    assert process.stdout is not None
    try:
        hits = set(scan((raw.decode("utf-8", "replace") for raw in process.stdout), literals))
    finally:
        process.stdout.close()
        returncode = process.wait()
    return hits if returncode == 0 else None


def run(repo: Path, prefix: str, denylist_file: Path | None) -> int:
    """Scans the split of `prefix` in `repo` and prints one line per hit. Returns the exit code."""
    if denylist_file is None or not denylist_file.is_file():
        print("refused: the private denylist is absent, so the scan cannot show the history clean")
        return EXIT_REFUSED
    try:
        literals = load_denylist(denylist_file)
    except (OSError, ValueError):
        print("refused: the denylist could not be loaded (a short entry, or unreadable text)")
        return EXIT_REFUSED
    split = _split(repo, prefix)
    if split is None:
        print(f"refused: git cannot split {prefix!r} from HEAD")
        return EXIT_REFUSED
    messages = _stream(repo, ["--no-patch", "--format=%x00%H%n%B", split], scan_message_lines, literals)
    patches = _stream(repo, ["-p", "--no-color", "--no-ext-diff", "--format=%x00%H", split],
                      scan_patch_lines, literals)
    if messages is None or patches is None:
        print("refused: git log failed on the split history")
        return EXIT_REFUSED
    hits = messages | patches
    for sha, kind in sorted(hits):
        print(f"{sha[:SHORT_SHA]} {kind}")
    if hits:
        print(f"{len(hits)} hit(s); fix them before publishing")
        return EXIT_HITS
    print("clean: no hits in the subtree history")
    return EXIT_CLEAN


def _monorepo_root(start: Path) -> Path | None:
    try:
        top = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, check=True, timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(top) if top else None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan the subtree split's history for private details.")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="the directory to split (default: %(default)s)")
    args = parser.parse_args(argv)
    monorepo = _monorepo_root(PROJECT.parent)
    if monorepo is None:
        print("refused: cannot find the monorepo root")
        return EXIT_REFUSED
    return run(monorepo, args.prefix, denylist_path(PROJECT))


if __name__ == "__main__":
    sys.exit(main())
