# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from fluxer_client.ids import is_snowflake, require_snowflake


@pytest.mark.parametrize("value", ["1", "0", "1" * 25])
def test_accepts_snowflake_ids(value: str) -> None:
    assert is_snowflake(value)
    assert require_snowflake(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "9" * 26,
        " 1",
        "1 ",
        "1\n",
        "١٢٣",  # Arabic-Indic digits
        "+1",
        "-1",
        "1.0",
        1,
        None,
    ],
)
def test_rejects_non_snowflake_values(value: object) -> None:
    assert not is_snowflake(value)
    with pytest.raises(ValueError):
        require_snowflake(value)  # type: ignore[arg-type]


def test_require_snowflake_error_does_not_echo_the_value() -> None:
    with pytest.raises(ValueError) as info:
        require_snowflake("SENTINEL-abc")
    assert "SENTINEL" not in str(info.value)
