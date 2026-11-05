# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gateway dispatch parsing: payloads are hand-built with fake ids."""

from __future__ import annotations

import logging

import pytest

from fluxer_client import events as events_module
from fluxer_client.events import (
    Emoji,
    GuildRolesChanged,
    MessageCreate,
    MessageDelete,
    Ready,
    ReactionAdd,
    ReactionRemove,
    ReactionRemoveAll,
    ReactionRemoveEmoji,
    Resumed,
    parse,
)

LOGGER = "fluxer_client.events"


def warnings_for(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.WARNING]


# READY and RESUMED


def test_ready_gives_user_and_session() -> None:
    event = parse("READY", {"user": {"id": "1001"}, "session_id": "sess-abc"})
    assert event == Ready(user_id="1001", session_id="sess-abc")


def test_ready_session_id_is_not_in_repr() -> None:
    event = parse("READY", {"user": {"id": "1001"}, "session_id": "SENTINEL-SESSION"})
    assert "SENTINEL-SESSION" not in repr(event)


def test_resumed_gives_resumed() -> None:
    assert parse("RESUMED", {}) == Resumed()


# MESSAGE_CREATE


def test_guild_message_carries_guild_and_member_roles() -> None:
    event = parse("MESSAGE_CREATE", {
        "id": "5001",
        "channel_id": "3001",
        "guild_id": "2001",
        "author": {"id": "1001"},
        "content": "hi",
        "member": {"roles": ["4001", "4002"]},
    })
    assert event == MessageCreate(
        message_id="5001",
        channel_id="3001",
        guild_id="2001",
        author_id="1001",
        author_is_bot=False,
        member_role_ids=frozenset({"4001", "4002"}),
        content="hi",
    )


def test_dm_message_has_no_guild_and_no_roles() -> None:
    event = parse("MESSAGE_CREATE", {
        "id": "5001",
        "channel_id": "3001",
        "author": {"id": "1001"},
        "content": "hi",
    })
    assert event.guild_id is None
    assert event.member_role_ids is None


def test_author_bot_true_is_carried() -> None:
    event = parse("MESSAGE_CREATE", {
        "id": "5001", "channel_id": "3001", "author": {"id": "1001", "bot": True}, "content": "",
    })
    assert event.author_is_bot is True


def test_missing_author_bot_is_false() -> None:
    event = parse("MESSAGE_CREATE", {
        "id": "5001", "channel_id": "3001", "author": {"id": "1001"}, "content": "",
    })
    assert event.author_is_bot is False


def test_message_content_is_hidden_from_repr() -> None:
    event = parse("MESSAGE_CREATE", {
        "id": "5001", "channel_id": "3001", "author": {"id": "1001"}, "content": "SENTINEL-CONTENT",
    })
    assert event.content == "SENTINEL-CONTENT"
    assert "SENTINEL-CONTENT" not in repr(event)


# Reactions: guild and DM, unicode and custom emoji


def test_reaction_add_guild_unicode() -> None:
    event = parse("MESSAGE_REACTION_ADD", {
        "user_id": "1001",
        "channel_id": "3001",
        "message_id": "5001",
        "guild_id": "2001",
        "emoji": {"name": "👍"},
        "member": {"roles": ["4001"]},
    })
    assert event == ReactionAdd(
        user_id="1001",
        channel_id="3001",
        message_id="5001",
        guild_id="2001",
        emoji=Emoji(name="👍", id=None),
        member_role_ids=frozenset({"4001"}),
    )


def test_reaction_add_dm_custom_emoji() -> None:
    event = parse("MESSAGE_REACTION_ADD", {
        "user_id": "1001",
        "channel_id": "3001",
        "message_id": "5001",
        "emoji": {"name": "party_parrot", "id": "6001", "animated": True},
    })
    assert isinstance(event, ReactionAdd)
    assert event.guild_id is None
    assert event.member_role_ids is None
    assert event.emoji == Emoji(name="party_parrot", id="6001")


def test_reaction_remove_guild_unicode() -> None:
    event = parse("MESSAGE_REACTION_REMOVE", {
        "user_id": "1001",
        "channel_id": "3001",
        "message_id": "5001",
        "guild_id": "2001",
        "emoji": {"name": "👍"},
        "member": {"roles": ["4001"]},
    })
    assert event == ReactionRemove(
        user_id="1001",
        channel_id="3001",
        message_id="5001",
        guild_id="2001",
        emoji=Emoji(name="👍", id=None),
        member_role_ids=frozenset({"4001"}),
    )


def test_reaction_remove_dm_custom_emoji() -> None:
    event = parse("MESSAGE_REACTION_REMOVE", {
        "user_id": "1001",
        "channel_id": "3001",
        "message_id": "5001",
        "emoji": {"name": "party_parrot", "id": "6001"},
    })
    assert isinstance(event, ReactionRemove)
    assert event.guild_id is None
    assert event.emoji == Emoji(name="party_parrot", id="6001")


def test_reaction_remove_all_with_and_without_guild() -> None:
    guild = parse("MESSAGE_REACTION_REMOVE_ALL", {"channel_id": "3001", "message_id": "5001", "guild_id": "2001"})
    dm = parse("MESSAGE_REACTION_REMOVE_ALL", {"channel_id": "3001", "message_id": "5001"})
    assert guild == ReactionRemoveAll(channel_id="3001", message_id="5001", guild_id="2001")
    assert dm == ReactionRemoveAll(channel_id="3001", message_id="5001", guild_id=None)


def test_reaction_remove_emoji_with_and_without_guild() -> None:
    emoji = {"name": "👍"}
    guild = parse("MESSAGE_REACTION_REMOVE_EMOJI", {
        "channel_id": "3001", "message_id": "5001", "guild_id": "2001", "emoji": emoji,
    })
    dm = parse("MESSAGE_REACTION_REMOVE_EMOJI", {"channel_id": "3001", "message_id": "5001", "emoji": emoji})
    assert guild == ReactionRemoveEmoji(
        channel_id="3001", message_id="5001", guild_id="2001", emoji=Emoji(name="👍", id=None),
    )
    assert dm == ReactionRemoveEmoji(channel_id="3001", message_id="5001", guild_id=None, emoji=Emoji("👍", None))


# Guild role events


@pytest.mark.parametrize(
    "event_type",
    ["GUILD_ROLE_CREATE", "GUILD_ROLE_UPDATE", "GUILD_ROLE_DELETE", "GUILD_ROLE_UPDATE_BULK"],
)
def test_guild_role_events_give_guild_roles_changed(event_type: str) -> None:
    event = parse(event_type, {"guild_id": "2001", "role": {"id": "4001"}, "roles": []})
    assert event == GuildRolesChanged(guild_id="2001")


# Unknown types


@pytest.mark.parametrize("event_type", ["TYPING_START", "MESSAGE_REACTION_ADD_MANY"])
def test_unknown_types_give_none_without_warning(event_type: str, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert parse(event_type, {"channel_id": "3001"}) is None
    assert warnings_for(caplog) == []


# Malformed payloads: None, one warning naming the field and never the value


def test_bad_channel_id_names_field_not_value(caplog) -> None:
    payload = {"id": "5001", "channel_id": "12x", "author": {"id": "1001"}, "content": ""}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert parse("MESSAGE_CREATE", payload) is None
    records = warnings_for(caplog)
    assert len(records) == 1
    assert "channel_id" in records[0].getMessage()
    assert "12x" not in records[0].getMessage()


def test_bad_author_id_names_author_id(caplog) -> None:
    payload = {"id": "5001", "channel_id": "3001", "author": {"id": "1\n"}, "content": ""}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert parse("MESSAGE_CREATE", payload) is None
    (record,) = warnings_for(caplog)
    assert "author.id" in record.getMessage()


def test_bad_member_roles_names_member_roles(caplog) -> None:
    payload = {
        "id": "5001", "channel_id": "3001", "author": {"id": "1001"}, "content": "",
        "guild_id": "2001", "member": {"roles": ["x"]},
    }
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert parse("MESSAGE_CREATE", payload) is None
    (record,) = warnings_for(caplog)
    assert "member.roles" in record.getMessage()


def test_non_dict_payload_gives_none(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert parse("MESSAGE_CREATE", "not-a-dict") is None
    assert len(warnings_for(caplog)) == 1


def test_non_string_content_names_content(caplog) -> None:
    payload = {"id": "5001", "channel_id": "3001", "author": {"id": "1001"}, "content": 7}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert parse("MESSAGE_CREATE", payload) is None
    (record,) = warnings_for(caplog)
    assert "content" in record.getMessage()


def test_bad_reaction_user_id_names_field(caplog) -> None:
    payload = {"user_id": "bad id", "channel_id": "3001", "message_id": "5001", "emoji": {"name": "👍"}}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert parse("MESSAGE_REACTION_ADD", payload) is None
    (record,) = warnings_for(caplog)
    assert "user_id" in record.getMessage()
    assert "bad id" not in record.getMessage()


def test_missing_emoji_names_emoji(caplog) -> None:
    payload = {"user_id": "1001", "channel_id": "3001", "message_id": "5001"}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert parse("MESSAGE_REACTION_REMOVE", payload) is None
    (record,) = warnings_for(caplog)
    assert "emoji" in record.getMessage()


def test_bad_ready_user_names_user_id(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert parse("READY", {"user": {"id": "x"}, "session_id": "s"}) is None
    (record,) = warnings_for(caplog)
    assert "user.id" in record.getMessage()


def test_empty_session_id_names_session_id(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert parse("READY", {"user": {"id": "1001"}, "session_id": ""}) is None
    (record,) = warnings_for(caplog)
    assert "session_id" in record.getMessage()


# Malformed optional and nested fields that have no other coverage


@pytest.mark.parametrize(
    "event_type, payload, field_name, bad_value",
    [
        # emoji.id: present but not a snowflake
        (
            "MESSAGE_REACTION_ADD",
            {"user_id": "1001", "channel_id": "3001", "message_id": "5001", "emoji": {"name": "x", "id": "bad"}},
            "emoji.id",
            "bad",
        ),
        # emoji.name: present but not a string
        (
            "MESSAGE_REACTION_REMOVE",
            {"user_id": "1001", "channel_id": "3001", "message_id": "5001", "emoji": {"name": 5}},
            "emoji.name",
            5,
        ),
        # guild_id: optional, but present and invalid
        (
            "MESSAGE_CREATE",
            {"id": "5001", "channel_id": "3001", "guild_id": "zz", "author": {"id": "1001"}, "content": ""},
            "guild_id",
            "zz",
        ),
        # guild_id on a GUILD_ROLE_* event, which requires it
        ("GUILD_ROLE_DELETE", {"guild_id": "zz", "role_id": "4001"}, "guild_id", "zz"),
    ],
    ids=["emoji-id", "emoji-name", "optional-guild-id", "role-guild-id"],
)
def test_malformed_field_gives_none_and_one_warning_naming_field(
    event_type, payload, field_name, bad_value, caplog,
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert parse(event_type, payload) is None
    (record,) = warnings_for(caplog)
    message = record.getMessage()
    assert f"field {field_name}" in message
    assert str(bad_value) not in message


# Defensive catch-all in parse(): no current parser raises TypeError or AttributeError, so
# the test swaps one in. It must still give None and log only the placeholder field name.


@pytest.mark.parametrize("error_type", [TypeError, AttributeError])
def test_unexpected_parser_error_logs_placeholder_and_never_the_value(
    error_type, monkeypatch, caplog,
) -> None:
    def broken(raw):
        raise error_type("SENTINEL-VALUE")

    monkeypatch.setitem(events_module._PARSERS, "READY", broken)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert parse("READY", {"user": {"id": "1001"}, "session_id": "s"}) is None
    (record,) = warnings_for(caplog)
    message = record.getMessage()
    assert message == 'malformed READY payload: field "?"'
    assert "SENTINEL-VALUE" not in message


# Observed live shapes (2026-10-31). Ids are fake placeholders; the extra keys are as observed and must be ignored.

# Full guild `member` object as observed on guild MESSAGE_REACTION_ADD and MESSAGE_REACTION_REMOVE.
OBSERVED_GUILD_MEMBER = {
    "accent_color": None, "avatar": None, "banner": None, "communication_disabled_until": None,
    "deaf": False, "joined_at": "2026-01-01T00:00:00.000000+00:00", "mute": False, "nick": None,
    "roles": ["4001"],
    "user": {"avatar": None, "avatar_color": None, "discriminator": "0001", "flags": 0,
             "global_name": "Alice", "id": "1001", "username": "alice"},
}

# Fake 32-character session_id, the length observed on DM reaction events.
OBSERVED_SESSION_ID = "0123456789abcdef0123456789abcdef"


def test_observed_guild_reaction_add_without_emoji_id_key() -> None:
    event = parse("MESSAGE_REACTION_ADD", {
        "channel_id": "3001",
        "emoji": {"name": "👌"},
        "guild_id": "2001",
        "member": OBSERVED_GUILD_MEMBER,
        "message_id": "5001",
        "user_id": "1001",
    })
    assert event == ReactionAdd(
        user_id="1001",
        channel_id="3001",
        message_id="5001",
        guild_id="2001",
        emoji=Emoji(name="👌", id=None),
        member_role_ids=frozenset({"4001"}),
    )


def test_observed_guild_reaction_remove_matches_add_shape() -> None:
    event = parse("MESSAGE_REACTION_REMOVE", {
        "channel_id": "3001",
        "emoji": {"name": "👌"},
        "guild_id": "2001",
        "member": OBSERVED_GUILD_MEMBER,
        "message_id": "5001",
        "user_id": "1001",
    })
    assert event == ReactionRemove(
        user_id="1001",
        channel_id="3001",
        message_id="5001",
        guild_id="2001",
        emoji=Emoji(name="👌", id=None),
        member_role_ids=frozenset({"4001"}),
    )


def test_observed_dm_reaction_add_with_session_id_has_no_guild_or_member() -> None:
    event = parse("MESSAGE_REACTION_ADD", {
        "channel_id": "3002",
        "emoji": {"name": "🎉"},
        "message_id": "5002",
        "session_id": OBSERVED_SESSION_ID,
        "user_id": "1001",
    })
    assert event == ReactionAdd(
        user_id="1001",
        channel_id="3002",
        message_id="5002",
        guild_id=None,
        emoji=Emoji(name="🎉", id=None),
        member_role_ids=None,
    )
    assert OBSERVED_SESSION_ID not in repr(event)


def test_observed_dm_reaction_remove_with_session_id() -> None:
    event = parse("MESSAGE_REACTION_REMOVE", {
        "channel_id": "3002",
        "emoji": {"name": "🎉"},
        "message_id": "5002",
        "session_id": OBSERVED_SESSION_ID,
        "user_id": "1001",
    })
    assert event == ReactionRemove(
        user_id="1001",
        channel_id="3002",
        message_id="5002",
        guild_id=None,
        emoji=Emoji(name="🎉", id=None),
        member_role_ids=None,
    )


def test_observed_dm_message_create_has_no_guild_member_or_bot_key() -> None:
    event = parse("MESSAGE_CREATE", {
        "attachments": [],
        "author": {"avatar": None, "avatar_color": None, "discriminator": "0001", "flags": 0,
                   "global_name": "Alice", "id": "1001", "username": "alice"},
        "channel_id": "3002",
        "channel_type": 1,
        "content": "reply 1",
        "edited_timestamp": None,
        "embeds": [],
        "flags": 0,
        "id": "5003",
        "mention_everyone": False,
        "mention_roles": [],
        "mentions": [],
        "nonce": "6003",
        "pinned": False,
        "stickers": [],
        "timestamp": "2026-01-01T00:00:00.000000+00:00",
        "tts": False,
        "type": 0,
    })
    assert event == MessageCreate(
        message_id="5003",
        channel_id="3002",
        guild_id=None,
        author_id="1001",
        author_is_bot=False,
        member_role_ids=None,
        content="reply 1",
    )


# MESSAGE_DELETE: only id, channel_id and guild_id are read


def test_message_delete_with_guild_gives_ids() -> None:
    assert parse("MESSAGE_DELETE", {"id": "3001", "channel_id": "2001", "guild_id": "1001"}) == \
        MessageDelete("3001", "2001", "1001")


def test_message_delete_without_guild_has_no_guild() -> None:
    event = parse("MESSAGE_DELETE", {"id": "3001", "channel_id": "2001"})
    assert event == MessageDelete("3001", "2001", None)
    assert event.guild_id is None


def test_message_delete_ignores_content_and_author() -> None:
    event = parse("MESSAGE_DELETE", {
        "id": "3001", "channel_id": "2001", "guild_id": "1001",
        "content": "SENTINEL-CONTENT", "author_id": "1002",
    })
    assert event == MessageDelete("3001", "2001", "1001")
    assert "SENTINEL" not in repr(event)


def test_message_delete_bad_id_names_field_not_value(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert parse("MESSAGE_DELETE", {"id": "x", "channel_id": "2001"}) is None
    [record] = warnings_for(caplog)
    assert record.getMessage() == "malformed MESSAGE_DELETE payload: field id"


# Bot flag on reaction events: user_is_bot is True only for member.user.bot is True in a guild


BOT_MEMBER = {"roles": ["4001"], "user": {"id": "1001", "bot": True}}
HUMAN_MEMBER = {"roles": ["4001"], "user": {"id": "1001"}}


@pytest.mark.parametrize("event_type", ["MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE"])
def test_reaction_from_guild_bot_member_sets_user_is_bot(event_type: str) -> None:
    event = parse(event_type, {
        "user_id": "1001", "channel_id": "3001", "message_id": "5001", "guild_id": "2001",
        "emoji": {"name": "👍"}, "member": BOT_MEMBER,
    })
    assert event.user_is_bot is True


@pytest.mark.parametrize("event_type", ["MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE"])
def test_reaction_from_guild_human_member_is_not_bot(event_type: str) -> None:
    event = parse(event_type, {
        "user_id": "1001", "channel_id": "3001", "message_id": "5001", "guild_id": "2001",
        "emoji": {"name": "👍"}, "member": HUMAN_MEMBER,
    })
    assert event.user_is_bot is False


@pytest.mark.parametrize("event_type", ["MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE"])
def test_reaction_with_string_bot_flag_is_not_bot(event_type: str) -> None:
    member = {"roles": ["4001"], "user": {"id": "1001", "bot": "true"}}
    event = parse(event_type, {
        "user_id": "1001", "channel_id": "3001", "message_id": "5001", "guild_id": "2001",
        "emoji": {"name": "👍"}, "member": member,
    })
    assert event.user_is_bot is False


@pytest.mark.parametrize("event_type", ["MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE"])
def test_dm_reaction_is_not_bot(event_type: str) -> None:
    event = parse(event_type, {
        "user_id": "1001", "channel_id": "3002", "message_id": "5002", "emoji": {"name": "👍"},
    })
    assert event.user_is_bot is False


@pytest.mark.parametrize("event_type", ["MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE"])
def test_reaction_with_member_without_user_is_not_bot(event_type: str) -> None:
    event = parse(event_type, {
        "user_id": "1001", "channel_id": "3001", "message_id": "5001", "guild_id": "2001",
        "emoji": {"name": "👍"}, "member": {"roles": ["4001"]},
    })
    assert event.user_is_bot is False
