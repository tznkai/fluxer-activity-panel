# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for tools/history_scan.py: the subtree-history scan.

Parser branches are tested on synthetic `git log` streams. End-to-end behaviour is tested on temp git repos only.
Literals are built from parts, so this file carries none of the values it checks for.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "history_scan.py"
_spec = importlib.util.spec_from_file_location("history_scan", TOOL)
history_scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(history_scan)

LITERAL = "SENT" + "INEL-HIST-7d1a"
DENY = [LITERAL]
SAMPLE_IP = ".".join(["203", "0", "113", "9"])
SAMPLE_HOST = "".join(["nas", ".", "lan"])
SAMPLE_IPV6 = ":".join(["fd00", "", "1"])
LONG_RUN = "1" * 15
SHA = "a" * 40
NUL = "\x00"
IDENTITY = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *IDENTITY, *args], capture_output=True, text=True,
                          check=True, timeout=60).stdout


def _repo(root: Path, commits: list[tuple[str, dict[str, str | bytes | None]]]) -> Path:
    """Each commit is (message, {path: text or bytes}); a None text deletes the path."""
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    for message, files in commits:
        for name, text in files.items():
            path = root / name
            if text is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(text, bytes):
                    path.write_bytes(text)
                else:
                    path.write_text(text, "utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "--allow-empty", "-m", message)
    return root


def _denylist(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "denylist.txt"
    path.write_text(text, "utf-8")
    return path


def _patch_hits(lines: list[str], literals: list[str] = DENY) -> set[tuple[str, str]]:
    return set(history_scan.scan_patch_lines(lines, literals))


def _message_hits(lines: list[str], literals: list[str] = DENY) -> set[tuple[str, str]]:
    return set(history_scan.scan_message_lines(lines, literals))


# Synthetic streams: every parser branch, without git.


def test_removed_line_whose_content_starts_with_dashes_is_checked() -> None:
    # A removed SQL comment "-- LIT" shows as "--- LIT". It is a hunk line, not a header line.
    lines = [NUL + SHA, "", "diff --git a/src/x.sql b/src/x.sql", "index 1..2 100644",
             "--- a/src/x.sql", "+++ b/src/x.sql", "@@ -1 +0,0 @@", "--- " + LITERAL]
    assert _patch_hits(lines) == {(SHA, "patch:denylist")}


def test_a_new_diff_header_after_a_hunk_is_not_hunk_content() -> None:
    lines = [NUL + SHA, "", "diff --git a/one.txt b/one.txt", "@@ -0,0 +1 @@", "+plain",
             "diff --git a/two.txt b/two.txt", "--- a/two.txt", "+++ b/" + LITERAL]
    assert _patch_hits(lines) == set()


def test_header_lines_are_not_checked_as_patch_content() -> None:
    lines = [NUL + SHA, "", "diff --git a/clean.txt b/clean.txt", "--- a/clean.txt", "+++ b/" + LITERAL]
    assert _patch_hits(lines) == set()


def test_diff_header_path_is_checked_as_path() -> None:
    lines = [NUL + SHA, "", f"diff --git a/{LITERAL}.txt b/{LITERAL}.txt"]
    assert _patch_hits(lines) == {(SHA, "path:denylist")}


def test_message_lines_are_checked_and_the_sha_line_is_not() -> None:
    sha_line_with_digits = NUL + LONG_RUN + "a" * 25
    lines = [sha_line_with_digits, "subject", "", "body mentions " + LITERAL]
    assert _message_hits(lines) == {(LONG_RUN + "a" * 25, "message:denylist")}


@pytest.mark.parametrize(("sample", "kind"), [
    (LITERAL, "denylist"),
    (SAMPLE_IP, "ipv4"),
    ("ssh " + SAMPLE_HOST, "host"),
    ("ws://" + SAMPLE_HOST + "/gateway", "url-host"),
    (SAMPLE_IPV6, "ipv6"),
    (LONG_RUN, "long-digits"),
])
def test_each_kind_is_reported_from_a_patch_line(sample: str, kind: str) -> None:
    lines = [NUL + SHA, "", "diff --git a/x.txt b/x.txt", "@@ -0,0 +1 @@", "+" + sample]
    assert _patch_hits(lines) == {(SHA, f"patch:{kind}")}


@pytest.mark.parametrize(("sample", "kind"), [
    (LITERAL, "denylist"),
    (SAMPLE_IP, "ipv4"),
    ("ssh " + SAMPLE_HOST, "host"),
    ("ws://" + SAMPLE_HOST + "/gateway", "url-host"),
    (SAMPLE_IPV6, "ipv6"),
    (LONG_RUN, "long-digits"),
])
def test_each_kind_is_reported_from_a_message_line(sample: str, kind: str) -> None:
    assert _message_hits([NUL + SHA, sample]) == {(SHA, f"message:{kind}")}


def test_a_clean_stream_has_no_hits() -> None:
    lines = [NUL + SHA, "clean subject", "", "diff --git a/x.txt b/x.txt", "@@ -0,0 +1 @@", "+plain text"]
    assert _patch_hits(lines) == set()
    assert _message_hits(lines) == set()


def test_each_hit_is_attributed_to_the_commit_that_contains_it() -> None:
    other = "b" * 40
    lines = [NUL + SHA, "", "diff --git a/x.txt b/x.txt", "@@ -0,0 +1 @@", "+plain",
             NUL + other, "", "diff --git a/y.txt b/y.txt", "@@ -0,0 +1 @@", "+" + LITERAL]
    assert _patch_hits(lines) == {(other, "patch:denylist")}


# Attribution trailers. Their hosts are built from parts, as the other host literals in this file are.
ANTHROPIC_HOST = ".".join(["anthropic", "com"])
CLAUDE_AI_HOST = ".".join(["claude", "ai"])
CO_AUTHORED = "Co-Authored-By: Claude Haiku 5.5 <noreply@" + ANTHROPIC_HOST + ">"
CLAUDE_SESSION = "Claude-Session: https://" + CLAUDE_AI_HOST + "/code/session_Ab3dE5fG7hJ9kLmN"


def test_both_attribution_trailers_are_not_hits() -> None:
    assert _message_hits([NUL + SHA, "subject", "", CO_AUTHORED, CLAUDE_SESSION]) == set()


@pytest.mark.parametrize("line", [
    "Co-Authored-By: Claude <noreply@" + ANTHROPIC_HOST.upper() + ">",
    "Claude-Session: https://" + CLAUDE_AI_HOST.upper() + "/code/session_Ab3dE5fG7hJ9kLmN",
])
def test_an_upper_case_attribution_host_on_a_trailer_is_not_a_hit(line: str) -> None:
    assert _message_hits([NUL + SHA, "subject", "", line]) == set()


def test_a_denylist_literal_that_contains_an_attribution_host_is_still_a_hit_on_a_trailer() -> None:
    # The literal holds the attribution host, so checking the stripped text would miss it.
    literal = "tag:" + ANTHROPIC_HOST + ";"
    line = "Co-Authored-By: Claude " + literal
    assert _message_hits([NUL + SHA, line], [literal]) == {(SHA, "message:denylist")}


@pytest.mark.parametrize(("line", "kind"), [
    (CO_AUTHORED + " " + LITERAL, "denylist"),
    (CO_AUTHORED + " " + SAMPLE_IP, "ipv4"),
    (CLAUDE_SESSION + " " + LONG_RUN, "long-digits"),
    (CO_AUTHORED + " " + SAMPLE_HOST, "host"),
    (CLAUDE_SESSION + " https://" + SAMPLE_HOST + "/x", "url-host"),
])
def test_an_attribution_line_still_gets_every_other_check(line: str, kind: str) -> None:
    assert _message_hits([NUL + SHA, line]) == {(SHA, f"message:{kind}")}


@pytest.mark.parametrize("line", [
    "Mention noreply@" + ANTHROPIC_HOST,
    "note: " + CO_AUTHORED,
    "co-authored-by: Claude <noreply@" + ANTHROPIC_HOST + ">",
])
def test_the_attribution_host_outside_a_trailer_line_is_a_hit(line: str) -> None:
    assert _message_hits([NUL + SHA, line]) == {(SHA, "message:host")}


@pytest.mark.parametrize(("line", "kinds"), [
    ("Co-Authored-By: Claude <noreply@sub." + ANTHROPIC_HOST + ">", ("host",)),
    ("Co-Authored-By: Claude <noreply@" + ANTHROPIC_HOST + ".lan>", ("host",)),
    ("Claude-Session: https://" + "www." + CLAUDE_AI_HOST + "/code/x", ("url-host",)),
    ("Claude-Session: https://" + CLAUDE_AI_HOST + "@" + SAMPLE_HOST + "/code/x", ("host", "url-host")),
])
def test_the_trailer_exemption_covers_only_the_exact_attribution_hosts(line: str, kinds: tuple[str, ...]) -> None:
    assert _message_hits([NUL + SHA, line]) == {(SHA, f"message:{kind}") for kind in kinds}


# End to end on temp repos.


@needs_git
def test_literal_on_a_prefixed_patch_is_reported_by_short_sha_and_kind(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path / "mono", [("add notes", {"proj/notes.txt": f"clean\nhas {LITERAL} inside\n"})])

    code = history_scan.run(repo, "proj", _denylist(tmp_path, LITERAL + "\n"))

    out = capsys.readouterr().out
    assert code == 1
    assert re.search(r"^[0-9a-f]{12} patch:denylist$", out, re.MULTILINE), out
    assert LITERAL not in out, "the matched literal must never be printed"


@needs_git
def test_literal_only_outside_the_prefix_is_not_reported(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path / "mono", [
        ("add prefixed file", {"proj/a.txt": "clean\n"}),
        (f"outside note {LITERAL}", {"other/b.txt": f"{LITERAL}\n"}),
    ])

    code = history_scan.run(repo, "proj", _denylist(tmp_path, LITERAL + "\n"))

    out = capsys.readouterr().out
    assert code == 0
    assert "clean" in out
    assert LITERAL not in out


@needs_git
def test_literal_only_in_a_prefixed_commit_message_is_reported_as_message(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path / "mono", [(f"note {LITERAL}", {"proj/a.txt": "clean\n"})])

    code = history_scan.run(repo, "proj", _denylist(tmp_path, LITERAL + "\n"))

    out = capsys.readouterr().out
    assert code == 1
    assert re.search(r"^[0-9a-f]{12} message:denylist$", out, re.MULTILINE), out
    assert " patch:" not in out
    assert LITERAL not in out


@needs_git
def test_literal_on_a_removed_line_is_reported_as_patch(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path / "mono", [
        ("add migration", {"proj/m.sql": f"-- {LITERAL}\nSELECT 1;\n"}),
        ("drop comment", {"proj/m.sql": "SELECT 1;\n"}),
    ])

    code = history_scan.run(repo, "proj", _denylist(tmp_path, LITERAL + "\n"))

    out = capsys.readouterr().out
    assert code == 1
    assert "patch:denylist" in out
    assert LITERAL not in out


@needs_git
def test_literal_added_only_by_a_merge_resolution_is_reported(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path / "mono", [("base", {"proj/f.txt": "base\n"})])
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "proj" / "f.txt").write_text("side\n", "utf-8")
    _git(repo, "commit", "-q", "-am", "side edit")
    _git(repo, "checkout", "-q", "-")
    (repo / "proj" / "f.txt").write_text("main\n", "utf-8")
    _git(repo, "commit", "-q", "-am", "main edit")
    # The merge conflicts on f.txt; the resolution is the only place the literal appears.
    subprocess.run(["git", "-C", str(repo), *IDENTITY, "merge", "--no-ff", "-q", "side"], capture_output=True,
                   check=False, timeout=60)
    (repo / "proj" / "f.txt").write_text(f"resolved {LITERAL}\n", "utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-edit")

    code = history_scan.run(repo, "proj", _denylist(tmp_path, LITERAL + "\n"))

    out = capsys.readouterr().out
    assert code == 1, out
    assert "patch:denylist" in out
    assert LITERAL not in out


@needs_git
def test_literal_in_a_binary_file_is_reported(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path / "mono", [("add blob", {"proj/blob.bin": b"\x00\x01" + LITERAL.encode() + b"\n"})])

    code = history_scan.run(repo, "proj", _denylist(tmp_path, LITERAL + "\n"))

    out = capsys.readouterr().out
    assert code == 1, out
    assert "patch:denylist" in out
    assert LITERAL not in out


@needs_git
def test_literal_hidden_by_a_textconv_filter_is_reported(tmp_path: Path, capsys) -> None:
    # Git takes attributes from the working tree, so the pattern is at the repo root and matches by basename.
    repo = _repo(tmp_path / "mono", [
        ("add doc", {"proj/doc.dat": f"{LITERAL}\n", ".gitattributes": "*.dat diff=hide\n"}),
    ])
    _git(repo, "config", "diff.hide.textconv", "echo clean")

    code = history_scan.run(repo, "proj", _denylist(tmp_path, LITERAL + "\n"))

    out = capsys.readouterr().out
    assert code == 1, out
    assert "patch:denylist" in out
    assert LITERAL not in out


@needs_git
def test_a_denylisted_literal_in_a_non_ascii_path_is_reported_as_path(tmp_path: Path, capsys) -> None:
    denied = "é" + LITERAL
    repo = _repo(tmp_path / "mono", [("add file", {f"proj/{denied}.txt": "plain text\n"})])

    code = history_scan.run(repo, "proj", _denylist(tmp_path, denied + "\n"))

    out = capsys.readouterr().out
    assert code == 1, out
    assert re.search(r"^[0-9a-f]{12} path:denylist$", out, re.MULTILINE), out
    assert LITERAL not in out


@needs_git
def test_a_clean_subtree_exits_0(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path / "mono", [("add file", {"proj/a.txt": "plain text\n"})])

    code = history_scan.run(repo, "proj", _denylist(tmp_path, LITERAL + "\n"))

    assert code == 0
    assert "clean" in capsys.readouterr().out


@needs_git
def test_a_commit_with_both_attribution_trailers_exits_0(tmp_path: Path, capsys) -> None:
    message = "add notes\n\n" + CO_AUTHORED + "\n" + CLAUDE_SESSION + "\n"
    repo = _repo(tmp_path / "mono", [(message, {"proj/a.txt": "plain text\n"})])

    code = history_scan.run(repo, "proj", _denylist(tmp_path, LITERAL + "\n"))

    out = capsys.readouterr().out
    assert code == 0, out
    assert "clean" in out


@needs_git
def test_an_attribution_host_in_the_body_outside_a_trailer_exits_1(tmp_path: Path, capsys) -> None:
    message = "add notes\n\nsee mail@" + ANTHROPIC_HOST + "\n" + CO_AUTHORED + "\n"
    repo = _repo(tmp_path / "mono", [(message, {"proj/a.txt": "plain text\n"})])

    code = history_scan.run(repo, "proj", _denylist(tmp_path, LITERAL + "\n"))

    out = capsys.readouterr().out
    assert code == 1
    assert re.search(r"^[0-9a-f]{12} message:host$", out, re.MULTILINE), out


@needs_git
def test_missing_denylist_refuses_with_exit_2(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path / "mono", [("add file", {"proj/a.txt": "plain text\n"})])

    assert history_scan.run(repo, "proj", None) == 2
    assert history_scan.run(repo, "proj", tmp_path / "absent.txt") == 2
    assert "refused" in capsys.readouterr().out


@needs_git
def test_short_denylist_entry_refuses_with_exit_2_without_echoing_it(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path / "mono", [("add file", {"proj/a.txt": "plain text\n"})])

    code = history_scan.run(repo, "proj", _denylist(tmp_path, "abc\n"))

    out = capsys.readouterr().out
    assert code == 2
    assert "abc" not in out


@needs_git
def test_prefix_git_cannot_split_refuses_with_exit_2(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path / "mono", [("add file", {"proj/a.txt": "plain text\n"})])

    code = history_scan.run(repo, "no-such-prefix", _denylist(tmp_path, LITERAL + "\n"))

    assert code == 2
    assert "git cannot split" in capsys.readouterr().out


@needs_git
def test_git_log_failure_on_the_split_refuses_with_exit_2(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path / "mono", [("add file", {"proj/a.txt": "plain text\n"})])

    def failing_log(log_repo, args, scan, literals):
        # The real log runner, given a revision that does not exist, so git itself exits with an error.
        return history_scan._stream(log_repo, ["no-such-revision"], scan, literals)

    code = history_scan.run(repo, "proj", _denylist(tmp_path, LITERAL + "\n"), stream=failing_log)

    assert code == 2
    assert "git log failed" in capsys.readouterr().out


@needs_git
def test_stream_returns_none_when_git_log_exits_with_an_error(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "mono", [("add file", {"proj/a.txt": "plain text\n"})])

    assert history_scan._stream(repo, ["no-such-revision"], history_scan.scan_message_lines, DENY) is None


@needs_git
def test_main_scans_the_named_prefix_with_the_given_denylist(tmp_path: Path, capsys, monkeypatch) -> None:
    repo = _repo(tmp_path / "mono", [("add notes", {"proj/notes.txt": f"has {LITERAL} inside\n"})])
    monkeypatch.setattr(history_scan, "_monorepo_root", lambda start: repo)
    monkeypatch.setattr(history_scan, "denylist_path", lambda root: _denylist(tmp_path, LITERAL + "\n"))

    assert history_scan.main(["--prefix", "proj"]) == 1
    assert "patch:denylist" in capsys.readouterr().out


def test_main_refuses_when_the_monorepo_root_cannot_be_found(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(history_scan, "_monorepo_root", lambda start: None)
    monkeypatch.setattr(history_scan, "denylist_path", lambda root: _denylist(tmp_path, LITERAL + "\n"))

    assert history_scan.main([]) == 2
    assert "cannot find the monorepo root" in capsys.readouterr().out
