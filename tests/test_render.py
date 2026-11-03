# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Rendering: escaping, the inside-Fluxer embed and the outside-Fluxer text, checked on built embeds and text."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_type_hints

import pytest

from fluxer_panel import render, text
from fluxer_panel.cards import Address, Card, Location, ManagerRef, ProbeRef, Voice
from fluxer_panel.render import CHANNEL_LINK_RE, ZWJ, channel_link
from fluxer_panel.resolver import CardStatus, StatusKind, next_action_text, resolve
from fluxer_panel.sources.base import PlayerCounts, PowerAction, ProbeDetails, ProbeResult
from fluxer_panel.sources.minecraft import JavaProbe
from fluxer_panel.sources.pterodactyl import parse_schedules, select_next
from tests.fake_probes import java_server

INSTANCE = "https://fluxer.invalid"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
SENTINEL_LOCATION = "SENTINEL-LOCATION-77"
ALL_DETAILS = ("map", "version", "game_mode")
READY_FULL = CardStatus(
    StatusKind.READY,
    counts=PlayerCounts(online=3, max=10),
    details=ProbeDetails(map="Plains", version="1.21.4", game_mode="Survival"),
)


def make_card(**overrides: object) -> Card:
    base: dict[str, object] = {
        "id": 1, "slug": "lobby", "title": "Lobby", "description": None, "how_to_join": None,
        "first_time": None, "sort_order": 0, "show_details": (), "state": "live", "channel_id": None,
        "message_id": None, "created_by": "1001",
    }
    base.update(overrides)
    return Card(**base)  # type: ignore[arg-type]


def fields_of(embed: dict) -> dict[str, str]:
    return {f["name"]: f["value"] for f in embed["fields"]}


def user_strings(embed: dict) -> list[str]:
    """Every string in the embed that can carry card text: the title, the description and every field."""
    out = [embed["title"]]
    if "description" in embed:
        out.append(embed["description"])
    for f in embed["fields"]:
        out.append(f["name"])
        out.append(f["value"])
    return out


# Each mention form is broken in every text field, including probe details.
# Each form is listed with the broken string it must become, so a removed alternative shows up as a raw match.
MENTIONS = {
    "@everyone": "@" + ZWJ + "everyone",
    "@here": "@" + ZWJ + "here",
    "@EveryOne": "@" + ZWJ + "EveryOne",
    "<@1001>": "\\<" + ZWJ + "@1001",
    "<@!1001>": "\\<" + ZWJ + "@!1001",
    "<@&1001>": "\\<" + ZWJ + "@&1001",
    "<#1001>": "\\<" + ZWJ + "\\#1001",
}


@pytest.mark.parametrize(("payload", "broken"), list(MENTIONS.items()))
def test_each_mention_form_is_neutralised_in_every_field(payload: str, broken: str) -> None:
    card = make_card(
        title=f"T {payload}",
        description=f"D {payload}",
        how_to_join=f"J {payload}",
        first_time=f"F {payload}",
        location=Location(f"L {payload}"),
        voice=Voice(guild_id="1001", channel_id="2001", channel_name=f"V {payload}", verified=True),
        show_details=ALL_DETAILS,
    )
    status = CardStatus(
        StatusKind.READY,
        details=ProbeDetails(map=f"M {payload}", version=f"X {payload}", game_mode=f"G {payload}"),
    )
    embed = render.render_embed(card, status, None, instance_url=INSTANCE)
    for value in user_strings(embed):
        assert payload.lower() not in value.lower()
    assert broken in embed["title"]
    assert broken in embed["description"]
    assert broken in fields_of(embed)[text.FIELD_JOIN]
    assert broken in fields_of(embed)[text.FIELD_FIRST_TIME]
    assert broken in fields_of(embed)[text.FIELD_LOCATION]
    assert broken in fields_of(embed)[text.FIELD_VOICE]
    assert broken in fields_of(embed)[text.FIELD_MAP]
    assert broken in fields_of(embed)[text.FIELD_VERSION]
    assert broken in fields_of(embed)[text.FIELD_GAME_MODE]


# Each mention form is matched whole. Two forms differ only in how much of the prefix they consume, so the
# broken output alone cannot tell `<@!` from `<@`: the match itself is checked.
@pytest.mark.parametrize(
    ("payload", "prefix"),
    [("@everyone", "@everyone"), ("@Here", "@Here"), ("<@1001>", "<@"), ("<@!1001>", "<@!"),
     ("<@&1001>", "<@&"), ("<#1001>", "<#")],
)
def test_mention_pattern_matches_each_form_whole(payload: str, prefix: str) -> None:
    match = render._MENTION.match(payload)
    assert match is not None
    assert match.group(0) == prefix


# The mention neutraliser leaves ordinary text alone, including a lone @ or angle bracket.
def test_neutralise_leaves_plain_text_and_lone_at_and_angle_untouched() -> None:
    plain = "meet at 12:00, a@b.invalid, <b>, and @someone"
    assert render.neutralise(plain) == plain


# Every markdown control character is backslash-escaped.
@pytest.mark.parametrize("char", list("\\`*_~|>#[]()<:-"))
def test_each_markdown_control_character_is_escaped(char: str) -> None:
    assert render.escape(f"a{char}b") == f"a\\{char}b"


def test_characters_outside_the_control_set_are_not_escaped() -> None:
    plain = "a.b@c!d&e%f+g=h?i,j;k'l\" m"
    assert render.escape(plain) == plain


# Markdown constructs cannot survive inside the embed.
@pytest.mark.parametrize(
    ("payload", "escaped"),
    [
        ("```", "\\`\\`\\`"),
        ("**bold**", "\\*\\*bold\\*\\*"),
        ("> quote", "\\> quote"),
        ("# heading", "\\# heading"),
        ("||spoiler||", "\\|\\|spoiler\\|\\|"),
    ],
)
def test_markdown_constructs_are_escaped(payload: str, escaped: str) -> None:
    embed = render.render_embed(make_card(title=payload), None, None, instance_url=INSTANCE)
    assert escaped in embed["title"]


# A link in any text field does not render as a masked link.
def test_link_syntax_is_escaped_in_every_text_field_but_voice() -> None:
    payload = "[click](https://evil.invalid/x)"
    card = make_card(
        title=payload,
        description=payload,
        how_to_join=payload,
        first_time=payload,
        location=Location(payload),
        voice=Voice(guild_id="1001", channel_id="2001", channel_name=payload, verified=True),
        show_details=ALL_DETAILS,
    )
    embed = render.render_embed(card, CardStatus(StatusKind.READY, details=ProbeDetails(map=payload)),
                                None, instance_url=INSTANCE)
    masked = re.compile(r"(?<!\\)\]\(")
    for name, value in fields_of(embed).items():
        if name != text.FIELD_VOICE:
            assert masked.search(value) is None, name
    assert masked.search(embed["title"]) is None
    assert masked.search(embed["description"]) is None
    assert "\\[click\\]\\(" in embed["title"]
    voice = fields_of(embed)[text.FIELD_VOICE]
    assert "\\[click\\]" in voice


def test_backtick_in_an_address_host_cannot_break_out_of_code() -> None:
    card = make_card(address=Address(host="mc`x.example.invalid", port=19132, public=True))
    embed = render.render_embed(card, None, None, instance_url=INSTANCE)
    assert fields_of(embed)[text.FIELD_ADDRESS].count("`") == 2


def test_code_strips_backticks_and_wraps_in_one_span() -> None:
    assert render.code("a`b") == "`ab`"
    assert render.code("x") == "`x`"


# fit: cut to a limit with an ellipsis, and never leave an escape backslash without its character.
def test_fit_returns_short_text_unchanged() -> None:
    assert render.fit("abc", 5) == "abc"
    assert render.fit("abcde", 5) == "abcde"


def test_fit_cuts_to_the_limit_including_the_ellipsis() -> None:
    assert render.fit("abcdef", 4) == "abc…"
    assert len(render.fit("abcdef", 4)) == 4


def test_fit_drops_a_dangling_odd_backslash() -> None:
    # One trailing backslash in the cut would escape the ellipsis, so it goes.
    assert render.fit("xy\\zzz", 4) == "xy…"


def test_fit_keeps_an_even_run_of_trailing_backslashes() -> None:
    # Two backslashes form a complete escaped backslash, so they stay.
    assert render.fit("x\\\\zzzz", 4) == "x\\\\…"


def test_fit_drops_an_odd_run_of_three_trailing_backslashes() -> None:
    assert render.fit("x\\\\\\zzzz", 5) == "x\\\\…"


# channel_link: the only place a channel link is built, and CHANNEL_LINK_RE parses it.
def test_channel_link_builds_the_url_and_strips_a_trailing_slash() -> None:
    assert channel_link("https://fluxer.invalid/", "1001", "2001") == "https://fluxer.invalid/channels/1001/2001"


def test_channel_link_refuses_a_non_snowflake_id() -> None:
    with pytest.raises(ValueError):
        channel_link(INSTANCE, "12a", "2001")
    with pytest.raises(ValueError):
        channel_link(INSTANCE, "1001", "")


def test_channel_link_regex_parses_what_the_builder_makes() -> None:
    match = CHANNEL_LINK_RE.fullmatch(channel_link(INSTANCE, "1001", "2001"))
    assert match is not None
    assert match.groups() == ("1001", "2001")


# Colours: one per status kind, and the neutral colour when there is no status.
COLOUR_CASES = [
    (StatusKind.READY, 0x2ECC71),
    (StatusKind.WAKING, 0xF1C40F),
    (StatusKind.GOING_TO_SLEEP, 0xE67E22),
    (StatusKind.ASLEEP, 0x95A5A6),
    (StatusKind.UNAVAILABLE, 0xE74C3C),
    (StatusKind.UNKNOWN, 0x7F8C8D),
    (StatusKind.NOT_RESPONDING, 0xE74C3C),
]


@pytest.mark.parametrize(("kind", "colour"), COLOUR_CASES)
def test_colour_per_status_kind(kind: StatusKind, colour: int) -> None:
    embed = render.render_embed(make_card(), CardStatus(kind), None, instance_url=INSTANCE)
    assert embed["color"] == colour


def test_no_status_uses_the_neutral_colour() -> None:
    embed = render.render_embed(make_card(), None, None, instance_url=INSTANCE)
    assert embed["color"] == 0x5865F2


# The inside embed for a card with every slot filled.
def full_card(**overrides: object) -> Card:
    base: dict[str, object] = {
        "voice": Voice(guild_id="1001", channel_id="2001", channel_name="lobby", verified=True),
        "address": Address(host="mc.example.invalid", port=19132, public=True),
        "location": Location("Behind the hall"),
        "seats": 4,
        "manager": ManagerRef(kind="pterodactyl", ref="abcd1234"),
        "probe": ProbeRef(kind="minecraft_java", host="mc.example.invalid", port=19132),
        "show_details": ("map",),
        "description": "A description",
        "how_to_join": "Join with the link",
        "first_time": "Ask for a role",
    }
    base.update(overrides)
    return make_card(**base)


def test_full_card_inside_embed_fields() -> None:
    embed = render.render_embed(full_card(), READY_FULL, "Restarts in 30 min", instance_url=INSTANCE)
    fields = fields_of(embed)
    assert fields[text.FIELD_ADDRESS] == "`mc.example.invalid:19132`"
    assert fields[text.FIELD_LOCATION] == "Behind the hall"
    assert fields[text.FIELD_VOICE] == f"[#lobby]({INSTANCE}/channels/1001/2001)"


def test_only_opted_in_details_appear() -> None:
    embed = render.render_embed(full_card(), READY_FULL, None, instance_url=INSTANCE)
    fields = fields_of(embed)
    assert fields[text.FIELD_MAP] == "Plains"
    assert text.FIELD_VERSION not in fields
    assert text.FIELD_GAME_MODE not in fields


def test_unverified_voice_shows_the_plain_label_inside_the_link() -> None:
    card = full_card(voice=Voice(guild_id="1001", channel_id="2001", channel_name=None, verified=False))
    embed = render.render_embed(card, READY_FULL, None, instance_url=INSTANCE)
    assert fields_of(embed)[text.FIELD_VOICE] == f"[{text.VOICE_UNNAMED}]({INSTANCE}/channels/1001/2001)"


def test_unverified_voice_ignores_a_stored_name() -> None:
    card = full_card(voice=Voice(guild_id="1001", channel_id="2001", channel_name="lobby", verified=False))
    embed = render.render_embed(card, READY_FULL, None, instance_url=INSTANCE)
    assert f"[{text.VOICE_UNNAMED}]" in fields_of(embed)[text.FIELD_VOICE]
    assert "lobby" not in fields_of(embed)[text.FIELD_VOICE]


def test_voice_without_instance_url_has_no_link() -> None:
    embed = render.render_embed(full_card(), READY_FULL, None, instance_url=None)
    assert fields_of(embed)[text.FIELD_VOICE] == "#lobby"
    unverified = full_card(voice=Voice(guild_id="1001", channel_id="2001", channel_name=None, verified=False))
    embed = render.render_embed(unverified, READY_FULL, None, instance_url=None)
    assert fields_of(embed)[text.FIELD_VOICE] == text.VOICE_UNNAMED


def test_ipv6_host_is_bracketed_in_the_address() -> None:
    card = make_card(address=Address(host="::1", port=19132, public=True))
    embed = render.render_embed(card, None, None, instance_url=INSTANCE)
    assert fields_of(embed)[text.FIELD_ADDRESS] == "`[::1]:19132`"


def test_ipv4_host_is_not_bracketed() -> None:
    card = make_card(address=Address(host="127.0.0.1", port=19132, public=True))
    embed = render.render_embed(card, None, None, instance_url=INSTANCE)
    assert fields_of(embed)[text.FIELD_ADDRESS] == "`127.0.0.1:19132`"


def test_card_fields_appear_in_the_documented_order() -> None:
    embed = render.render_embed(full_card(), READY_FULL, "Restarts in 30 min", instance_url=INSTANCE)
    assert [f["name"] for f in embed["fields"]] == [
        text.FIELD_STATUS, text.FIELD_NEXT, text.FIELD_JOIN, text.FIELD_FIRST_TIME,
        text.FIELD_ADDRESS, text.FIELD_VOICE, text.FIELD_LOCATION, text.FIELD_MAP,
    ]
    assert all(f["inline"] is False for f in embed["fields"])


def test_status_field_shows_the_label_and_counts() -> None:
    embed = render.render_embed(make_card(), READY_FULL, None, instance_url=INSTANCE)
    assert fields_of(embed)[text.FIELD_STATUS] == "Ready · 3 / 10"


def test_status_field_is_absent_without_a_status() -> None:
    embed = render.render_embed(make_card(), None, None, instance_url=INSTANCE)
    assert text.FIELD_STATUS not in fields_of(embed)


def test_no_content_key_and_no_seats_field() -> None:
    embed = render.render_embed(full_card(), READY_FULL, None, instance_url=INSTANCE)
    assert "content" not in embed
    assert not any("seat" in f["name"].lower() for f in embed["fields"])


def test_description_key_is_absent_when_the_card_has_none() -> None:
    embed = render.render_embed(make_card(description=None), None, None, instance_url=INSTANCE)
    assert "description" not in embed


def test_description_is_present_when_set() -> None:
    embed = render.render_embed(make_card(description="Hello"), None, None, instance_url=INSTANCE)
    assert embed["description"] == "Hello"


# Each optional field is present only when its source is set.
@pytest.mark.parametrize(
    ("overrides", "field_name"),
    [
        ({"how_to_join": "Join"}, text.FIELD_JOIN),
        ({"first_time": "Ask"}, text.FIELD_FIRST_TIME),
        ({"address": Address(host="mc.example.invalid", port=1, public=False)}, text.FIELD_ADDRESS),
        ({"voice": Voice(guild_id="1001", channel_id="2001", channel_name=None, verified=False)}, text.FIELD_VOICE),
        ({"location": Location("Hall")}, text.FIELD_LOCATION),
    ],
)
def test_optional_field_appears_only_when_set(overrides: dict, field_name: str) -> None:
    present = fields_of(render.render_embed(make_card(**overrides), None, None, instance_url=INSTANCE))
    absent = fields_of(render.render_embed(make_card(), None, None, instance_url=INSTANCE))
    assert field_name in present
    assert field_name not in absent


# A Next field appears only when next_text is given, and nothing else changes.
def test_next_field_is_present_only_with_next_text() -> None:
    card = full_card()
    with_next = render.render_embed(card, READY_FULL, "Restarts in 30 min", instance_url=INSTANCE)
    without = render.render_embed(card, READY_FULL, None, instance_url=INSTANCE)
    assert fields_of(with_next)[text.FIELD_NEXT] == "Restarts in 30 min"
    assert text.FIELD_NEXT not in fields_of(without)
    trimmed = [f for f in with_next["fields"] if f["name"] != text.FIELD_NEXT]
    assert trimmed == without["fields"]
    assert {k: v for k, v in with_next.items() if k != "fields"} == {k: v for k, v in without.items() if k != "fields"}


# show_details filter: each item is shown only when opted in and its value is set.
@pytest.mark.parametrize(
    ("item", "field_name"),
    [("map", text.FIELD_MAP), ("version", text.FIELD_VERSION), ("game_mode", text.FIELD_GAME_MODE)],
)
def test_detail_item_is_shown_when_opted_in_and_set(item: str, field_name: str) -> None:
    card = make_card(show_details=(item,))
    embed = render.render_embed(card, READY_FULL, None, instance_url=INSTANCE)
    details = {name for name in fields_of(embed) if name in {text.FIELD_MAP, text.FIELD_VERSION, text.FIELD_GAME_MODE}}
    assert details == {field_name}


@pytest.mark.parametrize("item", ALL_DETAILS)
def test_detail_item_is_hidden_when_not_opted_in(item: str) -> None:
    others = tuple(i for i in ALL_DETAILS if i != item)
    embed = render.render_embed(make_card(show_details=others), READY_FULL, None, instance_url=INSTANCE)
    names = set(fields_of(embed))
    field_name = {"map": text.FIELD_MAP, "version": text.FIELD_VERSION, "game_mode": text.FIELD_GAME_MODE}[item]
    assert field_name not in names


@pytest.mark.parametrize("item", ALL_DETAILS)
def test_detail_item_is_hidden_when_its_value_is_none(item: str) -> None:
    details = dataclasses.replace(READY_FULL.details, **{item: None})
    status = CardStatus(StatusKind.READY, details=details)
    embed = render.render_embed(make_card(show_details=ALL_DETAILS), status, None, instance_url=INSTANCE)
    field_name = {"map": text.FIELD_MAP, "version": text.FIELD_VERSION, "game_mode": text.FIELD_GAME_MODE}[item]
    assert field_name not in fields_of(embed)


def test_details_are_absent_without_a_status() -> None:
    embed = render.render_embed(make_card(show_details=ALL_DETAILS), None, None, instance_url=INSTANCE)
    assert not set(fields_of(embed)) & {text.FIELD_MAP, text.FIELD_VERSION, text.FIELD_GAME_MODE}


def repeated(unit: str, length: int) -> str:
    """`unit` repeated to exactly `length` characters; `length` must be a multiple of len(unit)."""
    assert length % len(unit) == 0
    return unit * (length // len(unit))


# `*` doubles when escaped and `<#` grows 2.5 times, the worst case, so both are exercised.
GROWTH_UNITS = ["*", "<#"]


# Title, description and text values are fitted to the Discord-compatible limits.
@pytest.mark.parametrize("unit", GROWTH_UNITS)
def test_maximum_length_card_stays_within_embed_limits(unit: str) -> None:
    card = make_card(
        title=repeated(unit, 200),
        description=repeated(unit, 1000),
        how_to_join=repeated(unit, 1000),
        first_time=repeated(unit, 1000),
        location=Location(repeated(unit, 200)),
        voice=Voice(guild_id="1001", channel_id="2001", channel_name=repeated(unit, 100), verified=True),
        address=Address(host="mc.example.invalid", port=19132, public=True),
        show_details=ALL_DETAILS,
    )
    status = CardStatus(
        StatusKind.READY,
        details=ProbeDetails(
            map=repeated(unit, 48), version=repeated(unit, 48), game_mode=repeated(unit, 48),
        ),
    )
    embed = render.render_embed(card, status, "Restarts in 30 min", instance_url=INSTANCE)
    assert render.LIMITS["description"] == 1024
    assert len(embed["title"]) <= 256
    assert len(embed["description"]) <= render.LIMITS["description"]
    assert embed["description"].endswith("…")
    total = len(embed["title"]) + len(embed["description"])
    for f in embed["fields"]:
        assert len(f["name"]) <= 256
        assert len(f["value"]) <= 1024
        total += len(f["name"]) + len(f["value"])
    assert total <= 6000
    for value in user_strings(embed):
        assert not re.search(r"(?<!\\)(\\\\)*\\$", value)


# A location (200 characters) and each detail (48 characters) at the validated maximum are rendered whole.
@pytest.mark.parametrize("unit", GROWTH_UNITS)
def test_maximum_location_and_details_are_rendered_whole(unit: str) -> None:
    location = repeated(unit, 200)
    detail = repeated(unit, 48)
    card = make_card(location=Location(location), show_details=ALL_DETAILS)
    status = CardStatus(
        StatusKind.READY,
        details=ProbeDetails(map=detail, version=detail, game_mode=detail),
    )
    embed = render.render_embed(card, status, None, instance_url=INSTANCE)
    fields = fields_of(embed)
    assert fields[text.FIELD_LOCATION] == render.escape(location)
    for name in (text.FIELD_MAP, text.FIELD_VERSION, text.FIELD_GAME_MODE):
        assert fields[name] == render.escape(detail), name
    for name in (text.FIELD_LOCATION, text.FIELD_MAP, text.FIELD_VERSION, text.FIELD_GAME_MODE):
        assert "…" not in fields[name], name


def test_long_escaped_title_is_cut_to_the_limit_with_an_ellipsis() -> None:
    # 300 stars escape to 600 characters. The 256 cut lands on an escape pair, so the result is 255 with the ellipsis.
    embed = render.render_embed(make_card(title="*" * 300), None, None, instance_url=INSTANCE)
    assert len(embed["title"]) <= 256
    assert embed["title"].endswith("…")


# The next-action text is inserted as is, never escaped.
def test_next_text_is_not_escaped() -> None:
    embed = render.render_embed(make_card(), READY_FULL, "Restarts in 30 min", instance_url=INSTANCE)
    assert fields_of(embed)[text.FIELD_NEXT] == "Restarts in 30 min"


# Fixture scan: no embed says "guild", and cards without a probe never say "player".
def fixtures() -> list[tuple[Card, CardStatus | None]]:
    manager = ManagerRef(kind="pterodactyl", ref="abcd1234")
    probe = ProbeRef(kind="minecraft_java", host="mc.example.invalid", port=19132)
    cards: list[tuple[Card, CardStatus | None]] = [
        (make_card(), None),
        (make_card(manager=manager), CardStatus(StatusKind.ASLEEP)),
        (make_card(probe=probe), CardStatus(StatusKind.NOT_RESPONDING)),
        (full_card(), READY_FULL),
    ]
    cards += [(make_card(manager=manager), CardStatus(kind)) for kind in StatusKind]
    return cards


def test_no_fixture_mentions_guild() -> None:
    for card, status in fixtures():
        embed = render.render_embed(card, status, "Restarts in 30 min", instance_url=INSTANCE)
        assert "guild" not in json.dumps(embed, ensure_ascii=False).lower()


def test_card_without_probe_never_mentions_players() -> None:
    for card, status in fixtures():
        if card.probe is not None:
            continue
        embed = render.render_embed(card, status, None, instance_url=INSTANCE)
        assert "player" not in json.dumps(embed, ensure_ascii=False).lower()


# The next-action text comes from next_action_text and renders unchanged.
def test_next_action_text_renders_unescaped_alongside_a_card() -> None:
    action = PowerAction(kind="stop", at=NOW + timedelta(hours=5))
    embed = render.render_embed(make_card(), READY_FULL, next_action_text(action, NOW), instance_url=INSTANCE)
    assert fields_of(embed)[text.FIELD_NEXT] == "Goes to sleep in 5 h"


# The hash: identical embeds hash equal; key order is irrelevant; any change to the embed changes it.
def test_identical_embeds_hash_equal() -> None:
    card = full_card()
    first = render.render_embed(card, READY_FULL, None, instance_url=INSTANCE)
    second = render.render_embed(card, READY_FULL, None, instance_url=INSTANCE)
    assert render.rendered_hash(first) == render.rendered_hash(second)


def test_hash_ignores_dict_key_order() -> None:
    embed = render.render_embed(full_card(), READY_FULL, None, instance_url=INSTANCE)
    reordered = dict(reversed(list(embed.items())))
    assert render.rendered_hash(reordered) == render.rendered_hash(embed)


def test_hash_changes_when_any_field_changes() -> None:
    base = render.render_embed(full_card(), READY_FULL, None, instance_url=INSTANCE)
    changed = dict(base)
    changed["title"] = base["title"] + "!"
    assert render.rendered_hash(changed) != render.rendered_hash(base)
    assert len(render.rendered_hash(base)) == 64


# The hash is over one canonical form: sorted keys, compact separators and raw non-ASCII text.
def test_rendered_hash_is_the_canonical_serialisation() -> None:
    embed = render.render_embed(make_card(title="@everyone lobby"), READY_FULL, None, instance_url=INSTANCE)
    assert ZWJ in embed["title"]
    assert "·" in fields_of(embed)[text.FIELD_STATUS]
    canonical = json.dumps(embed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert render.rendered_hash(embed) == hashlib.sha256(canonical.encode()).hexdigest()


def test_card_message_wraps_one_embed_with_no_mentions() -> None:
    embed = render.render_embed(make_card(), None, None, instance_url=INSTANCE)
    assert render.card_message(embed) == {"embeds": [embed], "allowed_mentions": {"parse": []}}


# A schedules payload sentinel never reaches the embed.
async def test_schedule_payload_sentinels_do_not_reach_the_embed() -> None:
    doc = {"data": [
        {"attributes": {
            "name": "SENTINEL-SCHEDULE-NAME", "is_active": True, "next_run_at": "2026-11-02T13:00:00+00:00",
            "relationships": {"tasks": {"data": [
                {"attributes": {"sequence_id": 1, "action": "command", "payload": "SENTINEL-COMMAND-PAYLOAD"}},
                {"attributes": {"sequence_id": 2, "action": "power", "payload": "stop"}},
            ]}},
        }},
        {"attributes": {
            "name": "other", "is_active": True, "next_run_at": "2026-11-02T12:30:00+00:00",
            "relationships": {"tasks": {"data": [
                {"attributes": {"sequence_id": 1, "action": "power", "payload": "SENTINEL-POWER-PAYLOAD"}},
            ]}},
        }},
    ]}
    action = select_next(parse_schedules(doc), NOW)
    assert action is not None
    assert action.kind == "stop"
    embed = render.render_embed(make_card(), READY_FULL, next_action_text(action, NOW), instance_url=INSTANCE)
    rendered = json.dumps(embed, ensure_ascii=False)
    for sentinel in ("SENTINEL-SCHEDULE-NAME", "SENTINEL-COMMAND-PAYLOAD", "SENTINEL-POWER-PAYLOAD"):
        assert sentinel not in rendered


# A Java probe's player sample never reaches the embed.
async def test_java_player_sample_never_reaches_the_embed() -> None:
    sentinel = "SENTINEL-PLAYER-a1"
    body = {
        "version": {"name": "1.21.4", "protocol": 769},
        "players": {"online": 3, "max": 10, "sample": [{"name": sentinel, "id": "00000000-0000-0000-0000-000000000001"}]},
        "description": "A server",
    }
    async with java_server(json.dumps(body).encode()) as (port, _):
        result = await JavaProbe().probe("127.0.0.1", port, timeout=2.0)
    assert isinstance(result, ProbeResult)
    status = resolve(
        has_manager=False, power=None, has_probe=True, probe=result,
        failing_since=None, alert_sent=False, now=0, grace=600,
    ).status
    card = make_card(
        probe=ProbeRef(kind=JavaProbe.kind, host="mc.example.invalid", port=19132),
        show_details=("version",),
    )
    embed = render.render_embed(card, status, None, instance_url=INSTANCE)
    assert sentinel not in json.dumps(embed, ensure_ascii=False)
    assert fields_of(embed)[text.FIELD_VERSION] == "1.21.4"


# Outside Fluxer: plain text, mention-neutralised, never markdown-escaped.
RENDER_PATH = Path(render.__file__)


def outside_full_card() -> Card:
    return full_card(description="A description", first_time="Ask for a role")


def test_outside_address_shows_host_and_port_when_public() -> None:
    address = Address(host="mc.example.invalid", port=19132, public=True)
    assert render.outside_address(address, True) == "mc.example.invalid:19132"


def test_outside_address_hides_the_host_when_not_public() -> None:
    address = Address(host="mc.example.invalid", port=19132, public=False)
    shown = render.outside_address(address, False)
    assert shown == text.OUTSIDE_ADDRESS_HIDDEN
    assert "mc.example.invalid" not in shown


def test_outside_address_brackets_an_ipv6_host() -> None:
    assert render.outside_address(Address(host="::1", port=19132, public=True), True) == "[::1]:19132"


def test_outside_text_reads_the_public_flag_from_the_address() -> None:
    public = make_card(address=Address(host="mc.example.invalid", port=19132, public=True))
    hidden = make_card(address=Address(host="mc.example.invalid", port=19132, public=False))
    assert "mc.example.invalid:19132" in render.outside_text(public, None, None)
    hidden_text = render.outside_text(hidden, None, None)
    assert text.OUTSIDE_ADDRESS_HIDDEN in hidden_text
    assert "mc.example.invalid" not in hidden_text


def test_outside_location_takes_only_a_bool() -> None:
    params = inspect.signature(render.outside_location).parameters
    assert list(params) == ["has_location"]
    assert get_type_hints(render.outside_location)["has_location"] is bool


def test_outside_location_is_the_fixed_text_or_none() -> None:
    assert render.outside_location(True) == text.OUTSIDE_LOCATION
    assert render.outside_location(False) is None


def test_location_text_never_appears_outside() -> None:
    card = make_card(location=Location(SENTINEL_LOCATION))
    outside = render.outside_text(card, None, None)
    assert SENTINEL_LOCATION not in outside
    assert text.OUTSIDE_LOCATION in outside


def test_outside_text_never_reads_location_text() -> None:
    tree = ast.parse(RENDER_PATH.read_text(encoding="utf-8"))
    body = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "outside_text"
    )
    reads = [n for n in ast.walk(body) if isinstance(n, ast.Attribute) and n.attr == "text"]
    assert reads == []


def test_outside_voice_names_a_verified_channel() -> None:
    assert render.outside_voice("lobby") == "Fluxer: #lobby"


@pytest.mark.parametrize("name", [None, ""])
def test_outside_voice_without_a_name_is_the_generic_label(name: str | None) -> None:
    assert render.outside_voice(name) == text.OUTSIDE_VOICE


def test_outside_voice_neutralises_a_mention_in_the_name() -> None:
    assert render.outside_voice("@everyone") == "Fluxer: #@" + ZWJ + "everyone"


def test_outside_voice_line_uses_the_name_only_when_verified() -> None:
    verified = make_card(voice=Voice(guild_id="1001", channel_id="2001", channel_name="lobby", verified=True))
    unverified = make_card(voice=Voice(guild_id="1001", channel_id="2001", channel_name="lobby", verified=False))
    assert "Fluxer: #lobby" in render.outside_text(verified, None, None).splitlines()
    unverified_lines = render.outside_text(unverified, None, None).splitlines()
    assert text.OUTSIDE_VOICE in unverified_lines
    assert "Fluxer: #lobby" not in unverified_lines


def test_full_outside_text_has_each_line_in_order() -> None:
    status = READY_FULL
    expected = "\n".join([
        "Lobby",
        "Ready · 3 / 10",
        "Restarts in 30 min",
        "Join with the link",
        "mc.example.invalid:19132",
        "Fluxer: #lobby",
        text.OUTSIDE_LOCATION,
    ])
    assert render.outside_text(outside_full_card(), status, "Restarts in 30 min") == expected


def test_outside_text_for_a_bare_card_is_only_the_title() -> None:
    assert render.outside_text(make_card(), None, None) == "Lobby"


@pytest.mark.parametrize(
    ("override", "line_absent"),
    [
        ({"how_to_join": None}, "Join with the link"),
        ({"address": None}, "mc.example.invalid:19132"),
        ({"voice": None}, "Fluxer: #lobby"),
        ({"location": None}, text.OUTSIDE_LOCATION),
    ],
)
def test_each_optional_outside_line_is_absent_when_unset(override: dict, line_absent: str) -> None:
    card = full_card(**override)
    assert line_absent not in render.outside_text(card, READY_FULL, "Restarts in 30 min").splitlines()


def test_outside_status_and_next_lines_follow_their_arguments() -> None:
    card = full_card()
    no_status = render.outside_text(card, None, "Restarts in 30 min").splitlines()
    assert "Ready · 3 / 10" not in no_status
    assert "Restarts in 30 min" in no_status
    no_next = render.outside_text(card, READY_FULL, None).splitlines()
    assert "Restarts in 30 min" not in no_next
    assert "Ready · 3 / 10" in no_next


def test_outside_text_neutralises_mentions_in_title_and_join_but_does_not_escape() -> None:
    card = make_card(title="*Lobby* @everyone", how_to_join="**go** <#1001>")
    outside = render.outside_text(card, None, None)
    assert "*Lobby* @" + ZWJ + "everyone" in outside
    assert "**go** <" + ZWJ + "#1001>" in outside
    assert "\\*" not in outside


def test_outside_text_has_no_markdown_link_or_control_escapes() -> None:
    outside = render.outside_text(make_card(title="[x](https://evil.invalid/x)"), None, None)
    assert outside == "[x](https://evil.invalid/x)"


def test_no_outside_fixture_mentions_guild() -> None:
    for card, status in fixtures():
        outside = render.outside_text(card, status, "Restarts in 30 min")
        assert "guild" not in outside.lower()


def test_outside_text_without_probe_never_mentions_players() -> None:
    for card, status in fixtures():
        if card.probe is not None:
            continue
        assert "player" not in render.outside_text(card, status, None).lower()

