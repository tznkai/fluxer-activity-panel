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
FIELD_TEXT_CHANNEL = "Channel"
# A slot's field name with its suffix: "Voice (Raid night)". The suffix is escaped before it is placed.
SLOT_LABEL = "{kind} ({suffix})"
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
# A slot's suffix outside Fluxer, after the kind's usual line: "Fluxer voice channel (Raid night)".
OUTSIDE_SUFFIX = " ({suffix})"
OUTSIDE_ADDRESS_PUBLIC_SUFFIX = "Address ({suffix}): {address}"
OUTSIDE_ADDRESS_HIDDEN_SUFFIX = "Address ({suffix}) in Fluxer"
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
    "no_slot": "needs its slot set first",
    "suffix_chars": "may contain only letters, digits, spaces and - + . '",
}
# A field that was renamed: the old name is refused with this reply, which names both. Never an alias.
FIELD_RENAMED = "{old} is now {new}. Use {new} instead."

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
    "!card subscribe <slug> <channel> [mode=redirect|local|both] [channel=<channel>] [role=<role id>]: post a copy "
    "of the card in another text channel, or run its sessions and polls there\n"
    "!card unsubscribe <slug> <channel>: remove that copy and stop posting there\n"
    "!card interactions on|off [slug]: turn the reaction taps on or off, for every card or for one\n"
    "!card help fields: each field, its format and its limits\n"
    "Fields: title, description, join, first_time, sort, seats, location, voice, voice2, voice3, text_channel, "
    "text_channel2, text_channel3, address, address2, address3, address_public, address2_public, address3_public, "
    "voice_suffix, voice2_suffix, voice3_suffix, text_channel_suffix, text_channel2_suffix, text_channel3_suffix, "
    "address_suffix, address2_suffix, address3_suffix, manager, probe, show, follow_role, play_now_channel"
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
    "voice, voice2, voice3: a voice channel, as <community id> <channel id> or a pasted channel link. Checked and "
    "named when the bot is in that community.\n"
    "text_channel, text_channel2, text_channel3: a text channel, given as for voice. text_channel was text_channel1.\n"
    "address, address2, address3: <host>:<port>. An IPv6 host goes in brackets: [host]:port.\n"
    "address_public, address2_public, address3_public: yes or no (default no). Shows that address outside Fluxer. "
    "Needs the address first.\n"
    "voice_suffix, voice2_suffix, voice3_suffix, text_channel_suffix, text_channel2_suffix, text_channel3_suffix, "
    "address_suffix, address2_suffix, address3_suffix: a label for that slot, as in Voice (Raid night). 1 to "
    "{suffix_max} letters, digits, spaces and - + . ' only. Needs the slot set first. Clearing a slot clears its "
    "label too.\n"
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
SUBSCRIPTION_USAGE = ("Usage: !card subscribe <slug> <channel> [mode=redirect|local|both] "
                      "[channel=<channel or link>] [role=<role id>], where <channel> is a channel link or "
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
SUBSCRIPTION_MODE_IN_GAMES = ("Copies in the games community always point at the card, so they can't be local or "
                              "both.")
SUBSCRIPTION_GAMES_LOOKUP_FAILED = "I couldn't check the games community. Try again in a minute."
SUBSCRIPTION_NEEDS_SCOPE = "channel= and role= only work with mode=local or mode=both."
SUBSCRIPTION_CHANNEL_IS_GAMES = "That's the games channel. Sessions, polls and play-now posts never go there."
SUBSCRIPTION_MODE_SET = "Copy in <#{channel_id}> is now {mode}."
SUBSCRIPTION_LOCAL_CHANNEL_SET = "It posts in <#{channel_id}>."
SUBSCRIPTION_RETRY = "Something changed at the same time. Run the command again."
SUBSCRIPTION_ROLE_OK = "Follow role here: <@&{role_id}>."
SUBSCRIPTION_ROLE_ASK = ("A role with the card's follow name is already in use in that community. Give one with "
                         "role=<role id>.")
SUBSCRIPTION_ROLE_FAILED = "The mode is set, but the follow role isn't: {reason}"
SUBSCRIPTION_ROLE_UNAVAILABLE = "The mode is set, but following isn't available right now, so there's no follow role."
SUBSCRIPTION_ROLE_GONE = "That channel was unsubscribed, so it has no follow role."
SUBSCRIPTION_MIRRORS_LEFT = "I couldn't delete my posts in {channels}."
# The role step's refusal reasons, as one sentence each. Nothing here names the role.
SUBSCRIPTION_ROLE_REASON = {
    "hierarchy": "That role is at or above the bot's top role.",
    "role_permissions": "That role grants permissions. A follow role must have none.",
    "missing": "That role isn't in this community.",
    "everyone": "That's the @everyone role.",
    "not_mentionable": "That role isn't mentionable.",
    "create_failed": "I couldn't create or change the role there (I need Manage Roles, ranked above it).",
    "mentionable_failed": "I couldn't create or change the role there (I need Manage Roles, ranked above it).",
    "lookup_failed": "I couldn't reach Fluxer to check that role. Try again in a minute.",
    "no_bot_id": "Try again in a moment.",
}
SUBSCRIPTIONS_HEADING = "Subscribed channels:"
# `!card show` lines: the mode after each channel, and for a local or both copy the channel it posts in and its role.
SUBSCRIPTION_LINE_MODE = " · {mode}"
SUBSCRIPTION_LINE_SCOPE = " · posts in <#{channel_id}> · {role}"
SUBSCRIPTION_LINE_ROLE = "role <@&{role_id}>"
SUBSCRIPTION_LINE_NO_ROLE = "no follow role"
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
# Session times: the refusal replies for a typed time (`session_time.parse`), and the example that follows each one.
SESSION_TIME_EXAMPLE = "Reply like `sat 14:00`, `tomorrow 19:30`, `today 9:05` or `2026-11-14 14:00`."
SESSION_TIME_REFUSED = {
    "format": "I couldn't read that time. Use a day and a 24-hour time.",
    "date": "That date doesn't exist.",
    "weekday_today": "That's today's weekday. Use `today HH:MM`, or a date like `2026-11-21 14:00`.",
    "gap": "That time is skipped when the clocks change. Pick another time.",
    "fold": "That time happens twice when the clocks change. Pick another time.",
    "past": "That time has already passed.",
    "too_far": "That's more than 60 days away.",
}
# Poll replies: the refusal for each reason in `poll_reply.REASONS`, and the line that names the item.
POLL_REPLY_ITEM = "Item {index}: {reason}"
POLL_REPLY_REFUSED = {
    "empty": "There's nothing there. Separate items with commas, like `a, c`.",
    "keyword_alone": "`all` and `clear` must be the whole reply.",
    "too_many": "That's too many items. Use 20 or fewer.",
    "no_option": "There's no option with that letter.",
    "too_soon": "That time is less than an hour away.",
    "letters_only": "This is a runoff, so reply with letters only.",
    "no_all": "This is a runoff, so `all` doesn't work. Rank the letters instead, favourite first.",
    "repeated": "Each letter can appear only once.",
}
# The session message: the field names, the When and counts lines, the footer and the closed-form statuses.
SESSION_FIELD_WHEN = "When"
SESSION_FIELD_LOCATION = "Location"
SESSION_FIELD_SEATS = "Seats"
SESSION_FIELD_RSVPS = "RSVPs"
SESSION_FIELD_STATUS = "Status"
SESSION_WHEN = "<t:{ts}:F> · <t:{ts}:R>"
SESSION_COUNTS = "{going} going · {maybe} maybe"
SESSION_COUNTS_WAITING = " · {waiting} waiting"
SESSION_FOOTER = "Session {n} · ✅ going · ❔ maybe · ❌ can't"
SESSION_ENDED = "Ended"
SESSION_CANCELLED = "Cancelled"
SESSION_MOVED = "Time changed"
# The promotion DM for a waiting user who gets a seat. `{card}` is the escaped title, `{link}` a jump link.
SESSION_PROMOTED = "A seat opened up: you're now going to {card}. {link}"
SESSION_PROMOTED_NO_LINK = "A seat opened up: you're now going to {card}."
SESSION_LINK_LABEL = "Open the session"
# The calendar DM flow. `{card}` is always render.escape(card.title); the keycaps are passed in as the `key` value.
SESSION_PROMPT = ("Pick a time for {card}: react with its number, or reply with a time like `sat 14:00`. "
                  "This expires in 15 minutes.")
SESSION_PROMPT_SLOT = "{key} <t:{ts}:F>"
SESSION_CONFIRM = "{card} at <t:{ts}:F> (<t:{ts}:R>). React ✅ to post it."
SESSION_CONFLICT = "You're already going to {card} then."
SESSION_POSTED = "Your session is posted: {link}"
SESSION_POSTED_NO_LINK = "Your session is posted."
SESSION_POST_FAILED = "I couldn't post that session. Try again later."
SESSION_DRAFT_EXPIRED = "This session draft expired. Press \U0001F4C5 on the card to start again."
SESSION_ELSEWHERE = "Sessions are planned on the card itself: {link}"
SESSION_ELSEWHERE_NO_LINK = "Sessions are planned on the card itself, in the games channel."
SESSION_SCOPE_GONE = "That copy no longer runs its own sessions, so nothing was posted."
SESSION_NO_CHANNEL = "This card has no channel for sessions yet."
SESSION_CAP = {
    "proposer": "You already have 3 upcoming sessions for {card}.",
    "card": "{card} already has 10 upcoming sessions.",
}
# `!session` in DMs. `{n}` is the session id, `{card}` the escaped card title, `{ts}` a Unix time in seconds.
SESSION_USAGE = ("Commands: `!session list`, `!session cancel <session>`, `!session move <session> <time>`, "
                 "`!session repeat <session> weekly|off`, `!session location <session> <text|clear>`, "
                 "`!session seats <session> <n|clear>`. A session is its number or its message link.")
SESSION_LIST_EMPTY = "You have no upcoming sessions."
SESSION_LIST_MINE = "Session {n} · {card} · <t:{ts}:F> · you: {state}"
SESSION_LIST_ALL = "Session {n} · {card} · <t:{ts}:F> · {counts}"
SESSION_STATE = {"going": "going", "waiting": "waitlist #{pos}", "maybe": "maybe", "no": "can't",
                 "proposer": "proposer"}
SESSION_UNKNOWN = "No such session."
SESSION_NOT_ALLOWED = "Only the proposer or an admin can change that session."
SESSION_NOT_UPCOMING = "That session has already ended or been cancelled."
SESSION_CANCEL_DONE = "Session {n} is cancelled."
SESSION_MOVE_DONE = "Session {n} moved to <t:{ts}:F>."
SESSION_REPEAT_ON = "Session {n} now repeats weekly."
SESSION_REPEAT_OFF = "Session {n} no longer repeats."
SESSION_LOCATION_DONE = "Session {n}'s location is updated."
SESSION_SEATS_DONE = "Session {n}'s seats are updated."
SESSION_CANCELLED_DM = "The {card} session at <t:{ts}:F> was cancelled."
SESSION_MOVED_DM = "The {card} session moved to <t:{ts}:F> (<t:{ts}:R>). {link}"
SESSION_MOVED_DM_NO_LINK = "The {card} session moved to <t:{ts}:F> (<t:{ts}:R>)."
# The timed jobs. The series note goes to the proposer; the heads-up goes to each admin, with the card's escaped title.
SESSION_SERIES_STOPPED = "Your weekly {card} session stopped repeating: the card already has 10 upcoming sessions."
SESSION_HEADSUP = "{card} session at <t:{ts}:F>, {n} going, server is asleep."
# The card's next session: the field, its value, the link's label, the outside line and the footer under every card.
FIELD_NEXT_SESSION = "Next session"
NEXT_SESSION = "<t:{ts}:F> (<t:{ts}:R>) · {going} going"
NEXT_SESSION_OPEN = "Open"
OUTSIDE_NEXT_SESSION = "Next session: <t:{ts}:F> · {going} going"
CARD_DM_HINT = "Pressing a reaction sends you a DM. Allow DMs from this server's members to get the reply."
# Polls: the poll message, its closed form and the ballot. Card titles are escaped with `render.escape` before they are
# placed. The instruction blocks take the time examples and the zone through placeholders, so the examples are not
# repeated here.
POLL_TITLE = "Time poll: {card}"
POLL_OPTION_LINE = "{letter}. <t:{ts}:F> ({votes})"
POLL_VOTES = {"one": "1 vote", "other": "{n} votes"}
POLL_MORE = "+{k} more"
POLL_FIELD_CLOSES = "Closes"
POLL_CLOSES = "<t:{ts}:F> · <t:{ts}:R>"
POLL_FIELD_LEADER = "Leader"
POLL_LEADER = "{letter}. <t:{ts}:F> ({votes})"
POLL_NO_LEADER = "No votes yet."
POLL_FIELD_ANSWERED = "Answered"
POLL_ANSWERED = "{n} of {m} answered"
POLL_REACHED = " · {r} of {m} reached"
POLL_FIELD_NOTE = "Note"
POLL_RELETTERED = "Options were re-lettered <t:{ts}:R>."
POLL_FOOTER = "Poll {n} · reply to the bot's DM to vote"
POLL_FIELD_STATUS = "Status"
POLL_STATE = {"open": "Open", "closed": "Closed"}
POLL_FIELD_RESULT = "Result"
POLL_WINNER = "Winner: {letter}. <t:{ts}:F>"
POLL_NO_WINNER = "No winner."
POLL_ROUND = "Round {n}: {counts}."
POLL_ROUND_COUNT = "{letter}: {votes}"
POLL_ROUND_SEP = ", "
POLL_DROPPED = " {letter} was dropped."
POLL_FIELD_SESSION = "Session"
POLL_SESSION_LINK = "Open the session"
# The ballot.
POLL_BALLOT_HEAD = "Time poll {n} for {card}."       # {n} is the poll id: a voter with several ballots names it
POLL_BALLOT_HEAD_RUNOFF = "Runoff {n} for {card}."
POLL_BALLOT_MORE = "+{k} more options in the poll message: {link}"
POLL_LINK_LABEL = "Open the poll"
POLL_HOW_APPROVAL = (
    "Pick by letter, like `a, c`.\n"
    "Or add your own time in {zone}. {examples}\n"
    "Mix both, like `a, fri 20:00`. `all` picks every time shown here. `clear` removes your picks.\n"
    "Each reply replaces your earlier answer."
)
POLL_HOW_RUNOFF = (
    "Rank the times you can make, favourite first, like `b, a, c`. Rank as many as you like.\n"
    "`clear` removes your ranking.\n"
    "Each reply replaces your earlier answer."
)
POLL_YOUR_PICKS = "Your picks: {letters}"
POLL_NOT_ANSWERED = "You haven't answered yet."
POLL_CLOSES_LINE = "Closes <t:{ts}:F> (<t:{ts}:R>)."
POLL_BALLOT_CLOSED = "This poll is closed: {link}"
POLL_BALLOT_REMOVED = "This poll was removed."
# The halfway reminder DM. `{link}` is a jump link to the poll message; `{n}` is the poll's number, and `{ts}` its close time.
POLL_REMINDER_DM = ("There's still time to answer the time poll for {card}: {link}. Reply to your ballot, or send "
                    "`!poll vote {n} <reply>`. It closes <t:{ts}:R>.")
# Starting a poll (the 🗳️ press). `{link}` is a jump link, labelled with POLL_LINK_LABEL or the card's own label.
POLL_ELSEWHERE = "Polls are started on the card itself: {link}"
POLL_ELSEWHERE_NO_LINK = "Polls are started on the card itself, in the games channel."
POLL_NO_CHANNEL = "This card has no channel for polls yet."
POLL_SCOPE_GONE = "That copy no longer runs its own polls, so nothing was opened."
POLL_ALREADY_OPEN = "This card already has an open poll: {link}"
POLL_ALREADY_OPEN_NO_LINK = "This card already has an open poll."
POLL_TRY_LATER = "I couldn't read the member list. Please try again later."
POLL_POST_FAILED = "I couldn't post that poll. Try again later."
POLL_LEFT_OUT = "{n} members with the follow role were not sent a ballot, because the poll is capped."
POLL_NO_ROLE = "This card has no follow role, so only you were sent a ballot."
POLL_TOO_LATE = "That time is less than an hour away, so no session was scheduled."
POLL_STARTER_AT_CAP = ("The poll's starter already has the most upcoming sessions allowed for {card}, so I can't "
                       "schedule this one.")
POLL_SCHEDULED_DM = "The time poll for {card} is scheduled: {link}"
POLL_SCHEDULED_DM_NO_LINK = "The time poll for {card} is scheduled. Its session is in the card's session channel."
POLL_RUNOFF_NEEDS_TWO = ("Fewer than two usable times have votes, so there is no runoff. Press the calendar (\U0001F4C5) "
                         "to schedule the winner instead.")
POLL_RUNOFF_TOO_SOON = ("There isn't time for a runoff before its earliest time, <t:{ts}:F>. Press the calendar "
                        "(\U0001F4C5) to schedule the winner instead.")
POLL_SCHEDULE_RUNOFF_EXISTS = "A runoff already decides this poll's time, so nothing was scheduled: {link}"
POLL_SCHEDULE_RUNOFF_EXISTS_NO_LINK = "A runoff already decides this poll's time, so nothing was scheduled."
POLL_RUNOFF_SCHEDULED = "This poll's winner is already scheduled, so no runoff was started: {link}"
POLL_RUNOFF_SCHEDULED_NO_LINK = "This poll's winner is already scheduled, so no runoff was started."
POLL_RUNOFF_EXISTS = "This poll already has a runoff: {link}"
POLL_RUNOFF_EXISTS_NO_LINK = "This poll already has a runoff."
POLL_RUNOFF_STARTED = "The runoff is open, and its ballots are with the voters: {link}"
POLL_RUNOFF_STARTED_NO_LINK = "The runoff is open, and its ballots are with the voters."
# Voting by DM reply. POLL_FORMAT follows every item refusal (POLL_REPLY_ITEM), before the time examples.
POLL_NO_PICKS = "You have no picks on this poll now."
POLL_REPLY_CLOSED = "This poll is closed, so your reply changed nothing."
POLL_NOT_RECIPIENT = "You weren't sent a ballot for this poll."
POLL_WHICH = "You have more than one open poll ({polls}). Reply with `!poll vote <poll> <reply>` to pick one."
POLL_FULL = "This poll has no room for another time. Pick from the options shown."
POLL_FORMAT = "Pick letters, like `a, c`, or type times, or both."

# `!poll` in DMs. A poll is named by its number or its message link; a duration is a number and m, h or d.
POLL_USAGE = ("Commands: `!poll list`, `!poll vote <poll> <reply>`, `!poll sort <poll>`, `!poll shown <poll> <n>`, "
              "`!poll extend <poll> [duration]`, `!poll close <poll>`, `!poll help`. A poll is named by its number "
              "or its message link. A duration is a number and m, h or d, like `90m`, `12h` or `2d`.")
POLL_UNKNOWN = "I couldn't find that poll. Give its number or its message link."
POLL_NOT_ALLOWED = "Only the poll's starter or an admin can change it."
POLL_NOT_OPEN = "That poll is closed, so nothing changed."
POLL_SORTED = ("The options are re-lettered by votes, then soonest. The poll message and every ballot are updated, "
               "and any pending ballot size now applies.")
POLL_SHOWN_PENDING = "The ballot will show {n} options at the next sort. Nothing has changed yet."
POLL_SHOWN_RANGE = "The number of options shown must be from 1 to 20."
POLL_EXTENDED = "The poll now closes <t:{ts}:F> (<t:{ts}:R>)."
POLL_EXTEND_CAP = "That would make the poll run longer than a poll may run, so nothing changed."
POLL_EXTEND_RUNOFF_CAP = ("A runoff can't close later than <t:{ts}:F>, an hour and ten minutes before its earliest time, "
                          "so nothing changed.")
POLL_CLOSED_DONE = "The poll is closed."
POLL_LIST_EMPTY = "You have no polls to show."
POLL_LIST_LINE = "{n}. {card} · {state} · {when}{link}"
POLL_LIST_LINK = " · {link}"
POLL_LIST_OPEN_AT = "closes <t:{ts}:F>"
POLL_LIST_CLOSED_AT = "closed <t:{ts}:F>"

# `help` and `!help` in a DM: the command heads, each with where its own help is. Admins also get HELP_ADMIN.
HELP_TOP = (
    "Commands, sent to me by direct message:\n"
    "!session: list, move or cancel game sessions. Send `!session help` for the details.\n"
    "!poll: list, vote in and manage time polls. Send `!poll help` for the details.\n"
    "On a card, press \U0001F4C5 to propose a session or \U0001F5F3️ to start a time poll."
)
HELP_ADMIN = (
    "Admin commands:\n"
    "!card: create, edit and publish cards. Send `!card help` for the details.\n"
    "!request: list or clear wake requests. Send `!request` for the details.\n"
    "!admin ping: check that I am running."
)
