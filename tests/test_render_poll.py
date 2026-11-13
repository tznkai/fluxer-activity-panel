# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The poll message and the ballot (AC4.1-AC4.3, AC9.2). The views are built directly, with no database."""

from __future__ import annotations

import json
from typing import Any

from fluxer_panel import poll_count, render, text
from fluxer_panel.polls import Poll, PollOption, PollView, Vote

URL = "https://fluxer.invalid"
NOW = 1_000_000
HOUR = 3600
ZONE = "UTC"
CLOSES = NOW + 86_400
SLOT = NOW + 2 * HOUR


def make_poll(**overrides: Any) -> Poll:
    base: dict[str, Any] = {
        "id": 7, "card_id": 1, "guild_id": "2001", "channel_id": "2002", "message_id": "5001",
        "starter_id": "1001", "kind": "approval", "parent_poll_id": None, "state": "open", "created_at": 1,
        "closes_at": CLOSES, "closed_at": None, "ballot_size": 6, "pending_ballot_size": None, "sorted_at": None,
        "outcome": None, "winner_option_id": None, "session_id": None,
    }
    base.update(overrides)
    return Poll(**base)


def options_at(count: int, *, start: int = SLOT) -> tuple[PollOption, ...]:
    return tuple(PollOption(id=position + 1, poll_id=7, position=position, starts_at=start + position * HOUR,
                            origin="offered") for position in range(count))


def make_view(*, options: tuple[PollOption, ...] | None = None, votes: tuple[Vote, ...] = (),
              poll: Poll | None = None, title: str = "Lobby", recipients: int = 2, answered: int = 0,
              reached: int = 0, session_link: tuple[str, str, str] | None = None) -> PollView:
    return PollView(poll=poll if poll is not None else make_poll(),
                    card_title=title,
                    options=options if options is not None else options_at(3),
                    votes=votes, recipients=recipients, answered=answered, reached=reached,
                    session_link=session_link)


def lines_of(content: str) -> list[str]:
    return content.split("\n")


def fields_of(embed: dict[str, Any]) -> dict[str, str]:
    return {field["name"]: field["value"] for field in embed["fields"]}


def embed_of(payload: dict[str, Any]) -> dict[str, Any]:
    [embed] = payload["embeds"]
    return embed


# AC4.1: the ballot's options ----------------------------------------------------------------------

def test_ballot_shows_the_first_ballot_size_options_then_a_link_to_the_rest() -> None:
    options = options_at(8)
    votes = (Vote("1002", options[0].id, None), Vote("1003", options[0].id, None))
    view = make_view(options=options, votes=votes, poll=make_poll(ballot_size=6))
    content = render.render_poll_ballot(view, user_id="1004", zone_name=ZONE, instance_url=URL)["content"]
    lines = lines_of(content)
    assert f"a. <t:{options[0].starts_at}:F> (2 votes)" in lines
    assert f"b. <t:{options[1].starts_at}:F> (0 votes)" in lines
    assert f"f. <t:{options[5].starts_at}:F> (0 votes)" in lines
    assert not any(line.startswith("g. ") for line in lines)
    assert "+2 more options in the poll message: [Open the poll](https://fluxer.invalid/channels/2001/2002/5001)" in lines


def test_ballot_has_no_more_line_when_every_option_fits() -> None:
    view = make_view(options=options_at(6), poll=make_poll(ballot_size=6))
    content = render.render_poll_ballot(view, user_id="1004", zone_name=ZONE, instance_url=URL)["content"]
    assert "more options" not in content


def test_ballot_names_the_poll_message_without_a_link_when_there_is_no_instance_url() -> None:
    view = make_view(options=options_at(8), poll=make_poll(ballot_size=6))
    content = render.render_poll_ballot(view, user_id="1004", zone_name=ZONE, instance_url=None)["content"]
    assert "+2 more options in the poll message: Open the poll" in content
    assert "](" not in content


def test_runoff_ballot_counts_only_first_choices() -> None:
    options = options_at(3)
    poll = make_poll(kind="runoff", parent_poll_id=1)
    votes = (Vote("1002", options[1].id, 1), Vote("1002", options[0].id, 2), Vote("1003", options[0].id, 1))
    view = make_view(options=options, votes=votes, poll=poll)
    content = render.render_poll_ballot(view, user_id="1004", zone_name=ZONE, instance_url=URL)["content"]
    lines = lines_of(content)
    assert f"a. <t:{options[0].starts_at}:F> (1 vote)" in lines
    assert f"b. <t:{options[1].starts_at}:F> (1 vote)" in lines


# AC4.2: the instructions ----------------------------------------------------------------------------

def test_approval_ballot_carries_every_instruction() -> None:
    view = make_view(options=options_at(3))
    content = render.render_poll_ballot(view, user_id="1004", zone_name=ZONE, instance_url=URL)["content"]
    assert "Pick by letter, like `a, c`." in content
    assert "`sat 14:00`" in content
    assert "`2026-11-14 14:00`" in content
    assert "in UTC" in content
    assert "`a, fri 20:00`" in content
    assert "`all`" in content
    assert "`clear`" in content
    assert "Each reply replaces your earlier answer." in content


def test_runoff_ballot_omits_the_own_time_line_and_all_and_says_favourite_first() -> None:
    view = make_view(options=options_at(3), poll=make_poll(kind="runoff", parent_poll_id=1))
    content = render.render_poll_ballot(view, user_id="1004", zone_name=ZONE, instance_url=URL)["content"]
    assert "favourite first" in content
    assert "`b, a, c`" in content
    assert "Each reply replaces your earlier answer." in content
    assert "`all`" not in content
    assert "sat 14:00" not in content
    assert "own time" not in content


def test_ballot_shows_the_voters_own_picks_and_the_relettered_note() -> None:
    options = options_at(3)
    votes = (Vote("1004", options[2].id, None), Vote("1004", options[0].id, None), Vote("1002", options[1].id, None))
    view = make_view(options=options, votes=votes, poll=make_poll(sorted_at=NOW))
    content = render.render_poll_ballot(view, user_id="1004", zone_name=ZONE, instance_url=URL)["content"]
    assert "Your picks: a, c" in content
    assert f"Options were re-lettered <t:{NOW}:R>." in content
    assert f"Closes <t:{CLOSES}:F> (<t:{CLOSES}:R>)." in content


def test_ballot_says_when_the_voter_has_not_answered() -> None:
    view = make_view(options=options_at(3))
    content = render.render_poll_ballot(view, user_id="1004", zone_name=ZONE, instance_url=URL)["content"]
    assert "You haven't answered yet." in content


def test_closed_ballot_says_the_poll_is_closed_with_a_link() -> None:
    view = make_view(options=options_at(3), poll=make_poll(state="closed", closed_at=NOW, outcome="none"))
    content = render.render_poll_ballot(view, user_id="1004", zone_name=ZONE, instance_url=URL)["content"]
    assert "This poll is closed: [Open the poll](https://fluxer.invalid/channels/2001/2002/5001)" in content
    assert "`all`" not in content


def test_removed_ballot_says_the_poll_was_removed() -> None:
    view = make_view(options=options_at(3), poll=make_poll(state="closed", closed_at=NOW, outcome="removed"))
    content = render.render_poll_ballot(view, user_id="1004", zone_name=ZONE, instance_url=URL)["content"]
    assert content == text.POLL_BALLOT_REMOVED


def test_a_hostile_title_is_escaped_and_the_ballot_stays_within_its_limit() -> None:
    title = "@everyone <@1> " * 40
    view = make_view(options=options_at(702), poll=make_poll(ballot_size=20), title=title)
    payload = render.render_poll_ballot(view, user_id="1004", zone_name=ZONE, instance_url=URL)
    assert "@everyone" not in payload["content"]
    assert "<@1>" not in payload["content"]
    assert render.units(payload["content"]) <= render.POLL_BALLOT_LIMIT


def test_a_full_ballot_of_long_picks_stays_within_its_limit_and_keeps_the_instructions() -> None:
    options = options_at(702)
    votes = tuple(Vote("1004", option.id, None) for option in options)
    view = make_view(options=options, votes=votes, poll=make_poll(ballot_size=20))
    content = render.render_poll_ballot(view, user_id="1004", zone_name=ZONE, instance_url=URL)["content"]
    assert render.units(content) <= render.POLL_BALLOT_LIMIT
    assert "Each reply replaces your earlier answer." in content


# AC4.3: the open poll message ------------------------------------------------------------------------

def test_the_open_message_lists_every_option_with_counts_closing_leader_and_answered() -> None:
    options = options_at(3)
    votes = (Vote("1002", options[0].id, None), Vote("1003", options[0].id, None), Vote("1004", options[2].id, None))
    view = make_view(options=options, votes=votes, recipients=5, answered=2, reached=5)
    payload = render.render_poll_message(view, instance_url=URL)
    embed = embed_of(payload)
    assert embed["description"].split("\n") == [
        f"a. <t:{options[0].starts_at}:F> (2 votes)",
        f"b. <t:{options[1].starts_at}:F> (0 votes)",
        f"c. <t:{options[2].starts_at}:F> (1 vote)",
    ]
    fields = fields_of(embed)
    assert fields["Closes"] == f"<t:{CLOSES}:F> · <t:{CLOSES}:R>"
    assert fields["Leader"] == f"a. <t:{options[0].starts_at}:F> (2 votes)"
    assert fields["Answered"] == "2 of 5 answered"


def test_a_tie_for_the_leader_goes_to_the_soonest_time() -> None:
    options = options_at(3)
    votes = (Vote("1002", options[2].id, None), Vote("1003", options[1].id, None))
    view = make_view(options=options, votes=votes)
    embed = embed_of(render.render_poll_message(view, instance_url=URL))
    assert fields_of(embed)["Leader"].startswith("b. ")


def test_the_leader_is_none_when_nobody_has_voted() -> None:
    embed = embed_of(render.render_poll_message(make_view(), instance_url=URL))
    assert fields_of(embed)["Leader"] == text.POLL_NO_LEADER


def test_the_answered_line_says_how_many_reached_when_some_did_not() -> None:
    embed = embed_of(render.render_poll_message(make_view(recipients=5, answered=1, reached=3),
                                                instance_url=URL))
    assert fields_of(embed)["Answered"] == "1 of 5 answered · 3 of 5 reached"


def test_a_runoff_message_leads_with_first_choices() -> None:
    options = options_at(3)
    votes = (Vote("1002", options[2].id, 1), Vote("1002", options[0].id, 2), Vote("1003", options[2].id, 1))
    view = make_view(options=options, votes=votes, poll=make_poll(kind="runoff", parent_poll_id=1))
    embed = embed_of(render.render_poll_message(view, instance_url=URL))
    assert fields_of(embed)["Leader"].startswith("c. ")
    assert "(2 votes)" in embed["description"].split("\n")[2]


def test_a_relettered_poll_says_so() -> None:
    embed = embed_of(render.render_poll_message(make_view(poll=make_poll(sorted_at=NOW)), instance_url=URL))
    assert fields_of(embed)[text.POLL_FIELD_NOTE] == f"Options were re-lettered <t:{NOW}:R>."


def test_the_footer_names_the_poll() -> None:
    embed = embed_of(render.render_poll_message(make_view(), instance_url=URL))
    assert embed["footer"]["text"] == "Poll 7 · reply to the bot's DM to vote"


def test_300_options_are_cut_to_the_list_limit_with_a_correct_more_line() -> None:
    options = options_at(300)
    view = make_view(options=options)
    description = embed_of(render.render_poll_message(view, instance_url=URL))["description"]
    assert render.units(description) <= render.POLL_LIST_LIMIT
    lines = description.split("\n")
    shown = len(lines) - 1
    assert lines[-1] == f"+{300 - shown} more"
    assert lines[0].startswith("a. ")


def test_the_worst_case_message_stays_within_the_embed_limit() -> None:
    title = "@everyone <@1> *" * 40
    options = options_at(702)
    votes = tuple(Vote(f"10{n:02d}", option.id, None) for n, option in enumerate(options[:50]))
    view = make_view(options=options, votes=votes, poll=make_poll(sorted_at=NOW, session_id=3), title=title,
                     recipients=702, answered=50, reached=702, session_link=("2001", "2002", "5010"))
    payload = render.render_poll_message(view, instance_url=URL)
    embed = embed_of(payload)
    assert render.embed_units(embed) <= render.EMBED_CHAR_LIMIT
    assert "@everyone" not in embed["title"]
    assert payload["allowed_mentions"] == {"parse": []}


# Closed forms ----------------------------------------------------------------------------------------

def test_a_closed_approval_message_ranks_only_the_options_an_hour_away_at_close() -> None:
    options = (PollOption(1, 7, 0, NOW + HOUR // 2, "offered"), PollOption(2, 7, 1, NOW + 2 * HOUR, "offered"),
               PollOption(3, 7, 2, NOW + 3 * HOUR, "offered"))
    votes = (Vote("1002", 1, None), Vote("1003", 2, None), Vote("1004", 2, None))
    poll = make_poll(state="closed", closed_at=NOW, outcome="winner", winner_option_id=2)
    view = make_view(options=options, votes=votes, poll=poll)
    embed = embed_of(render.render_poll_message(view, instance_url=URL))
    assert embed["description"].split("\n") == [
        f"b. <t:{NOW + 2 * HOUR}:F> (2 votes)",
        f"c. <t:{NOW + 3 * HOUR}:F> (0 votes)",
    ]
    assert fields_of(embed)["Status"] == text.POLL_STATE["closed"]
    assert fields_of(embed)["Result"] == f"Winner: b. <t:{NOW + 2 * HOUR}:F>"


def test_a_closed_poll_with_no_winner_says_so() -> None:
    poll = make_poll(state="closed", closed_at=NOW, outcome="none")
    embed = embed_of(render.render_poll_message(make_view(poll=poll), instance_url=URL))
    assert fields_of(embed)["Status"] == "Closed"
    assert fields_of(embed)["Result"] == text.POLL_NO_WINNER


def test_a_closed_runoff_lists_each_round_and_the_drops() -> None:
    options = options_at(3)
    rounds = poll_count.Rounds(
        rounds=(poll_count.Round(counts=((1, 2), (2, 1), (3, 0)), dropped=3, exhausted=0),
                poll_count.Round(counts=((1, 3), (2, 1)), dropped=None, exhausted=0)),
        winner=1)
    poll = make_poll(kind="runoff", parent_poll_id=1, state="closed", closed_at=NOW, outcome="winner",
                     winner_option_id=1)
    embed = embed_of(render.render_poll_message(make_view(options=options, poll=poll), instance_url=URL, rounds=rounds))
    assert embed["description"].split("\n") == [
        "Round 1: a: 2 votes, b: 1 vote, c: 0 votes. c was dropped.",
        "Round 2: a: 3 votes, b: 1 vote.",
    ]


def test_a_closed_message_links_the_session_when_there_is_one_and_an_instance_url() -> None:
    poll = make_poll(state="closed", closed_at=NOW, outcome="winner", winner_option_id=1, session_id=3)
    view = make_view(poll=poll, session_link=("2001", "2002", "5010"))
    with_link = fields_of(embed_of(render.render_poll_message(view, instance_url=URL)))
    assert with_link["Session"] == "[Open the session](https://fluxer.invalid/channels/2001/2002/5010)"
    without = fields_of(embed_of(render.render_poll_message(view, instance_url=None)))
    assert "Session" not in without


# AC9.2: empty mentions and neutralised text -----------------------------------------------------------

def test_both_renderers_send_empty_allowed_mentions() -> None:
    view = make_view()
    message = render.render_poll_message(view, instance_url=URL)
    ballot = render.render_poll_ballot(view, user_id="1004", zone_name=ZONE, instance_url=URL)
    assert message["allowed_mentions"] == {"parse": []}
    assert ballot["allowed_mentions"] == {"parse": []}


def test_a_hostile_title_is_neutralised_in_the_message_and_the_ballot() -> None:
    view = make_view(title="@everyone <@1>")
    message = render.render_poll_message(view, instance_url=URL)
    ballot = render.render_poll_ballot(view, user_id="1004", zone_name=ZONE, instance_url=URL)
    for payload in (message, ballot):
        dumped = json.dumps(payload)
        assert "@everyone" not in dumped
        assert "<@1>" not in dumped


# The provisional form: the first poll message, sent before the row exists -------------------------------

def test_a_provisional_message_has_no_footer_and_no_answered_field() -> None:
    view = make_view(poll=make_poll(id=0, message_id=""))
    message = render.render_poll_message(view, instance_url=URL, provisional=True)
    embed = embed_of(message)
    assert "footer" not in embed
    assert text.POLL_FIELD_ANSWERED not in fields_of(embed)
    assert text.POLL_FIELD_CLOSES in fields_of(embed)


def test_a_stored_message_keeps_its_footer_and_answered_field() -> None:
    message = render.render_poll_message(make_view(), instance_url=URL)
    embed = embed_of(message)
    assert "footer" in embed
    assert text.POLL_FIELD_ANSWERED in fields_of(embed)
