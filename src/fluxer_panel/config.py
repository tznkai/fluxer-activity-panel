# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Environment-variable configuration."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_ADMIN_ID = re.compile(r"[1-9][0-9]{0,24}")        # snowflake without leading zeros
_POSITIVE_INT = re.compile(r"[1-9][0-9]{0,9}")
_TOKEN = re.compile(r"[\x21-\x7e]{1,512}")          # printable ASCII, no spaces: it goes in a header
INSTANCE_URL_MAX = 128   # longest FLUXER_INSTANCE_URL: a longer one pushes a card's Channels field past Fluxer's limit


class ConfigError(ValueError):
    """Message names the variable, never its value."""


def _opt(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _url(env: Mapping[str, str], name: str, schemes: tuple[str, ...]) -> str | None:
    value = _opt(env, name)
    if value is None:
        return None
    message = f"{name} must be a {' or '.join(s + '://' for s in schemes)} URL"
    try:
        parts = urlsplit(value)
    except ValueError:           # e.g. "http://[::1" — never let the raw value surface
        raise ConfigError(message) from None
    if parts.scheme not in schemes or not parts.netloc or any(c.isspace() for c in value):
        raise ConfigError(message)
    return value


def _positive_int(env: Mapping[str, str], name: str, default: str) -> int:
    raw = _opt(env, name) or default
    if not _POSITIVE_INT.fullmatch(raw):
        raise ConfigError(f"{name} must be a positive integer")
    return int(raw)


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    instance_url: str | None
    api_url: str | None
    gateway_url: str | None
    admin_user_ids: frozenset[str]
    prefix: str
    data_dir: Path
    db_path: Path
    purge_status_path: Path
    purge_interval_seconds: int
    log_level: str
    games_channel_id: str | None = None
    pterodactyl_url: str | None = None
    pterodactyl_api_key: str | None = field(default=None, repr=False)
    poll_interval_seconds: int = 60
    schedule_refresh_seconds: int = 600
    poll_concurrency: int = 8
    probe_timeout_seconds: int = 3
    probe_grace_seconds: int = 600

    @property
    def pterodactyl_configured(self) -> bool:
        return self.pterodactyl_url is not None and self.pterodactyl_api_key is not None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        token = _opt(env, "FLUXER_TOKEN")
        if token is None:
            raise ConfigError("FLUXER_TOKEN is required")
        if not _TOKEN.fullmatch(token):
            raise ConfigError("FLUXER_TOKEN must be printable ASCII without spaces")
        instance_url = _url(env, "FLUXER_INSTANCE_URL", ("http", "https"))
        if instance_url is not None and len(instance_url) > INSTANCE_URL_MAX:
            raise ConfigError(f"FLUXER_INSTANCE_URL must be at most {INSTANCE_URL_MAX} characters")
        api_url = _url(env, "FLUXER_API_URL", ("http", "https"))
        if instance_url is None and api_url is None:
            raise ConfigError("set FLUXER_INSTANCE_URL (or FLUXER_API_URL)")
        gateway_url = _url(env, "FLUXER_GATEWAY_URL", ("ws", "wss"))
        raw_admins = _opt(env, "ADMIN_USER_IDS")
        if raw_admins is None:
            raise ConfigError("ADMIN_USER_IDS is required (comma-separated user ids)")
        admins = [part.strip() for part in raw_admins.split(",")]
        if not admins or any(not _ADMIN_ID.fullmatch(a) for a in admins):
            raise ConfigError("ADMIN_USER_IDS must be comma-separated snowflakes without leading zeros")
        log_level = (_opt(env, "LOG_LEVEL") or "INFO").upper()
        if log_level not in LOG_LEVELS:
            raise ConfigError(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
        purge_interval = _positive_int(env, "PURGE_INTERVAL_SECONDS", "3600")
        games_channel_id = _opt(env, "GAMES_CHANNEL_ID")
        if games_channel_id is not None and not _ADMIN_ID.fullmatch(games_channel_id):
            raise ConfigError("GAMES_CHANNEL_ID must be a snowflake id")
        pterodactyl_url = _url(env, "PTERODACTYL_URL", ("http", "https"))
        pterodactyl_key = _opt(env, "PTERODACTYL_API_KEY")
        if pterodactyl_key is not None and not _TOKEN.fullmatch(pterodactyl_key):
            raise ConfigError("PTERODACTYL_API_KEY must be printable ASCII with no spaces")
        if (pterodactyl_url is None) != (pterodactyl_key is None):
            raise ConfigError("set both PTERODACTYL_URL and PTERODACTYL_API_KEY, or neither")
        data_dir = Path(_opt(env, "DATA_DIR") or "data")
        return cls(
            token=token, instance_url=instance_url, api_url=api_url, gateway_url=gateway_url,
            admin_user_ids=frozenset(admins),
            prefix=env.get("COMMAND_PREFIX", "").strip() or "!",
            data_dir=data_dir,
            db_path=Path(_opt(env, "DB_PATH") or data_dir / "panel.db"),
            purge_status_path=Path(_opt(env, "PURGE_STATUS_PATH") or data_dir / "purge-status.json"),
            purge_interval_seconds=purge_interval,
            log_level=log_level,
            games_channel_id=games_channel_id,
            pterodactyl_url=pterodactyl_url,
            pterodactyl_api_key=pterodactyl_key,
            poll_interval_seconds=_positive_int(env, "POLL_INTERVAL_SECONDS", "60"),
            schedule_refresh_seconds=_positive_int(env, "SCHEDULE_REFRESH_SECONDS", "600"),
            poll_concurrency=_positive_int(env, "POLL_CONCURRENCY", "8"),
            probe_timeout_seconds=_positive_int(env, "PROBE_TIMEOUT_SECONDS", "3"),
            probe_grace_seconds=_positive_int(env, "PROBE_GRACE_SECONDS", "600"),
        )
