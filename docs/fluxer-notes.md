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

## Implications for later panel phases

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
- `MESSAGE_REACTION_ADD_MANY` was not observed, and is left unparsed in this phase.

## Panel smoke test (Phase 4)

- 2026-11-01 (UTC), server 2026.1031.154542: `python -m fluxer_panel` resolved endpoints, logged `ready`, answered an admin's DM `!admin ping` with `pong` (`admin_command` `ping`; a bare or unknown subcommand got the list, `admin_command` `unknown`), and on SIGTERM from `timeout` logged `shutting_down` then closed the gateway cleanly. No message content appeared in any log line.
