# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repo hygiene: no tracked secrets, no real ids, no non-allowlisted addresses or hosts, REUSE-clean,
no source-bot imports.

Secret files are matched by name only and are never opened by the scan.
"""

from __future__ import annotations

import ast
import builtins
import fnmatch
import ipaddress
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


# Network scan. Everything not listed here is a finding. The allowlist holds only what tracked files
# contain today, plus the reserved and public names a fixture or a doc may need.
NETWORK_SCHEMES = ("http", "https", "ws", "wss", "ftp", "ssh")
ALLOWED_IPS = frozenset({"127.0.0.1", "0.0.0.0", "::1", "::"})  # loopback and unspecified addresses only
ALLOWED_HOSTS = frozenset({
    "example.com", "example.org", "example.net",  # reserved for documentation (RFC 2606)
    "www.gnu.org", "fsf.org",                     # the AGPL text in LICENSES/
    "docs.fluxer.app",                            # Fluxer's public documentation
    "unused", "override",                         # placeholder hosts in test fixtures
})
RESERVED_SUFFIXES = (".invalid", ".test", ".example", ".localhost")  # RFC 2606 and RFC 6761
# Bare dotted names are checked only when they end in one of these: public TLDs and the names a
# private network uses. Other dotted words (file names, Python attribute access) are ignored.
# "info" is left out on purpose: `log.info(...)` and `logging.INFO` would be findings on every run.
# Tailnet names need no entry of their own: their last label is "net", which is already checked.
CHECKED_TLDS = frozenset({
    "com", "net", "org", "io", "app", "cloud", "dev", "me", "xyz", "co", "uk", "de",
    "lan", "local", "home", "arpa", "internal", "intranet", "corp",
    "family", "network", "site", "online", "tech", "biz", "us", "ca", "eu", "nl", "fr",
})
URL_AUTHORITY = re.compile(r"\b(?:" + "|".join(NETWORK_SCHEMES) + r")://([^\s/?#'\"`<>]*)", re.IGNORECASE)
# Every 4-part dotted quad is a finding unless it is literally allowlisted. No parse is required, so a
# zero-padded form (octets with leading zeros) is still caught.
DOTTED_IPV4 = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?![\w]|\.\d)")
DOTTED_QUAD = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
# A bare IPv6 candidate has at least two colons and hex groups. It is a finding only if ipaddress parses
# it as IPv6, which rules out times (12:30:45) and MAC-style runs (6 groups, no ::). Bracketed forms are
# URL host syntax and are checked by the URL branch; inside brackets a candidate is a Python slice
# (xs[::2]), so brackets are excluded here.
IPV6_CANDIDATE = re.compile(r"(?<![\w:.\[])([0-9A-Fa-f]*(?::[0-9A-Fa-f]*){2,8})(?![\w:\]]|\.[0-9A-Za-z])")
BARE_NAME = re.compile(r"(?<![\w./-])((?:[a-z0-9-]+\.)+[a-z]{2,63})(?![\w-])", re.IGNORECASE)


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _ip_outside_allowlist(value: str) -> bool:
    return _is_ip(value) and str(ipaddress.ip_address(value)) not in ALLOWED_IPS


def _host_allowed(host: str) -> bool:
    return host in ALLOWED_HOSTS or host.endswith(RESERVED_SUFFIXES)


def _url_host(authority: str) -> str:
    """The host of a URL authority, with userinfo, port and brackets removed. Empty when there is none."""
    host = authority.rsplit("@", 1)[-1]
    if host.startswith("["):
        inside = host[1:].split("]", 1)[0]
        return inside if _is_ip(inside) else ""  # a bracket with no address is a fragment, not a host
    return host.split(":", 1)[0].rstrip(".").lower()


def network_kinds(line: str) -> list[str]:
    """The kinds of non-allowlisted address or host on one line. Values are never returned, only kinds."""
    kinds: set[str] = set()
    for match in DOTTED_IPV4.finditer(line):
        if match.group(1) not in ALLOWED_IPS:
            kinds.add("ipv4")
    for match in IPV6_CANDIDATE.finditer(line):
        if _ip_outside_allowlist(match.group(1)) and ":" in match.group(1):
            kinds.add("ipv6")
    for match in URL_AUTHORITY.finditer(line):
        host = _url_host(match.group(1))
        if not host:
            continue
        if DOTTED_QUAD.fullmatch(host):
            if host not in ALLOWED_IPS:
                kinds.add("ipv4")
        elif _is_ip(host):
            if ":" in host and _ip_outside_allowlist(host):
                kinds.add("ipv6")
        elif not _host_allowed(host):
            kinds.add("url-host")
    for match in BARE_NAME.finditer(line):
        host = match.group(1).lower()
        if host.rsplit(".", 1)[-1] in CHECKED_TLDS and not _host_allowed(host):
            kinds.add("host")
    return sorted(kinds)


def network_offenders(root: Path) -> list[str]:
    """Tracked files under `root` with a non-allowlisted address or host, as `path:line kind`.

    Secrets are skipped before any read, and a tracked path that is missing or no longer a regular
    file is skipped. A finding names the location and kind only, never the value: CI logs are public.
    """
    offenders = []
    for path in tracked_files(root):
        if is_secret(path):
            continue
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", "ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            for kind in network_kinds(line):
                offenders.append(f"{path.relative_to(root)}:{number} {kind}")
    return offenders


def test_no_network_leaks_in_tracked_files():
    offenders = network_offenders(PROJECT)
    assert offenders == [], f"tracked files contain non-allowlisted addresses or hosts (path:line kind): {offenders}"


def _tracked_repo(root: Path, files: dict[str, str]) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=60)
    for name, text in files.items():
        (root / name).write_text(text, "utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-f", *files], check=True, timeout=60)
    return root


# Built from parts so this file carries no dotted address or private host as a literal.
SAMPLE_IP = ".".join(["203", "0", "113", "9"])  # a documentation address, not on the allowlist
SAMPLE_HOST = "".join(["nas", ".", "lan"])      # a private-network name, not on the allowlist


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_network_scan_catches_ipv4_outside_allowlist(tmp_path):
    repo = _tracked_repo(tmp_path, {"notes.txt": f"reach it at {SAMPLE_IP}\n"})
    offenders = network_offenders(repo)
    assert offenders == ["notes.txt:1 ipv4"]
    assert SAMPLE_IP not in "".join(offenders), "a finding must not echo the value"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_network_scan_catches_private_host_in_url(tmp_path):
    url = "".join(["ws://", SAMPLE_HOST, "/gateway"])
    repo = _tracked_repo(tmp_path, {"cfg.txt": f"gateway {url}\n"})
    assert network_offenders(repo) == ["cfg.txt:1 url-host"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_network_scan_catches_bare_private_host(tmp_path):
    repo = _tracked_repo(tmp_path, {"ops.txt": f"log in to {SAMPLE_HOST} first\n"})
    assert network_offenders(repo) == ["ops.txt:1 host"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_network_scan_passes_allowlisted_hosts_and_code_shapes(tmp_path):
    text = "\n".join([
        "https://fluxer.invalid/api", "ws://fake-gateway.test/gateway", "http://example.com/",
        "ws://unused/gw", "docs.fluxer.app", "127.0.0.1:8080", "0.0.0.0", "http://[::1]:9000/", "bind ::",
        "https://<instance>/api", "attachment://card.png",
        "self.rest.request(logging.getLogger(__name__))  # os.environ, asyncio.run",
        "panel.db ci.yml REVIEW.md pyproject.toml .env.example build 2026.1031.154542",
    ]) + "\n"
    repo = _tracked_repo(tmp_path, {"ok.txt": text})
    assert network_offenders(repo) == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_network_scan_catches_private_host_after_at_sign(tmp_path):
    repo = _tracked_repo(tmp_path, {"mail.txt": f"write to k@{SAMPLE_HOST} for access\n"})
    assert network_offenders(repo) == ["mail.txt:1 host"]


# Built from parts: a dotted quad with leading zeros, which ipaddress does not parse.
LEADING_ZERO_IP = ".".join(["192", "168", "001", "005"])
# Built from parts: a unique-local IPv6 address, not on the allowlist.
SAMPLE_IPV6 = ":".join(["fd00", "", "1"])


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_network_scan_catches_dotted_ip_with_leading_zeros(tmp_path):
    repo = _tracked_repo(tmp_path, {"notes.txt": f"reach it at {LEADING_ZERO_IP}\n"})
    assert network_offenders(repo) == ["notes.txt:1 ipv4"]
    assert LEADING_ZERO_IP not in "".join(network_offenders(repo)), "a finding must not echo the value"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_network_scan_catches_leading_zero_ip_in_url(tmp_path):
    url = "".join(["http://", LEADING_ZERO_IP, "/api"])
    repo = _tracked_repo(tmp_path, {"u.txt": f"call {url}\n"})
    assert network_offenders(repo) == ["u.txt:1 ipv4"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_network_scan_catches_bare_ipv6_outside_url(tmp_path):
    repo = _tracked_repo(tmp_path, {"r.txt": f"route via {SAMPLE_IPV6}.\n"})
    assert network_offenders(repo) == ["r.txt:1 ipv6"]
    assert SAMPLE_IPV6 not in "".join(network_offenders(repo)), "a finding must not echo the value"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
@pytest.mark.parametrize("tld", ["family", "network", "site", "online", "tech", "biz", "us", "ca", "eu", "nl", "fr"])
def test_network_scan_catches_host_on_added_tld(tmp_path, tld):
    repo = _tracked_repo(tmp_path, {"hosts.txt": f"ssh to nas.{tld} today\n"})
    assert network_offenders(repo) == ["hosts.txt:1 host"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_network_scan_catches_tailnet_host(tmp_path):
    host = ".".join(["box", "tail1234", "ts", "net"])
    repo = _tracked_repo(tmp_path, {"t.txt": f"ssh {host}\n"})
    assert network_offenders(repo) == ["t.txt:1 host"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_network_scan_passes_times_slices_and_logging_levels(tmp_path):
    text = "\n".join([
        "log.info(msg) logging.INFO timeout 12:30:45 xs[1:2] ys[::2] ops@example.com",
        "at 2026-11-01T12:30:45Z mac aa:bb:cc:dd:ee:ff",
    ]) + "\n"
    repo = _tracked_repo(tmp_path, {"code.txt": text})
    assert network_offenders(repo) == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_network_scan_never_opens_secret_files(tmp_path, monkeypatch):
    repo = _tracked_repo(tmp_path, {
        ".env": f"HOST={SAMPLE_HOST}\n",
        "key.pem": f"{SAMPLE_IP}\n",
        ".env.example": "FLUXER_TOKEN=\n",
    })

    opened: list[str] = []

    def recording(real):
        def wrapper(self, *args, **kwargs):
            opened.append(self.name)
            return real(self, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(Path, "open", recording(Path.open))
    real_open = builtins.open

    def recording_open(file, *args, **kwargs):
        opened.append(Path(file).name)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording_open)

    assert network_offenders(repo) == []
    assert ".env.example" in opened, "the scan must still read ordinary tracked files"
    assert not set(opened) & {".env", "key.pem"}, f"secret files were opened: {sorted(set(opened))}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")
def test_network_scan_skips_tracked_file_deleted_from_disk(tmp_path):
    repo = _tracked_repo(tmp_path, {"gone.txt": f"{SAMPLE_IP}\n"})
    (repo / "gone.txt").unlink()
    assert network_offenders(repo) == []


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
    # Guard against a vacuous pass: the boundary is only meaningful once the panel package exists.
    assert (PROJECT / "src" / "fluxer_panel" / "__init__.py").is_file(), "fluxer_panel package is missing"
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
