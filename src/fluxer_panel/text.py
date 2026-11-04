# SPDX-FileCopyrightText: 2026 K. Chen
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Every user-facing string for the card feature. Constants only, with no logic."""

STATUS_UNAVAILABLE = "Unavailable"
STATUS_ASLEEP = "Asleep"
STATUS_WAKING = "Waking up"
STATUS_GOING_TO_SLEEP = "Going to sleep"
STATUS_UNKNOWN = "Status unknown"
STATUS_READY = "Ready"
STATUS_NOT_RESPONDING = "Not responding"
READY_COUNTS = "Ready · {online} / {max}"

NEXT_RESTART = "Restarts"
NEXT_SLEEP = "Goes to sleep"
NEXT_WAKE = "Wakes up"
IN_MINUTES = "in {n} min"
IN_HOURS = "in {n} h"

FIELD_STATUS = "Status"
FIELD_NEXT = "Next"
FIELD_JOIN = "How to join"
FIELD_FIRST_TIME = "First time?"
FIELD_ADDRESS = "Address"
FIELD_VOICE = "Voice"
FIELD_CHANNELS = "Channels"
FIELD_LOCATION = "Location"
FIELD_MAP = "Map"
FIELD_VERSION = "Version"
FIELD_GAME_MODE = "Game mode"
VOICE_UNNAMED = "Voice channel"
TEXT_UNNAMED = "Text channel"

OUTSIDE_CHANNEL_NAMED = "Fluxer: #{name}"
OUTSIDE_VOICE = "Fluxer voice channel"
OUTSIDE_TEXT = "Fluxer text channel"
OUTSIDE_ADDRESS_HIDDEN = "Address in Fluxer"
OUTSIDE_LOCATION = "In person — location in Fluxer"

# Admin card commands: validation replies. The field and rule are fixed names, never the value typed.
FIELD_ERROR = "Can't set {field}: it {rule}."
FIELD_FALLBACK = "this field"
RULES = {
    "length": "must be 1–{limit} characters",
    "control": "must not contain control characters",
    "slug": "must be 1–32 of a-z, 0-9 and -",
    "taken": "is already used by another card",
    "snowflake": "must be a numeric id",
    "host": "must be a host name or IP address",
    "port": "must be a port from 1 to 65535",
    "address": "must be host:port",
    "range": "is out of range",
    "yes_no": "must be yes or no",
    "show_item": "must be none or a list of map, version, game_mode",
    "channel_target": "must be <community id> <channel id> or a channel link",
    "manager_format": "must be <kind> <ref>",
    "probe_format": "must be <kind> <host> <port>",
    "unknown_kind": "is not a known kind",
    "not_configured": "is not configured on this bot",
    "pterodactyl_ref": "must be an 8-character Pterodactyl server id",
    "no_address": "needs an address first",
}

# Admin card commands: replies. None of them contains user input.
CARD_USAGE = ("Usage: !card new <slug> <title> · !card set <slug> <field> <value> · "
              "!card clear <slug> <field> · !card show|publish|archive <slug> · !card list · !card help")
# `!card help`: the subcommands and the field names. Under 2000 characters, the message limit.
HELP_COMMANDS = (
    "Card commands, sent by direct message:\n"
    "!card new <slug> <title>: create a draft card\n"
    "!card set <slug> <field> <value>: set one field\n"
    "!card clear <slug> <field>: clear one field\n"
    "!card show <slug>: preview the inside embed and the outside text\n"
    "!card list: every card, by sort order\n"
    "!card publish <slug>: make the card live\n"
    "!card archive <slug>: remove the posted message and keep the card\n"
    "!card subscribe <slug> <channel>: post a copy of the card in another text channel\n"
    "!card unsubscribe <slug> <channel>: remove that copy and stop posting there\n"
    "!card help fields: each field, its format and its limits\n"
    "Fields: title, description, join, first_time, sort, seats, location, voice, text_channel1, text_channel2, "
    "text_channel3, address, address_public, manager, probe, show"
)
# `!card help fields`: one line per field. The placeholders are filled from the code's limits and kinds.
HELP_FIELDS = (
    "title: text, 1 to {title} characters.\n"
    "description: text, 1 to {description} characters.\n"
    "join: how to join, text, 1 to {join} characters.\n"
    "first_time: text for first-timers, 1 to {first_time} characters.\n"
    "sort: a whole number from {sort_min} to {sort_max}.\n"
    "seats: a whole number from {seats_min} to {seats_max}.\n"
    "location: text, 1 to {location} characters. Outside Fluxer it shows only a fixed line.\n"
    "voice: <community id> <channel id>, or a pasted channel link. Verified when the bot is in that community.\n"
    "text_channel1: a text channel, as voice: <community id> <channel id> or a pasted channel link.\n"
    "text_channel2: a text channel, as voice: <community id> <channel id> or a pasted channel link.\n"
    "text_channel3: a text channel, as voice: <community id> <channel id> or a pasted channel link.\n"
    "address: <host>:<port>, the server address. An IPv6 host goes in brackets: [host]:port.\n"
    "address_public: yes or no (default no). Shows the address outside Fluxer. Needs an address first.\n"
    "manager: <kind> <ref>, kind one of {manager_kinds}. The ref each kind expects: {manager_refs}.\n"
    "probe: <{probe_kinds}> <host> <port>.\n"
    "show: any of {show_items}, comma separated, or none."
)
PUBLISHED_NO_CHANNEL = "Published, but GAMES_CHANNEL_ID isn't set, so nothing will be posted."
NO_SUCH_CARD = "No such card."
UNKNOWN_FIELD = "I don't know that field."
CARD_CREATED = "Created a draft card. Use !card set to fill it in, then !card publish."
CARD_UPDATED = "Card updated."
CARD_CLEARED = "Cleared."
CARD_PUBLISHED = "Published. The card will post on the next check."
CARD_ARCHIVED = "Archived. The posted message is removed and the card's settings are kept."
ALREADY_LIVE = "That card is already live."
ALREADY_ARCHIVED = "That card is already archived."
ARCHIVE_RETRY = ("I couldn't remove a posted message, so the archive stopped and the card is still live. "
                 "Try again in a minute.")
ARCHIVED_COPY_LEFT = "Archived, but I couldn't remove the message in <#{channel_id}>, so it is still there. Delete it by hand."
NO_CARDS = "No cards yet."
SHOW_OUTSIDE_LABEL = "Outside Fluxer:"
SHOW_NOTE = "Preview: live status appears once the card is published."
# Channel replies, for the voice slot and the text channel slots. CHANNEL_WRONG_TYPE takes a CHANNEL_KIND_* phrase.
CHANNEL_UNVERIFIED = "I can't check this channel: the bot isn't in that community."
CHANNEL_VERIFIED = "Channel saved and checked."
CHANNEL_MISSING = "I can't find that channel."
CHANNEL_WRONG_TYPE = "That channel isn't {kind}."
CHANNEL_KIND_VOICE = "a voice channel"
CHANNEL_KIND_TEXT = "a text channel"
CHANNEL_OTHER_COMMUNITY = "That channel is in a different community."
CHANNEL_RETRY = "I couldn't reach Fluxer to check that channel. Try again in a minute."
PROBE_ALERT = "Heads up: card {slug} reports its server running, but the game isn't answering queries."
# Card subscriptions: a copy of a card in another text channel. The channel is shown as a mention, the only input here.
SUBSCRIPTION_USAGE = ("Usage: !card subscribe <slug> <channel>, where <channel> is a channel link or "
                      "<community id> <channel id>.")
UNSUBSCRIBE_USAGE = "Usage: !card unsubscribe <slug> <channel>, with the channel given as for subscribe."
SUBSCRIBED = "Subscribed. The card will post in that channel on the next check."
SUBSCRIPTION_RESUMED = "Resumed. The card will post in that channel on the next check."
SUBSCRIPTION_ALREADY = "That channel is already subscribed to this card."
SUBSCRIPTION_LIMIT = "A card can be subscribed to at most {limit} channels. Unsubscribe one first."
SUBSCRIPTION_IS_GAMES = "That is the games channel, where the card is already posted."
SUBSCRIPTION_NOT_IN_COMMUNITY = "I can't post there: the bot isn't in that community."
SUBSCRIPTION_NOT_FOUND = "That channel isn't subscribed to this card."
UNSUBSCRIBED = "Unsubscribed. The copy in that channel is removed."
UNSUBSCRIBED_COPY_LEFT = ("Unsubscribed, but I couldn't remove the copy in <#{channel_id}>, so it is still there. "
                          "Delete it by hand.")
SUBSCRIPTIONS_HEADING = "Subscribed channels:"
SUBSCRIPTION_PAUSED_SUFFIX = " (paused)"
# The two pause alerts take the card's slug (a-z, 0-9 and -) and the channel, shown as a mention. No other card text.
SUBSCRIPTION_PAUSED_ALERT = ("Heads up: Fluxer refused the bot's post of card {slug} in <#{channel_id}>, so that copy is "
                             "paused. Run !card subscribe for that channel to resume it. !card show lists each card's "
                             "channels.")
SUBSCRIPTION_CHANNEL_GONE_ALERT = ("Heads up: the channel <#{channel_id}> for card {slug}'s copy can't be found, so that "
                                   "copy is paused. Run !card unsubscribe for that channel to remove it.")
# The summary board: one message in the games channel. None of these strings contains user input.
BOARD_TITLE = "Games"
BOARD_EMPTY = "No games are live."
BOARD_NO_STATUS = "—"    # a card with no manager and no probe has no live status
BOARD_TRUNCATED = "Some games are not shown. Ask an admin for the full list."
BOARD_PIN_ALERT = "I posted the summary board in the games channel. Pin it so it stays at the top."
