# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The poll store against real SQLite: one open poll per card, compare-and-set state changes, picks, sorting and cascades."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fluxer_panel import polls, subscriptions
from fluxer_panel.db import Database
from fluxer_panel.polls import MAX_OPTIONS, TooManyOptions


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database.open(tmp_path / "panel.db")
    try:
        yield database
    finally:
        database.close()


def _insert_card(c: sqlite3.Connection) -> int:
    return c.execute("INSERT INTO cards (title, created_by, created_at, updated_at) VALUES ('t', '1001', 1, 1)").lastrowid


async def _card(db: Database) -> int:
    return await db.run(_insert_card)


def _create(c: sqlite3.Connection, card_id: int, *, message_id: str = "5001", recipients: tuple[str, ...] = ("1002", "1003"),
            options: tuple[tuple[int, str], ...] = ((10, "offered"), (20, "offered")), kind: str = "approval",
            parent: int | None = None, closes_at: int = 100, ballot_size: int = 6,
            scope: int | None = None) -> int | None:
    return polls.create(c, card_id=card_id, guild_id="3001", channel_id="3002", message_id=message_id,
                        starter_id="1001", kind=kind, parent_poll_id=parent, now=1, closes_at=closes_at,
                        ballot_size=ballot_size, options=list(options), recipients=list(recipients),
                        scope_sub_id=scope)


async def _poll(db: Database, card_id: int, **kwargs: object) -> int:
    poll_id = await db.run(lambda c: _create(c, card_id, **kwargs))  # type: ignore[arg-type]
    assert poll_id is not None
    return poll_id


async def _option_ids(db: Database, poll_id: int) -> list[int]:
    return [option.id for option in await db.run(lambda c: polls.options(c, poll_id))]


def _count(c: sqlite3.Connection, table: str) -> int:
    return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# AC1.2: one open poll per card ----------------------------------------------------------------------

async def test_a_second_open_poll_for_a_card_is_not_created(db: Database) -> None:
    card = await _card(db)
    await _poll(db, card)

    second = await db.run(lambda c: _create(c, card, message_id="5002"))

    assert second is None
    assert await db.run(lambda c: _count(c, "polls")) == 1


async def test_the_index_refuses_a_second_raw_open_poll_for_a_card(db: Database) -> None:
    card = await _card(db)
    await _poll(db, card)

    with pytest.raises(sqlite3.IntegrityError):
        await db.run(lambda c: c.execute(
            "INSERT INTO polls (card_id, guild_id, channel_id, message_id, starter_id, kind, state, created_at, "
            "closes_at, ballot_size) VALUES (?, '3001', '3002', '5009', '1001', 'approval', 'open', 1, 100, 6)",
            (card,)))


async def test_a_closed_poll_does_not_block_a_new_poll_for_its_card(db: Database) -> None:
    card = await _card(db)
    first = await _poll(db, card)
    assert await db.run(lambda c: polls.close_if_open(c, first, now=5, outcome="none", winner_option_id=None))

    second = await db.run(lambda c: _create(c, card, message_id="5002"))

    assert second is not None and second != first


# AC1.3: options are unique by starts_at through the store ---------------------------------------------

async def test_add_option_returns_the_existing_option_for_a_repeated_start(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    existing = (await db.run(lambda c: polls.options(c, poll_id)))[1]

    result = await db.run(lambda c: polls.add_option(c, poll_id, starts_at=20, origin="typed"))

    assert result == (existing.id, False)
    assert await db.run(lambda c: _count(c, "poll_options")) == 2


# AC1.4: a card's poll rows cascade with the card ------------------------------------------------------

async def test_deleting_a_card_cascades_to_its_polls_options_ballots_and_votes(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    option_ids = await _option_ids(db, poll_id)
    await db.run(lambda c: polls.replace_picks_if_open(c, poll_id, "1002", [option_ids[0]], ranked=False))

    await db.run(lambda c: c.execute("DELETE FROM cards WHERE id = ?", (card,)))

    counts = await db.run(lambda c: [_count(c, t) for t in ("polls", "poll_options", "poll_ballots", "poll_votes")])
    assert counts == [0, 0, 0, 0]


# add_option -------------------------------------------------------------------------------------------

async def test_add_option_appends_at_the_next_position(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)

    result = await db.run(lambda c: polls.add_option(c, poll_id, starts_at=30, origin="typed"))

    assert result[1] is True
    options = await db.run(lambda c: polls.options(c, poll_id))
    assert [(o.position, o.starts_at, o.origin) for o in options] == [(0, 10, "offered"), (1, 20, "offered"),
                                                                      (2, 30, "typed")]


async def test_add_option_refuses_one_more_than_the_maximum(db: Database) -> None:
    card = await _card(db)
    full = tuple((start, "offered") for start in range(MAX_OPTIONS))
    poll_id = await _poll(db, card, options=full, recipients=())

    with pytest.raises(TooManyOptions):
        await db.run(lambda c: polls.add_option(c, poll_id, starts_at=10_000, origin="typed"))
    assert await db.run(lambda c: _count(c, "poll_options")) == MAX_OPTIONS


# Picks ------------------------------------------------------------------------------------------------

async def test_replace_picks_replaces_the_previous_picks(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    first, second = await _option_ids(db, poll_id)

    assert await db.run(lambda c: polls.replace_picks_if_open(c, poll_id, "1002", [first], ranked=False))
    assert await db.run(lambda c: polls.replace_picks_if_open(c, poll_id, "1002", [second], ranked=False))

    assert await db.run(lambda c: polls.picks(c, poll_id, "1002")) == [second]


async def test_an_empty_replace_clears_the_picks_and_still_counts_as_answered(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    first, _ = await _option_ids(db, poll_id)
    await db.run(lambda c: polls.replace_picks_if_open(c, poll_id, "1002", [first], ranked=False))

    assert await db.run(lambda c: polls.replace_picks_if_open(c, poll_id, "1002", [], ranked=False))

    assert await db.run(lambda c: polls.picks(c, poll_id, "1002")) == []
    ballot = await db.run(lambda c: polls.ballot(c, poll_id, "1002"))
    assert ballot is not None and ballot.answered is True


async def test_a_ranked_replace_stores_the_ranks_in_order(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    first, second = await _option_ids(db, poll_id)

    assert await db.run(lambda c: polls.replace_picks_if_open(c, poll_id, "1002", [second, first], ranked=True))

    assert await db.run(lambda c: polls.picks(c, poll_id, "1002")) == [second, first]
    votes = await db.run(lambda c: polls.votes(c, poll_id))
    assert [(v.user_id, v.option_id, v.rank) for v in votes] == [("1002", second, 1), ("1002", first, 2)]


async def test_an_unranked_replace_stores_no_ranks(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    first, second = await _option_ids(db, poll_id)

    await db.run(lambda c: polls.replace_picks_if_open(c, poll_id, "1002", [second, first], ranked=False))

    assert await db.run(lambda c: polls.picks(c, poll_id, "1002")) == [first, second]
    votes = await db.run(lambda c: polls.votes(c, poll_id))
    assert [v.rank for v in votes] == [None, None]


async def test_replace_picks_for_a_user_without_a_ballot_changes_nothing(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    first, _ = await _option_ids(db, poll_id)

    assert await db.run(lambda c: polls.replace_picks_if_open(c, poll_id, "9999", [first], ranked=False)) is False
    assert await db.run(lambda c: polls.picks(c, poll_id, "9999")) == []


async def test_replace_picks_refuses_a_duplicate_option(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    first, _ = await _option_ids(db, poll_id)

    with pytest.raises(ValueError):
        await db.run(lambda c: polls.replace_picks_if_open(c, poll_id, "1002", [first, first], ranked=False))


async def test_replace_picks_refuses_another_polls_option_and_changes_nothing(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    other = await _poll(db, await _card(db), message_id="5002")
    foreign, _ = await _option_ids(db, other)

    with pytest.raises(sqlite3.IntegrityError):
        await db.run(lambda c: polls.replace_picks_if_open(c, poll_id, "1002", [foreign], ranked=False))

    ballot = await db.run(lambda c: polls.ballot(c, poll_id, "1002"))
    assert ballot is not None and ballot.answered is False
    assert await db.run(lambda c: _count(c, "poll_votes")) == 0


async def test_replace_picks_on_a_closed_poll_changes_nothing(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    first, _ = await _option_ids(db, poll_id)
    await db.run(lambda c: polls.close_if_open(c, poll_id, now=5, outcome="none", winner_option_id=None))

    assert await db.run(lambda c: polls.replace_picks_if_open(c, poll_id, "1002", [first], ranked=False)) is False

    ballot = await db.run(lambda c: polls.ballot(c, poll_id, "1002"))
    assert ballot is not None and ballot.answered is False
    assert await db.run(lambda c: polls.picks(c, poll_id, "1002")) == []


# Sorting ----------------------------------------------------------------------------------------------

async def test_sort_if_open_rewrites_the_positions_and_applies_the_pending_ballot_size(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card, options=((10, "offered"), (20, "offered"), (30, "offered")))
    first, second, third = await _option_ids(db, poll_id)
    assert await db.run(lambda c: polls.set_pending_ballot_size_if_open(c, poll_id, 4))

    assert await db.run(lambda c: polls.sort_if_open(c, poll_id, [third, first, second], now=7)) is True

    options = await db.run(lambda c: polls.options(c, poll_id))
    assert [(o.id, o.position, o.starts_at) for o in options] == [(third, 0, 30), (first, 1, 10), (second, 2, 20)]
    poll = await db.run(lambda c: polls.get(c, poll_id))
    assert poll is not None
    assert (poll.ballot_size, poll.pending_ballot_size, poll.sorted_at) == (4, None, 7)


async def test_sort_if_open_refuses_ids_that_are_not_exactly_this_polls_options(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    first, second = await _option_ids(db, poll_id)
    foreign = await _option_ids(db, await _poll(db, await _card(db), message_id="5002"))

    # wrong length: rejected by the length check
    with pytest.raises(ValueError):
        await db.run(lambda c: polls.sort_if_open(c, poll_id, [first], now=7))
    with pytest.raises(ValueError):
        await db.run(lambda c: polls.sort_if_open(c, poll_id, [first, second, second], now=7))
    # right length, wrong members: only the set check rejects these
    with pytest.raises(ValueError):
        await db.run(lambda c: polls.sort_if_open(c, poll_id, [first, first], now=7))
    with pytest.raises(ValueError):
        await db.run(lambda c: polls.sort_if_open(c, poll_id, [first, 9999], now=7))
    with pytest.raises(ValueError):
        await db.run(lambda c: polls.sort_if_open(c, poll_id, [first, foreign[0]], now=7))

    options = await db.run(lambda c: polls.options(c, poll_id))
    assert [(o.id, o.position) for o in options] == [(first, 0), (second, 1)]
    poll = await db.run(lambda c: polls.get(c, poll_id))
    assert poll is not None and poll.sorted_at is None


# Compare-and-set on a closed poll ---------------------------------------------------------------------

async def test_state_changes_refuse_a_closed_poll(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    first, second = await _option_ids(db, poll_id)
    assert await db.run(lambda c: polls.close_if_open(c, poll_id, now=5, outcome="none", winner_option_id=None))

    results = await db.run(lambda c: [
        polls.replace_picks_if_open(c, poll_id, "1002", [first], ranked=False),
        polls.sort_if_open(c, poll_id, [second, first], now=6),
        polls.set_pending_ballot_size_if_open(c, poll_id, 3),
        polls.extend_if_open(c, poll_id, closes_at=200),
        polls.close_if_open(c, poll_id, now=7, outcome="removed", winner_option_id=None),
    ])

    assert results == [False, False, False, False, False]
    poll = await db.run(lambda c: polls.get(c, poll_id))
    assert poll is not None
    assert (poll.closes_at, poll.ballot_size, poll.pending_ballot_size, poll.sorted_at, poll.closed_at,
            poll.outcome) == (100, 6, None, None, 5, "none")
    options = await db.run(lambda c: polls.options(c, poll_id))
    assert [o.id for o in options] == [first, second]
    assert [o.position for o in options] == [0, 1]


async def test_close_if_open_is_true_only_once(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    first, _ = await _option_ids(db, poll_id)

    assert await db.run(lambda c: polls.close_if_open(c, poll_id, now=5, outcome="winner",
                                                     winner_option_id=first)) is True
    assert await db.run(lambda c: polls.close_if_open(c, poll_id, now=6, outcome="none",
                                                     winner_option_id=None)) is False

    poll = await db.run(lambda c: polls.get(c, poll_id))
    assert poll is not None
    assert (poll.state, poll.closed_at, poll.outcome, poll.winner_option_id) == ("closed", 5, "winner", first)


async def test_extend_if_open_moves_the_close_time_while_open(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)

    assert await db.run(lambda c: polls.extend_if_open(c, poll_id, closes_at=200)) is True

    poll = await db.run(lambda c: polls.get(c, poll_id))
    assert poll is not None and poll.closes_at == 200


# Reads ------------------------------------------------------------------------------------------------

async def test_due_and_next_close_follow_the_open_polls(db: Database) -> None:
    first_card, second_card = await _card(db), await _card(db)
    later = await _poll(db, first_card, message_id="5001", closes_at=100)
    sooner = await _poll(db, second_card, message_id="5002", closes_at=50)

    assert await db.run(lambda c: [p.id for p in polls.open_polls(c)]) == [later, sooner]
    assert await db.run(lambda c: [p.id for p in polls.due(c, 60)]) == [sooner]
    assert await db.run(lambda c: [p.id for p in polls.due(c, 100)]) == [sooner, later]
    assert await db.run(lambda c: polls.next_close(c)) == 50

    await db.run(lambda c: polls.close_if_open(c, sooner, now=55, outcome="none", winner_option_id=None))

    assert await db.run(lambda c: [p.id for p in polls.due(c, 100)]) == [later]
    assert await db.run(lambda c: polls.next_close(c)) == 100


async def test_next_close_is_none_without_open_polls(db: Database) -> None:
    assert await db.run(lambda c: polls.next_close(c)) is None


async def test_for_user_lists_the_starters_and_ballot_holders_newest_first(db: Database) -> None:
    first_card, second_card = await _card(db), await _card(db)
    older = await _poll(db, first_card, message_id="5001")
    newer = await _poll(db, second_card, message_id="5002", recipients=("1002",))

    assert await db.run(lambda c: [p.id for p in polls.for_user(c, "1002")]) == [newer, older]
    assert await db.run(lambda c: [p.id for p in polls.for_user(c, "1001")]) == [newer, older]
    assert await db.run(lambda c: polls.for_user(c, "9999")) == []


async def test_open_ballot_polls_lists_only_open_polls_where_the_user_has_a_ballot(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    assert await db.run(lambda c: [p.id for p in polls.open_ballot_polls(c, "1002")]) == [poll_id]
    assert await db.run(lambda c: polls.open_ballot_polls(c, "1001")) == []

    await db.run(lambda c: polls.close_if_open(c, poll_id, now=5, outcome="none", winner_option_id=None))

    assert await db.run(lambda c: polls.open_ballot_polls(c, "1002")) == []


async def test_child_of_returns_the_runoff_started_from_a_poll(db: Database) -> None:
    card = await _card(db)
    parent = await _poll(db, card)
    await db.run(lambda c: polls.close_if_open(c, parent, now=5, outcome="none", winner_option_id=None))
    runoff = await db.run(lambda c: _create(c, card, message_id="5002", kind="runoff", parent=parent))
    assert runoff is not None

    child = await db.run(lambda c: polls.child_of(c, parent))
    assert child is not None and child.id == runoff and child.is_runoff is True
    assert await db.run(lambda c: polls.child_of(c, runoff)) is None


# Create -----------------------------------------------------------------------------------------------

async def test_create_deduplicates_recipients_and_keeps_their_order(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card, recipients=("1003", "1002", "1003"))

    ballots = await db.run(lambda c: polls.ballots(c, poll_id))
    assert [b.user_id for b in ballots] == ["1002", "1003"]


async def test_create_refuses_a_runoff_without_a_parent_and_an_approval_with_one(db: Database) -> None:
    card = await _card(db)
    parent = await _poll(db, card)
    await db.run(lambda c: polls.close_if_open(c, parent, now=5, outcome="none", winner_option_id=None))

    with pytest.raises(ValueError):
        await db.run(lambda c: _create(c, card, kind="runoff", parent=None))
    with pytest.raises(ValueError):
        await db.run(lambda c: _create(c, card, kind="approval", parent=parent))
    assert await db.run(lambda c: _count(c, "polls")) == 1


# Ballot bookkeeping -----------------------------------------------------------------------------------

async def test_mark_reminded_if_is_true_once_and_never_for_an_answered_ballot(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    first, _ = await _option_ids(db, poll_id)
    await db.run(lambda c: polls.replace_picks_if_open(c, poll_id, "1003", [first], ranked=False))

    assert await db.run(lambda c: polls.mark_reminded_if(c, poll_id, "1002")) is True
    assert await db.run(lambda c: polls.mark_reminded_if(c, poll_id, "1002")) is False
    assert await db.run(lambda c: polls.mark_reminded_if(c, poll_id, "1003")) is False

    reminded = await db.run(lambda c: polls.ballot(c, poll_id, "1002"))
    assert reminded is not None and reminded.reminded is True
    answered = await db.run(lambda c: polls.ballot(c, poll_id, "1003"))
    assert answered is not None and answered.reminded is False


async def test_mark_reminded_if_is_false_once_the_poll_is_closed(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    await db.run(lambda c: polls.close_if_open(c, poll_id, now=5, outcome="none", winner_option_id=None))

    assert await db.run(lambda c: polls.mark_reminded_if(c, poll_id, "1002")) is False
    reminded = await db.run(lambda c: polls.ballot(c, poll_id, "1002"))
    assert reminded is not None and reminded.reminded is False


async def test_reminder_due_lists_unanswered_unreminded_ballots_from_halfway_until_close(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card, recipients=("1002", "1003", "1004"))   # created at 1, closes at 100: halfway is 50
    first, _ = await _option_ids(db, poll_id)
    await db.run(lambda c: polls.replace_picks_if_open(c, poll_id, "1003", [first], ranked=False))
    await db.run(lambda c: polls.mark_reminded_if(c, poll_id, "1004"))

    assert await db.run(lambda c: polls.reminder_due(c, 49)) == []
    due = await db.run(lambda c: polls.reminder_due(c, 50))
    assert [(poll.id, user) for poll, user in due] == [(poll_id, "1002")]
    assert await db.run(lambda c: polls.reminder_due(c, 100)) == []


async def test_next_reminder_is_the_soonest_halfway_still_ahead_among_open_polls(db: Database) -> None:
    card = await _card(db)
    assert await db.run(lambda c: polls.next_reminder(c, 0)) is None
    await _poll(db, card)                                       # halfway 50
    assert await db.run(lambda c: polls.next_reminder(c, 0)) == 50
    assert await db.run(lambda c: polls.next_reminder(c, 50)) is None   # passed: nothing left to wait for


async def test_set_ballot_message_and_mark_dm_failed_are_stored(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)

    await db.run(lambda c: polls.set_ballot_message(c, poll_id, "1002", channel_id="4001", message_id="4002"))
    await db.run(lambda c: polls.mark_dm_failed(c, poll_id, "1003"))

    first = await db.run(lambda c: polls.ballot(c, poll_id, "1002"))
    second = await db.run(lambda c: polls.ballot(c, poll_id, "1003"))
    assert first is not None and (first.dm_channel_id, first.dm_message_id) == ("4001", "4002")
    assert second is not None and second.dm_failed is True


async def test_set_session_if_unset_is_true_once_and_only_for_a_winner(db: Database) -> None:
    card = await _card(db)
    winner = await _poll(db, card)
    first, _ = await _option_ids(db, winner)
    assert await db.run(lambda c: polls.close_if_open(c, winner, now=5, outcome="winner",
                                                     winner_option_id=first))
    session = await db.run(lambda c: c.execute(
        "INSERT INTO sessions (card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, created_at) "
        "VALUES (?, '3001', '3002', '9001', 10, 20, '1001', 1)", (card,)).lastrowid)

    assert await db.run(lambda c: polls.set_session_if_unset(c, winner, session)) is True
    assert await db.run(lambda c: polls.set_session_if_unset(c, winner, session)) is False
    poll = await db.run(lambda c: polls.get(c, winner))
    assert poll is not None and poll.session_id == session

    other_card = await _card(db)
    loser = await _poll(db, other_card, message_id="5002")
    await db.run(lambda c: polls.close_if_open(c, loser, now=5, outcome="none", winner_option_id=None))
    assert await db.run(lambda c: polls.set_session_if_unset(c, loser, session)) is False


# AC4.3 (the read): one transaction gives the poll view the renderers need ----------------------------

async def test_view_counts_the_ballots_and_votes_and_reads_the_session_link(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card, recipients=("1002", "1003", "1004"))
    first, second = await _option_ids(db, poll_id)
    assert await db.run(lambda c: polls.replace_picks_if_open(c, poll_id, "1002", [first], ranked=False))
    await db.run(lambda c: polls.set_ballot_message(c, poll_id, "1002", channel_id="9100", message_id="9101"))
    assert await db.run(lambda c: polls.close_if_open(c, poll_id, now=5, outcome="winner", winner_option_id=first))
    session = await db.run(lambda c: c.execute(
        "INSERT INTO sessions (card_id, guild_id, channel_id, message_id, starts_at, ends_at, proposer_id, created_at) "
        "VALUES (?, '3001', '3002', '9001', 10, 20, '1001', 1)", (card,)).lastrowid)
    assert await db.run(lambda c: polls.set_session_if_unset(c, poll_id, session))

    view = await db.run(lambda c: polls.view(c, poll_id))
    assert view is not None
    assert view.card_title == "t"
    assert [option.id for option in view.options] == [first, second]
    assert [(vote.user_id, vote.option_id) for vote in view.votes] == [("1002", first)]
    assert (view.recipients, view.answered, view.reached) == (3, 1, 1)
    assert view.session_link == ("3001", "3002", "9001")


async def test_view_of_a_missing_poll_is_none(db: Database) -> None:
    assert await db.run(lambda c: polls.view(c, 999)) is None


async def test_view_without_a_session_has_no_link(db: Database) -> None:
    card = await _card(db)
    poll_id = await _poll(db, card)
    view = await db.run(lambda c: polls.view(c, poll_id))
    assert view is not None and view.session_link is None and view.reached == 0


def test_a_provisional_view_is_unstored_and_lettered_from_its_positions() -> None:
    view = polls.provisional_view(card_id=1, card_title="Lobby", guild_id="2001", channel_id="3001", starter_id="1001",
                            now=100, closes_at=200, ballot_size=6, starts=[1_800_003_600, 1_800_007_200], recipients=3)
    assert view.poll.id == 0
    assert view.poll.state == "open" and view.poll.closes_at == 200
    assert [o.position for o in view.options] == [0, 1]
    assert len({o.id for o in view.options}) == 2
    assert [o.starts_at for o in view.options] == [1_800_003_600, 1_800_007_200]
    assert (view.recipients, view.answered, view.reached, view.votes) == (3, 0, 0, ())


# Scopes: one open poll per scope, and the origin of each ballot ---------------------------------------

async def _sub(db: Database, card_id: int, channel_id: str = "3060") -> int:
    return await db.run(lambda c: subscriptions.add(c, card_id, "3001", channel_id))


async def test_open_for_scope_sees_only_its_own_scope(db: Database) -> None:
    card = await _card(db)
    sub = await _sub(db, card)
    home = await _poll(db, card)
    scoped = await db.run(lambda c: _create(c, card, message_id="5002", scope=sub))
    assert scoped is not None
    assert (await db.run(lambda c: polls.open_for_scope(c, card, None))).id == home  # type: ignore[union-attr]
    assert (await db.run(lambda c: polls.open_for_scope(c, card, sub))).id == scoped  # type: ignore[union-attr]
    assert {poll.id for poll in await db.run(lambda c: polls.open_for_card_any(c, card))} == {home, scoped}


async def test_create_refuses_a_second_open_poll_in_a_scope_and_accepts_another_scope(db: Database) -> None:
    card = await _card(db)
    sub = await _sub(db, card)
    assert await db.run(lambda c: _create(c, card)) is not None
    assert await db.run(lambda c: _create(c, card, message_id="5002")) is None
    assert await db.run(lambda c: _create(c, card, message_id="5003", scope=sub)) is not None
    assert await db.run(lambda c: _create(c, card, message_id="5004", scope=sub)) is None


async def test_create_stores_the_via_subscription_per_ballot(db: Database) -> None:
    card = await _card(db)
    sub = await _sub(db, card)
    poll_id = await db.run(lambda c: polls.create(
        c, card_id=card, guild_id="3001", channel_id="3002", message_id="5002", starter_id="1001", kind="approval",
        parent_poll_id=None, now=1, closes_at=100, ballot_size=6, options=[(10, "offered")],
        recipients=["1002", "1003"], scope_sub_id=sub, via={"1002": sub}))
    assert poll_id is not None
    reached = await db.run(lambda c: polls.ballot(c, poll_id, "1002"))
    home_only = await db.run(lambda c: polls.ballot(c, poll_id, "1003"))
    assert reached is not None and reached.via_sub_id == sub
    assert home_only is not None and home_only.via_sub_id is None


async def test_list_for_scope_returns_only_that_scope(db: Database) -> None:
    card = await _card(db)
    sub = await _sub(db, card)
    await _poll(db, card)
    scoped = await db.run(lambda c: _create(c, card, message_id="5002", scope=sub))
    assert [poll.id for poll in await db.run(lambda c: polls.list_for_scope(c, sub))] == [scoped]
