# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The card's "Next session" line, the far-copy variant without its link, the DM hint footer (AC3.5-AC3.8) and the
session line in the outside text (AC11.1). These are pure renderings: a session arrives as a NextSession, so nothing
here reads the database. Escapes are used for the invisible characters."""

from __future__ import annotations

import dataclasses

import pytest

from fluxer_panel import render, text
from fluxer_panel.cards import ChannelLink, Location
from fluxer_panel.render import NextSession
from fluxer_panel.resolver import CardStatus, StatusKind
from fluxer_panel.sources.base import PlayerCounts, ProbeDetails
from tests.test_render import CAP_INSTANCE, INSTANCE, READY_FULL, fields_of, full_card, make_card, repeated

START = 1_800_000_000
JUMP = ("2001", "3200", "5101")                       # (guild, channel, message) of the session message
JUMP_URL = f"{INSTANCE}/channels/2001/3200/5101"
ON = NextSession(starts_at=START, going=3, jump=JUMP, has_location=False)
READY = CardStatus(StatusKind.READY)
GROWTH_UNITS = ["*", "<#"]                            # the escaping worst cases, as in the card rendering tests
SENTINEL_LOCATION = "SENTINEL-LOCATION-42"


def value_of(embed: dict) -> str:
    return fields_of(embed)[text.FIELD_NEXT_SESSION]


def test_the_next_session_field_shows_the_time_the_count_and_a_jump_link() -> None:
    embed = render.render_embed(make_card(), None, None, instance_url=INSTANCE, next_session=ON)
    assert value_of(embed) == f"<t:{START}:F> (<t:{START}:R>) · 3 going · [Open]({JUMP_URL})"


def test_the_field_sits_right_after_the_next_line() -> None:
    embed = render.render_embed(make_card(), READY, "Restarts in 30 min", instance_url=INSTANCE, next_session=ON)
    names = [field["name"] for field in embed["fields"]]
    assert names[:3] == [text.FIELD_STATUS, text.FIELD_NEXT, text.FIELD_NEXT_SESSION]


def test_a_far_copy_variant_has_the_time_and_the_count_but_no_link() -> None:
    far = dataclasses.replace(ON, jump=None)
    embed = render.render_embed(make_card(), None, None, instance_url=INSTANCE, next_session=far)
    assert value_of(embed) == f"<t:{START}:F> (<t:{START}:R>) · 3 going"
    assert "](" not in value_of(embed)


def test_no_jump_link_without_an_instance_url_even_with_a_jump() -> None:
    embed = render.render_embed(make_card(), None, None, instance_url=None, next_session=ON)
    assert value_of(embed) == f"<t:{START}:F> (<t:{START}:R>) · 3 going"


def test_no_next_session_gives_no_such_field() -> None:
    embed = render.render_embed(make_card(), None, None, instance_url=INSTANCE, next_session=None)
    assert text.FIELD_NEXT_SESSION not in fields_of(embed)


@pytest.mark.parametrize("card", [make_card(), full_card()])
@pytest.mark.parametrize("status", [None, READY, READY_FULL, CardStatus(StatusKind.ASLEEP)])
@pytest.mark.parametrize("next_session", [None, ON])
def test_every_card_embed_carries_the_dm_hint_footer(card, status, next_session) -> None:
    embed = render.render_embed(card, status, "Restarts in 30 min", instance_url=INSTANCE, next_session=next_session)
    assert embed["footer"] == {"text": text.CARD_DM_HINT}


def test_the_board_message_gains_no_footer_and_no_next_session_field() -> None:
    board = render.board_message([(make_card(), READY)], games_channel_id="3001", games_guild_id="2001",
                                 instance_url=INSTANCE)
    assert "footer" not in board["embeds"][0]
    assert all(field["name"] != text.FIELD_NEXT_SESSION for field in board["embeds"][0]["fields"])


@pytest.mark.parametrize("unit", GROWTH_UNITS)
def test_the_longest_card_with_a_next_session_and_the_footer_stays_within_the_embed_limit(unit: str) -> None:
    link = ChannelLink(guild_id="1001", channel_id="2001", channel_name=repeated(unit, 100), verified=True)
    card = make_card(
        title=repeated(unit, 200), description=repeated(unit, 1000), how_to_join=repeated(unit, 1000),
        first_time=repeated(unit, 1000), location=Location(repeated(unit, 200)),
        voice=link, voice2=link, voice3=link, text_channels=(link, link, link),
        show_details=("map", "version", "game_mode"),
    )
    status = CardStatus(StatusKind.READY, counts=PlayerCounts(online=3, max=10),
                        details=ProbeDetails(map=repeated(unit, 48), version=repeated(unit, 48),
                                             game_mode=repeated(unit, 48)))
    session = NextSession(starts_at=START, going=10_000, jump=("2001", "3200", "5101"), has_location=True)
    embed = render.render_embed(card, status, repeated(unit, 200), instance_url=CAP_INSTANCE, next_session=session)
    assert text.FIELD_NEXT_SESSION in fields_of(embed)
    assert embed["footer"] == {"text": text.CARD_DM_HINT}
    assert render.embed_units(embed) <= render.EMBED_CHAR_LIMIT


def test_the_outside_text_has_the_session_line_after_the_next_line_with_only_the_placeholder() -> None:
    card = make_card(location=Location(SENTINEL_LOCATION))
    session = NextSession(starts_at=START, going=3, jump=JUMP, has_location=True)
    outside = render.outside_text(card, None, "Restarts in 30 min", next_session=session)
    line = text.OUTSIDE_NEXT_SESSION.format(ts=START, going=3) + " · " + text.OUTSIDE_LOCATION
    assert line in outside.splitlines()
    assert outside.splitlines().index(line) == outside.splitlines().index("Restarts in 30 min") + 1
    assert SENTINEL_LOCATION not in outside


def test_the_outside_session_line_never_carries_a_link() -> None:
    card = make_card()
    far = dataclasses.replace(ON, jump=None)
    outside = render.outside_text(card, None, None, next_session=far)
    assert f"Next session: <t:{START}:F> · 3 going" in outside.splitlines()
    assert "](" not in outside and JUMP_URL not in outside


def test_the_outside_session_line_without_a_location_has_no_placeholder() -> None:
    outside = render.outside_text(make_card(), None, None, next_session=ON)
    assert f"Next session: <t:{START}:F> · 3 going" in outside.splitlines()
    assert text.OUTSIDE_LOCATION not in outside


def test_no_session_gives_the_outside_text_it_always_gave() -> None:
    card = make_card(location=Location(SENTINEL_LOCATION))
    assert render.outside_text(card, None, None, next_session=None) == render.outside_text(card, None, None)
