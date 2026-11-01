# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""REVIEW.md must cite real code: each `path:line` points at a line that contains the anchor."""

from __future__ import annotations

import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
REVIEW = PROJECT / "REVIEW.md"

# A reference line: "- `path:line` `anchor` — reason".
REFERENCE = re.compile(r"^- `([^`:]+):(\d+)` `([^`]+)` — (.+)$", re.MULTILINE)

# Any citation anywhere in the file: a path containing a dot or a slash, a line number, and the
# backticked anchor that must appear on that line. The anchor group is None when it is missing.
CITATION = re.compile(r"`([^`\s]*[./][^`\s]*):(\d+)`(?: `([^`\n]*)`)?")

# A line that starts a bullet with a citation, indented or not, whatever the bullet marker.
BULLET_CITATION = re.compile(r"^\s*[-*+]\s+`[^`]*:\d+`")

MIN_ANCHOR = 4

TOPICS = [
    "Token handling",
    "Logging",
    "Admin authorization",
    "Role assignment",
    "Reaction removal",
    "Purge deletion",
    "Emoji and path encoding",
    "DB quarantine and backups",
    "Live-tool redaction",
    ".env handling",
    "What the phase 1 prompt got wrong",
]


def _sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = ""
        elif current is not None:
            sections[current] += line + "\n"
    return sections


def _headings(text: str) -> list[str]:
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def _read_review() -> str:
    assert REVIEW.is_file(), f"{REVIEW.name} is missing"
    return REVIEW.read_text(encoding="utf-8")


def test_review_file_exists() -> None:
    _read_review()


def test_no_duplicate_headings() -> None:
    headings = _headings(_read_review())
    duplicates = sorted({h for h in headings if headings.count(h) > 1})
    assert not duplicates, f"duplicate REVIEW.md headings: {duplicates}"


def test_every_reference_points_at_its_anchor() -> None:
    text = _read_review()
    citations = list(CITATION.finditer(text))
    assert citations, f"{REVIEW.name} has no `path:line` references"

    problems: list[str] = []
    for line in text.splitlines():
        if BULLET_CITATION.match(line) and not REFERENCE.fullmatch(line):
            problems.append(f"malformed reference line: {line!r}")
    for match in REFERENCE.finditer(text):
        if not match.group(4).strip():
            problems.append(f"{match.group(1)}:{match.group(2)} -> empty reason")

    for match in citations:
        rel, line_text, anchor = match.groups()
        label = f"{rel}:{line_text}"
        if anchor is None:
            problems.append(f"{label} -> citation has no backticked anchor after it")
            continue
        if len(anchor.strip()) < MIN_ANCHOR:
            problems.append(f"{label} `{anchor}` -> anchor shorter than {MIN_ANCHOR} characters")
        path = PROJECT / rel
        if not path.is_file():
            problems.append(f"{label} -> file not found")
            continue
        code = path.read_text(encoding="utf-8").splitlines()
        number = int(line_text)
        if not 1 <= number <= len(code):
            problems.append(f"{label} -> line out of range (file has {len(code)} lines)")
            continue
        found = code[number - 1]
        if anchor not in found:
            problems.append(f"{label} `{anchor}` -> anchor not on that line; line is {found.strip()!r}")

    assert not problems, "REVIEW.md reference problems:\n" + "\n".join(problems)


def test_every_required_topic_is_present_and_has_a_reference() -> None:
    text = _read_review()
    sections = _sections(text)
    missing = [topic for topic in TOPICS if topic not in sections]
    assert not missing, f"missing REVIEW.md headings: {missing}"

    empty = [topic for topic in TOPICS[:-1] if not REFERENCE.search(sections[topic])]
    assert not empty, f"REVIEW.md sections with no reference: {empty}"
