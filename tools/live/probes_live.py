# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run probe check: query each LIVE_PROBES target and print its counts, or the reason it is unreachable.

Run from the project root: .venv/bin/python tools/live/probes_live.py (needs .env; not run by CI).
LIVE_PROBES is a comma-separated list of kind:host:port entries. Hosts and ports are never printed: targets are
labelled <target#n> and <port#n>. An unreachable target is recorded with its reason and is not a failure.
"""
from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from _live import (
    LiveRefused,
    Redactor,
    Results,
    main,
    out,
    read_env,
    redacted_logging,
    require_vars,
    use_redactor,
)
from fluxer_panel.sources.base import ProbeFailed, ProbeResult
from fluxer_panel.sources.registry import PROBES

PROBE_TIMEOUT = 3.0   # seconds, the same default as PROBE_TIMEOUT_SECONDS in the bot
PORT = re.compile(r"[0-9]{1,5}")


@dataclass(frozen=True)
class Target:
    kind: str
    host: str = field(repr=False)
    port: int = field(repr=False)


def parse_probe_targets(raw: str, kinds: Collection[str]) -> list[Target]:
    """kind:host:port entries. Refuses by naming the variable and the entry number, never a value."""
    entries = [part.strip() for part in raw.split(",") if part.strip()]
    if not entries:
        raise LiveRefused("LIVE_PROBES lists no targets")
    targets: list[Target] = []
    for number, entry in enumerate(entries, start=1):
        kind, _, rest = entry.partition(":")
        host, _, port_text = rest.rpartition(":")
        if kind not in kinds or not host or not PORT.fullmatch(port_text) or not 0 < int(port_text) <= 65535:
            raise LiveRefused(f"LIVE_PROBES entry {number} is not kind:host:port with a known kind and port")
        targets.append(Target(kind=kind, host=host, port=int(port_text)))
    return targets


def probe_summary(result: ProbeResult | ProbeFailed) -> str:
    """Counts and the details the probe returned (absent details are left out), or the failure reason."""
    if isinstance(result, ProbeFailed):
        return f"unreachable reason={result.reason}"
    details = [f"{name}={value}" for name, value in (("map", result.details.map),
                                                      ("version", result.details.version),
                                                      ("game_mode", result.details.game_mode))
               if value is not None]
    return " ".join([f"reachable counts={result.counts.online}/{result.counts.max}", *details])


def probe_redactor(env: Mapping[str, str], targets: Sequence[Target]) -> Redactor:
    """The script's redactor, with each target host hidden and registered for main()'s error line."""
    redactor = Redactor(token=env.get("FLUXER_TOKEN", ""), extra_secrets=tuple(target.host for target in targets))
    use_redactor(redactor)
    return redactor


async def run() -> Results:
    env = read_env()
    require_vars(env, ("LIVE_PROBES",))
    targets = parse_probe_targets(env["LIVE_PROBES"], PROBES.keys())
    redactor = probe_redactor(env, targets)
    res = Results(redactor)
    with redacted_logging(redactor):
        for target in targets:
            host_label = redactor.label_text(target.host, "target")
            port_label = redactor.label_text(str(target.port), "port")
            name = f"{host_label}:{port_label} ({target.kind})"
            result = await PROBES[target.kind].probe(target.host, target.port, PROBE_TIMEOUT)
            out(redactor, name, probe_summary(result))
            if isinstance(result, ProbeResult):
                res.ok(name)
    return res


if __name__ == "__main__":
    main(run)
