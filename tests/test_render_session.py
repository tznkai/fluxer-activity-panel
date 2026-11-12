# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The session message (AC3.1-AC3.4), and the guard that the play-now message keeps its exact output while the join
fields are shared with it."""

from __future__ import annotations

import json
from typing import Any

import pytest

from fluxer_panel import render, text
from fluxer_panel.cards import Address, ChannelLink, Location
from fluxer_panel.rsvp import Counts
from fluxer_panel.sessions import Session
from tests.test_render import fields_of, make_card

URL = "https://fluxer.invalid"
GUILD = "1001"
CHANNEL = "2001"
STARTS = 1_800_000_000
# No markdown control character, so `render.escape` leaves it as it is and the raw absence checks below are real.
LOCATION_SENTINEL = "SESSION LOCATION SENTINEL 31"
WHEN = {"name": "When", "value": f"<t:{STARTS}:F> · <t:{STARTS}:R>", "inline": False}


def session(**overrides: Any) -> Session:
    base: dict[str, Any] = {
        "id": 42, "card_id": 1, "guild_id": GUILD, "channel_id": CHANNEL, "message_id": "5001",
        "starts_at": STARTS, "ends_at": STARTS + 3600, "proposer_id": "1002", "location_text": None,
        "seats": None, "recurrence": None, "series_id": 42, "next_posted": False, "state": "upcoming",
        "closed_at": None, "moved": False, "headsup_sent": False, "created_at": 1,
    }
    base.update(overrides)
    return Session(**base)


NO_COUNTS = Counts(0, 0, 0)


def rendered(card=None, sess: Session | None = None, counts: Counts = NO_COUNTS, *,
             effective_location: str | None = None, effective_seats: int | None = None,
             scrubbed: bool = False) -> dict[str, Any]:
    return render.session_message(card if card is not None else make_card(title="Lobby"),
                                  sess if sess is not None else session(), counts,
                                  effective_location=effective_location, effective_seats=effective_seats,
                                  instance_url=URL, scrubbed=scrubbed)


def embed_of(payload: dict[str, Any]) -> dict[str, Any]:
    [embed] = payload["embeds"]
    return embed


# The play-now message stays byte-for-byte what it was before the join fields were shared with the session message.

def test_play_now_output_is_unchanged_by_the_shared_join_fields() -> None:
    card = make_card(title="Lobby", how_to_join="Join with the link",
                     voice=ChannelLink("1001", "2001", "lobby", True),
                     voice2=ChannelLink("1001", "2002", None, False, suffix="Raid"),
                     address=Address(host="mc.example.invalid", port=19132, public=True, suffix="Java"),
                     address2=Address(host="hidden.example.invalid", port=25565, public=False))
    payload = render.play_now_message(card, role_id="6000", playing=3, ended=False, instance_url=URL)
    assert payload == {
        "content": "Someone wants to play Lobby now <@&6000>",
        "embeds": [{
            "title": "Lobby",
            "color": render.NEUTRAL,
            "fields": [
                {"name": "Status", "value": "3 playing", "inline": False},
                {"name": "Address (Java)", "value": "`mc.example.invalid:19132`", "inline": False},
                {"name": "Voice", "value": f"[#lobby]({URL}/channels/1001/2001)", "inline": False},
                {"name": "Voice (Raid)", "value": f"[{text.VOICE_UNNAMED}]({URL}/channels/1001/2002)",
                 "inline": False},
                {"name": "How to join", "value": "Join with the link", "inline": False},
            ],
        }],
    }


# AC3.1: the message shows the title, When, the join details, the location, the seats, the counts and the footer.

def test_the_title_and_colour_are_the_card_title_and_neutral() -> None:
    embed = embed_of(rendered(make_card(title="Lobby")))
    assert embed["title"] == "Lobby"
    assert embed["color"] == render.NEUTRAL


def test_the_when_field_is_the_discord_timestamp_pair() -> None:
    assert fields_of(embed_of(rendered()))[text.SESSION_FIELD_WHEN] == f"<t:{STARTS}:F> · <t:{STARTS}:R>"


def test_the_fields_come_in_the_documented_order_with_the_shared_join_fields() -> None:
    card = make_card(title="Lobby", how_to_join="Join with the link",
                     voice=ChannelLink("1001", "2001", "lobby", True),
                     address=Address(host="mc.example.invalid", port=19132, public=True))
    embed = embed_of(rendered(card, session(seats=6), Counts(3, 1, 0), effective_location="Hall B",
                              effective_seats=6))
    assert embed["fields"] == [
        WHEN,
        *render._join_fields(card, URL),
        {"name": "Location", "value": "Hall B", "inline": False},
        {"name": "Seats", "value": "6", "inline": False},
        {"name": "RSVPs", "value": "3 going · 1 maybe", "inline": False},
    ]


def test_the_location_is_the_effective_location_it_is_given() -> None:
    card = make_card(location=Location("Card hall"))
    assert fields_of(embed_of(rendered(card, effective_location="Override hall")))[text.SESSION_FIELD_LOCATION] \
        == "Override hall"
    assert fields_of(embed_of(rendered(card, effective_location="Card hall")))[text.SESSION_FIELD_LOCATION] \
        == "Card hall"


def test_the_seats_field_is_present_only_when_set() -> None:
    assert text.SESSION_FIELD_SEATS not in fields_of(embed_of(rendered(effective_seats=None)))
    assert fields_of(embed_of(rendered(effective_seats=8)))[text.SESSION_FIELD_SEATS] == "8"


def test_counts_show_going_and_maybe() -> None:
    assert fields_of(embed_of(rendered(counts=Counts(3, 1, 0))))[text.SESSION_FIELD_RSVPS] == "3 going · 1 maybe"


def test_counts_add_the_waiting_part_only_when_some_are_waiting() -> None:
    assert fields_of(embed_of(rendered(counts=Counts(3, 1, 2))))[text.SESSION_FIELD_RSVPS] \
        == "3 going · 1 maybe · 2 waiting"


def test_the_footer_carries_the_session_number() -> None:
    assert embed_of(rendered(sess=session(id=42)))["footer"] == {
        "text": "Session 42 · ✅ going · ❔ maybe · ❌ can't",
    }


# AC3.2: every session payload has no mention and sends no users, roles or everyone.

def test_allowed_mentions_are_empty_and_no_mention_form_survives_a_hostile_title() -> None:
    card = make_card(title="<@1001> @everyone", how_to_join="<@&2002> <#2003>")
    payload = rendered(card, effective_location="<@1004> @here")
    assert payload["allowed_mentions"] == {"parse": []}
    assert "<@" not in json.dumps(payload)


# AC3.3: cancelled, ended and scrubbed forms.

def test_the_sentinel_location_reaches_an_upcoming_payload_so_the_absence_checks_can_fail() -> None:
    assert render.escape(LOCATION_SENTINEL) == LOCATION_SENTINEL, "the sentinel must survive render.escape"
    payload = rendered(effective_location=LOCATION_SENTINEL)
    assert fields_of(embed_of(payload))[text.SESSION_FIELD_LOCATION] == LOCATION_SENTINEL
    assert LOCATION_SENTINEL in json.dumps(payload)


def test_a_cancelled_session_says_cancelled_and_has_no_location() -> None:
    payload = rendered(sess=session(state="cancelled", closed_at=2), effective_location=LOCATION_SENTINEL)
    fields = fields_of(embed_of(payload))
    assert fields[text.SESSION_FIELD_STATUS] == text.SESSION_CANCELLED
    assert text.SESSION_FIELD_LOCATION not in fields
    assert LOCATION_SENTINEL not in json.dumps(payload)


def test_an_ended_session_says_ended_and_keeps_its_location() -> None:
    payload = rendered(sess=session(state="ended", closed_at=2), effective_location="Hall B")
    fields = fields_of(embed_of(payload))
    assert fields[text.SESSION_FIELD_STATUS] == text.SESSION_ENDED
    assert fields[text.SESSION_FIELD_LOCATION] == "Hall B"


def test_a_scrubbed_ended_session_says_ended_and_has_no_location() -> None:
    payload = rendered(sess=session(state="ended", closed_at=2), effective_location=LOCATION_SENTINEL,
                       scrubbed=True)
    fields = fields_of(embed_of(payload))
    assert fields[text.SESSION_FIELD_STATUS] == text.SESSION_ENDED
    assert text.SESSION_FIELD_LOCATION not in fields
    assert LOCATION_SENTINEL not in json.dumps(payload)


def test_a_scrubbed_cancelled_session_says_cancelled_and_has_no_location() -> None:
    payload = rendered(sess=session(state="cancelled", closed_at=2), effective_location=LOCATION_SENTINEL,
                       scrubbed=True)
    fields = fields_of(embed_of(payload))
    assert fields[text.SESSION_FIELD_STATUS] == text.SESSION_CANCELLED
    assert LOCATION_SENTINEL not in json.dumps(payload)


def test_an_upcoming_moved_session_shows_time_changed() -> None:
    fields = fields_of(embed_of(rendered(sess=session(moved=True))))
    assert fields[text.SESSION_FIELD_STATUS] == text.SESSION_MOVED


def test_an_upcoming_session_that_was_never_moved_has_no_status_field() -> None:
    assert text.SESSION_FIELD_STATUS not in fields_of(embed_of(rendered()))


# AC3.4: the longest title, join, location and every slot stay within the embed limit.

def _worst_session_payload() -> dict[str, Any]:
    marks = "-" * 20                                  # the suffix that grows most when escaped
    big_id = "9" * 25
    channel = ChannelLink(big_id, big_id, "<#" * 50, True, suffix=marks)
    address = Address("h" * 63 + "." + "h" * 63 + "." + "h" * 63 + "." + "h" * 61, 65535, True, marks)
    card = make_card(title="<#" * 200, how_to_join="<#" * 1000,
                     voice=channel, voice2=channel, voice3=channel, text_channels=(channel, channel, channel),
                     address=address, address2=address, address3=address)
    worst = session(state="upcoming", moved=True, seats=999_999, id=999_999_999)
    return rendered(card, worst, Counts(999_999, 999_999, 999_999), effective_location="<#" * 100,
                    effective_seats=999_999)


def test_the_worst_case_session_stays_within_the_embed_limit() -> None:
    embed = embed_of(_worst_session_payload())
    assert render.embed_units(embed) <= render.EMBED_CHAR_LIMIT
    assert len(embed["fields"]) <= render.EMBED_MAX_FIELDS
    assert render.units(embed["title"]) <= 256
    for field in embed["fields"]:
        assert render.units(field["name"]) <= 256
        assert render.units(field["value"]) <= 1024


def test_the_footer_counts_toward_the_embed_limit() -> None:
    embed = {"title": "T", "fields": [], "footer": {"text": "Session 1 · ✅ going"}}
    assert render.embed_units(embed) == render.units("T") + render.units("Session 1 · ✅ going")


def test_the_session_budget_shortens_the_join_first_and_keeps_the_location_whole(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(render, "EMBED_CHAR_LIMIT", 400)
    embed = embed_of(rendered(make_card(how_to_join="j" * 300), effective_location="L" * 200))
    assert render.embed_units(embed) <= 400
    fields = fields_of(embed)
    assert fields[text.FIELD_JOIN].endswith("…")
    assert fields[text.SESSION_FIELD_LOCATION] == "L" * 200
