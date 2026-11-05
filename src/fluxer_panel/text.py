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
REQUESTED_SUFFIX = " · Requested · {n}"

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
    "!card interactions on|off [slug]: turn the reaction taps on or off, for every card or for one\n"
    "!card help fields: each field, its format and its limits\n"
    "Fields: title, description, join, first_time, sort, seats, location, voice, text_channel1, text_channel2, "
    "text_channel3, address, address_public, manager, probe, show, follow_role, play_now_channel"
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
    "show: any of {show_items}, comma separated, or none.\n"
    "follow_role: a role id, as a snowflake. The role must have permissions 0, be mentionable and sit below the bot's "
    "top role. Clear it with !card clear <slug> follow_role.\n"
    "play_now_channel: a text channel, as voice: <community id> <channel id> or a pasted channel link. Not the games "
    "channel. Play-now messages go there; without it they go to PLAY_NOW_CHANNEL_ID."
)
PUBLISHED_NO_CHANNEL = "Published, but GAMES_CHANNEL_ID isn't set, so nothing will be posted."
# Used when the card has active copies: the card's own message is not posted, but its copies are.
PUBLISHED_COPIES_ONLY = ("Published, but GAMES_CHANNEL_ID isn't set, so the card itself won't be posted. "
                         "Only its copies will be, in the channels it is subscribed to.")
NO_SUCH_CARD = "No such card."
INTERACTIONS_USAGE = "Usage: !card interactions on|off [slug]"
INTERACTIONS_OFF = "Reactions are off. Taps do nothing until you turn them back on."
INTERACTIONS_ON = "Reactions are on."
INTERACTIONS_CARD_OFF = "Reactions are off for {slug}."
INTERACTIONS_CARD_ON = "Reactions are on for {slug}."
UNKNOWN_FIELD = "I don't know that field."
CARD_CREATED = "Created a draft card. Use !card set to fill it in, then !card publish."
CARD_UPDATED = "Card updated."
CARD_CLEARED = "Cleared."
CARD_PUBLISHED = "Published. The card will post on the next check."
CARD_ARCHIVED = "Archived. The posted message is removed and the card's settings are kept."
# Used only when a posted message was refused (403) and is still in Fluxer. The header never says the message was removed;
# each channel left then gets one ARCHIVE_LEFT_LINE, which names it by mention.
CARD_ARCHIVED_SOME_LEFT = "Archived. The card's settings are kept, but these posted messages are still in Fluxer:"
ARCHIVE_LEFT_LINE = "I couldn't remove the message in <#{channel_id}>, so it is still there. Delete it by hand."
ALREADY_LIVE = "That card is already live."
ALREADY_ARCHIVED = "That card is already archived."
ARCHIVE_RETRY = ("I couldn't remove a posted message, so the archive stopped and the card is still live. "
                 "Try again in a minute.")
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
# play_now_channel refuses the games channel: play-now messages never go there.
PLAY_NOW_IS_GAMES = "Play-now messages can't go in the games channel."
# The play-now message: the content names the card (escaped), and the sent mention is the follow role only. The ended
# content has no mention. PLAY_NOW_PLAYING is the count line, as in "3 playing".
PLAY_NOW_CONTENT = "Someone wants to play {card} now"
PLAY_NOW_ENDED_CONTENT = "The call to play {card} has ended."
PLAY_NOW_PLAYING = "{n} playing"
# The presser's DMs for a play-now press that is refused or cannot post. The card title goes through render.escape.
# The cooldown's link is a jump link to the earlier play-now message, labelled with our own wording (JUMP_LABEL_MESSAGE).
JUMP_LABEL_MESSAGE = "open it"
PLAY_NOW_ELSEWHERE = ("Play now only works in the main community. Tap \U0001F64B on the card in the games channel.")
PLAY_NOW_UNAVAILABLE = "Play now isn't available for {card} right now."
PLAY_NOW_NO_CHANNEL = "Play now isn't set up for {card}."
PLAY_NOW_COOLDOWN = "Someone already called for {card}: {link}"
PLAY_NOW_COOLDOWN_NO_LINK = "Someone already called for {card} a moment ago."
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
# Reactions. The presser's DM after a press fails, and the admins' alert when a channel refuses removals. The alert
# takes a channel id only.
PRESS_FAILED = "Something went wrong with that. Try again in a moment."
REACTION_DEGRADED_ALERT = "<#{channel_id}> doesn't let the bot remove reactions, so taps there count when added."
# Follow roles, on the card commands and at publish. The bell is written as an escape, as presses.FOLLOW_EMOJI is.
FOLLOW_ON = " Following is on: \U0001F514 on the card toggles its role."
FOLLOW_OFF = " Following is off for this card (see !card set <slug> follow_role <role id>)."
FOLLOW_NO_GAMES = "Following needs GAMES_CHANNEL_ID."
FOLLOW_ROLE_SET = "Follow role set for {slug}."
FOLLOW_ROLE_CLEARED = "Forgot the follow role {role} for {slug}. The role itself is kept."
FOLLOW_ROLE_NONE = "{slug} has no follow role."
# One sentence per refusal reason of the set path. None of them names the role.
FOLLOW_REFUSED = {
    "missing": "That role isn't in the games channel's community.",
    "everyone": "That's the @everyone role.",
    "role_permissions": "That role grants permissions. A follow role must have none.",
    "not_mentionable": "That role isn't mentionable.",
    "hierarchy": "That role is at or above the bot's top role.",
    "taken": "That role is another card's follow role.",
    "no_bot_id": "Try again in a moment.",
    "lookup_failed": "I couldn't reach Fluxer to check that role. Try again in a minute.",
}
# Follow presses: the presser's DMs, the link label, and the admins' alert. The card's title is passed through
# render.escape; the alert takes the card's slug and one plain phrase per refusal reason.
JUMP_LABEL_CARD = "open the card"
FOLLOW_ADDED = "You're following {card}."
FOLLOW_REMOVED = "You stopped following {card}."
FOLLOW_UNAVAILABLE = "Following isn't available for {card} right now."
FOLLOW_ELSEWHERE = "Following {card} works in the main community: {link}"
FOLLOW_ELSEWHERE_NO_LINK = "Following {card} works in the main community's games channel."
FOLLOW_ADMIN_ALERT = "Following is failing for {slug}: {reason}"
FOLLOW_REASON = {
    "not_allowlisted": "the card's follow role is not set up",
    "role_missing": "the follow role is gone from the community",
    "role_permissions": "the follow role now grants permissions",
    "hierarchy": "the follow role is no longer below the bot's top role",
    "forbidden": "Fluxer refused the role change",
}
# Wake requests: the requester's DM, with a note after it when no request opens; the admins' DM; and the close DMs.
# The note's key is the status kind, or "none" (no status yet), or "no_manager" (the card has no Pterodactyl manager).
WAKE_SENT = "Sending your request to the admins for {card}."
WAKE_NOTES = {
    "ready": "It's already running.",
    "waking": "It's already starting.",
    "going_to_sleep": "It's shutting down right now.",
    "not_responding": "It's running but not answering.",
    "unavailable": "It's unavailable right now.",
    "unknown": "Its status is unknown.",
    "none": "It has no status yet.",
    "no_manager": "The bot can't see its server, so the admins will check.",
}
WAKE_ADMIN = "Someone wants {card} ({slug}). Status: {status}. Asked by: {requesters}"
WAKE_ADMIN_OPEN = " · open request"
WAKE_READY = "{card} is ready."
WAKE_GAVE_UP = "Nobody could start {card} right now."
# `!request list|clear <slug>`: admin replies.
REQUEST_USAGE = "Usage: !request list | !request clear <slug>"
REQUEST_LIST_EMPTY = "No open requests."
REQUEST_LIST_LINE = "{slug}: {n} requested, open for {age}"
REQUEST_NONE_OPEN = "There's no open request for {slug}."
REQUEST_CLEARED = "Cleared the request for {slug} ({people})."
WAKE_ADMIN_CLOSED = {
    "ready": "{card} ({slug}) is ready. The request from {people} is closed.",
    "expired": "Nobody started {card} ({slug}). The request from {people} has expired.",
    "cleared": "The request for {card} ({slug}) from {people} was cleared.",
}
