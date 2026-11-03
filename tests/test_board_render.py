# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The summary board's embeds: titles, status cells, jump links, chunking and the 6000-character message total."""

from __future__ import annotations

import math
from typing import Any

import pytest

from fluxer_panel import render, text
from fluxer_panel.cards import Card
from fluxer_panel.resolver import CardStatus, StatusKind, status_label
from fluxer_panel.sources.base import PlayerCounts
from tests.fakes import instance_url_of_length

INSTANCE = "https://fluxer.invalid"
GUILD = "1001"
GAMES = "3001"
MESSAGE = "5001"
READY = CardStatus(StatusKind.READY)


def card(title: str = "Card", *, id: int = 1, sort: int = 0, channel: str | None = GAMES,
         message: str | None = MESSAGE) -> Card:
    return Card(id=id, slug=f"card-{id}", title=title, description=None, how_to_join=None, first_time=None,
                sort_order=sort, show_details=(), state="live", channel_id=channel, message_id=message,
                created_by="1001")


def board(entries: list[tuple[Card, CardStatus | None]], **overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = {"games_channel_id": GAMES, "games_guild_id": GUILD, "instance_url": INSTANCE}
    options.update(overrides)
    return render.board_message(entries, **options)


def cells(payload: dict[str, Any]) -> list[tuple[str, str]]:
    return [(f["name"], f["value"]) for embed in payload["embeds"] for f in embed.get("fields", [])]


def link(label: str, message: str = MESSAGE) -> str:
    return f"[{label}]({INSTANCE}/channels/{GUILD}/{GAMES}/{message})"


# The jump link -------------------------------------------------------------------------------------

def test_jump_link_is_a_masked_link_to_the_message() -> None:
    assert render.jump_link("Ready", INSTANCE, GUILD, GAMES, MESSAGE) == link("Ready")


def test_jump_link_regex_parses_the_url_it_builds() -> None:
    match = render.JUMP_LINK_RE.fullmatch(render.jump_link("Asleep", INSTANCE, GUILD, GAMES, MESSAGE))

    assert match is not None
    assert match.groups() == ("Asleep", GUILD, GAMES, MESSAGE)


def test_jump_link_drops_a_trailing_slash_from_the_instance_url() -> None:
    assert render.jump_link("Ready", INSTANCE + "/", GUILD, GAMES, MESSAGE) == link("Ready")


@pytest.mark.parametrize("ids", [("10a1", GAMES, MESSAGE), (GUILD, "3 01", MESSAGE), (GUILD, GAMES, "5-001")],
                         ids=["community", "channel", "message"])
def test_jump_link_refuses_an_id_that_is_not_a_snowflake(ids: tuple[str, str, str]) -> None:
    with pytest.raises(ValueError):
        render.jump_link("Ready", INSTANCE, *ids)


def test_every_status_label_is_safe_inside_a_masked_link() -> None:
    labels = [status_label(CardStatus(kind)) for kind in StatusKind]
    labels.append(status_label(CardStatus(StatusKind.READY, counts=PlayerCounts(3, 10))))
    labels.append(text.BOARD_NO_STATUS)

    for label in labels:
        assert not set(label) & set("[]()")


# Cells ---------------------------------------------------------------------------------------------

def test_a_linked_status_cell_is_the_jump_link_and_the_name_is_the_title() -> None:
    [(name, value)] = cells(board([(card("Alpha"), READY)]))

    assert name == "Alpha"
    assert value == link("Ready")


def test_a_status_cell_uses_the_status_line_wording_from_the_resolver() -> None:
    status = CardStatus(StatusKind.READY, counts=PlayerCounts(3, 10))

    [(_, value)] = cells(board([(card(), status)], instance_url=None))

    assert value == status_label(status) == "Ready · 3 / 10"


def test_a_card_without_a_live_status_shows_the_fixed_dash() -> None:
    [(_, value)] = cells(board([(card(), None)], instance_url=None))

    assert value == "—"
    assert value == text.BOARD_NO_STATUS


def test_a_card_without_a_live_status_links_to_its_message_when_it_can() -> None:
    [(_, value)] = cells(board([(card(), None)]))

    assert value == link(text.BOARD_NO_STATUS)


def test_a_card_with_no_message_yet_shows_plain_status_text() -> None:
    [(_, value)] = cells(board([(card(message=None), READY)]))

    assert value == "Ready"


def test_no_instance_url_shows_plain_status_text() -> None:
    [(_, value)] = cells(board([(card(), READY)], instance_url=None))

    assert value == "Ready"


def test_an_unknown_games_community_shows_plain_status_text() -> None:
    [(_, value)] = cells(board([(card(), READY)], games_guild_id=None))

    assert value == "Ready"


def test_no_games_channel_shows_plain_status_text() -> None:
    [(_, value)] = cells(board([(card(), READY)], games_channel_id=None))

    assert value == "Ready"


def test_a_message_outside_the_games_channel_shows_plain_status_text() -> None:
    [(_, value)] = cells(board([(card(channel="3999"), READY)]))

    assert value == "Ready"


def test_cells_keep_the_order_they_are_given_in() -> None:
    entries = [(card("First", id=1), READY), (card("Second", id=2), READY), (card("Third", id=3), READY)]

    assert [name for name, _ in cells(board(entries))] == ["First", "Second", "Third"]


def test_a_title_is_escaped_neutralised_and_fitted_to_the_board_limit() -> None:
    title = "@everyone <#" + "*" * 200
    [(name, _)] = cells(board([(card(title), READY)]))

    assert "@everyone" not in name
    assert "<#" not in name
    assert len(name) <= render.LIMITS["board_title"]
    assert name.endswith("…")


def test_a_short_title_is_not_truncated() -> None:
    [(name, _)] = cells(board([(card("Dune *night*"), READY)]))

    assert name == render.escape("Dune *night*")


def test_board_title_limit_is_in_the_limits_table() -> None:
    assert render.LIMITS["board_title"] == 50


# Embeds, chunking and the message total --------------------------------------------------------

def test_fields_per_embed_is_a_multiple_of_three_and_the_cap_is_ten_embeds() -> None:
    assert render.BOARD_FIELDS_PER_EMBED % 3 == 0
    assert render.BOARD_MAX_EMBEDS * render.BOARD_FIELDS_PER_EMBED == render.BOARD_MAX_CARDS == 240


def test_every_field_is_inline_and_every_payload_suppresses_mentions() -> None:
    payload = board([(card(), READY)])

    assert payload["allowed_mentions"] == {"parse": []}
    assert all(f["inline"] is True for embed in payload["embeds"] for f in embed["fields"])


def test_zero_live_cards_show_the_empty_description_and_no_fields() -> None:
    payload = board([])

    [embed] = payload["embeds"]
    assert embed["title"] == text.BOARD_TITLE
    assert embed["description"] == text.BOARD_EMPTY
    assert "fields" not in embed or embed["fields"] == []


@pytest.mark.parametrize("count", [1, 23, 24, 25, 48, 49, 240])
def test_cards_are_chunked_into_full_embeds_with_the_title_on_the_first(count: int) -> None:
    payload = board([(card(f"Card {n}", id=n), READY) for n in range(count)], instance_url=None)

    sizes = [len(embed.get("fields", [])) for embed in payload["embeds"]]
    assert len(sizes) == math.ceil(count / render.BOARD_FIELDS_PER_EMBED)
    assert sizes[:-1] == [render.BOARD_FIELDS_PER_EMBED] * (len(sizes) - 1)
    assert sum(sizes) == count
    assert payload["embeds"][0]["title"] == text.BOARD_TITLE
    assert all("title" not in embed for embed in payload["embeds"][1:])
    assert all("description" not in embed for embed in payload["embeds"])


def test_the_first_card_past_a_full_embed_starts_the_next_embed() -> None:
    payload = board([(card(f"Card {n}", id=n), READY) for n in range(25)])

    first, second = payload["embeds"]
    assert first["fields"][-1]["name"] == "Card 23"
    assert second["fields"][0]["name"] == "Card 24"


def test_240_cards_fit_ten_embeds_with_no_truncation_note() -> None:
    payload = board([(card(f"Card {n}", id=n), READY) for n in range(240)], instance_url=None)

    assert len(payload["embeds"]) == 10
    assert len(cells(payload)) == 240
    assert all("description" not in embed for embed in payload["embeds"])


def test_card_241_is_not_shown_and_the_fixed_note_is_added() -> None:
    entries = [(card(f"Card {n}", id=n), READY) for n in range(241)]

    payload = board(entries, instance_url=None)

    assert len(cells(payload)) == 240
    assert payload["embeds"][-1]["description"] == text.BOARD_TRUNCATED
    assert all(name != "Card 240" for name, _ in cells(payload))


def test_the_message_total_stays_within_6000_characters_in_the_worst_case() -> None:
    worst_url = instance_url_of_length(128)
    max_id = "2" * 25
    worst_title = "*" * 200
    entries = [(card(worst_title, id=n, channel=max_id, message=max_id), CardStatus(
        StatusKind.READY, counts=PlayerCounts(999, 999))) for n in range(240)]

    payload = render.board_message(entries, games_channel_id=max_id, games_guild_id=max_id,
                                   instance_url=worst_url)

    total = len(text.BOARD_TITLE)
    for embed in payload["embeds"]:
        total += len(embed.get("title", "")) + len(embed.get("description", ""))
        for field in embed.get("fields", []):
            assert len(field["name"]) <= 256
            assert len(field["value"]) <= 1024
            total += len(field["name"]) + len(field["value"])
    assert total <= render.BOARD_CHAR_LIMIT == 6000
    assert len(cells(payload)) < 240
    assert payload["embeds"][-1]["description"] == text.BOARD_TRUNCATED


def test_a_board_that_fits_the_total_is_not_truncated_even_at_full_count() -> None:
    entries = [(card(f"Card {n}", id=n), READY) for n in range(240)]

    payload = board(entries, instance_url=None)

    assert len(cells(payload)) == 240
    assert all("description" not in embed for embed in payload["embeds"])


def test_the_total_budget_drops_whole_cells_and_keeps_the_leading_ones() -> None:
    worst_url = instance_url_of_length(128)
    max_id = "2" * 25
    entries = [(card("*" * 200, id=n, channel=max_id, message=max_id), READY) for n in range(240)]

    payload = render.board_message(entries, games_channel_id=max_id, games_guild_id=max_id,
                                   instance_url=worst_url)

    shown = cells(payload)
    assert shown == cells(render.board_message(entries[: len(shown)], games_channel_id=max_id,
                                               games_guild_id=max_id, instance_url=worst_url))
