# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The far-side renderings of a mirror (AC3.6). A mirror payload never holds a home jump link, and never a link into the
games community: its channel slots are plain names. Pure: no database, no REST."""

from __future__ import annotations

import json
from typing import Any

from fluxer_panel import render
from fluxer_panel.cards import ChannelLink
from fluxer_panel.polls import Poll, PollOption, PollView, Vote
from fluxer_panel.rsvp import Counts
from fluxer_panel.sessions import Session
from tests.test_render import make_card

URL = "https://fluxer.invalid"
# Distinct fake ids, so a leak of any one of them cannot hide behind a coincidental match.
GAMES_GUILD = "3001"        # the games community, and the community of the card's voice channel
VOICE = "3002"              # the card's voice channel
TEXT = "3003"               # the card's text channel
HOME_CHANNEL = "3004"       # where the home object lives
HOME_MESSAGE = "3005"       # the home message
MIRROR_GUILD = "4001"       # a both subscription's community
MIRROR_CHANNEL = "4002"     # the mirror's channel
MIRROR_MESSAGE = "4003"     # the mirror message
ROLE = "5050"
STARTS = 1_800_000_000
CLOSES = STARTS + 86_400
PLAIN_VOICE = "Lobby voice"
HOME_IDS = (GAMES_GUILD, VOICE, TEXT, HOME_CHANNEL, HOME_MESSAGE)


def card() -> Any:
    return make_card(title="Lobby", how_to_join="Join with the link",
                     voice=ChannelLink(GAMES_GUILD, VOICE, PLAIN_VOICE, True),
                     text_channels=(ChannelLink(GAMES_GUILD, TEXT, "Lobby text", True), None, None))


def session() -> Session:
    return Session(id=42, card_id=1, guild_id=GAMES_GUILD, channel_id=HOME_CHANNEL, message_id=HOME_MESSAGE,
                   starts_at=STARTS, ends_at=STARTS + 3600, proposer_id="1002", location_text=None, seats=None,
                   recurrence=None, series_id=42, next_posted=False, state="upcoming", closed_at=None, moved=False,
                   headsup_sent=False, created_at=1)


def poll(**overrides: Any) -> Poll:
    base: dict[str, Any] = {
        "id": 7, "card_id": 1, "guild_id": GAMES_GUILD, "channel_id": HOME_CHANNEL, "message_id": HOME_MESSAGE,
        "starter_id": "1001", "kind": "approval", "parent_poll_id": None, "state": "open", "created_at": 1,
        "closes_at": CLOSES, "closed_at": None, "ballot_size": 6, "pending_ballot_size": None, "sorted_at": None,
        "outcome": None, "winner_option_id": None, "session_id": 42,
    }
    base.update(overrides)
    return Poll(**base)


def poll_view(*, state: str = "open", count: int = 2) -> PollView:
    """`count` options. The ballot size is 6, so more than six options put the link in the open ballot's
    "more" line, which is where an open ballot shows its poll link."""
    options = tuple(PollOption(position + 1, 7, position, STARTS + 3600 * (position + 1), "offered")
                    for position in range(count))
    votes = (Vote("6001", 1, None), Vote("6002", 2, None))
    if state == "closed":
        view_poll = poll(state="closed", closed_at=CLOSES, outcome="winner", winner_option_id=1)
    else:
        view_poll = poll()
    return PollView(poll=view_poll, card_title="Lobby", options=options, votes=votes, recipients=2, answered=2,
                    reached=2, session_link=(GAMES_GUILD, HOME_CHANNEL, HOME_MESSAGE))


def leaked(payload: object, ids: tuple[str, ...]) -> list[str]:
    serialized = json.dumps(payload, ensure_ascii=False)
    return [value for value in ids if value in serialized]


def test_a_far_session_names_its_channels_without_any_link() -> None:
    payload = render.session_message(card(), session(), Counts(1, 0, 0), effective_location=None,
                                     effective_seats=None, instance_url=URL, far=True)
    assert leaked(payload, HOME_IDS) == []
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "/channels/" not in serialized
    assert f"#{PLAIN_VOICE}" in serialized


def test_a_home_session_keeps_its_channel_links() -> None:
    payload = render.session_message(card(), session(), Counts(1, 0, 0), effective_location=None,
                                     effective_seats=None, instance_url=URL)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert f"/channels/{GAMES_GUILD}/{VOICE}" in serialized


def test_a_far_play_now_post_mentions_only_its_role_and_names_no_channel() -> None:
    payload = render.play_now_message(card(), role_id=ROLE, playing=2, ended=False, instance_url=URL, far=True)
    assert leaked(payload, HOME_IDS) == []
    assert f"<@&{ROLE}>" in payload["content"]
    assert f"#{PLAIN_VOICE}" in json.dumps(payload, ensure_ascii=False)


def test_a_far_poll_message_has_no_home_session_link_when_given_no_jump() -> None:
    payload = render.render_poll_message(poll_view(), instance_url=URL, session_jump=None)
    assert leaked(payload, HOME_IDS) == []
    assert "/channels/" not in json.dumps(payload, ensure_ascii=False)


def test_a_far_poll_message_links_only_the_jump_it_is_given() -> None:
    jump = (MIRROR_GUILD, MIRROR_CHANNEL, MIRROR_MESSAGE)
    payload = render.render_poll_message(poll_view(), instance_url=URL, session_jump=jump)
    serialized = json.dumps(payload, ensure_ascii=False)
    assert MIRROR_MESSAGE in serialized
    assert leaked(payload, (HOME_CHANNEL, HOME_MESSAGE)) == []


def test_a_home_poll_message_keeps_its_session_link() -> None:
    jump = (GAMES_GUILD, HOME_CHANNEL, HOME_MESSAGE)
    payload = render.render_poll_message(poll_view(), instance_url=URL, session_jump=jump)
    assert HOME_MESSAGE in json.dumps(payload, ensure_ascii=False)


def test_a_far_ballot_without_a_jump_has_no_link_while_open() -> None:
    # Eight options against a ballot size of six: the open ballot's "more" line is the one place it could link.
    content = render.render_poll_ballot(poll_view(count=8), user_id="6001", zone_name="UTC", instance_url=URL,
                                        poll_jump=None)["content"]
    assert "more options in the poll message: Open the poll" in content   # the link text with no URL behind it
    assert "/channels/" not in content
    assert leaked({"content": content}, HOME_IDS) == []


def test_a_far_ballot_without_a_jump_has_no_link_once_closed() -> None:
    content = render.render_poll_ballot(poll_view(state="closed"), user_id="6001", zone_name="UTC", instance_url=URL,
                                        poll_jump=None)["content"]
    assert "/channels/" not in content
    assert leaked({"content": content}, HOME_IDS) == []


def test_a_far_ballot_links_the_mirror_it_is_given_once_closed() -> None:
    jump = (MIRROR_GUILD, MIRROR_CHANNEL, MIRROR_MESSAGE)
    content = render.render_poll_ballot(poll_view(state="closed"), user_id="6001", zone_name="UTC", instance_url=URL,
                                        poll_jump=jump)["content"]
    assert MIRROR_MESSAGE in content
    assert leaked({"content": content}, (HOME_CHANNEL, HOME_MESSAGE)) == []
