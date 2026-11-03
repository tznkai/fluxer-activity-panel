# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-run Pterodactyl check: the bot user's Panel permissions, power state and filtered schedules per server.

Run from the project root: .venv/bin/python tools/live/ptero_live.py (needs .env; not run by CI).
Only the Panel is contacted, and only through the plugin's own methods. Server ids come from LIVE_PTERO_SERVERS.
Schedule names, the Panel URL and the API key are never printed.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

import aiohttp
from yarl import URL

from _live import LiveRefused, Redactor, Results, main, out, read_env, redacted_logging, require_vars, use_redactor
from fluxer_panel.sources.base import PowerAction, SourceError
from fluxer_panel.sources.pterodactyl import PterodactylManager, Schedule, parse_schedules, select_next

SERVER_ID = re.compile(r"[0-9a-f]{8}")
EXPECTED_PERMISSIONS = frozenset({"schedule.read", "websocket.connect"})   # websocket.connect is forced by the Panel
REQUIRED = ("PTERODACTYL_URL", "PTERODACTYL_API_KEY", "LIVE_PTERO_SERVERS")


def ptero_secrets(env: Mapping[str, str]) -> tuple[str, ...]:
    """Values the redactor must hide: the Panel URL, its host, the host with its port (plain and bracketed), and the
    bot token. An aiohttp error names host:port, not the URL. The port is the effective one, so a default port
    is hidden too. Empty values are left out."""
    url = URL(env["PTERODACTYL_URL"])
    host, port = url.host, url.port
    host_forms = (host, f"{host}:{port}", f"[{host}]:{port}") if host and port else (host,)
    candidates = (env["PTERODACTYL_URL"], *host_forms, env.get("FLUXER_TOKEN"))
    return tuple(value for value in candidates if value)


def ptero_redactor(env: Mapping[str, str]) -> Redactor:
    """The script's redactor, registered for main()'s error line before any request is made."""
    redactor = Redactor(token=env["PTERODACTYL_API_KEY"], extra_secrets=ptero_secrets(env))
    use_redactor(redactor)
    return redactor


def parse_server_ids(raw: str) -> list[str]:
    """Comma-separated 8-hex Panel server ids. Refuses by naming the variable, never an entry."""
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    if not ids or not all(SERVER_ID.fullmatch(server_id) for server_id in ids):
        raise LiveRefused("LIVE_PTERO_SERVERS needs one or more 8-hex server ids separated by commas")
    return ids


def user_permissions(doc: object) -> list[str]:
    """meta.user_permissions from GET /api/client/servers/{id}, sorted. Raises ValueError when it is absent or not a
    list of strings."""
    meta = doc.get("meta") if isinstance(doc, dict) else None
    permissions = meta.get("user_permissions") if isinstance(meta, dict) else None
    if not isinstance(permissions, list) or not all(isinstance(item, str) for item in permissions):
        raise ValueError("meta.user_permissions is missing or not a list of strings")
    return sorted(permissions)


def extra_permissions(permissions: Iterable[str]) -> list[str]:
    """The permissions beyond the two the plugin needs. Any of these is a finding."""
    return sorted(item for item in permissions if item not in EXPECTED_PERMISSIONS)


def schedule_summary(schedule: Schedule) -> str:
    """Name length, activity, next run and the first power task's kind. The name itself is never printed. A power
    task whose payload is not an allowlisted kind is reported as other."""
    power = next((task for task in sorted(schedule.tasks, key=lambda task: task.sequence) if task.action == "power"),
                 None)
    if power is None:
        kind = "none"
    elif power.power_payload is None:
        kind = "other"
    else:
        kind = power.power_payload
    next_run = schedule.next_run_at.isoformat() if schedule.next_run_at is not None else "none"
    return f"name_len={len(schedule.name)} active={schedule.is_active} next={next_run} first_power={kind}"


def next_action_text(action: PowerAction | None) -> str:
    return "none" if action is None else f"{action.kind} at {action.at.isoformat()}"


async def check_server(manager: PterodactylManager, res: Results, redactor: Redactor, server_id: str,
                       now: datetime) -> None:
    label = redactor.label_text(server_id, "server")
    try:
        permissions = user_permissions(await manager._get(server_id, ""))
    except SourceError as exc:
        res.fail(f"{label} permissions", exc.reason)
        return
    except ValueError:
        res.fail(f"{label} permissions", "malformed")
        return
    out(redactor, f"{label} user_permissions:", ", ".join(permissions) or "none")
    unexpected = extra_permissions(permissions)
    if unexpected:
        res.fail(f"{label} permissions", f"unexpected: {', '.join(unexpected)}")
    else:
        res.ok(f"{label} permissions")

    out(redactor, f"{label} power_state:", (await manager.power_state(server_id)).value)

    try:
        schedules = parse_schedules(await manager._get(server_id, "/schedules"))
    except SourceError as exc:
        res.fail(f"{label} schedules", exc.reason)
        return
    for schedule in schedules:
        out(redactor, f"{label} schedule", schedule_summary(schedule))
    out(redactor, f"{label} next_power_action:", next_action_text(select_next(schedules, now)))


async def run() -> Results:
    env = read_env()
    require_vars(env, REQUIRED)
    servers = parse_server_ids(env["LIVE_PTERO_SERVERS"])
    redactor = ptero_redactor(env)
    res = Results(redactor)
    now = datetime.now(UTC)
    with redacted_logging(redactor):
        async with aiohttp.ClientSession() as session:
            manager = PterodactylManager(session, env["PTERODACTYL_URL"], env["PTERODACTYL_API_KEY"])
            for server_id in servers:
                await check_server(manager, res, redactor, server_id, now)
    return res


if __name__ == "__main__":
    main(run)
