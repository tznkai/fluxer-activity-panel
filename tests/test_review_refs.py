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

# Spans that are not citations and are skipped: a span containing "://" (a URL, with or without a
# port) and a bare IPv4 address with a port. A clock time such as 12:30 has no letter before its
# colon, so it is never treated as a citation candidate.
URL_OR_IPV4_PORT = r"(?![^`\s]*://)(?!\d{1,3}(?:\.\d{1,3}){3}:\d+`)"

# Any citation anywhere in the file: a path containing a dot or a slash, a line number, and the
# backticked anchor that must appear on that line. The anchor group is None when it is missing.
CITATION = re.compile(r"`" + URL_OR_IPV4_PORT + r"([^`\s]*[./][^`\s]*):(\d+)`(?: `([^`\n]*)`)?")

# Every backticked span, and the shapes that look like a citation but are not one CITATION can
# check: a span with a letter before its colon and a digit after it (a range such as `:9-10`, or a
# path with no dot or slash such as `Makefile:9`). Such a span fails rather than being skipped.
SPAN = re.compile(r"`([^`\n]+)`")
CITATION_LIKE = re.compile(r"[^`\s]*[A-Za-z][^`\s]*:\d[^`]*")
CITATION_BODY = re.compile(r"([^`\s]*[./][^`\s]*):(\d+)")
IPV4_PORT = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}:\d+")

# A line that starts a bullet with a citation, indented or not, whatever the bullet marker.
BULLET_CITATION = re.compile(r"^\s*[-*+]\s+`[^`]*:\d+`")

MIN_ANCHOR = 4

TOPICS = [
    "Token handling",
    "Logging",
    "Admin authorization",
    "Role assignment",
    "Role creation",
    "Reaction removal",
    "Reactions and presses",
    "Play-now mentions",
    "Purge deletion",
    "Emoji and path encoding",
    "DB quarantine and backups",
    "Live-tool redaction",
    ".env handling",
    "Published-tree hygiene",
    "Pterodactyl read-only access",
    "Schedule payloads",
    "Probe details",
    "Plugin boundary",
    "Card rendering",
    "Card commands",
    "Card poller",
    "Private denylist",
    "Session time parser",
    "Session purge scrub",
    "Waitlist promotion",
    "Session channel guard",
    "Mirror links and mentions",
    "Poll channel guard",
    "Non-admin DM path",
    "DM backoff",
    "What the polls prompt got wrong",
    "What the phase 1 prompt got wrong",
    "What the phase 2 prompt got wrong",
    "What the phase 3 prompt got wrong",
    "What the phase 4 prompt got wrong",
]

# Prose sections state findings in words and cite no code. Every other topic must hold a reference.
PROSE_TOPICS = {
    "What the polls prompt got wrong",
    "What the phase 1 prompt got wrong",
    "What the phase 2 prompt got wrong",
    "What the phase 3 prompt got wrong",
    "What the phase 4 prompt got wrong",
}


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


def _unsupported_citations(text: str) -> list[str]:
    problems: list[str] = []
    for match in SPAN.finditer(text):
        span = match.group(1)
        if "://" in span or IPV4_PORT.fullmatch(span):
            continue
        if CITATION_LIKE.match(span) and not CITATION_BODY.fullmatch(span):
            problems.append(
                f"`{span}` -> unsupported citation form "
                "(use a single path:line with a file extension or directory)"
            )
    return problems


def test_review_file_exists() -> None:
    _read_review()


def test_no_unsupported_citation_form() -> None:
    problems = _unsupported_citations(_read_review())
    assert not problems, "REVIEW.md unsupported citation forms:\n" + "\n".join(problems)


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

    empty = [topic for topic in TOPICS if topic not in PROSE_TOPICS and not REFERENCE.search(sections[topic])]
    assert not empty, f"REVIEW.md sections with no reference: {empty}"
