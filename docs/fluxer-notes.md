<!-- SPDX-FileCopyrightText: 2026 K. Chen -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Fluxer live capability notes

Recorded 2026-10-31 by hand-running the five scripts in `tools/live/` against a self-hosted Fluxer instance.
Hostnames, user IDs, channel IDs, message IDs, tokens and session IDs are deliberately omitted.
Placeholders such as `<channel>`, `<user>`, `<id>` and `<str len=N>` are as the harness printed them.

## Version

- Client (user-confirmed): Fluxer Stable, Desktop `2026.1017.133957`, Web `2026.1017.133957`.
  This is the client build the user observed the results in, not the server.
- Server: `X-Fluxer-Version=2026.1031.154542`, `api_code_version=1`.
- Date of the run: 2026-10-31.

## Carried over from green-oaks-bot

Seeded from the green-oaks-bot notes (recorded 2026-10-24) and generalised. These were not re-observed
in this run, except where a section below says so.

- Discovery: `GET {instance}/.well-known/fluxer` returns `endpoints.api_public` (`https://<instance>/api`)
  and `endpoints.gateway` (`wss://<instance>/gateway`).
- `GET {api}/v1/gateway/bot` (bot auth) returns the same gateway URL. No gateway override was needed.
- Identify without an `intents` field was accepted. Re-confirmed in this run: reaction events arrived
  with identify sending no intents.
- On guild MESSAGE_CREATE, `author.bot` is present (`true` for bot-sent messages) and `member.roles`
  is present (an array of snowflake strings). These were NOT re-observed in this run: no script in
  `tools/live/` captures a guild MESSAGE_CREATE, so these facts come from the green-oaks-bot run only.
  The DM MESSAGE_CREATE differs (no `guild_id`, no `member`, no `bot` key for humans); see the `dm.py`
  section below. `member.roles` was re-observed in this run on guild MESSAGE_REACTION_ADD and
  MESSAGE_REACTION_REMOVE payloads, not on MESSAGE_CREATE.

## reactions.py (AC2.1)

**Works.** Guild and DM reaction add and remove all arrive and parse as `ReactionAdd` and `ReactionRemove`.
Four PASS checks. The script only observes: reactions are made by hand in the client.

**Requests.** Prompt messages are sent with `POST {api}/v1/channels/{channel}/messages`. The DM is opened
with `POST {api}/v1/users/@me/channels`.

**Event shapes (as printed).**

Guild `MESSAGE_REACTION_ADD` and `MESSAGE_REACTION_REMOVE` (identical shape):

```
{'channel_id': '<channel>', 'emoji': {'name': '👌'}, 'guild_id': '<guild>', 'member': {'accent_color': None, 'avatar': None, 'banner': None, 'communication_disabled_until': None, 'deaf': False, 'joined_at': '<str len=24>', 'mute': False, 'nick': None, 'roles': ['<id>'], 'user': {'avatar': None, 'avatar_color': None, 'discriminator': '<id>', 'flags': 0, 'global_name': '<str len=6>', 'id': '<user>', 'username': '<str len=6>'}}, 'message_id': '<message:1>', 'user_id': '<user>'}
```

DM `MESSAGE_REACTION_ADD` and `MESSAGE_REACTION_REMOVE` (identical shape):

```
{'channel_id': '<dm>', 'emoji': {'name': '🎉'}, 'message_id': '<message:2>', 'session_id': '<str len=32>', 'user_id': '<user>'}
```

**Quirks.**
- A unicode emoji has no `id` key at all. It is not `id: null`.
- DM reactions have no `guild_id` and no `member`. They carry an undocumented 32-character `session_id`.
- The `discriminator` value shows as `<id>` only because the harness shaper treats any short all-digit
  string as an id. It is a discriminator, not a snowflake.
- `MESSAGE_REACTION_ADD_MANY`, `MESSAGE_REACTION_REMOVE_ALL` and `MESSAGE_REACTION_REMOVE_EMOJI` were not
  observed in this run. No ADD_MANY shape is recorded, so it stays unparsed.

## edit.py (AC2.2)

**Works.** Edit content, edit embed, and edit content plus embed all PASS.

**Request.** `PATCH {api}/v1/channels/{channel}/messages/{message}` with a JSON body containing `content`
and/or `embeds`.

**`content: null`.** Accepted (ok). The returned content is an empty string, `{'content': '<str len=0>'}`,
and the embed is kept. An embed-only message is therefore possible.

**Rapid edits.** Six rapid edits (0 to 5) with the client's retries off (`max_retries=0`) all returned ok.
No 429 was seen: "no rate limit in 6 edits". This is not evidence that no limit exists.

## reaction_remove.py (AC2.3)

**Works.** Four PASS checks: user reaction add, remove of the user's reaction with Manage Messages, re-react
after the permission was removed, and removal without Manage Messages refused with 403.

**Requests.**
- Remove the user's reaction: `DELETE {api}/v1/channels/{channel}/messages/{message}/reactions/{emoji}/{user}`.
  The `{emoji}` is the unicode character URL-encoded, or `name:id` for custom emoji.
- The 403 case returned code `MISSING_PERMISSIONS`. The permission was denied with a channel override.

**Event.** The `MESSAGE_REACTION_REMOVE` for the bot's removal arrived and parsed as `ReactionRemove`. It has the
same guild shape as above.

**Quirks.**
- The script waits for Enter on stdin. Run from Claude Code's `!` prompt, stdin is closed and the script
  exits with `EOFError` before the permission step. It must be run in a real terminal.

## roles.py (AC2.4)

**Works.** Add member role PASS, remove member role PASS, and hierarchy add refused with 403 PASS.

**Requests.**
- Add: `PUT {api}/v1/guilds/{guild}/members/{user}/roles/{role}` with header `X-Audit-Log-Reason`.
- Remove: `DELETE {api}/v1/guilds/{guild}/members/{user}/roles/{role}` with header `X-Audit-Log-Reason`.
- Member check: `GET {api}/v1/guilds/{guild}/members/{user}`.

**Quirks.**
- The hierarchy refusal returns 403 with code `MISSING_PERMISSIONS`. That is the same code as a missing-permission
  refusal, so the two cannot be told apart by code.
- The guild Activity Log (Fluxer's name for the audit log) showed both add and remove, with the reason text
  `fluxer panel live test`.

## dm.py (AC2.5)

**Works.** DM send PASS. The reply was received as a guild-less `MESSAGE_CREATE`. Re-opening the DM returned the same
channel ID.

**Requests.**
- Open: `POST {api}/v1/users/@me/channels` with body `{"recipient_id": "<user>"}`.
- Send: `POST {api}/v1/channels/{dm}/messages`.

**Event shape (as printed).**

```
{'attachments': [], 'author': {'avatar': None, 'avatar_color': None, 'discriminator': '<id>', 'flags': 0, 'global_name': '<str len=6>', 'id': '<user>', 'username': '<str len=6>'}, 'channel_id': '<dm>', 'channel_type': 1, 'content': '<str len=8>', 'edited_timestamp': None, 'embeds': [], 'flags': 0, 'id': '<id>', 'mention_everyone': False, 'mention_roles': [], 'mentions': [], 'nonce': '<id>', 'pinned': False, 'stickers': [], 'timestamp': '<str len=24>', 'tts': False, 'type': 0}
```

**Quirks.**
- No `guild_id` and no `member`.
- A human author has no `bot` key at all, so `author.bot` is absent rather than `false`.
- Extra `channel_type: 1` field.

## Implications for later panel work

- Reaction events need no gateway intents. Identify without intents is enough for reaction-driven features.
- DM reaction events carry no `member`, so no role data is available in a DM. Role-gated behaviour in DMs
  cannot use `member.roles`; treat DMs as having no guild roles.
- The DM MESSAGE_CREATE has no `member` and no `bot` key for humans. Use `author.bot is True`, which is how the
  parser already reads it.
- Hierarchy refusals and missing-permission refusals both return 403 `MISSING_PERMISSIONS`. Do not branch on the
  code to tell them apart; check role hierarchy before calling the API.
- To make a message embed-only, send `content: null` on the edit. The text is erased and the embed is kept.
- To change only the embed and keep the text, omit `content` (the client's `UNSET`). Sending `null` erases
  the text, so never send `content: null` in that case.
- No edit rate limit was seen in 6 rapid edits. The panel must still handle 429 generically, since the limit is
  unmeasured.
- The interactive scripts must run in a real terminal. Any future live script that reads stdin needs the same.
- `events.py` matched all observed shapes; no parser changes. Observed-shape fixtures are in `tests/test_events.py`.
- `MESSAGE_REACTION_ADD_MANY` was not observed, and is left unparsed in this run.

## Panel smoke test

- 2026-11-01 (UTC), server 2026.1031.154542: `python -m fluxer_panel` resolved endpoints, logged `ready`, answered an admin's DM `!admin ping` with `pong` (`admin_command` `ping`; a bare or unknown subcommand got the list, `admin_command` `unknown`), and on SIGTERM from `timeout` logged `shutting_down` then closed the gateway cleanly. No message content appeared in any log line.

## Live card checks

- Run date 2026-11-03, against the server build in the Version section. Run by the operator with the live scripts in
  `tools/live/`.
- Targets and servers are placeholders: `<server#N>` for Panel servers and `<target#N>` for probe targets.
  Hosts, ports, map names and server names are not recorded.

### Panel (Pterodactyl) — read-only access

- Bot user permissions (`ptero_live.py`): each server the bot user could see had exactly `schedule.read` and
  `websocket.connect`. Every permission check passed, so no extra scope was flagged.
- Power state was read for every server.
- Schedules were read for every server, filtered to active ones. Each showed a name length, an active flag, a
  next-run time with its UTC offset, and the first power action (restart, or none when a schedule has no power
  task). `next_power_action` returned the earliest upcoming power task, for example "restart at <time>". Servers
  without power schedules gave "none".
- `/resources` gives 409 when a server is suspended and 504 when Wings is unreachable (as stated in the design;
  not re-observed in this run).
- `websocket.connect` is forced by the Panel for the plugin's access. Residual risk: the console can be read.
  Commands and power actions are not possible with this permission, and the plugin sends neither.

### Probes

- The A2S target: reachable. Counts and map reported (the script prints the map as its length only).
- The `minecraft_bedrock` target: reachable. Counts, map, version and game mode reported.
- `minecraft_java`: not tested in this run.

### Game query ports

- Valheim's A2S query port is the game port + 1. A probe against the game port times out (`reason=timeout`);
  a probe against the game port + 1 succeeds.

### Timestamp markup

- `<t:...:R>` and the full `<t:...:f>` form render as local times on web and mobile, in message content and in
  embed fields. R renders as relative time.
- Decision: `resolver.USE_TIMESTAMP_MARKUP = True`. The plain-text branch is kept and tested.

### Channel links

- Web: the bare link, the masked `[voice](...)` link in an embed description, and the `<#C>` mention each opened the
  voice channel.
- Mobile: the bare link, the masked link and the `<#C>` mention each opened the voice channel.
- All three were built for the voice channel (`LIVE_VOICE_CHANNEL_ID`), not the posting channel.
- The copied link shape is `H/channels/G/C`, which matches `channel_link`'s `{instance}/channels/{G}/{C}`.
- Decision: no change to `channel_link` or `CHANNEL_LINK_RE`. The Voice field stays a masked link.

### Masked links in embeds

- `[text](link)` in an embed field value and in the description is clickable on web and mobile.

### Mentions in embeds

- Web and mobile: `<@U>` in an embed field rendered as a mention.
- With empty `allowed_mentions`, no notification arrived on either client. This is the expected behaviour, not a
  Fluxer bug.
- Decision: no code change. No REVIEW.md note, since no ping fired.

### Escaping

- No visible backslashes on web or mobile for the `render.escape` test string.
- Decision: `render._MD` is unchanged.

### Edit bursts

- 20 edits in each of three runs: 20 ok, 0 rate-limited (429), 0 other errors. Each run took about 1.2 to 1.4
  seconds.
- The existing client retries 429. None was seen in these runs.
- This is an API-level measurement. It does not depend on the client (web or mobile).

### Voice channel type

- The voice channel's `type` is `2`.
- Decision: `card_commands.VOICE_CHANNEL_TYPE` is unchanged.

### Text channel type

- The posting channel's `type` is `0`, the same as Discord's.
- Decision: `card_commands.TEXT_CHANNEL_TYPE` is unchanged.

### Jump links

- A message jump link (`H/channels/G/C/M`) opened the linked message on desktop and web.
- Decision: the summary board links each status to its card with `render.jump_link`.

### Summary board sample

- The board sample (title and status cells, inline fields, three per row) was legible on desktop and web.
- Phones are not a target for the board. The detailed cards serve them.

### Pins

- Pinning a message only adds it to the channel's pin menu. The message does not stay at the top of the channel.
- So the board's pin DM does not keep the board on top. The bot still posts the board before the cards when no
  board is stored, so in a new channel it starts at the top.
- Stretch goal, deferred: keep the board at the top of the channel. One option is to post the cards again after the
  board whenever the board is posted again. The pin DM stays as it is for now.

### Copies in other channels

- Subscribing a card to a channel the bot could not see (the channel lookup was refused) was refused at subscribe
  time, with a reply. Subscribing does not check send permission: a copy in a channel where the bot cannot post is
  paused on its first post, and each admin gets one direct message.
- With the send permission removed from a channel holding a copy, a change to the card still edited the copy. The bot
  can edit its own message without the send permission, so no refusal and no DM. This is expected: the copy stays
  current.
- With the send permission removed, deleting the copy by hand made the bot post it again. Fluxer refused, the copy
  was paused and each admin got one DM. After the permission was restored, `!card subscribe` resumed the copy.
- Decision: no change. A 403 on an edit still pauses the copy, for example when the bot loses view access.

### GET /guilds for a community the bot isn't in

- The HTTP status is 403.
- Decision: `get_guild` keeps the 403 and 404 handling. 404 still covers nonexistent communities.

## Live reaction checks

- Recorded 2026-11-05. Run by the operator with the live scripts in `tools/live/` (`seed_reactions.py`,
  `role_ping.py`, `board_last.py`, `reaction_bulk.py`), against the server build in the Version section.
- Messages, users, roles and emoji are referred to by label. Emoji are written as Unicode code points.
- A source dive of the upstream server (`fluxerapp/fluxer`, commit `e20500ef`, 2026-11-05) is marked as read from
  source. The live build may predate that commit, so each source claim below was checked against the live run where
  the run could show it.

### Seeding and echo

- The bot added three reactions to a test message: U+25B6 U+FE0F, U+1F514 and U+1F64B. All three adds succeeded.
- The gateway echoed the bot's own add for every reaction. The echoed `MESSAGE_REACTION_ADD` carried a member with
  `user.bot` present and `true`.

### Play emoji (U+25B6) stored name

- The reaction the bot added was stored as U+25B6 U+FE0F, with count 1 and `me` true.
- The play reaction the operator added from the client was also stored as U+25B6 U+FE0F. So the variation selector
  is present whether the reaction comes from the bot or from a client.

### Reactions after an edit

- A reaction added after the message was edited still delivered its event. The edit did not stop reaction events.

### Role ping and allowed_mentions

- Shape A, `{"parse": [], "roles": [<role>], "replied_user": false}`, was accepted. Shape B, `{"roles": [<role>],
  "replied_user": false}`, was accepted. Both sends returned success.
- `create_role` ignores `mentionable`. The role was created with `mentionable` false even though the request asked
  for true. A `PATCH` on the role with `{"mentionable": true}` set it, and the returned role read back `true`.
- With the role mentionable, shape A and shape B each put the role in the message's `mention_roles`, with one entry
  and `mention_everyone` false. A control send with `allowed_mentions` set to `{"parse": []}` alone left the role out
  of `mention_roles`.
- With the role made non-mentionable again (`PATCH` to `false`, read back `false`), a ping to the role was dropped
  from `mention_roles`, which was then empty. The message itself was still delivered.
- The human check of silence was not done. The operator chose to move on, so the drop is evidenced by
  `mention_roles` and by the source only.
- The bot had neither `MENTION_EVERYONE` nor `ADMINISTRATOR`, and it is not the guild owner. Only those users pass the
  mention filter for a non-mentionable role.
- The operator's notification setting in the first run was the default, `ALL_MESSAGES`, which notifies on every
  message. In that run a ping to the non-mentionable role did notify. This is why the first run said "yes" and does
  not show that the ping reached the role. The later run, with the role mentionable and the setting "Only @mentions",
  gave an audible notification and an inbox notification for shape A, and the same for shape B. The two sends were
  separated by about 60 seconds, and each gave its own notification.
- Source (read from source): the mention filter keeps only mentionable roles in `mention_roles`, unless the sender has
  `MENTION_EVERYONE`. Owners and admins pass it. A non-mentionable role is silently dropped and the message is still
  delivered. There is no special case for bots. Webhooks bypass the filter. This matches the live run above.
- Source (read from source): `allowed_mentions` rejects `parse` together with `roles` only when `parse` is
  non-empty. So `parse: []` with `roles` is accepted, as shape A was live.

### Created role position

- The created role had position 1 and permissions 0. The source hard-codes position 1 on create.
- The bot's top role was at position 4, so the created role sat below the bot.
- One other role shared position 1 with it. Fluxer documents no tie rule for equal positions, and that was not tested
  further.

### Role assignment

- Removing and then adding the test role on a member both passed. Each change was read back from the member's roles.
- Adding a role above the bot was refused with 403 and code `MISSING_PERMISSIONS`. This is the same code as a missing
  permission, so the two still cannot be told apart by code. This matches the earlier hierarchy check.

### last_message_id and messages?limit=1

- After messages A and B were sent, `last_message_id` was B.
- After B was deleted, `last_message_id` was still B. It does not go back down when the newest message is deleted.
- `messages?limit=1` after the deletion returned A, the newest surviving message. The design's assumption holds.
- After C, D and C2 were sent and C was deleted, `last_message_id` was C2.

### Unread and notifications after a re-post

- The channel showed A, D and C2 in that order. A message re-posted in place of an earlier one lands at the bottom.
- Unread markers and notification behaviour on web versus mobile were NOT tested. The operator did not answer that
  question. It is deferred to testing with the real board.
- The "board" in this script was only a test message. The panel bot was not running, because the live scripts use the
  token alone.

### Reactor list and bulk removal

- The `/users` path returns a paged object with `items`, `has_more` and `next_after`. The client read the wrong key
  (`users`) and returned no reactors until it was corrected to read `items`. A plain list from the bare
  `.../reactions/{emoji}` path was also seen, and the client still accepts it.
- Each user object carries `bot` true for the bot.
- The operator added U+270B instead of U+1F64B, so the list for U+1F64B held only the bot.
- A stored reaction the bot did not add has `me` null or absent, not false. The script reads both as the same
  value, so treat null and absent as unknown.
- Removing one emoji's reactions passed. `MESSAGE_REACTION_REMOVE_EMOJI` arrived, and the other reactions stayed.
- Removing all reactions passed. `MESSAGE_REACTION_REMOVE_ALL` arrived, and the reactions list was empty.

### Reaction rate limit

- The probe made 30 alternating add and remove calls for one emoji on one message, as fast as possible.
- The first 429 came at call index 28. There were two 429 responses in total, each with the message "You're being
  rate limited."
- So a burst of about 28 calls passed before the first limit, which is above the design's 20 calls per 10 seconds.
  This was a burst probe, not a sustained rate, so the sustained limit is not measured here.

## First cards

Three cards were run against the test community, all three checked on web and mobile:

- A card with a `minecraft_bedrock` probe, an address and the version and game mode details.
- A card with a `pterodactyl` manager and a verified voice channel.
- A card with an in-person location and a voice channel in a community the bot is not in.

Result: rendered as expected on web and mobile. The reply for the unverified voice channel and the outside
placeholder for the location matched the design. A card message deleted by hand was posted again within seconds.

### Abiotic Factor and Palworld probes

- Neither game answered an A2S query, so both are manager-only (no probe).
- Abiotic Factor: the game port timed out, and the game port + 1 was refused.
- Palworld: the game port timed out. Palworld also has an RCON port, which is not an A2S port, so it was not probed.
