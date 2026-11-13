# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config validation: every error names its variable and never echoes its value."""

from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fluxer_panel.config import Config, ConfigError
from tests.fakes import instance_url_of_length, instance_url_of_units

TOKEN = "SENTINEL-TOKEN-7f3a9c"
PTERO_KEY = "SENTINEL-PTERO-KEY-91c2"
TOO_LONG_ID = "9" * 26
TOO_LONG_INSTANCE_URL = instance_url_of_length(129)   # one character over the 128 character cap


def env(**overrides: str | None) -> dict[str, str]:
    """Build a mapping from scratch (never os.environ). A value of None removes the key."""
    base = {
        "FLUXER_TOKEN": TOKEN, "SESSION_TIME_ZONE": "UTC",
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


def test_instance_url_of_128_characters_is_accepted():
    capped = instance_url_of_length(128)
    assert Config.from_env(env(FLUXER_INSTANCE_URL=capped)).instance_url == capped


def test_instance_url_of_128_utf16_units_with_astral_characters_is_accepted():
    capped = instance_url_of_units(128)
    # Fewer characters than units, so a count of characters would accept a longer URL than the cap allows.
    assert len(capped) < 128
    assert Config.from_env(env(FLUXER_INSTANCE_URL=capped)).instance_url == capped


def test_instance_url_of_129_utf16_units_with_astral_characters_is_refused():
    over_cap = instance_url_of_units(129)
    with pytest.raises(ConfigError) as info:
        Config.from_env(env(FLUXER_INSTANCE_URL=over_cap))
    assert "FLUXER_INSTANCE_URL" in str(info.value)
    assert "128 UTF-16 units" in str(info.value)
    assert over_cap not in str(info.value)


def test_api_url_has_no_length_cap():
    long_api = instance_url_of_length(300)
    assert Config.from_env(env(FLUXER_INSTANCE_URL=None, FLUXER_API_URL=long_api)).api_url == long_api


def test_instance_url_of_length_has_the_length_asked_for_and_labels_under_64_characters():
    for length in (128, 129, 300):
        url = instance_url_of_length(length)
        assert len(url) == length
        assert all(0 < len(label) < 64 for label in url.removeprefix("https://").split(".")), length


def test_instance_url_of_units_has_the_utf16_units_asked_for():
    for units in (128, 129):
        assert len(instance_url_of_units(units).encode("utf-16-le")) // 2 == units


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


REACTION_INTEGER_SETTINGS = {
    "wake_request_expiry_seconds": ("WAKE_REQUEST_EXPIRY_SECONDS", 7200),
    "play_now_expiry_seconds": ("PLAY_NOW_EXPIRY_SECONDS", 3600),
    "play_now_cooldown_seconds": ("PLAY_NOW_COOLDOWN_SECONDS", 1800),
    "board_repost_min_interval_seconds": ("BOARD_REPOST_MIN_INTERVAL_SECONDS", 300),
    "press_user_window_seconds": ("PRESS_USER_WINDOW_SECONDS", 10),
    "press_card_window_seconds": ("PRESS_CARD_WINDOW_SECONDS", 10),
    "press_card_burst": ("PRESS_CARD_BURST", 10),
}


def test_reaction_settings_defaults_when_unset():
    config = Config.from_env(env())
    assert config.play_now_channel_id is None
    assert config.follow_role_prefix == "card_subscription_"
    for attribute, (_, default) in REACTION_INTEGER_SETTINGS.items():
        assert getattr(config, attribute) == default, attribute


def test_reaction_settings_parse_when_valid():
    config = Config.from_env(env(
        GAMES_CHANNEL_ID="1002",
        PLAY_NOW_CHANNEL_ID="1003",
        FOLLOW_ROLE_PREFIX="follow-role_9",
        WAKE_REQUEST_EXPIRY_SECONDS="60",
        PLAY_NOW_EXPIRY_SECONDS="120",
        PLAY_NOW_COOLDOWN_SECONDS="180",
        BOARD_REPOST_MIN_INTERVAL_SECONDS="240",
        PRESS_USER_WINDOW_SECONDS="5",
        PRESS_CARD_WINDOW_SECONDS="6",
        PRESS_CARD_BURST="7",
    ))
    assert config.play_now_channel_id == "1003"
    assert config.follow_role_prefix == "follow-role_9"
    assert config.wake_request_expiry_seconds == 60
    assert config.play_now_expiry_seconds == 120
    assert config.play_now_cooldown_seconds == 180
    assert config.board_repost_min_interval_seconds == 240
    assert config.press_user_window_seconds == 5
    assert config.press_card_window_seconds == 6
    assert config.press_card_burst == 7


def test_play_now_channel_may_be_set_without_a_games_channel():
    config = Config.from_env(env(PLAY_NOW_CHANNEL_ID="1003"))
    assert config.play_now_channel_id == "1003"
    assert config.games_channel_id is None


def test_play_now_channel_equal_to_games_channel_is_refused():
    with pytest.raises(ConfigError) as info:
        Config.from_env(env(GAMES_CHANNEL_ID="1002", PLAY_NOW_CHANNEL_ID="1002"))
    assert str(info.value) == "PLAY_NOW_CHANNEL_ID must not be the games channel"
    assert "1002" not in str(info.value)


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
        ("FLUXER_INSTANCE_URL", {"FLUXER_INSTANCE_URL": TOO_LONG_INSTANCE_URL}, TOO_LONG_INSTANCE_URL),
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
        ("PLAY_NOW_CHANNEL_ID", {"PLAY_NOW_CHANNEL_ID": "12a"}, "12a"),
        ("PLAY_NOW_CHANNEL_ID", {"PLAY_NOW_CHANNEL_ID": "0"}, "0"),
        ("PLAY_NOW_CHANNEL_ID", {"PLAY_NOW_CHANNEL_ID": "SENTINEL"}, "SENTINEL"),
        ("FOLLOW_ROLE_PREFIX", {"FOLLOW_ROLE_PREFIX": "Card_"}, "Card_"),
        ("FOLLOW_ROLE_PREFIX", {"FOLLOW_ROLE_PREFIX": "a" * 41}, "a" * 41),
        ("FOLLOW_ROLE_PREFIX", {"FOLLOW_ROLE_PREFIX": "card sub"}, "card sub"),
        ("FOLLOW_ROLE_PREFIX", {"FOLLOW_ROLE_PREFIX": "SENTINEL"}, "SENTINEL"),
        ("POLL_LENGTH_SECONDS", {"POLL_LENGTH_SECONDS": "0"}, "0"),
        ("POLL_LENGTH_SECONDS", {"POLL_LENGTH_SECONDS": "-1"}, "-1"),
        ("POLL_LENGTH_SECONDS", {"POLL_LENGTH_SECONDS": "abc"}, "abc"),
        ("POLL_MAX_LENGTH_SECONDS", {"POLL_MAX_LENGTH_SECONDS": "0"}, "0"),
        ("POLL_MAX_LENGTH_SECONDS", {"POLL_MAX_LENGTH_SECONDS": "-1"}, "-1"),
        ("POLL_MAX_LENGTH_SECONDS", {"POLL_MAX_LENGTH_SECONDS": "abc"}, "abc"),
        ("POLL_MAX_LENGTH_SECONDS", {"POLL_LENGTH_SECONDS": "7200", "POLL_MAX_LENGTH_SECONDS": "3600"}, "7200"),
        # The message's own bounds "1 to 20" contain the digit 0, so no value check here; "21" and "abc" cover leaks.
        ("POLL_BALLOT_SIZE", {"POLL_BALLOT_SIZE": "0"}, None),
        ("POLL_BALLOT_SIZE", {"POLL_BALLOT_SIZE": "21"}, "21"),
        ("POLL_BALLOT_SIZE", {"POLL_BALLOT_SIZE": "abc"}, "abc"),
        ("POLL_MAX_RECIPIENTS", {"POLL_MAX_RECIPIENTS": "0"}, "0"),
        ("POLL_MAX_RECIPIENTS", {"POLL_MAX_RECIPIENTS": "abc"}, "abc"),
        ("POLL_REMINDER", {"POLL_REMINDER": "maybe"}, "maybe"),
        *[
            (variable, {variable: value}, value)
            for variable in (v for v, _ in REACTION_INTEGER_SETTINGS.values())
            for value in ("0", "-1", "abc")
        ],
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


def test_session_defaults_are_applied():
    config = Config.from_env(env())
    assert config.session_time_zone == ZoneInfo("UTC")
    assert config.session_slots_weekday == (dtime(16, 0), dtime(19, 0))
    assert config.session_slots_weekend == (dtime(10, 0), dtime(14, 0), dtime(19, 0))
    assert config.session_length_seconds == 7200
    assert config.session_retention_seconds == 604800
    assert config.dm_backoff_seconds == 3600


def test_session_time_zone_is_read_from_the_environment():
    config = Config.from_env(env(SESSION_TIME_ZONE="America/New_York"))
    assert config.session_time_zone == ZoneInfo("America/New_York")


@pytest.mark.parametrize("raw", [None, "   ", "", "Nope/Zone", "utc", "../etc", "America"])
def test_missing_or_invalid_session_time_zone_names_the_variable_only(raw):
    with pytest.raises(ConfigError) as info:
        Config.from_env(env(SESSION_TIME_ZONE=raw))
    message = str(info.value)
    assert message.startswith("SESSION_TIME_ZONE")
    assert "ZoneInfo" not in message
    if raw:
        assert raw not in message


def test_session_slots_are_sorted():
    config = Config.from_env(env(SESSION_SLOTS_WEEKDAY="19:00, 16:00", SESSION_SLOTS_WEEKEND="14:00,10:00"))
    assert config.session_slots_weekday == (dtime(16, 0), dtime(19, 0))
    assert config.session_slots_weekend == (dtime(10, 0), dtime(14, 0))


@pytest.mark.parametrize("name", ["SESSION_SLOTS_WEEKDAY", "SESSION_SLOTS_WEEKEND"])
@pytest.mark.parametrize("raw", [
    "9:00", "24:00", "16:00,16:00", "16:00,", "noon",
    "10:00,11:00,12:00,13:00,14:00,15:00,16:00",
])
def test_bad_session_slot_template_names_the_variable_not_the_value(name, raw):
    with pytest.raises(ConfigError) as info:
        Config.from_env(env(**{name: raw}))
    message = str(info.value)
    assert name in message
    assert "SENTINEL" not in message
    assert raw not in message


@pytest.mark.parametrize("name", ["SESSION_LENGTH_SECONDS", "SESSION_RETENTION_SECONDS", "DM_BACKOFF_SECONDS"])
@pytest.mark.parametrize("raw", ["0", "-1", "abc"])
def test_bad_session_integer_names_the_variable(name, raw):
    with pytest.raises(ConfigError) as info:
        Config.from_env(env(**{name: raw}))
    assert name in str(info.value)
    assert raw not in str(info.value)


def test_poll_defaults_are_applied():
    config = Config.from_env(env())
    assert config.poll_length_seconds == 86400
    assert config.poll_max_length_seconds == 604800
    assert config.poll_ballot_size == 6
    assert config.poll_max_recipients == 50
    assert config.poll_reminder is True


@pytest.mark.parametrize(("raw", "expected"), [("OFF", False), ("off", False), ("On", True)])
def test_poll_reminder_accepts_on_or_off_in_any_case(raw, expected):
    assert Config.from_env(env(POLL_REMINDER=raw)).poll_reminder is expected


@pytest.mark.parametrize("raw", ["1", "20"])
def test_poll_ballot_size_accepts_the_bounds(raw):
    assert Config.from_env(env(POLL_BALLOT_SIZE=raw)).poll_ballot_size == int(raw)


def test_poll_lengths_parse_when_valid():
    config = Config.from_env(env(POLL_LENGTH_SECONDS="3600", POLL_MAX_LENGTH_SECONDS="3600", POLL_MAX_RECIPIENTS="7"))
    assert config.poll_length_seconds == 3600
    assert config.poll_max_length_seconds == 3600
    assert config.poll_max_recipients == 7


def test_poll_max_below_length_names_both_variables_and_neither_number():
    with pytest.raises(ConfigError) as info:
        Config.from_env(env(POLL_LENGTH_SECONDS="7200", POLL_MAX_LENGTH_SECONDS="3600"))
    message = str(info.value)
    assert message == "POLL_MAX_LENGTH_SECONDS must be at least POLL_LENGTH_SECONDS"
    assert "7200" not in message and "3600" not in message


def test_poll_ballot_size_zero_gives_the_exact_message():
    with pytest.raises(ConfigError) as info:
        Config.from_env(env(POLL_BALLOT_SIZE="0"))
    assert str(info.value) == "POLL_BALLOT_SIZE must be an integer from 1 to 20"
