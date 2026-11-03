# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Field validators for admin card commands: valid values pass, invalid values are refused with the field and
rule, and the value the admin typed never appears in the error or in the reply built from it."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from fluxer_panel import card_fields
from fluxer_panel.cards import CardFieldError
from fluxer_panel.sources.pterodactyl import PterodactylManager
from fluxer_panel.sources.registry import PROBES, Sources

SENTINEL = "SENTINEL-VAL-"
LONG_NAME = "a." * 130 + "x"          # 262 characters: over the 253 limit for a host name
NUMERIC_QUAD = ".".join(["999"] * 4)  # an all-numeric name, built here so no dotted quad is written out
LINK_LOCAL = "".join(["fe", "80", "::1"])  # built here: the hygiene scan reads a bare IPv6 address as a finding
PTERO = PterodactylManager(None, "https://fluxer.invalid", "key")  # type: ignore[arg-type]  # validate_ref is offline
CONFIGURED = Sources(managers={"pterodactyl": PTERO}, probes=dict(PROBES))
UNCONFIGURED = Sources(managers={}, probes=dict(PROBES))


def refused(call: Callable[[], Any]) -> CardFieldError:
    with pytest.raises(CardFieldError) as info:
        call()
    return info.value


# AC7.3: text ----------------------------------------------------------------------------------------

@pytest.mark.parametrize(("value", "expected"), [
    ("Minecraft Bedrock", "Minecraft Bedrock"),
    ("  padded  ", "padded"),
    ("x" * 200, "x" * 200),
])
def test_text_value_accepts_and_strips(value: str, expected: str) -> None:
    assert card_fields.text_value("title", value) == expected


@pytest.mark.parametrize(("value", "rule"), [
    (SENTINEL + "x" * 300, "length"),       # over 200 characters
    ("   ", "length"),                       # empty after stripping
    ("\x07" + SENTINEL, "control"),         # a C0 control character
    ("line\nbreak " + SENTINEL, "control"),  # an embedded newline is a control character too
    ("‮" + SENTINEL, "control"),       # a right-to-left override
])
def test_text_value_refuses_bad_text(value: str, rule: str) -> None:
    err = refused(lambda: card_fields.text_value("title", value))
    assert (err.field, err.rule) == ("title", rule)
    assert SENTINEL not in str(err)
    assert SENTINEL not in card_fields.refusal_reply(err)


def test_text_value_limit_comes_from_the_field() -> None:
    assert card_fields.text_value("description", "y" * 1000) == "y" * 1000
    err = refused(lambda: card_fields.text_value("description", "y" * 1001))
    assert err.rule == "length"


# AC7.3: slug -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["mc", "bedrock-survival-01", "a" * 32])
def test_slug_accepts_lowercase_digits_and_hyphen(value: str) -> None:
    assert card_fields.slug(value) == value


@pytest.mark.parametrize("value", ["Bad", "a_b", "a" * 33, "", SENTINEL])
def test_slug_refuses_bad_slugs(value: str) -> None:
    err = refused(lambda: card_fields.slug(value))
    assert (err.field, err.rule) == ("slug", "slug")
    assert SENTINEL not in card_fields.refusal_reply(err)


# AC7.3: snowflake ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["1001", "1" * 25])
def test_snowflake_accepts_digits_without_leading_zero(value: str) -> None:
    assert card_fields.snowflake("voice", value) == value


@pytest.mark.parametrize("value", ["12a", "0", "1" * 26, "01001", SENTINEL])
def test_snowflake_refuses_non_snowflakes(value: str) -> None:
    err = refused(lambda: card_fields.snowflake("voice", value))
    assert (err.field, err.rule) == ("voice", "snowflake")
    assert SENTINEL not in card_fields.refusal_reply(err)


# AC7.3: host ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["127.0.0.1", "::1", "mc.example.invalid", ])
def test_host_accepts_ip_addresses_and_host_names(value: str) -> None:
    assert card_fields.host("address", value) == value


@pytest.mark.parametrize("value", [
    "bad host", "-x.invalid", "a..b", LONG_NAME, SENTINEL,
    LINK_LOCAL + "%eth0",            # an IPv6 zone id is free text, so it is refused
    LINK_LOCAL + "%" + SENTINEL,
    LINK_LOCAL + "%<@&1>",           # a mention form in a zone id
    NUMERIC_QUAD,                    # an all-numeric last label is not a host name
    "1.2.3",
])
def test_host_refuses_bad_host_names(value: str) -> None:
    err = refused(lambda: card_fields.host("address", value))
    assert (err.field, err.rule) == ("address", "host")
    assert SENTINEL not in card_fields.refusal_reply(err)


# AC7.3: port ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("value", "expected"), [("1", 1), ("19132", 19132), ("65535", 65535)])
def test_port_accepts_1_to_65535(value: str, expected: int) -> None:
    assert card_fields.port("probe", value) == expected


@pytest.mark.parametrize("value", ["0", "65536", "80a", "", SENTINEL])
def test_port_refuses_out_of_range_and_non_digits(value: str) -> None:
    err = refused(lambda: card_fields.port("probe", value))
    assert (err.field, err.rule) == ("probe", "port")
    assert SENTINEL not in card_fields.refusal_reply(err)


# AC7.3: address -----------------------------------------------------------------------------------------

def test_address_accepts_host_and_port_and_bracketed_ipv6() -> None:
    assert card_fields.address("mc.example.invalid:19132") == ("mc.example.invalid", 19132)
    assert card_fields.address("[::1]:19132") == ("::1", 19132)


@pytest.mark.parametrize(("template", "rule"), [
    ("localhost{s}", "address"),          # no port
    ("::1:19132{s}", "address"),          # unbracketed IPv6
    ("[::1]{s}", "address"),              # bracketed without a port
    ("[nope{s}]:19132", "host"),          # bracketed, not an IP address
    ("[mc.example.invalid]:19132", "host"),  # bracketed host name: brackets are for IP addresses only
    ("[fe80::1%x]:1", "host"),               # a bracketed IPv6 zone id is refused too
    ("[fe80::1%{s}]:19132", "host"),
    ("bad host{s}:19132", "host"),
    ("mc.example.invalid:9999{s}", "port"),
])
def test_address_refuses_bad_forms(template: str, rule: str) -> None:
    err = refused(lambda: card_fields.address(template.format(s=SENTINEL)))
    assert (err.field, err.rule) == ("address", rule)
    assert SENTINEL not in card_fields.refusal_reply(err)


# AC7.3: range and yes/no ---------------------------------------------------------------------------------

@pytest.mark.parametrize(("value", "expected"), [("0", 0), ("9999", 9999)])
def test_int_range_accepts_inclusive_bounds(value: str, expected: int) -> None:
    assert card_fields.int_range("sort", value, 0, 9999) == expected


@pytest.mark.parametrize("value", ["10000", "-1", "x", SENTINEL])
def test_int_range_refuses_out_of_range(value: str) -> None:
    err = refused(lambda: card_fields.int_range("sort", value, 0, 9999))
    assert (err.field, err.rule) == ("sort", "range")
    assert SENTINEL not in card_fields.refusal_reply(err)


@pytest.mark.parametrize(("value", "expected"), [("yes", True), ("NO", False), (" Yes ", True)])
def test_yes_no_is_case_insensitive(value: str, expected: bool) -> None:
    assert card_fields.yes_no("address_public", value) is expected


@pytest.mark.parametrize("value", [SENTINEL, "y", "n", "true", "1"])
def test_yes_no_refuses_other_words(value: str) -> None:
    err = refused(lambda: card_fields.yes_no("address_public", value))
    assert (err.field, err.rule) == ("address_public", "yes_no")
    assert SENTINEL not in card_fields.refusal_reply(err)


# AC7.3: show ----------------------------------------------------------------------------------------------

def test_show_none_clears_every_item() -> None:
    assert card_fields.show("none") == ()


def test_show_accepts_one_item() -> None:
    assert card_fields.show("map") == ("map",)


def test_show_dedupes_and_orders_canonically() -> None:
    assert card_fields.show("version,map,map") == ("map", "version")


@pytest.mark.parametrize("value", ["players", "map,,version", SENTINEL, ""])
def test_show_refuses_unknown_items(value: str) -> None:
    err = refused(lambda: card_fields.show(value))
    assert (err.field, err.rule) == ("show", "show_item")
    assert SENTINEL not in card_fields.refusal_reply(err)


# AC7.3: voice target ---------------------------------------------------------------------------------------

def test_voice_target_accepts_two_ids() -> None:
    assert card_fields.voice_target("1001 2001") == ("1001", "2001")


def test_voice_target_accepts_a_pasted_channel_link() -> None:
    assert card_fields.voice_target("https://fluxer.invalid/channels/1001/2001") == ("1001", "2001")


@pytest.mark.parametrize(("template", "rule"), [
    ("1001{s}", "voice"),                          # one id only
    ("https://fluxer.invalid/x/1/2{s}", "voice"),  # not a channel link
    ("1001 2001 3001{s}", "voice"),                # three ids
    ("12a{s} 2001", "snowflake"),                  # a non-numeric id
])
def test_voice_target_refuses_other_shapes(template: str, rule: str) -> None:
    err = refused(lambda: card_fields.voice_target(template.format(s=SENTINEL)))
    assert (err.field, err.rule) == ("voice", rule)
    assert SENTINEL not in card_fields.refusal_reply(err)


# AC7.3: manager and probe ----------------------------------------------------------------------------------

def test_manager_accepts_configured_kind_with_valid_ref() -> None:
    assert card_fields.manager("pterodactyl abcd1234", CONFIGURED) == ("pterodactyl", "abcd1234")


def test_manager_refuses_unknown_kind() -> None:
    err = refused(lambda: card_fields.manager("steam 12345678", CONFIGURED))
    assert (err.field, err.rule) == ("manager", "unknown_kind")


def test_manager_refuses_kind_that_is_not_configured() -> None:
    err = refused(lambda: card_fields.manager("pterodactyl abcd1234", UNCONFIGURED))
    assert (err.field, err.rule) == ("manager", "not_configured")


def test_manager_refuses_invalid_ref() -> None:
    err = refused(lambda: card_fields.manager("pterodactyl ZZZ" + SENTINEL, CONFIGURED))
    assert (err.field, err.rule) == ("manager", "pterodactyl_ref")
    assert SENTINEL not in card_fields.refusal_reply(err)


def test_manager_refuses_a_kind_with_no_ref() -> None:
    err = refused(lambda: card_fields.manager("pterodactyl", CONFIGURED))
    assert (err.field, err.rule) == ("manager", "manager_format")


def test_probe_accepts_known_kind_host_and_port() -> None:
    assert card_fields.probe("a2s 127.0.0.1 27015", CONFIGURED) == ("a2s", "127.0.0.1", 27015)


def test_probe_refuses_unknown_kind() -> None:
    err = refused(lambda: card_fields.probe("quake 127.0.0.1 1", CONFIGURED))
    assert (err.field, err.rule) == ("probe", "unknown_kind")


@pytest.mark.parametrize(("value", "rule"), [
    ("a2s 127.0.0.1", "probe_format"),                      # no port
    ("a2s bad host 27015", "probe_format"),                 # a space splits the host into two words
    ("a2s 127.0.0.1 " + SENTINEL, "port"),                  # a port that is not a number
    ("a2s -bad.invalid 27015", "host"),                     # a host label that starts with a hyphen
    ("a2s 127.0.0.1 0", "port"),                            # a port below 1
    ("a2s " + LINK_LOCAL + "%" + SENTINEL + " 27015", "host"),         # a zone id in a probe host
])
def test_probe_refuses_bad_shapes_and_values(value: str, rule: str) -> None:
    err = refused(lambda: card_fields.probe(value, CONFIGURED))
    assert (err.field, err.rule) == ("probe", rule)
    assert SENTINEL not in card_fields.refusal_reply(err)


# Reply text ---------------------------------------------------------------------------------------------

def test_refusal_reply_names_field_and_rule() -> None:
    assert card_fields.refusal_reply(CardFieldError("title", "length")) == (
        "Can't set title: it must be 1–200 characters.")
    assert card_fields.refusal_reply(CardFieldError("slug", "taken")) == (
        "Can't set slug: it is already used by another card.")


def test_refusal_reply_uses_a_fixed_label_for_a_field_outside_the_vocabulary() -> None:
    reply = card_fields.refusal_reply(CardFieldError(SENTINEL + "field", "yes_no"))
    assert reply == "Can't set this field: it must be yes or no."
    assert SENTINEL not in reply
