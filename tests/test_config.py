# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config validation: every error names its variable and never echoes its value."""

from pathlib import Path

import pytest

from fluxer_panel.config import Config, ConfigError

TOKEN = "SENTINEL-TOKEN-7f3a9c"
PTERO_KEY = "SENTINEL-PTERO-KEY-91c2"
TOO_LONG_ID = "9" * 26


def env(**overrides: str | None) -> dict[str, str]:
    """Build a mapping from scratch (never os.environ). A value of None removes the key."""
    base = {
        "FLUXER_TOKEN": TOKEN,
        "FLUXER_INSTANCE_URL": "https://fluxer.invalid",
        "ADMIN_USER_IDS": "1001",
    }
    for name, value in overrides.items():
        if value is None:
            base.pop(name, None)
        else:
            base[name] = value
    return base


def test_defaults_are_applied():
    config = Config.from_env(env())
    assert config.prefix == "!"
    assert config.db_path == Path("data/panel.db")
    assert config.purge_status_path == Path("data/purge-status.json")
    assert config.purge_interval_seconds == 3600
    assert config.log_level == "INFO"
    assert config.admin_user_ids == frozenset({"1001"})


def test_admin_ids_are_split_and_stripped():
    config = Config.from_env(env(ADMIN_USER_IDS=" 1001 , 1002 "))
    assert config.admin_user_ids == frozenset({"1001", "1002"})


def test_path_overrides_are_honoured():
    config = Config.from_env(env(
        DATA_DIR="/srv/panel",
        DB_PATH="/srv/db/panel.sqlite",
        PURGE_STATUS_PATH="/srv/status.json",
    ))
    assert config.data_dir == Path("/srv/panel")
    assert config.db_path == Path("/srv/db/panel.sqlite")
    assert config.purge_status_path == Path("/srv/status.json")


def test_db_path_follows_data_dir_when_unset():
    config = Config.from_env(env(DATA_DIR="/srv/panel"))
    assert config.db_path == Path("/srv/panel/panel.db")
    assert config.purge_status_path == Path("/srv/panel/purge-status.json")


def test_api_url_alone_is_enough():
    config = Config.from_env(env(FLUXER_INSTANCE_URL=None, FLUXER_API_URL="https://api.fluxer.invalid/v1"))
    assert config.api_url == "https://api.fluxer.invalid/v1"
    assert config.instance_url is None


def test_blank_prefix_defaults_and_custom_prefix_is_kept():
    assert Config.from_env(env(COMMAND_PREFIX="   ")).prefix == "!"
    assert Config.from_env(env(COMMAND_PREFIX="?")).prefix == "?"


def test_repr_never_shows_the_token():
    assert TOKEN not in repr(Config.from_env(env()))


def test_card_settings_defaults_when_unset():
    config = Config.from_env(env())
    assert config.games_channel_id is None
    assert config.pterodactyl_url is None
    assert config.pterodactyl_api_key is None
    assert config.pterodactyl_configured is False
    assert config.poll_interval_seconds == 60
    assert config.schedule_refresh_seconds == 600
    assert config.poll_concurrency == 8
    assert config.probe_timeout_seconds == 3
    assert config.probe_grace_seconds == 600


def test_card_settings_parse_when_valid():
    config = Config.from_env(env(
        GAMES_CHANNEL_ID="1002",
        PTERODACTYL_URL="https://panel.invalid",
        PTERODACTYL_API_KEY=PTERO_KEY,
        POLL_INTERVAL_SECONDS="30",
        SCHEDULE_REFRESH_SECONDS="300",
        POLL_CONCURRENCY="4",
        PROBE_TIMEOUT_SECONDS="2",
        PROBE_GRACE_SECONDS="900",
    ))
    assert config.games_channel_id == "1002"
    assert config.pterodactyl_url == "https://panel.invalid"
    assert config.pterodactyl_api_key == PTERO_KEY
    assert config.pterodactyl_configured is True
    assert config.poll_interval_seconds == 30
    assert config.schedule_refresh_seconds == 300
    assert config.poll_concurrency == 4
    assert config.probe_timeout_seconds == 2
    assert config.probe_grace_seconds == 900


def test_repr_hides_the_pterodactyl_key_and_the_token():
    text = repr(Config.from_env(env(
        PTERODACTYL_URL="https://panel.invalid",
        PTERODACTYL_API_KEY=PTERO_KEY,
    )))
    assert PTERO_KEY not in text
    assert TOKEN not in text
    assert "https://panel.invalid" in text


@pytest.mark.parametrize(
    ("variable", "overrides", "bad_value"),
    [
        ("FLUXER_TOKEN", {"FLUXER_TOKEN": None}, None),
        ("FLUXER_TOKEN", {"FLUXER_TOKEN": "   "}, None),
        ("FLUXER_TOKEN", {"FLUXER_TOKEN": "SENTINEL TOKEN"}, "SENTINEL TOKEN"),
        ("FLUXER_TOKEN", {"FLUXER_TOKEN": "SENTINEL\tTOKEN"}, "SENTINEL\tTOKEN"),
        ("FLUXER_TOKEN", {"FLUXER_TOKEN": "SENTINEL-é"}, "SENTINEL-é"),
        ("FLUXER_INSTANCE_URL", {"FLUXER_INSTANCE_URL": None}, None),
        ("FLUXER_INSTANCE_URL", {"FLUXER_INSTANCE_URL": "ftp://sentinel.invalid"}, "ftp://sentinel.invalid"),
        ("FLUXER_INSTANCE_URL", {"FLUXER_INSTANCE_URL": "http://[::1"}, "http://[::1"),
        ("FLUXER_API_URL", {"FLUXER_INSTANCE_URL": None, "FLUXER_API_URL": "not a url"}, "not a url"),
        ("FLUXER_GATEWAY_URL", {"FLUXER_GATEWAY_URL": "https://sentinel.invalid"}, "https://sentinel.invalid"),
        ("ADMIN_USER_IDS", {"ADMIN_USER_IDS": None}, None),
        ("ADMIN_USER_IDS", {"ADMIN_USER_IDS": ""}, None),
        ("ADMIN_USER_IDS", {"ADMIN_USER_IDS": "abc"}, "abc"),
        ("ADMIN_USER_IDS", {"ADMIN_USER_IDS": "01001"}, "01001"),
        ("ADMIN_USER_IDS", {"ADMIN_USER_IDS": "0"}, "0"),
        ("ADMIN_USER_IDS", {"ADMIN_USER_IDS": "1001,,1002"}, "1001,,1002"),
        ("ADMIN_USER_IDS", {"ADMIN_USER_IDS": "1001,"}, "1001,"),
        ("ADMIN_USER_IDS", {"ADMIN_USER_IDS": "١٠٠١"}, "١٠٠١"),
        ("ADMIN_USER_IDS", {"ADMIN_USER_IDS": TOO_LONG_ID}, TOO_LONG_ID),
        ("LOG_LEVEL", {"LOG_LEVEL": "LOUD"}, "LOUD"),
        ("PURGE_INTERVAL_SECONDS", {"PURGE_INTERVAL_SECONDS": "0"}, "0"),
        ("PURGE_INTERVAL_SECONDS", {"PURGE_INTERVAL_SECONDS": "-5"}, "-5"),
        ("PURGE_INTERVAL_SECONDS", {"PURGE_INTERVAL_SECONDS": "1.5"}, "1.5"),
        ("PURGE_INTERVAL_SECONDS", {"PURGE_INTERVAL_SECONDS": "abc"}, "abc"),
        ("PURGE_INTERVAL_SECONDS", {"PURGE_INTERVAL_SECONDS": "+5"}, "+5"),
        ("PTERODACTYL_URL", {"PTERODACTYL_URL": "https://panel.invalid"}, None),
        ("PTERODACTYL_API_KEY", {"PTERODACTYL_API_KEY": PTERO_KEY}, None),
        ("PTERODACTYL_URL", {"PTERODACTYL_URL": "ftp://sentinel.invalid", "PTERODACTYL_API_KEY": PTERO_KEY}, "ftp://sentinel.invalid"),
        ("PTERODACTYL_API_KEY", {"PTERODACTYL_URL": "https://panel.invalid", "PTERODACTYL_API_KEY": "SENTINEL KEY"}, "SENTINEL KEY"),
        ("GAMES_CHANNEL_ID", {"GAMES_CHANNEL_ID": "12a"}, "12a"),
        ("GAMES_CHANNEL_ID", {"GAMES_CHANNEL_ID": "0"}, "0"),
        ("GAMES_CHANNEL_ID", {"GAMES_CHANNEL_ID": "SENTINEL"}, "SENTINEL"),
        ("POLL_INTERVAL_SECONDS", {"POLL_INTERVAL_SECONDS": "0"}, "0"),
        ("POLL_INTERVAL_SECONDS", {"POLL_INTERVAL_SECONDS": "-1"}, "-1"),
        ("POLL_INTERVAL_SECONDS", {"POLL_INTERVAL_SECONDS": "abc"}, "abc"),
        ("POLL_INTERVAL_SECONDS", {"POLL_INTERVAL_SECONDS": "SENTINEL"}, "SENTINEL"),
        ("SCHEDULE_REFRESH_SECONDS", {"SCHEDULE_REFRESH_SECONDS": "0"}, "0"),
        ("SCHEDULE_REFRESH_SECONDS", {"SCHEDULE_REFRESH_SECONDS": "-1"}, "-1"),
        ("SCHEDULE_REFRESH_SECONDS", {"SCHEDULE_REFRESH_SECONDS": "abc"}, "abc"),
        ("SCHEDULE_REFRESH_SECONDS", {"SCHEDULE_REFRESH_SECONDS": "SENTINEL"}, "SENTINEL"),
        ("POLL_CONCURRENCY", {"POLL_CONCURRENCY": "0"}, "0"),
        ("POLL_CONCURRENCY", {"POLL_CONCURRENCY": "-1"}, "-1"),
        ("POLL_CONCURRENCY", {"POLL_CONCURRENCY": "abc"}, "abc"),
        ("POLL_CONCURRENCY", {"POLL_CONCURRENCY": "SENTINEL"}, "SENTINEL"),
        ("PROBE_TIMEOUT_SECONDS", {"PROBE_TIMEOUT_SECONDS": "0"}, "0"),
        ("PROBE_TIMEOUT_SECONDS", {"PROBE_TIMEOUT_SECONDS": "-1"}, "-1"),
        ("PROBE_TIMEOUT_SECONDS", {"PROBE_TIMEOUT_SECONDS": "abc"}, "abc"),
        ("PROBE_TIMEOUT_SECONDS", {"PROBE_TIMEOUT_SECONDS": "SENTINEL"}, "SENTINEL"),
        ("PROBE_GRACE_SECONDS", {"PROBE_GRACE_SECONDS": "0"}, "0"),
        ("PROBE_GRACE_SECONDS", {"PROBE_GRACE_SECONDS": "-1"}, "-1"),
        ("PROBE_GRACE_SECONDS", {"PROBE_GRACE_SECONDS": "abc"}, "abc"),
        ("PROBE_GRACE_SECONDS", {"PROBE_GRACE_SECONDS": "SENTINEL"}, "SENTINEL"),
    ],
)
def test_invalid_config_names_variable_not_value(variable, overrides, bad_value):
    with pytest.raises(ConfigError) as info:
        Config.from_env(env(**overrides))
    message = str(info.value)
    assert variable in message
    if bad_value is not None:
        assert bad_value not in message
    assert "SENTINEL" not in message
