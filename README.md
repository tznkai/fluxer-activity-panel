<!-- SPDX-FileCopyrightText: 2026 K. Chen -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Fluxer activity panel

This project holds two Python packages under `src/`:

- `fluxer_client`: a panel-agnostic Fluxer client with REST, gateway, role cache
  and snowflake id helpers.
- `fluxer_panel`: the activity panel bot built on `fluxer_client`.

Configuration comes from environment variables. See `.env.example` for the names. The code
never loads `.env`; the operator sources it in the shell.

## Bot setup

The operator does these steps in the Fluxer community the panel serves:

1. Give the bot these permissions. Manage Roles creates follow roles and adds or removes a member's follow role.
   Manage Messages removes a member's reaction after a press. Add Reactions lets the bot seed the buttons.
   Fluxer has no "Mention Roles" permission: a follow role is pinged only while it is mentionable.
2. Place the bot's top role above every follow role it changes. A role at or above the bot is refused. Fluxer
   answers that refusal with the same 403 as a missing permission, so the bot checks the hierarchy before it acts.
3. Make the games channel (`GAMES_CHANNEL_ID`) read-only for members, so the bot's posts are the only messages
   in it.
4. Follow roles: when a card is published, the bot creates a role named `<FOLLOW_ROLE_PREFIX><slug>` with no
   permissions. Fluxer ignores the mentionable flag on create, so the bot then makes the role mentionable in a
   second call and reads it back. It adopts a matching role instead when exactly one exists. It never deletes a
   role. To drop a card's follow role, use `!card clear <slug> follow_role`, then delete the role by hand in Fluxer
   if you want it gone.
   Cards published before following existed have no role. To give one a role and its bell, run `!card publish <slug>`
   on it again: the reply still says the card is already live, then adds the following line. The bot makes the role
   the same way as for a fresh publish, and seeds the bell on the card and its copies.

## Configuration

Every setting is an environment variable. A setting marked "default" can be left unset.
Positive-integer settings must be whole numbers above zero.

Core:

- `FLUXER_TOKEN` (required): the bot token. It is never logged, and `repr` does not show it.
- `FLUXER_INSTANCE_URL` (required unless `FLUXER_API_URL` is set): the instance's base URL, at most
  128 UTF-16 code units (an emoji or other character outside the Basic Multilingual Plane counts two). The
  API and gateway addresses are discovered from it.
- `FLUXER_API_URL` (optional): the API base URL. When it is set, no discovery is needed.
- `FLUXER_GATEWAY_URL` (optional): the gateway URL (`ws` or `wss`). When it is unset, the API
  reports the gateway address.
- `ADMIN_USER_IDS` (required): comma-separated user ids that may run admin and `!card` commands
  in direct messages. A command sent by a bot or in a guild channel is refused: the bot logs `admin_denied`
  with the user and channel ids, and posts nothing in the channel.
- `COMMAND_PREFIX` (default `!`).
- `DATA_DIR` (default `data`): the directory for the database and the purge status file.
- `DB_PATH` (default `DATA_DIR/panel.db`).
- `PURGE_STATUS_PATH` (default `DATA_DIR/purge-status.json`).
- `PURGE_INTERVAL_SECONDS` (default `3600`).
- `LOG_LEVEL` (default `INFO`): one of `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`.

Cards and sources:

- `GAMES_CHANNEL_ID` (optional): the channel where published cards and the summary board are posted.
  Without it, neither a card nor the board is posted, and the log says `games_channel_unset`. A live card's
  copies (see Subscriptions) are still posted in their own channels. The publish reply says which of these
  will happen.
  Changing it does not move anything already posted. A card or the board stays in the channel it was
  posted in, and its edits go there. New cards go to the new channel. To move the board or a card,
  delete its message by hand, and the bot posts it again in the current channel.
- `PTERODACTYL_URL` and `PTERODACTYL_API_KEY`: the Panel's base URL and a client API key. Set both
  or neither. Setting only one refuses to start with exit code 2. Without both, no Panel host
  manager is registered.
- `POLL_INTERVAL_SECONDS` (default `60`): how often the card poller runs when nothing kicks it.
- `SCHEDULE_REFRESH_SECONDS` (default `600`): how often a card re-reads its Panel schedules and the
  names of its verified voice and text channels.
- `POLL_CONCURRENCY` (default `8`): how many cards are polled at once.
- `PROBE_TIMEOUT_SECONDS` (default `3`): the timeout for one probe.
- `PROBE_GRACE_SECONDS` (default `600`): how long a running server's failing probe is tolerated
  before its card shows Ready without counts. The admins then get one direct message.
- `PLAY_NOW_CHANNEL_ID` (optional): the channel where play-now posts are made. It must differ from
  `GAMES_CHANNEL_ID`, and the bot refuses to start when they are equal. A press on a card posts there (or in
  the card's own `play_now_channel`). A press on a copy in the games community posts in the copy's channel too,
  and in the card's play-now channel when that is a different channel (see Play now).
- `FOLLOW_ROLE_PREFIX` (default `card_subscription_`): the prefix for follow-role names. It is 1 to 40
  characters of `a-z`, `0-9`, `_` or `-`.
- `WAKE_REQUEST_EXPIRY_SECONDS` (default `7200`): how long a wake request stays open before it expires.
- `PLAY_NOW_EXPIRY_SECONDS` (default `3600`): how long a play-now post stays live before it expires.
- `PLAY_NOW_COOLDOWN_SECONDS` (default `1800`): the wait between play-now posts for one card.
- `BOARD_REPOST_MIN_INTERVAL_SECONDS` (default `300`): the least time between reposts of the summary board.
- `PRESS_USER_WINDOW_SECONDS` (default `10`): the window in which one user's repeated presses are limited.
- `PRESS_CARD_WINDOW_SECONDS` (default `10`): the window over which presses on one card are counted.
- `PRESS_CARD_BURST` (default `10`): how many presses on one card are allowed within `PRESS_CARD_WINDOW_SECONDS`.

Sessions (see Sessions under Cards):

- `SESSION_TIME_ZONE` (required): an IANA time zone name, such as `America/New_York` or `UTC`. Typed and offered
  session times are in this zone. Startup exits with code 2 without it, and so does `purge`.
- `SESSION_SLOTS_WEEKDAY` (default `16:00,19:00`): 1 to 6 distinct 24-hour `HH:MM` times offered on Monday to Friday.
- `SESSION_SLOTS_WEEKEND` (default `10:00,14:00,19:00`): the same, for Saturday and Sunday.
- `SESSION_LENGTH_SECONDS` (default `7200`): how long a session lasts.
- `SESSION_RETENTION_SECONDS` (default `604800`): how long an ended or cancelled session is kept before the purge
  scrubs its message and deletes it. Only sessions use it.
- `DM_BACKOFF_SECONDS` (default `3600`): after a direct message to a user fails, the bot sends that user no ordinary
  direct message for this long. Promotion, cancel, move and series-stopped messages, and the wake request close
  messages (ready, expired and cleared), bypass the backoff. Admin messages never use it.

Polls (see Polls under Cards). Every one has a default, so none is required. A bad value exits with code 2:

- `POLL_LENGTH_SECONDS` (default `86400`): how long a poll stays open after it starts.
- `POLL_MAX_LENGTH_SECONDS` (default `604800`): the most a poll may run, counted from its start. It caps `!poll extend`.
  It must be at least `POLL_LENGTH_SECONDS`.
- `POLL_BALLOT_SIZE` (default `6`): how many options a ballot shows, from 1 to 20.
- `POLL_MAX_RECIPIENTS` (default `50`): how many people a poll sends a ballot to. The starter counts toward it.
- `POLL_REMINDER` (default `on`): `on` or `off`. When it is on, each recipient who has not answered gets one reminder
  at the halfway point.

`POLL_INTERVAL_SECONDS` is the poller's tick, not a poll setting.

Hand-run tools only (`tools/live/`, see `CLAUDE.md`): `LIVE_GUILD_ID`, `LIVE_CHANNEL_ID`,
`LIVE_USER_ID`, `LIVE_ROLE_ID`, `LIVE_HIGH_ROLE_ID`, `LIVE_PTERO_SERVERS`, `LIVE_PROBES`,
`LIVE_VOICE_CHANNEL_ID` and `LIVE_OTHER_GUILD_ID`. The
bot never reads them. `.env.example` lists them all.

## Commands

- `python -m fluxer_panel run` (the default) connects to Fluxer and runs the bot.
- `python -m fluxer_panel purge` runs one retention purge, writes the status file and exits.
  It needs the same configuration as the bot (`FLUXER_TOKEN`, the instance URL and
  `ADMIN_USER_IDS` are all required), but it never connects to Fluxer. It exits 0 when the
  purge succeeded, 1 when it failed (the log has `purge_failed`), 2 on a configuration error
  and 3 when the database is refused. Due sessions are left in place and counted as `sessions_deferred`,
  because this subcommand has no network to scrub their messages.

## Retention purge

`run` purges once at startup, after it has resolved its endpoints, and then every
`PURGE_INTERVAL_SECONDS` (default 3600). A purge that fails is logged and recorded in the
status file, and the bot keeps running.

A session is due when it was ended or cancelled more than `SESSION_RETENTION_SECONDS` ago (default 7 days).
Upcoming sessions are never purged. For each due session, `run` first edits its message to its closed form
without the location (the scrub). Then it deletes the session and its RSVPs. The delete happens even when the edit
failed; a failed scrub is logged as `session_scrub_failed` with the session id. A weekly session is kept while its
next occurrence is not posted yet, so purging one occurrence cannot end its series.

The `purge` subcommand has no network, so it does not scrub. It leaves due sessions in place and reports them as
`sessions_deferred` in the status file. The running bot scrubs and deletes them at its next purge.

The purge also deletes:

- a request created more than 30 days ago, whatever its status.
- a play-now post created more than 30 days ago, whatever its state.
- a closed poll that was closed more than `SESSION_RETENTION_SECONDS` ago, with its options, ballots and votes. A closed
  poll has no REST call, so the `purge` subcommand deletes these too.

The cutoffs are strict: a row exactly at a cutoff is kept.

### Status file

The status file is `purge-status.json` in the data directory, or the path in
`PURGE_STATUS_PATH`. It is rewritten after every attempt. It is world-readable (mode 0644)
so a monitor running as another user can read it. It holds only timestamps and counts:

```json
{
 "last_success_at": 1767225600,
 "last_attempt_at": 1767225600,
 "ok": true,
 "deleted": {"sessions": 0, "rsvps": 0, "requests": 0, "play_now": 0, "polls": 0},
 "sessions_deferred": 2,
 "schema_version": 2
}
```

- `last_success_at`: when the last successful purge ran. It is `null` until the first success,
  and a failed attempt keeps the previous value.
- `last_attempt_at`: when the latest attempt ran, successful or not.
- `ok`: whether the latest attempt succeeded. `deleted` holds that attempt's counts (zeros on failure):
  `sessions`, `rsvps`, `requests`, `play_now` (the play-now posts deleted) and `polls` (the closed polls deleted).
  A checkpoint failure after the commit is reported as `ok: false` with zero `deleted`, but the rows may already be
  gone. The next run checkpoints again.
- `sessions_deferred`: the number of due sessions that the `purge` subcommand left in place, because it cannot scrub
  them. Deferred sessions are not counted in `deleted`. A `run` purge always reports 0 here. The running bot deletes
  the deferred sessions at its next purge. `schema_version` is 2 for this layout; the `polls` key was added without
  a version change.

Alert when `last_success_at` is `null` or older than twice `PURGE_INTERVAL_SECONDS`. A missing
file means the bot has never run a purge.

## Cards

Admins manage cards by direct message with `!card` commands. A card is one message in the games
channel. It shows how to join, what a first-timer should know, and optional extras: up to three voice channels,
up to three text channels, up to three server addresses, an in-person location and seats. A card for a game
server can also show a status line, such as Asleep, Waking up or Ready with a player count.

### Lifecycle

- **Draft:** `!card new` creates a card as a draft. A draft is never posted.
- **Live:** `!card publish` makes the card live. The poller posts it in the games channel on its
  next tick, and edits the message when its content changes. Without `GAMES_CHANNEL_ID` the card is not
  posted, but its active copies are. Publishing a card that is already live does not repost it, but it does
  give the card a follow role if it has none (see Follow roles).
- **Archived:** `!card archive` deletes the card's message and keeps the card. Publishing it again
  makes it live.

A card message that is deleted by hand is posted again. The delete kicks the poller, so the
repost does not wait for the next poll interval.

### Subscriptions

An admin can subscribe a card to other text channels. Each subscription is a copy of the full card
message, rendered exactly as the games-channel card, with the same embed and status and no mentions.
Copies are posted, edited on change and posted again after a hand delete, the same way the card is.
Subscriptions never appear on the board. A board link still points only at the card's message in the
games channel.

- **Channel:** `<channel>` is a pasted channel link or `<community id> <channel id>`, as for a text
  channel field. The bot must be in that community, and the channel must be a text channel there. A
  channel the bot cannot check gets the retry reply, and `GAMES_CHANNEL_ID` is refused, since the card
  is already posted there.
- **Limit:** a card has at most five subscribed channels. A paused channel counts toward the five.
- **Already subscribed:** subscribing an active channel again changes nothing and says so.
- **Pause:** when Fluxer refuses a copy's post or edit with 403, or its post returns 404 (the channel is gone),
  that copy is paused and the bot stops posting and editing there. Each pause sends each admin one direct
  message, which names the card by its slug and the channel by a mention, and carries no other card text;
  `!card show` lists each card's channels. A 403 pause is cleared by `!card subscribe` for that channel,
  which resumes it. A channel-gone pause is cleared by `!card unsubscribe` for that channel.
- **Resume:** `!card subscribe` on a paused channel resumes it. The next check posts the copy, or edits
  it if the card changed.
- **Failures:** a copy whose edit returns 404 is posted again, as the card is. Any other failure is
  logged with the ids and the exception type, and retried on the next check. A failing copy never stops
  the card or the other copies.
- **Unsubscribe:** deletes the copy and removes the subscription. A copy that is already gone counts as
  removed. If the bot cannot delete it for another reason, the subscription is still removed, and the
  reply says the copy is still there, to delete by hand.
- **Archive:** deletes every copy, then the card's message last. A 403 on a copy or on the card's own message is
  handled the same way: that message stays where it is, the archive goes ahead, and the reply lists each
  channel left, as a mention, to delete by hand. The reply says the message was removed only when nothing is
  left; otherwise it says the card is archived and its settings are kept. Any other failure to delete (not
  already gone) stops the archive while the card is still live: the copies deleted before the stop are posted
  again by the next check, and the card's message stays in place, so running it again is safe. The
  subscriptions are kept. Publishing again posts every subscription that is not paused.
- **Show:** `!card show` lists the subscribed channels, with "(paused)" after each paused one.
- **Rate:** each copy costs one send, or one edit, each time the card's content changes, and nothing
  when it does not. Each poll compares every copy's stored hash, but sends nothing for an unchanged copy.
  A card with five copies sends six messages in one burst. The poller does not throttle copies. Fluxer's
  rate limits apply, and the REST client retries a 429.

### Summary board

When `GAMES_CHANNEL_ID` is set, the bot keeps one more message in the games channel: the summary
board. It is always on, with no setting of its own. It lists every live card, in the same order as
`!card list`, as one inline cell: the card's title as the name and its status line as the value. A
card with no manager and no probe shows a dash, and so does a card whose status check failed in the
last poll. When the card's message is in the board's channel, its status is a link to the card's
message. The board is laid out for the desktop and web clients. Phones show the full cards as before.

- **Posting:** the cards are posted first in each poll, and the board is posted after them, so a new board
  sits below them. A new board links every card that is already posted in the board's channel. If the
  stored board cannot be read at the start of a poll, the cards are still posted, and no new board is
  posted in that poll. The next poll posts it.
- **Staying the newest message:** the board is kept as the newest message in its channel. When a card or
  another message lands below it, the bot checks at the next poll and, if the board is no longer the newest,
  posts a new board and then deletes the old one. Several messages within `BOARD_REPOST_MIN_INTERVAL_SECONDS`
  cause one re-post, and the next poll is brought forward to the time the re-post is due. Edits
  never cause a re-post. A failed re-post is logged and tried again at the next check, after the interval.
- **The board's channel:** the board is posted in the games channel and then stays there. A stored board
  is edited where it was posted, whatever `GAMES_CHANNEL_ID` is now. Its links go to the channel's own
  community, and a card is linked only when its message is in the same channel as the board. To move the
  board, delete its message by hand. The bot then posts a new board in the current games channel.
- **Edits:** the board is edited when its content changes, for example when a status or a title changes.
  When a board is stored, each card is posted as soon as its own check ends, so a slow check does not hold
  back the other cards. The board is brought up to date after all the cards.
- **No pin request.** The bot does not pin the board and sends no admin a DM when it posts one.
- **Size:** the board shows at most 240 cards. The bot keeps the whole board message under 6000 characters of
  text, which is Fluxer's limit on embed text. The count is in UTF-16 code units, so an emoji counts two. A
  board with long titles, long links or many cards shows only the cards that fit, and a note at the bottom
  says that some games are not shown.
- **Failed checks:** a card whose status check fails in a poll stays on the board, with the dash. Its
  message is not edited in that poll.
- **Deleted by hand:** a board message that is deleted is posted again, the same way a card is.
- **No live cards:** the board still posts, with "No games are live."

### Syntax

```
!card new <slug> <title…>                  creates a draft
!card set <slug> <field> <value…>          sets one field
!card clear <slug> <field>                 clears one field
!card show <slug>                          the inside embed and the outside text
!card list                                 slug, state and title, by sort order; a long list is sent in several
                                           messages, each under 1900 UTF-16 code units
!card publish <slug>                       draft or archived -> live
!card archive <slug>                       deletes the message and every subscription copy, keeps the card
!card subscribe <slug> <channel>           posts a copy of the card in another text channel
!card unsubscribe <slug> <channel>         deletes that copy and stops posting there
!card interactions on|off [slug]           turns the reaction taps off or on, for every card or for one
!card help                                 the subcommands and the field names
!card help fields                          each field, its format and its limits
```

The last argument takes the rest of the line, so no quoting is needed. `!card help` and
`!card help fields` are the replies to read when you forget a format or a limit.

### Wake requests

A member who presses the wake reaction on an asleep card opens a request: the admins get a direct
message, and the card shows "Requested" with the number of people who asked. A request closes when the
card is ready, when it expires (`WAKE_REQUEST_EXPIRY_SECONDS`), or when an admin clears it. Each
requester gets one direct message when a request closes. Admins manage open requests by direct message:

```
!request list                              each card with an open request: its slug, the number of people and the age
!request clear <slug>                      closes the card's open requests as cleared
```

### Play now

A press on a card's play-now reaction (the raised hand) posts "Someone wants to play <card> now" in the card's
play-now channel, or in `PLAY_NOW_CHANNEL_ID` when the card has none. The post is never made in the games channel.
It mentions only the card's follow role, and only when that role is in the same community as the post.

A press on the raised hand on a copy of the card in the games community posts in two places: the copy's own
channel, and the card's play-now channel when that is a different channel. Each post gets its own reactions and
expires on its own. The cooldown (`PLAY_NOW_COOLDOWN_SECONDS`) counts the press once, so a press inside it posts
nothing in either channel and the presser gets a link to the latest post. If one of the two posts fails and the
other succeeds, the one that succeeded stays and the failure is logged. A copy outside the games community shows
the raised hand when the card has a play-now target (its own verified play-now channel, or `PLAY_NOW_CHANNEL_ID`),
and the bell when the card has a follow role, but neither works there: a press posts nothing and changes no role,
and the presser gets a direct message pointing at the card in the games channel.

### Sessions

A card can plan sessions, posted in the card's session channel: its own verified play-now channel, or
`PLAY_NOW_CHANNEL_ID` when the card has none. With neither set, a press on the calendar says the card has no channel
for sessions yet, and the calendar is not added to the card.

**Proposing.** A press on the calendar (📅) on the card, or on a home copy in the games community, opens a direct
message with up to six offered times, one per keycap. React with a keycap to pick a time, or reply with a time
instead:

```
sat 14:00          tomorrow 19:30          today 9:05          2026-11-14 14:00
```

Times are 24-hour, in `SESSION_TIME_ZONE`. A weekday name means the next such day, so a weekday name for today is
refused (write `today HH:MM`). A typed time must be in the future and at most 60 days away. The offered times come
from `SESSION_SLOTS_WEEKDAY` and `SESSION_SLOTS_WEEKEND`, start at least an hour from now and fall within the next
14 days. The prompt expires after 15 minutes, and a new press on the calendar replaces an open prompt. A copy outside
the games community only points back to the card.

Picking a time sends a confirmation. React ✅ to post the session. The confirmation adds a line when the proposer is
already going to an overlapping session. A proposer may have three upcoming sessions for one card, and a card may
have ten upcoming sessions.

**RSVPs.** The session message has three reactions: ✅ going, ❔ maybe and ❌ can't. The message counts update as
people react. A session's seats are its own, set with `!session seats`, or else the card's `seats` field. When the
seats are full, a ✅ joins the waitlist, and a waiting user who gets a seat is sent a direct message. A session that
has ended or been cancelled takes no RSVP changes.

**Commands.** A session is changed by `!session` in a direct message to the bot. A session is named by its number,
as `!session list` shows it, or by its message link. The proposer and admins can run every change. `list` shows a
member their own upcoming sessions, and an admin every upcoming session with its counts. A guild channel never gets
a reply to `!session`.

```
!session list                              your upcoming sessions; an admin sees every upcoming session
!session cancel <session>                  cancels it, edits its message and DMs its attendees
!session move <session> <time>             moves it, keeping its length and RSVPs, and DMs its attendees
!session repeat <session> weekly|off       marks the session's series weekly, or stops it
!session location <session> <text|clear>   sets or clears the location
!session seats <session> <n|clear>         sets the seats (1 to 999), or clears them back to the card's seats
!session help                              lists these commands
```

Cancel and move DM each going and waiting attendee, except the person who made the change. Cancelling leaves a weekly
series in place; run `!session repeat <session> off` before cancelling, or on the next occurrence, to stop it.
`!card archive` cancels the card's upcoming sessions the
same way and stops their series. When a session's message is deleted, the session is cancelled and its attendees are
told.

### Polls

A time poll asks a card's people which of several times they can make. It is posted in the card's session channel, so
a card needs one, as for sessions. Polls never post to the games channel.

**Starting.** A press on the ballot box (🗳️) on the card, or on a home copy in the games community, posts the poll and
DMs a ballot to each recipient. The poll offers up to six times, soonest first, from the same slots as 📅. The
recipients are the starter and the holders of the card's follow role, bots excluded. When `POLL_MAX_RECIPIENTS` cuts
the list, the holders with the highest ids are left out, and the starter is told how many. A card with no follow role
sends a ballot only to the starter, and the starter is told that too. If the member list cannot be read, the start is
refused with a direct message, and nothing is posted. A card has one open poll at a time: a second press links the open
one. A press on a copy outside the games community only points back to the card.

**Voting.** A ballot is a direct message to the bot. Its first line names the poll, as `Time poll 7 for <card>.` or
`Runoff 8 for <card>.`, so a voter with several ballots can tell them apart. Reply with letters, like `a, c`, to pick times. Or type a time in
`SESSION_TIME_ZONE`, in a form 📅 accepts, like `fri 20:00`, to add a time to the poll for everyone. A typed time must
be at least an hour away and within 60 days. Mix both, like `a, fri 20:00`. `all` picks every time the ballot shows, and
`clear` removes your picks. Each reply replaces your last one. A refused reply names the item that failed, such as
`Item 2: That time is less than an hour away.`. With more than one open poll, a plain reply changes nothing and lists the
open polls: reply with `!poll vote <poll> <reply>` to pick one.

**The poll message.** It lists the times with their votes, the current leader, how many people have answered, and the
close time. It never shows who voted.

**Closing.** A poll closes at its close time, which is `POLL_LENGTH_SECONDS` after it starts unless `!poll extend`
moves it. The poller wakes for that time, so the poll closes at that time or on the first tick after it. A time less
than an hour away is left out of the count, so a winner is always a time that can still be scheduled. The winner is the
time with the most votes, and a tie goes to the soonest time. A poll with no votes has no winner. When reminders are on,
each recipient who has not answered gets one reminder at the halfway point. A recipient in DM backoff is skipped, and can
still get the reminder if the backoff ends before the close.

After a close with a winner, the poll message gets 📅 and 🔁 for the starter and admins to press. A runoff's message gets
only 📅. Pressing either removes the presser's reaction, and only the starter or an admin acts.

- 📅 schedules the winning time as a session in the card's session channel. The starter is the proposer, and each voter
  for that time is a maybe. The starter and each voter get one direct message with the link. A winning time less than an
  hour away, or a starter at the limit of upcoming sessions for the card, gets a direct message to the presser and
  nothing is created. A poll that already has a session does nothing.
- 🔁 starts a runoff on a closed poll with a winner. Its candidates are the top three usable times that have votes,
  ranked by votes and then soonest. They are offered as letters a to c in that rank order. The ballots go to the
  starter and the follow role's current holders (the member list is read again), and the runoff keeps the starter.
  Voters rank the times they can make,
  favourite first, like `b, a, c`, and may rank as many as they like. Runoff replies take letters only, so `all` does
  not work there.
  The runoff is counted by instant runoff. A time with more than half of the first choices still counting wins.
  Otherwise the time with the fewest first choices is dropped, a tie for fewest drops the later time, and its ballots
  move to their next choice. With two times left and no majority, the time with more first choices wins, and a tie goes
  to the soonest.
  A poll can have one runoff. 🔁 is refused with a direct message when there already is one, when fewer than two usable
  times have votes (the message says to press 📅 instead), and when there is too little time for one.
  A runoff closes at the earlier of two times: `POLL_LENGTH_SECONDS` after it starts, or one hour and ten minutes
  before its earliest candidate time. The ten minutes are a margin, so every candidate is still usable when a late tick
  closes it. If less than 15 minutes would remain, 🔁 is refused and the presser is told to use 📅. A closed runoff gets
  only 📅.

**Commands.** A poll is named by its number, which its footer shows as `Poll <n>` and `!poll list` shows, or by its
message link. A member's `!poll list` shows the polls they started or were sent, newest first. An admin's also shows
every open poll. Poll commands and ballot replies share the direct-message limit for non-admins, 5 per 60 seconds, and
admins are exempt. An admin can run every command.

```
!poll list                        your polls, newest first; an admin also sees every open poll
!poll vote <poll> <reply>         a ballot reply, needed when you have more than one open poll
!poll sort <poll>                 re-letters the times by votes, then soonest (starter or admin)
!poll shown <poll> <1-20>         how many times a ballot shows, from the next sort (starter or admin)
!poll extend <poll> [duration]    moves the close later; the duration is described below (starter or admin)
!poll close <poll>                closes it now, with the winner so far (starter or admin)
!poll help                        lists these commands
```

`!poll extend` takes an optional duration: a number of minutes (`m`), hours (`h`) or days (`d`), from 1 to 9999, in
upper or lower case, like `90m`, `12h` or `2d`. Without one, it adds `POLL_LENGTH_SECONDS`. It cannot make a poll run
longer than `POLL_MAX_LENGTH_SECONDS` from when it started. `!poll shown` does not
change the ballots until the next `!poll sort`, which also re-letters the times.

**Archive and the kill switch.** `!card archive` closes the card's open poll with no winner, before it deletes the
card's messages. A poll whose message the bot finds deleted when it next edits it closes as removed. `!card interactions
off` removes the ballot box (🗳️) from the card's messages and copies. It does not close open polls and does not touch
poll messages, so 📅 and 🔁 stay on closed polls.

### Fields

- `title` (up to 200 characters), `description`, `join` (how to join) and `first_time` (up to 1000
  characters each): text. Control characters and bidirectional overrides are refused.
- `sort`: an integer from 0 to 9999. `seats`: an integer from 1 to 999.
- `location`: text up to 200 characters. Outside Fluxer it shows only a fixed placeholder.
- `voice`, `voice2` and `voice3 <guild_id> <channel_id>`, or a pasted channel link: three slots for voice
  channels. When the bot is in that community, the channel must be a voice channel there, and its name is stored
  and kept up to date. When the bot is not in it, the channel is stored unverified and the reply says so.
- `text_channel`, `text_channel2` and `text_channel3`: three slots for text channels. Each takes the same input
  as `voice`, and each is checked the same way, except that the channel must be a text channel there.
  `text_channel` was called `text_channel1`; the old name is refused with a reply that gives the new one.
- `address`, `address2` and `address3 <host>:<port>`: three slots for server addresses, for example a Java and a
  Bedrock address for a Paper server with Geyser. `address_public`, `address2_public` and `address3_public` (`yes`
  or `no`, default `no`) decide, for each slot on its own, whether the outside text and the play-now post show
  that address. The card itself, and every subscription copy of it in any community, shows all address slots.
- `<slot>_suffix <label>`, for each of the nine slots above (`voice_suffix`, `voice2_suffix` … `address3_suffix`):
  a short label that tells the slot apart, shown as "Voice (Raid night)" on the card and as "Fluxer voice channel
  (Raid night)" outside Fluxer. It is 1 to 20 characters of letters, digits, spaces and `- + . '`; spaces at the
  ends are trimmed and runs of spaces become one. A suffix, or a public flag, can be set only on a slot that has a
  value.
- Each slot is its own line on the card, in this order: the voice slots, the text channel slots, then the address
  slots, each in slot order. Empty slots are skipped. Slots are independent: `!card clear <slug> voice` clears the
  first voice slot with its suffix (and an address slot with its public flag), and never moves another slot up into
  its place. `!card clear <slug> voice_suffix` clears only the suffix.
- `manager <kind> <ref>`: a configured host manager and its reference. Only `pterodactyl` exists.
- `probe <kind> <host> <port>`: a probe kind (`minecraft_bedrock`, `minecraft_java` or `a2s`) with
  its host and port.
- `show map,version,game_mode`: any subset of those three, or `none`. These are the probe details
  a card shows.
- `follow_role <role id>`: the card's follow role, which the bot creates at publish or adopts from a matching
  role. The role must have no permissions, be mentionable and sit below the bot's top role. A role id that fails
  any of those is refused, and nothing is stored. `!card clear <slug> follow_role` forgets the role but keeps it in
  Fluxer. The bot never deletes a role. Following needs `GAMES_CHANNEL_ID`.
- `play_now_channel <guild_id> <channel_id>`, or a pasted channel link: the text channel where the card's play-now
  messages go. It is checked as a text channel slot is, but it must be verified: when the bot is not in that
  community, the channel is refused and nothing is stored. The games channel is refused. Without it, play-now messages
  go to `PLAY_NOW_CHANNEL_ID`, and without that the press is refused. A press on a copy in the games community posts
  in the copy's channel and also here, when this is a different channel. `!card clear <slug> play_now_channel` removes it.

Every reply names the field and the rule, never the value you sent. Replies use the word
"community".

## Adding a host manager or probe

Plugins are modules in `src/fluxer_panel/sources/`.

1. Write a module that implements a protocol from `sources/base.py`: `HostManager` (`kind`,
   `validate_ref`, `power_state`, `next_power_action`) or `Probe` (`kind`, `probe`).
2. Add one line to `MANAGER_PLUGINS` or `PROBES` in `sources/registry.py`. A manager plugin is
   `ManagerPlugin(create, ref_hint)`: `create` returns `None` when its settings are absent, and `ref_hint` is the
   ref's shape for the card help (for example `<8-character server id>`). A manager module therefore also exposes a
   `REF_HINT` string. A blank hint is refused.
3. Add a fake under `tests/` and tests for the plugin, as the existing probe tests do.
4. Return `ProbeFailed` or `PowerState.UNKNOWN` instead of raising. `validate_ref` raises
   `CardFieldError`, and `next_power_action` may raise `SourceError`; the caller handles both.

A result or an error must never carry a library message, a Panel response body, a player name or
a command payload. Errors leave a plugin as fixed reason keys.

## Development

```sh
uv venv -p 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
```

Run the three checks:

```sh
.venv/bin/python -m pytest -q -W error
.venv/bin/ruff check src tests tools
.venv/bin/reuse --root . lint
```

## Exit 3: database refused

The panel opens its SQLite database before it connects to Fluxer. If the database
cannot be used safely, it exits with code 3 and logs one of these events. Nothing is
connected and the file is not written to except as described.

- **`db_quarantined` or `db_quarantine_pending`**: the database was corrupt (or failed
  its integrity check). The panel moved `panel.db` (and any `-wal`, `-shm` or `-journal` files) to
  `panel.db.corrupt`, or `.corrupt.1` and so on on repeat. The panel refuses every
  start until the operator acts. Either restore `panel.db` from `panel.db.corrupt` if
  that file is recoverable, from a `panel.db.pre-v<N>` backup, or from an external
  backup; or move the `panel.db.corrupt*` files out of the data directory. Starting
  with an empty database is always a deliberate operator choice: the panel never
  creates one on its own while quarantined files are present.
- **`db_schema_newer`**: the database was written by a newer version of the code (its
  schema version is higher than this build knows). The file is left unchanged. Deploy
  the code that knows that schema version (for example, the code was rolled back, and
  the newer release should be redeployed).
- **`db_unavailable`**: the database, or the data directory it lives in, cannot be read or
  written by this process, or the database could not be used. The file was not touched.
  The panel checks that it can write the database before it connects, so this covers:
  - the data directory is not readable, writable and searchable by this process. The panel
    needs to read it to look for quarantined files, and to write and search it to create
    `panel.db`, `panel.db-wal` and `panel.db-shm`. This includes a missing database in a
    read-only data directory, and a data directory that cannot be created because a parent
    directory is not writable. Fix the permissions or ownership of the data directory, then
    start again. A refused start creates no new side files.
  - a database file that is not readable or writable. Fix the permissions or ownership of
    `panel.db`, then start again.
  - another connection holding the write lock for longer than 5 seconds. Stop the other
    process, then start again.
  - a `panel.db-journal` file next to `panel.db`. This is a pre-WAL rollback journal left
    behind by an interrupted transaction, and the panel refuses to read past it. The panel
    does not roll it back itself. Open the database once with a read-write SQLite client (for
    example the `sqlite3` shell, where it is installed, running `PRAGMA user_version;`). That
    open rolls the journal back and removes it. Do not delete the journal by hand.

The panel does not prevent a second instance from running against the same database.
Running two panels against one database is unsupported.

Exit 1 with a `fatal_error` event means something else failed while opening the database,
such as the data directory could not be created (for example, its parent is a regular file).
That is not a refusal and does not quarantine anything.

## Release notes

### v0.4.0

- No new variable is required. Every poll setting has a default. A bad value exits with code 2, and so does a
  `POLL_MAX_LENGTH_SECONDS` below `POLL_LENGTH_SECONDS`.
- Back up the database before upgrading. The bot also writes a `.pre-v9` copy beside the database before it applies
  the migration.
- The schema moves to 9 and adds the poll tables: polls, their options, ballots and votes. v0.3.0 refuses a v9 database
  (exit 3, reason `schema_newer`) and leaves the file unchanged, so a downgrade means restoring the `.pre-v9` backup.
- Cards and home copies that have a session channel gain 🗳️ after 📅, which is seeded on the next reconcile.
- The purge status file gains `deleted.polls`. `schema_version` stays 2.
- Starting a poll, and each runoff, reads the community's member list once to find the follow role's holders. A
  failed read refuses the start. The live check for this is in `docs/fluxer-notes.md`.

### v0.3.0

- `SESSION_TIME_ZONE` is now required. Startup exits with code 2 without it, so set it (for example
  `America/New_York` or `UTC`) before you upgrade. The other session settings have defaults.
- Back up the database before upgrading. The bot also writes a `.pre-v8` copy beside the database before it applies
  the migration.
- The schema moves to 8. v0.2.0 refuses a v8 database (exit 3, reason `schema_newer`) and leaves the file unchanged,
  so a downgrade means restoring the backup.
- Cards gain a 📅 reaction, which is seeded on the next reconcile on cards and home copies that have a session
  channel.
- Every card message is edited once after the upgrade, to carry the new footer and field rules.
- The `purge` subcommand defers sessions (`sessions_deferred`, status file schema 2) and leaves them in place. The
  running bot scrubs and deletes them.

## How this was developed

This project was coded using Claude Code, orchestrated by Opus 5.5 with Haiku 5.5 subagents writing much of the code,
under instructions from K. Chen. Its Fluxer client was carried over from his earlier green-oaks-bot project, which was
built the same way. As with all free and open source projects, the user's ultimate protections lie in the ability to
examine the code and the right to fork it.
