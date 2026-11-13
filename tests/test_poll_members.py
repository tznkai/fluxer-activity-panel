# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Poll recipients: role holders found by paging the member list, and the cap on who is polled."""

from __future__ import annotations

import logging

import aiohttp
import pytest

from fluxer_client.rest import FluxerREST
from fluxer_panel.poll_members import MembersUnavailable, Recipients, recipients, role_holders
from tests.fakes import FakeFluxerState, Override, make_rest_app

TOKEN = "test.token"
GUILD = "1001"
ROLE = "6000"
MEMBERS_PATH = f"/api/v1/guilds/{GUILD}/members"


@pytest.fixture
async def fake(aiohttp_server):
    state = FakeFluxerState()
    server = await aiohttp_server(make_rest_app(state))
    return state, f"{str(server.make_url('/')).rstrip('/')}/api"


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


def gets(state: FakeFluxerState) -> list[str]:
    """The raw paths (query included) of every GET, in order."""
    return [r.raw_path for r in state.requests if r.method == "GET"]


# AC2.4: paging the member list


async def test_role_holders_pages_until_a_short_page_and_skips_bots_and_non_holders(fake, session):
    state, api = fake
    state.members = {
        (GUILD, "2001"): {"roles": [ROLE]},
        (GUILD, "2002"): {"roles": []},
        (GUILD, "2003"): {"roles": [ROLE]},
        (GUILD, "2004"): {"roles": [ROLE], "user": {"bot": True}},
        (GUILD, "2005"): {"roles": [ROLE]},
    }
    holders = await role_holders(FluxerREST(session, api, TOKEN), GUILD, ROLE, page_size=2)
    assert holders == ["2001", "2003", "2005"]
    assert gets(state) == [
        f"{MEMBERS_PATH}?limit=2",
        f"{MEMBERS_PATH}?limit=2&after=2002",
        f"{MEMBERS_PATH}?limit=2&after=2004",
    ]


async def test_a_full_page_then_an_empty_page_ends_cleanly(fake, session):
    state, api = fake
    state.members = {(GUILD, "2001"): {"roles": [ROLE]}, (GUILD, "2002"): {"roles": [ROLE]}}
    holders = await role_holders(FluxerREST(session, api, TOKEN), GUILD, ROLE, page_size=2)
    assert holders == ["2001", "2002"]
    assert gets(state) == [f"{MEMBERS_PATH}?limit=2", f"{MEMBERS_PATH}?limit=2&after=2002"]


async def test_role_holders_are_sorted_by_numeric_id(fake, session):
    state, api = fake
    state.members = {(GUILD, uid): {"roles": [ROLE]} for uid in ("2010", "2002", "2100")}
    holders = await role_holders(FluxerREST(session, api, TOKEN), GUILD, ROLE)
    assert holders == ["2002", "2010", "2100"]


async def test_no_members_gives_no_holders(fake, session):
    _, api = fake
    assert await role_holders(FluxerREST(session, api, TOKEN), GUILD, ROLE) == []


# Failures refuse the start instead of returning a partial list (used by AC2.8 in Phase 4)


@pytest.mark.parametrize("status", [403, 500])
async def test_list_failure_raises_members_unavailable(fake, session, status, caplog):
    state, api = fake
    state.overrides[f"GET {MEMBERS_PATH}"] = Override(status, b'{"code":"X","message":"x"}')
    caplog.set_level(logging.WARNING, logger="fluxer_panel.poll_members")
    with pytest.raises(MembersUnavailable):
        await role_holders(FluxerREST(session, api, TOKEN), GUILD, ROLE)
    (record,) = [r for r in caplog.records if r.name == "fluxer_panel.poll_members"]
    fields = record.panel_fields
    assert fields["reason"] == "FluxerHTTPError"
    assert "user_id" not in fields


# AC2.5: the cap on recipients


def test_recipients_polls_the_starter_then_the_lowest_ids_up_to_the_cap():
    result = recipients(["2005", "2003", "2004"], starter_id="2001", cap=3)
    assert result == Recipients(user_ids=("2001", "2003", "2004"), left_out=1)


def test_a_starter_who_holds_the_role_is_polled_once():
    result = recipients(["2001", "2003", "2004"], starter_id="2001", cap=10)
    assert result == Recipients(user_ids=("2001", "2003", "2004"), left_out=0)


def test_a_cap_of_one_polls_only_the_starter_and_counts_every_holder_left_out():
    result = recipients(["2003", "2004"], starter_id="2001", cap=1)
    assert result == Recipients(user_ids=("2001",), left_out=2)


def test_the_starter_is_polled_even_when_no_one_holds_the_role():
    assert recipients([], starter_id="2001", cap=5) == Recipients(user_ids=("2001",), left_out=0)


def test_duplicate_holders_are_counted_once():
    result = recipients(["2003", "2003", "2002"], starter_id="2001", cap=10)
    assert result.user_ids == ("2001", "2002", "2003")
    assert result.left_out == 0
