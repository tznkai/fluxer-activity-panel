# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Slot field names and the suffix validator. A suffix is card text from an admin today and from members later, so it
is untrusted input: a refusal names the field and the rule, never the value."""

from __future__ import annotations

import pytest

from fluxer_panel import card_fields, text
from fluxer_panel.cards import CardFieldError

SENTINEL = "SENTINEL"


# The field names ----------------------------------------------------------------------------------------

def test_three_slots_of_each_kind_with_text_channel_renamed() -> None:
    assert list(card_fields.SLOT_FIELDS) == ["voice", "voice2", "voice3", "text_channel", "text_channel2",
                                             "text_channel3", "address", "address2", "address3"]
    assert card_fields.SLOT_FIELDS["text_channel"] == ("text_channel", 1)
    assert card_fields.SLOT_FIELDS["address3"] == ("address", 3)
    assert "text_channel1" not in card_fields.FIELD_NAMES


def test_every_slot_has_a_suffix_field_and_every_address_slot_a_public_field() -> None:
    assert card_fields.SUFFIX_FIELDS == {f"{name}_suffix": slot for name, slot in card_fields.SLOT_FIELDS.items()}
    assert card_fields.PUBLIC_FIELDS == {"address_public": 1, "address2_public": 2, "address3_public": 3}
    assert set(card_fields.SLOT_FIELDS) | set(card_fields.SUFFIX_FIELDS) | set(card_fields.PUBLIC_FIELDS) \
        <= card_fields.FIELD_NAMES


# The suffix validator -------------------------------------------------------------------------------------

@pytest.mark.parametrize(("value", "expected"), [
    ("Java", "Java"),
    ("Raid night", "Raid night"),
    ("  Bedrock  ", "Bedrock"),                   # trimmed
    ("Raid    night", "Raid night"),              # an internal run of spaces collapsed
    ("Geyser-Bedrock", "Geyser-Bedrock"),
    ("1.21+", "1.21+"),
    ("O'Brien's", "O'Brien's"),
    ("Ñandú 2", "Ñandú 2"),                       # Unicode letters
    ("日本語", "日本語"),
    ("٣", "٣"),                                   # a Unicode decimal digit
    ("s" * 20, "s" * 20),                         # the longest
    ("  " + "s" * 20 + "  ", "s" * 20),           # the limit counts after trimming
    ("Nan\u0303du", "Na\u00f1du"),                     # composed to NFC before the checks, so the mark is a letter
])
def test_suffix_accepts_letters_digits_and_the_allowed_marks(value: str, expected: str) -> None:
    assert card_fields.suffix("voice_suffix", value) == expected


@pytest.mark.parametrize("value", ["", "   ", "s" * 21, "Raid night is long ok"])
def test_suffix_refuses_a_length_outside_1_to_20(value: str) -> None:
    with pytest.raises(CardFieldError) as info:
        card_fields.suffix("voice2_suffix", value)
    assert (info.value.field, info.value.rule) == ("voice2_suffix", "length")


@pytest.mark.parametrize("char", [
    "\n", "\r", "\t", "\x00", "\x7f", "\u200b",          # control and format characters
    "(", ")",                                          # the suffix already sits inside a pair
    "`", "*", "_", "~", "|", ">", "#", "[", "]",       # markdown
    "@", "<", ":", "&", "!", "?", "/", "\\", '"', ",", ";", "=", "%", "$", "^", "{", "}",
    "\u202e",                                          # a bidi override
    "\U0001F514",                                      # an emoji is a symbol, not a letter
    "\u20dd",                                          # a combining mark that NFC cannot compose
    "\u00a0",                                          # a no-break space is not the space
    "\u115f", "\u1160", "\u3164", "\uffa0",                # Hangul fillers: letters that render blank
])
def test_suffix_refuses_every_other_character(char: str) -> None:
    with pytest.raises(CardFieldError) as info:
        card_fields.suffix("address3_suffix", f"Ja{char}va")
    assert (info.value.field, info.value.rule) == ("address3_suffix", "suffix_chars")


def test_the_suffix_refusals_name_the_rule_and_never_the_value() -> None:
    for value, rule in ((SENTINEL + "(", "suffix_chars"), (SENTINEL * 3, "length")):
        with pytest.raises(CardFieldError) as info:
            card_fields.suffix("text_channel_suffix", value)
        reply = card_fields.refusal_reply(info.value)
        assert SENTINEL not in reply
        assert reply.startswith("Can't set text_channel_suffix: it ")
        assert info.value.rule == rule
    assert card_fields.refusal_reply(CardFieldError("voice_suffix", "length")) == \
        "Can't set voice_suffix: it must be 1–20 characters."
    assert "letters, digits, spaces and - + . '" in card_fields.refusal_reply(CardFieldError("voice_suffix",
                                                                                             "suffix_chars"))


def test_the_empty_slot_refusal_names_the_field() -> None:
    reply = card_fields.refusal_reply(CardFieldError("voice3_suffix", "no_slot"))
    assert reply == text.FIELD_ERROR.format(field="voice3_suffix", rule=text.RULES["no_slot"])


def test_an_address_refusal_names_its_own_slot() -> None:
    with pytest.raises(CardFieldError) as info:
        card_fields.address("no-port.invalid", "address2")
    assert info.value.field == "address2"
