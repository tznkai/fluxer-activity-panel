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
FIELD_LOCATION = "Location"
FIELD_MAP = "Map"
FIELD_VERSION = "Version"
FIELD_GAME_MODE = "Game mode"
VOICE_UNNAMED = "Voice channel"

OUTSIDE_VOICE_NAMED = "Fluxer: #{name}"
OUTSIDE_VOICE = "Fluxer voice channel"
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
    "voice": "must be <community id> <channel id> or a channel link",
    "manager_format": "must be <kind> <ref>",
    "probe_format": "must be <kind> <host> <port>",
    "unknown_kind": "is not a known kind",
    "not_configured": "is not configured on this bot",
    "pterodactyl_ref": "must be an 8-character Pterodactyl server id",
    "no_address": "needs an address first",
}

# Admin card commands: replies. None of them contains user input.
CARD_USAGE = ("Usage: !card new <slug> <title> · !card set <slug> <field> <value> · "
              "!card clear <slug> <field> · !card show|publish|archive <slug> · !card list")
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
ARCHIVE_RETRY = "I couldn't remove the posted message, so nothing changed. Try again in a minute."
NO_CARDS = "No cards yet."
SHOW_OUTSIDE_LABEL = "Outside Fluxer:"
SHOW_NOTE = "Preview: live status appears once the card is published."
VOICE_UNVERIFIED = "I can't check this channel: the bot isn't in that community."
VOICE_VERIFIED = "Voice channel saved and checked."
VOICE_MISSING = "I can't find that channel."
VOICE_NOT_VOICE = "That channel isn't a voice channel."
VOICE_OTHER_COMMUNITY = "That channel is in a different community."
VOICE_RETRY = "I couldn't reach Fluxer to check that channel. Try again in a minute."
PROBE_ALERT = "Heads up: card {slug} reports its server running, but the game isn't answering queries."
