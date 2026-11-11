# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rendering the card slots: one embed field per slot, labelled with the kind and the suffix; the outside lines; the
play-now fields; and the embed's field count and length at the worst case."""

from __future__ import annotations

import pytest

from fluxer_panel import render, text
from fluxer_panel.cards import Address, ChannelLink, Location
from fluxer_panel.config import INSTANCE_URL_MAX
from fluxer_panel.resolver import CardStatus, StatusKind
from fluxer_panel.sources.base import PlayerCounts, ProbeDetails
from tests.fakes import instance_url_of_length, instance_url_of_units
from tests.test_render import fields_of, make_card

INSTANCE = "https://fluxer.invalid"
LAN_HOST = "sentinel-lan-host.invalid"     # stands in for a private address: it must never show outside Fluxer


def voice(channel: str, name: str | None = None, suffix: str | None = None) -> ChannelLink:
    return ChannelLink("1001", channel, name, name is not None, suffix=suffix)


def names(embed: dict) -> list[str]:
    return [field["name"] for field in embed["fields"]]


# The embed: labels -------------------------------------------------------------------------------------------

def test_a_voice_slot_is_labelled_voice_and_its_suffix() -> None:
    embed = render.render_embed(make_card(voice=voice("2001", "lobby"), voice2=voice("2002", "raid", "Raid night")),
                                None, None, instance_url=INSTANCE)
    assert embed["fields"][:2] == [
        {"name": "Voice", "value": f"[#lobby]({INSTANCE}/channels/1001/2001)", "inline": False},
        {"name": "Voice (Raid night)", "value": f"[#raid]({INSTANCE}/channels/1001/2002)", "inline": False},
    ]


def test_each_text_channel_slot_gets_its_own_field() -> None:
    card = make_card(text_channels=(voice("2101", "builds", "Builds"), None, voice("2103")))
    embed = render.render_embed(card, None, None, instance_url=INSTANCE)
    assert embed["fields"] == [
        {"name": "Channel (Builds)", "value": f"[#builds]({INSTANCE}/channels/1001/2101)", "inline": False},
        {"name": "Channel", "value": f"[{text.TEXT_UNNAMED}]({INSTANCE}/channels/1001/2103)", "inline": False},
    ]
    assert "Channels" not in names(embed)


def test_an_address_slot_is_labelled_address_and_its_suffix_in_code() -> None:
    card = make_card(address=Address("java.invalid", 25565, False, "Java"),
                     address2=Address("bedrock.invalid", 19132, True))
    embed = render.render_embed(card, None, None, instance_url=INSTANCE)
    assert embed["fields"] == [
        {"name": "Address (Java)", "value": "`java.invalid:25565`", "inline": False},
        {"name": "Address", "value": "`bedrock.invalid:19132`", "inline": False},
    ]


def test_channel_values_keep_the_unnamed_label_without_an_instance_url() -> None:
    card = make_card(voice3=voice("2003", None, "Overflow"))
    embed = render.render_embed(card, None, None, instance_url=None)
    assert fields_of(embed) == {"Voice (Overflow)": text.VOICE_UNNAMED}


def test_a_suffix_is_escaped_in_the_label() -> None:
    card = make_card(voice=voice("2001", "lobby", "Geyser-Bedrock"))
    embed = render.render_embed(card, None, None, instance_url=INSTANCE)
    assert names(embed) == ["Voice (Geyser\\-Bedrock)"]


# The embed: order ---------------------------------------------------------------------------------------------

def test_slots_render_voice_then_text_then_address_skipping_empty_slots() -> None:
    card = make_card(
        how_to_join="Join", first_time="First", location=Location("Hall"),
        voice2=voice("2002", "two"), voice3=voice("2003", "three", "C"),
        text_channels=(None, voice("2102", "t2"), voice("2103", "t3", "T")),
        address=Address("a.invalid", 1, False), address3=Address("c.invalid", 3, False, "Z"),
    )
    embed = render.render_embed(card, None, None, instance_url=INSTANCE)
    assert names(embed) == [text.FIELD_JOIN, text.FIELD_FIRST_TIME, "Voice", "Voice (C)", "Channel", "Channel (T)",
                            "Address", "Address (Z)", text.FIELD_LOCATION]


# Outside Fluxer -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "suffix", "line"), [
    ("lobby", None, "Fluxer: #lobby"),
    ("lobby", "Raid night", "Fluxer: #lobby (Raid night)"),
    (None, None, text.OUTSIDE_VOICE),
    (None, "Raid night", "Fluxer voice channel (Raid night)"),
])
def test_outside_voice_appends_the_suffix(name: str | None, suffix: str | None, line: str) -> None:
    assert render.outside_voice(name, suffix) == line


@pytest.mark.parametrize(("name", "suffix", "line"), [
    ("builds", "Builds", "Fluxer: #builds (Builds)"),
    (None, "Builds", "Fluxer text channel (Builds)"),
])
def test_outside_text_channel_appends_the_suffix(name: str | None, suffix: str | None, line: str) -> None:
    assert render.outside_text_channel(name, suffix) == line


@pytest.mark.parametrize(("public", "suffix", "line"), [
    (True, None, "play.invalid:19132"),
    (True, "Bedrock", "Address (Bedrock): play.invalid:19132"),
    (False, None, "Address in Fluxer"),
    (False, "Bedrock", "Address (Bedrock) in Fluxer"),
])
def test_outside_address_shows_or_hides_and_appends_the_suffix(public: bool, suffix: str | None, line: str) -> None:
    assert render.outside_address(Address("play.invalid", 19132, public, suffix), public) == line


def test_outside_text_has_one_line_per_slot_in_slot_order() -> None:
    card = make_card(
        voice=voice("2001", "lobby"), voice2=voice("2002", None, "Raid night"),
        text_channels=(voice("2101", "builds", "Builds"), None, None),
        address=Address(LAN_HOST, 25565, False, "Java"), address2=Address("play.invalid", 19132, True, "Bedrock"),
    )
    assert render.outside_text(card, None, None).splitlines() == [
        "Lobby", "Fluxer: #lobby", "Fluxer voice channel (Raid night)", "Fluxer: #builds (Builds)",
        "Address (Java) in Fluxer", "Address (Bedrock): play.invalid:19132",
    ]


def test_each_address_slot_reads_its_own_public_flag() -> None:
    card = make_card(address=Address("one.invalid", 1, True), address2=Address(LAN_HOST, 2, False),
                     address3=Address("three.invalid", 3, True))
    lines = render.outside_text(card, None, None).splitlines()
    assert lines[1:] == ["one.invalid:1", text.OUTSIDE_ADDRESS_HIDDEN, "three.invalid:3"]


@pytest.mark.parametrize("slot", ["address", "address2", "address3"])
def test_a_private_address_in_any_slot_never_appears_outside(slot: str) -> None:
    others = {name: Address(f"{name}.invalid", 1, True, "Pub") for name in ("address", "address2", "address3")}
    others[slot] = Address(LAN_HOST, 25565, False, "Java")
    outside = render.outside_text(make_card(**others), None, None)
    assert LAN_HOST not in outside
    assert "25565" not in outside
    assert "Address (Java) in Fluxer" in outside


def test_a_suffix_is_neutralised_on_the_outside_line() -> None:
    # A validated suffix cannot hold a mention; the outside line still neutralises it, as a second layer.
    assert render.outside_voice("lobby", "@everyone") == "Fluxer: #lobby (@" + render.ZWJ + "everyone)"


# The play-now message -----------------------------------------------------------------------------------------

def test_play_now_shows_every_public_address_slot_and_every_voice_slot() -> None:
    card = make_card(
        voice=voice("2001", "lobby"), voice3=voice("2003", "raid", "Raid night"),
        address=Address(LAN_HOST, 25565, False, "Java"), address2=Address("play.invalid", 19132, True, "Bedrock"),
    )
    message = render.play_now_message(card, role_id=None, playing=None, ended=False, instance_url=INSTANCE)
    fields = message["embeds"][0]["fields"]
    assert [field["name"] for field in fields] == ["Address (Bedrock)", "Voice", "Voice (Raid night)"]
    assert LAN_HOST not in str(message)


# Embed limits -------------------------------------------------------------------------------------------------

def _worst_card(instance: str):
    marks = "-" * 20                          # the suffix that grows most when escaped: every character doubles
    big_id = "9" * 25                         # the longest id the schema accepts
    channel = ChannelLink(big_id, big_id, "<#" * 50, True, suffix=marks)   # a name that escapes past its limit
    address = Address("h" * 63 + "." + "h" * 63 + "." + "h" * 63 + "." + "h" * 61, 65535, True, marks)
    return make_card(
        title="<#" * 100, description="<#" * 500, how_to_join="<#" * 500, first_time="<#" * 500,
        location=Location("<#" * 100), show_details=("map", "version", "game_mode"),
        voice=channel, voice2=channel, voice3=channel, text_channels=(channel, channel, channel),
        address=address, address2=address, address3=address,
    )


WORST_STATUS = CardStatus(StatusKind.READY, counts=PlayerCounts(online=999_999, max=999_999),
                          details=ProbeDetails(map="<#" * 48, version="<#" * 48, game_mode="<#" * 48))


@pytest.mark.parametrize("instance", [instance_url_of_length(INSTANCE_URL_MAX), instance_url_of_units(INSTANCE_URL_MAX)],
                         ids=["ascii", "astral"])
def test_every_slot_and_suffix_at_maximum_fits_one_embed(instance: str) -> None:
    embed = render.render_embed(_worst_card(instance), WORST_STATUS, "Restarts in 59 min", instance_url=instance,
                                requested=999_999)
    slot_fields = [f for f in embed["fields"] if f["name"].split(" ")[0] in ("Voice", "Channel", "Address")]
    assert len(slot_fields) == 9
    assert len(embed["fields"]) <= render.EMBED_MAX_FIELDS
    assert render.embed_units(embed) <= render.EMBED_CHAR_LIMIT
    for field in embed["fields"]:
        assert 1 <= render.units(field["name"]) <= 256
        assert 1 <= render.units(field["value"]) <= 1024
    assert render.units(embed["title"]) <= 256


def test_the_budget_shortens_first_time_then_description_then_join_and_nothing_else(
        monkeypatch: pytest.MonkeyPatch) -> None:
    instance = instance_url_of_length(INSTANCE_URL_MAX)
    card = _worst_card(instance)
    embed = render.render_embed(card, WORST_STATUS, "Restarts in 59 min", instance_url=instance, requested=999_999)
    monkeypatch.setattr(render, "EMBED_CHAR_LIMIT", 10**6)
    roomy = render.render_embed(card, WORST_STATUS, "Restarts in 59 min", instance_url=instance, requested=999_999)
    monkeypatch.undo()
    fields, roomy_fields = fields_of(embed), fields_of(roomy)
    assert render.embed_units(roomy) > render.EMBED_CHAR_LIMIT      # the worst case does need the budget
    assert fields[text.FIELD_FIRST_TIME].endswith("…")
    for name in fields:
        if name not in (text.FIELD_FIRST_TIME, text.FIELD_JOIN):
            assert fields[name] == roomy_fields[name], name
    shortened_description = embed["description"] != roomy["description"]
    if fields[text.FIELD_JOIN] != roomy_fields[text.FIELD_JOIN]:
        assert shortened_description               # join is cut only after description
    if shortened_description:
        assert render.units(fields[text.FIELD_FIRST_TIME]) == 1   # description is cut only after first_time is gone


def test_an_ordinary_card_is_never_shortened() -> None:
    card = make_card(description="d" * 1000, how_to_join="j" * 900, first_time="f" * 900,
                     voice=voice("2001", "lobby", "Raid night"), address=Address("play.invalid", 19132, True))
    embed = render.render_embed(card, None, None, instance_url=INSTANCE)
    assert embed["description"] == "d" * 1000
    assert fields_of(embed)[text.FIELD_FIRST_TIME] == "f" * 900
