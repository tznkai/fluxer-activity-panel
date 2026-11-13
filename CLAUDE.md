<!-- SPDX-FileCopyrightText: 2026 K. Chen -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Fluxer Activity Panel

Last verified: 2026-11-13

Standalone project with two packages under `src/`: `fluxer_client` (a panel-agnostic Fluxer REST and
gateway client) and `fluxer_panel` (the activity panel bot built on it). `README.md` has the
user-facing description, the purge status file format and the "Exit 3" recovery steps.

## Purpose and boundary

- `fluxer_client` never imports `fluxer_panel`. `fluxer_panel` imports only `fluxer_client`'s public modules.
- No code here imports the source bot package (`tests/test_repo_hygiene.py` enforces both).

## Layout

- `src/fluxer_client/`: `ids.py` (snowflake checks), `rest.py` (REST client), `gateway.py`, `events.py`
  (gateway dispatches to frozen dataclasses; unknown or malformed payloads give `None` and log the field name only),
  `roles.py`.
- `src/fluxer_panel/`: `__main__.py` (`run|purge` entry point), `config.py`, `db.py` (single SQLite connection),
  `migrations/` (numbered SQL, see Database), `purge.py`, `admin.py`, `bot.py`, `logs.py`, `fsutil.py`.
  Sessions: `sessions.py` (the session, presence and RSVP store), `rsvp.py` (the pure RSVP engine), `session_time.py`
  (the pure typed-time parser and slots), `session_flow.py` (the 📅 DM flow; in-memory drafts), `session_post.py`
  (every REST call on a session message; the games-channel guard), `session_commands.py` (`!session`),
  `session_jobs.py` (end, recurrence, heads-up, draft expiry), `dm_router.py` (the DM entry point; the non-admin path)
  and `dm_backoff.py` (the per-user DM-failure backoff).
  Polls: `polls.py` (the store for polls, options, ballots and votes, and the view a renderer reads), `poll_letters.py`
  (option letters from positions), `poll_reply.py` (the pure ballot-reply parser), `poll_count.py` (the pure approval and
  instant-runoff counts), `poll_members.py` (the follow-role holders from the member list, and the recipient cap),
  `poll_post.py` (every REST call on a poll message and a ballot DM, with debounced edits), `poll_flow.py` (🗳️ starts a
  poll, 📅 and 🔁 on a closed poll, ballot replies, runoffs), `poll_close.py` (the one close path), `poll_commands.py`
  (`!poll`) and `poll_jobs.py` (the due closes and the halfway reminders).
  Cards: `cards.py` (store and dataclasses), `card_fields.py` (field validators), `card_commands.py` (`!card`
  subcommands), `resolver.py` (status from manager and probe results), `render.py` (embed, outside text and the
  summary board's payload), `text.py` (user-facing strings), `poller.py` (the single card poller, which also keeps
  the board and each card's subscription copies). The summary board's single-row store is `board.py`. Card
  subscriptions (copies of a card in other text channels) are stored by `subscriptions.py`.
  Reactions: `presses.py` (filters, rate limits and removals for presses; the only module that knows presses are
  reactions), `interactions.py` (the press handler), `seeding.py` (adds and removes the bot's own reactions),
  `pacer.py` (the per-channel reaction budget), `games.py` (the games community lookup and the bot's identity),
  `notify.py` (DMs to the admins and to one user; `dm_user` applies the DM backoff unless `bypass`), `interaction_settings.py` (the global switch, the per-card off
  switch and the degraded channels) and `follow_roles.py` (the only module that changes a role: the bell (U+1F514) follow toggle
  and each card's follow role).
  `wake.py` (wake presses, and closing a request on ready or expiry), `wake_requests.py` (the request store),
  `request_commands.py` (`!request list|clear`). `play_now.py` is the play-now store: one row per press, live until
  its expiry, then expired. The play-now send itself is `interactions.send_play_now`.
- `src/fluxer_panel/sources/`: `base.py` (result types and the `HostManager`/`Probe` protocols), `registry.py`
  (the kind-to-plugin map), `pterodactyl.py` (host manager), `minecraft.py` (Bedrock and Java probes), `a2s.py`
  (A2S probe), `_probe_util.py` (the shared guard and limits).
- `tests/`: pytest suite. `tests/fakes.py` holds the fake REST and gateway servers. `tests/fake_ptero.py` and `tests/fake_probes.py` hold the Panel and probe fakes. `tests/test_review_refs.py`
  and `tests/test_repo_hygiene.py` are the structural checks.
- `tools/live/`: hand-run live scripts and the shared harness `_live.py` (see Live tools).
- `docs/`: `fluxer-notes.md` (live-run observations, recorded against a server build). The panel brief is a
  private design document kept outside this repository; it is not in the tree and is not linked from it.
- `REVIEW.md`: review guide. Its references are checked by `tests/test_review_refs.py`.
- `.github/workflows/ci.yml`: the CI workflow.

## Commands (run from this directory)

- Setup: `uv venv -p 3.12 .venv` then `uv pip install --python .venv/bin/python -e '.[dev]'`
- Tests: `.venv/bin/python -m pytest -q -W error` (warnings are errors)
- Lint: `.venv/bin/ruff check src tests tools`
- Licensing: `.venv/bin/reuse --root . lint`
- Run: `.venv/bin/python -m fluxer_panel [run|purge]` (`run` is the default). `purge` runs one retention purge,
  writes the status file and exits without connecting to Fluxer.
- CI runs exactly these three checks on every push and pull request.
- Config comes from environment variables only (names in `config.py` and `.env.example`). The code never loads
  `.env`; the operator sources it in the shell.

## Rules

- This directory is public: it is published by git subtree split. Keep it self-contained.
- No secrets, IDs, hostnames or instance details in tracked files. Test fixtures use fake values
  (`"1001"`, `"https://fluxer.invalid"`). Never write a run of 15+ digits. `tests/test_repo_hygiene.py` fails on
  any IP address or URL or bare host outside its allowlist, so a new public name needs an allowlist entry there.
- Never read or print `.env`. `.env.example` is the only tracked env file.
- The private denylist is `docs/private/fluxer-activity-panel/denylist.txt` in the main checkout. `denylist_path`
  in `tests/test_repo_hygiene.py` finds it through the git common directory, so worktrees use the same file. It is
  gitignored and absent in the published split, so the check skips there. Never print its entries or copy them into
  a tracked file. Denylist findings name the path and line only.
- `tools/history_scan.py` is a hand-run check of the subtree split's full history against the same denylist and the
  hygiene rules. Run it from this directory with `.venv/bin/python tools/history_scan.py`. It checks commit messages,
  diff paths and every added or removed line, and prints a short commit sha and a kind per hit, never the matched
  text. Exit 0 is clean, 1 means hits, and 2 means refused (no denylist, or git cannot make the split). A hit in
  history is raised with the user before anything is rewritten. The scan never rewrites history.
  A commit-message line that starts with `Co-Authored-By:` or `Claude-Session:` is exempt from the host check for
  the exact assistant hosts in `ATTRIBUTION_HOSTS` only; every other check still applies to it.
- Bidi overrides, isolates and zero-width characters are refused in tracked text
  (`test_no_bidi_or_zero_width_characters_in_tracked_files`). A finding names the path and line, never the character.
- Every new file needs an SPDX header or a `REUSE.toml` entry, or `reuse lint` fails.
- Do not modify `../green-oaks-bot`. It is the source of the copied client code and is read-only here.
- Do not re-add the source bot's imports (card rendering, Pillow). `fluxer_client` was cut loose from them on purpose.

## REVIEW.md

`REVIEW.md` lists security- and privacy-sensitive spots as `path:line` references with the text expected on that
line. `tests/test_review_refs.py` checks every reference, so a code move or edit that shifts an anchor fails the test.
Any change that moves referenced code must update `REVIEW.md` in the same change.

## fluxer_panel rules

- Exit codes: 0 clean stop (a signal, or a gateway that returns without error); 1 fatal runtime or gateway error
  (`FatalGatewayError` or any uncaught exception, including a failure to open the DB that is not a refusal); 2 config
  or usage error; 3 database refused (`DatabaseRefused`: quarantined, schema newer or unavailable). The DB is opened
  before `asyncio.run`, so a refused DB exits before any network connection.
- Logging: panel code logs only through `log_event(logger, name, level=..., **fields)`. It never logs message content,
  card text, locations, addresses, hosts, probe details, user names, channel names or URLs. The database stores a
  verified voice channel's display name (`card_voice.channel_name`) and a verified text channel's display name
  (`card_text_channel.channel_name`). Those are channel names, not people's, and they are never logged.
  `clean_fields` allowlists fields: id keys must be snowflakes, other keys must be numbers, bools or
  identifier-like strings. Known exception: `__main__.main` calls `log.exception("fatal_error")`,
  and no code enforces the no-URL rule for traceback text there. `fluxer_client` records are plain text that only
  get token redaction and may contain URLs.
- `setup_logging` installs one JSON stdout handler with `TokenRedactionFilter`, which replaces the token (raw and
  repr- or json-escaped up to three nested levels, `_ESCAPE_ROUNDS`) in message, args and traceback. Deeper nesting
  is not covered. It also calls `logging.captureWarnings(True)`.
- Token handling: the token is `repr=False` in `Config`, config errors name the variable and never its value, and
  the token goes only in the `Authorization` header or the gateway identify/resume frames.
- Admin: `admin.authorize` is the single check for DM commands: DM only (no guild id), non-bot author, author id a valid snowflake, author id in
  `ADMIN_USER_IDS`. Every admin command goes through `handle`, which calls `authorize` first. Denials log `admin_denied`.
  `admin.is_admin_id` (the same id rule, with no DM requirement) is the only other check, used for 📅 and 🔁 on a poll message.
  The bot skips its own messages only. Every other message goes to `DmRouter.route` (`dm_router.py`), which sends guild
  messages, bot or non-snowflake authors and admin heads to `handle`. `handle` ignores anything that is not a command.
  Other DMs take the non-admin path (see Sessions rules). A command from a bot or from a guild channel reaches `authorize`, is refused and logged as `admin_denied`
  (ids only), and gets no reply, so nothing is posted in a guild channel.
- Only `follow_roles.py` calls the role methods (`add_member_role`, `remove_member_role`, `create_role`,
  `set_role_mentionable`), enforced by `tests/test_role_callers.py`. Each call is preceded by the allowlist check
  (`cards.follow_role_ids`) and the hierarchy check against freshly fetched roles, because Fluxer returns the same 403
  for a hierarchy refusal and a missing permission. The bot never deletes roles and never creates a role with
  permissions.
- The publish-time role step, a manual `!card set <slug> follow_role` and `!card clear <slug> follow_role` are serialised
  per card by their own lock (`follow_roles.FollowLocks`), separate from the card lock. That role step never runs under
  the card lock; the bell toggle's member-role calls do run under it, by design.

- Card config: `GAMES_CHANNEL_ID` (a snowflake, optional). `PTERODACTYL_URL` and `PTERODACTYL_API_KEY` are both or
  neither, and setting one refuses to start with exit 2. `POLL_INTERVAL_SECONDS`, `SCHEDULE_REFRESH_SECONDS`,
  `POLL_CONCURRENCY`, `PROBE_TIMEOUT_SECONDS` and `PROBE_GRACE_SECONDS` are positive integers with defaults 60, 600,
  8, 3 and 600. `README.md` lists every variable.
  `FLUXER_INSTANCE_URL` is at most 128 UTF-16 code units (`config.INSTANCE_URL_MAX`; an astral character counts two),
  which keeps each channel slot's field inside Fluxer's 1024-unit field limit. `FLUXER_API_URL` has no cap.
- Reaction config: `PLAY_NOW_CHANNEL_ID` (a snowflake, optional, never `GAMES_CHANNEL_ID`) and `FOLLOW_ROLE_PREFIX`
  (1-40 of `a-z0-9_-`, default `card_subscription_`; it becomes part of a visible role name). `WAKE_REQUEST_EXPIRY_SECONDS`,
  `PLAY_NOW_EXPIRY_SECONDS`, `PLAY_NOW_COOLDOWN_SECONDS`, `BOARD_REPOST_MIN_INTERVAL_SECONDS`,
  `PRESS_USER_WINDOW_SECONDS`, `PRESS_CARD_WINDOW_SECONDS` and `PRESS_CARD_BURST` are positive integers with defaults
  7200, 3600, 1800, 300, 10, 10 and 10.
- Session config: `SESSION_TIME_ZONE` is required (an IANA name). Without it a config error exits 2, for `run` and
  `purge` alike. `SESSION_SLOTS_WEEKDAY` and `SESSION_SLOTS_WEEKEND` take 1 to 6 distinct `HH:MM` times. The
  `SESSION_LENGTH_SECONDS`, `SESSION_RETENTION_SECONDS` and `DM_BACKOFF_SECONDS` positive integers have defaults.
  `README.md` lists every variable.

- Poll config: `POLL_LENGTH_SECONDS` and `POLL_MAX_LENGTH_SECONDS` are positive integers, and the maximum must be at least
  the length (exit 2 otherwise). `POLL_BALLOT_SIZE` is 1 to 20, `POLL_MAX_RECIPIENTS` is a positive integer, and
  `POLL_REMINDER` is `on` or `off`. `README.md` lists every variable.

## Cards and sources rules

- `!card` subcommands are `card_commands.SUBCOMMANDS` (new, set, clear, show, list, publish, archive, subscribe,
  unsubscribe, interactions, help), routed by `admin.handle` after `authorize`. Fields are `SETTABLE` and `CLEARABLE`;
  `README.md` documents syntax and fields. A card is `draft`, `live` or `archived` (`cards.CARD_STATES`); only a live
  card is posted, and only to `GAMES_CHANNEL_ID`. With that unset, a live card's active copies are still posted
  (`poller._publish`), so `!card publish` replies with one of three texts: `text.CARD_PUBLISHED` (a games channel is
  set), `text.PUBLISHED_COPIES_ONLY` (no games channel, at least one active copy) or `text.PUBLISHED_NO_CHANNEL`
  (no games channel and no active copy, so nothing is posted). Rows without a slug (v1 data) are never read or listed.
  `!card publish` on a card that is already live replies `text.ALREADY_LIVE` and runs the same follow-role step as a
  fresh publish (`_publish_follow`), so a card published before following existed gets its role and bell. A live card
  is not kicked, and the role step makes no call without a games channel. Its only effects are the stored role and
  the bell seeds that follow from it.
- `!request list` and `!request clear <slug>` are the admin head `request`, routed by `admin.handle` after `authorize`
  to `request_commands.handle`, and documented in `README.md`. `clear` closes the card's open rows as `cleared` through
  `WakeRequests.close`, so each requester gets one close DM.
- `!card help` lists the subcommands; `!card help fields` lists every field with limits, ranges and kinds taken from
  the code that enforces them (`card_fields`, `sources/registry.py`). `tests/test_card_help.py` checks they match and
  that each reply fits one message, so a new field or limit needs no hand edit of the help numbers but a new field
  needs a line in `text.HELP_FIELDS`. A manager plugin must give a ref hint for the help.
- Every send, edit and store for a card (its message, its copies) runs under that card's lock from
  `card_commands.CardLocks`, which `!card archive` also takes, so a card cannot be posted after it is archived.
  The exception is the reaction seeding after a post, which runs after the lock is released (see Reactions rules).
- A plugin's `power_state` and `probe` never raise. `validate_ref` raises only `CardFieldError`, and
  `next_power_action` may raise `SourceError`, which the caller hides.
- No library message, Panel body, player name or command payload leaves a plugin. Errors leave as fixed reason keys.
- The Pterodactyl module is GET-only over three read paths. `tests/test_pterodactyl_ast.py` enforces it.
- Every card send and edit uses `allowed_mentions={"parse": []}` and goes through `render`.
- `outside_location` takes a bool, never the location text.
- Card replies and card text live in `text.py`, which never says "guild". `tests/test_text.py` enforces it.
- `!card list` sends its lines in messages under `card_commands.LIST_CHUNK` UTF-16 units each (`render.units`), so a
  message stays under Fluxer's 2000-unit limit. `!card show`'s outside preview is fitted to the same limit in units.
- Adding a plugin is one module plus one line in `sources/registry.py`.
- Card subscriptions (`subscriptions.py`, migration 0005): a copy of the full card message in another text channel.
  Its payload is the card's own, from `render.card_message`, with `allowed_mentions={"parse": []}`. The poller posts,
  edits and reposts a copy through the card's `_sync` path, under the card's lock, after the card's own message. A
  failed post or edit of the card's own message is logged and the copies still run. A copy is never on the board, and
  a board link never points at one. `subscriptions.MAX_SUBSCRIPTIONS` (5) is checked in `subscriptions.add`, in the
  insert's transaction. A 403 on a copy's post or edit, or a 404 on its post (the channel is gone), pauses it and DMs
  the admins once: the pause changes a row only while it is active. The DM takes `text.SUBSCRIPTION_PAUSED_ALERT`
  (403) or `text.SUBSCRIPTION_CHANNEL_GONE_ALERT` (404), with the card slug and channel id as its only inputs. A 404
  on an edit reposts the copy. `!card subscribe` on a paused channel resumes it, which clears a 403 pause;
  `!card unsubscribe` clears a channel-gone pause. `!card archive` first cancels the card's upcoming sessions, under the card lock (their messages are edited and
  their attendees DMed), and closes its open poll with no winner (`_close_open_poll_locked`; a failed close is logged
  as `poll_archive_failed` and the archive goes on). It then deletes every copy first and the card's message last. A 404 counts as deleted. A 403 on a copy or on the card's own message leaves that message in place, the
  archive goes on, and the reply names each channel left: `text.CARD_ARCHIVED_SOME_LEFT` with one
  `text.ARCHIVE_LEFT_LINE` per channel. `text.CARD_ARCHIVED` is used only when nothing is left. Any other failure
  stops the archive while the card stays live, and the copies it already deleted are forgotten so the poller posts
  them again. `!card unsubscribe` removes the row whatever the delete outcome, and says so when the copy is left.
  Copies log ids and exception types only.
- Summary board (`render.board_message`, `board.py`, `poller.py`): one message, always on when `GAMES_CHANNEL_ID` is
  set. It is posted to that channel when none is stored, and then it stays in the channel it was posted in: a changed
  `GAMES_CHANNEL_ID` never moves it, as with cards. Its payload comes only from `render.board_message`, which escapes
  each title with `escape`, fits it to `LIMITS["board_title"]` and keeps the whole message under `BOARD_CHAR_LIMIT`
  UTF-16 code units (`render.units`). Its statuses are the cards' own status lines from the resolver, and a card whose
  evaluation failed shows the dash. A status is a link only through `jump_link`, and only for a card whose message is
  in the board's channel, so the community is the one looked up for that channel. Board sends and edits use
  `allowed_mentions={"parse": []}`. Cards always post first: each card is published right after its own evaluation,
  and the board is brought up to date after them, so a board posted with no stored message sits below them. A stored
  board is edited when its hash changed. The board stays the games channel's newest message: a board check runs at the
  end of each games-channel tick when it is armed, by a post in the board's channel, by a MESSAGE_CREATE there (the
  bot only arms it, `Poller.on_games_message`, and never kicks), and by startup. An armed check reads the newest message
  with `GET messages?limit=1`. When that isn't the board, a re-post is due `BOARD_REPOST_MIN_INTERVAL_SECONDS` after the
  last one, and the run loop's wait is cut short to reach it (`_wait_timeout`). The re-post posts the new board in the
  stored board's channel, stores it, then deletes the old one. The old message's own MESSAGE_DELETE causes no post. A
  failed delete is retried by each later check. Edits never arm the check. No admin is sent a pin request, and no DM
  goes out on a board post. A failed read of the stored board at the start of a tick is logged. The cards then publish as they would
  with a board stored, and no new board is posted in that tick (a stored board is still edited); the next tick
  posts it. The poller logs the board's ids and exception types only: never a title, a status or a link.

## Sessions rules

- Sessions are never posted to the games channel. `session_post.send_session` and `edit_session` raise
  `GamesChannelRefused` before any REST call. The recurrence posts through `send_session` to the session's own
  channel. `tests/test_games_channel_sessions.py` runs a full scenario and checks it.
- Session messages come only from `render.session_message`, and their `allowed_mentions` is `{"parse": []}`. The
  sender passes that field on unchanged.
- RSVP reactions (✅ going, ❔ maybe, ❌ no) on a session message are state. `presses.on_reaction` hands them to
  `Interactions.rsvp_changed` before the kill switch and the rate limit. They are never removed and never unseeded.
- 📅 and 🔁 on a poll message are routed by message id to `Interactions.poll_reaction` in `presses.on_reaction`, before
  the kill switch and the rate limit, as RSVPs are. Only an add counts. `PollFlow.on_poll_reaction` removes the
  presser's reaction first, then acts only for the poll's starter or an admin (`admin.is_admin_id`).
- The RSVP engine (`rsvp.apply`) decides every RSVP row. `sessions.write_rsvps` is the only code that rewrites `rsvps`
  after a session exists. `sessions.add` writes the proposer's first going row when the session is created, and
  `sessions.add_presence` replaces the implicit presence row of any user who reacts. Any user may hold an implicit row:
  the proposer's going, and each maybe that `sessions.add(..., maybe=...)` writes for a scheduled poll's voters. The
  reconcile drops the implicit row of every user with a reaction before it adds that user's real row.
- The non-admin paths are `dm_router` with `!session` (`session_commands.py`), `!poll`, and ballot replies. `NonAdminLimiter.admit`
  allows 5 DM commands, draft or ballot replies, or 📅 DM reactions per 60 seconds per non-admin user. Admins are exempt. A `!session`
  change needs the session's proposer, a `!poll` sort, shown, extend or close needs the poll's starter, and either
  may be done by `admin.authorize`. A guild message goes to `admin.handle` and never reaches the session path.
- DM backoff (`dm_backoff.py`, in memory): a user whose DM failed gets no ordinary DM for `DM_BACKOFF_SECONDS`.
  Promotion, cancel, move and series-stopped DMs, and the wake request close DMs (ready, expired and cleared), pass
  `bypass=True`. Admin DMs (`notify.dm_admins`) never use the
  backoff. A 📅 start is refused while the proposer is in backoff.
- A reaction with no guild id (a DM reaction) goes to `SessionFlow.on_dm_reaction` and never to `presses`.
- A `!session` change is one compare-and-set transaction under the card's lock that reads the session again; the
  message edit and the DMs run after the lock is released. The exceptions are `cancel_card_sessions_locked` (called
  by `!card archive` with the lock held) and `on_message_gone`, which takes no card lock. A 404 on a session edit
  (its message was deleted) cancels an upcoming session through that hook (`SessionPost.set_on_gone`).
- `session_jobs.SessionJobs.run` runs on every poller tick (session ends, the weekly series, the admins' heads-up
  `sessions.HEADSUP_WINDOW` before a session with people going, draft expiry), and its next due time shortens the
  poller's wait. A tick whose card pass did not finish runs it with `headsup=False`, because the heads-up reads the
  cached statuses; that heads-up stays pending and goes out on the next tick whose card pass finishes. Each job lists
  without a lock and acts under the card's lock. Known limitation: a weekly occurrence
  is stored at most once, but a lost send response can post a second visible message, which has no row.

## Polls rules

- Poll content is never posted to the games channel: `PollPost.send_poll`, `flush` and the closed-poll reactions
  (`seed_closed`, `unseed_closed`, `remove_reaction`) refuse it before any REST call. `tests/test_games_channel_polls.py`
  runs a full scenario and checks it.
- `poll_post.py` makes every REST call on the poll message (send, edit, delete, reactions) and sends each ballot DM
  through `notify.dm_user`. Every poll send and edit uses `allowed_mentions={"parse": []}`. Other poll DMs (reminders,
  scheduling links, refusals) go through `notify.dm_user`. `!poll` replies and vote answers go to the reply channel.
- Poll state is in the database. A change to a poll is one compare-and-set statement, guarded on the poll still being
  open or on the value it expects, in one transaction. A vote is parsed and written in the same transaction
  (`_vote_tx`).
- `poll_reply`, `poll_count` and `poll_letters` are pure (no database, REST or logging).
- A poll posts to the channel from `interactions.target_channel_id`, the rule sessions share: the card's verified
  play-now channel, else `PLAY_NOW_CHANNEL_ID`. `PollPost` requires the bot `identity`, because unseeding removes the
  bot's own reactions.
- Letters come from positions; only `!poll sort` moves positions; votes point at option ids. A poll has at most 702
  options (`polls.MAX_OPTIONS`), and a typed time that would exceed that refuses the whole reply (`POLL_FULL`).
- The member list is read once per poll start and once per runoff, in ids only. A failed read refuses the start
  (`poll_members.MembersUnavailable`).
- A card has at most one open poll (the `polls_open_once` index).
- A runoff closes at min(now + `POLL_LENGTH_SECONDS`, earliest candidate time - 1 h - 10 min). With under 15 minutes
  left, 🔁 is refused (`POLL_RUNOFF_TOO_SOON`) and the presser is told to use 📅. A runoff keeps the parent's starter,
  and its candidates are the top three usable times that have votes; fewer than two is refused. The README's `### Polls`
  states the same rule.
- 📅 and 🔁 exclude each other on one poll. `_schedule` refuses when `polls.child_of` finds a runoff (`reason=runoff_exists`), and
  `_runoff` refuses when `poll.session_id` is set (`reason=scheduled`). Each check runs under the poll's lock after the re-read,
  and each refusal DMs the presser a link to the other (the `_NO_LINK` text when the instance URL is unset). A runoff's own 📅 is unaffected.
- `!poll extend` on a runoff is capped too: `_extend_tx` refuses, and changes nothing, when the new close is past
  `min(option starts_at) - poll_count.LEAD_SECONDS - poll_flow.RUNOFF_CLOSE_MARGIN`. The reply is `text.POLL_EXTEND_RUNOFF_CAP`.
  Approval polls keep only the creation cap (`POLL_MAX_LENGTH_SECONDS`), which applies to runoffs as well.
- Poll logs carry `poll_id`, `card_id`, counts and reason keys only, never typed text, names or user ids.
- The card lock covers the post and insert of a poll, the close transaction and the `!poll` changes; DMs and edits run
  after it. `PollCloser.close_locked` is only for a caller that holds the card lock (archive). The 404 hook
  (`PollPost.set_on_gone`) runs after the poll's lock is released and ignores a poll that is not open.
- Ballot DMs and reminders obey `DmBackoff`. A reminder is marked before its DM goes out, so it is never sent twice, and
  a recipient in backoff is skipped without being marked. `polls.mark_reminded_if` also requires the poll to be open, so a
  close that commits first stops the reminder. Close-time changes are edits, not new DMs.
- A voter's maybe on a scheduled session is an implicit RSVP row, replaced by any real reaction.
- 📅 on a closed poll schedules the winner through `SessionFlow.publish` (the starter is the proposer, each voter for
  the winner a `maybe`, `notify_proposer=False`), so every session post still goes through `SessionPost`. It is refused
  under `poll_count.LEAD_SECONDS` before the winning time, and `polls.set_session_if_unset` links one session only.
- `PollJobs.run` (due closes, halfway reminders) runs on every poller tick after the session jobs, even when the tick
  raised (`Poller.set_poll_jobs`), and its `due` shortens the poller's wait. It never raises.

## Reactions rules

- `presses.py` is the only code that knows presses are reactions.
- Every reaction REST call in `fluxer_panel` goes through `ChannelPacer.wait()`, so each channel's reaction calls share
  one budget.
- Seeding (`seeding.py`; the poller's `_seed_posted` and `_seed_surface`): the poller seeds a card's message or a copy
  after posting it, never after an edit, and on reconcile it checks each stored card message and copy. It adds only the
  emoji that are missing and never removes one. Wanted: the wake emoji (U+25B6 U+FE0F) always; the follow emoji (bell,
  U+1F514) when the card has a follow role; the play-now emoji (U+1F64B) when the surface has a play-now target. The calendar (U+1F4C5) is wanted when the
  surface has a session channel: a card or a home copy, never a far copy or a play-now post
  (`seeding.has_session_channel`). A card
  message is always at home, and a copy is at home when its community is the games channel's. A card message's play-now
  target is its own verified play-now channel or `PLAY_NOW_CHANNEL_ID`; a home copy always has one, and a copy elsewhere
  has one when the card does. A copy elsewhere carries the bell and the raised hand too, but a press there makes no
  role call and no post: it gets a DM pointing at the card (`text.FOLLOW_ELSEWHERE`, `text.PLAY_NOW_ELSEWHERE`).
  Seeding runs only while `Seeder.enabled_for` allows it (the global switch on and the card not switched off), and
  `Seeder.seed` asks again before each add. It runs after the card's lock is released, so a reaction send is the one
  card call not under the lock. A failure is logged as `seed_failed` with the card id and the exception type, and the
  next surface is still seeded.
- The ballot box (🗳️, U+1F5F3 U+FE0F) is wanted after the calendar, on the same surfaces (`seeding.has_session_channel`).
  A poll message gets only 📅 and 🔁, from `PollPost.seed_closed`: both on a closed approval poll with a winner, and 📅
  alone on a closed runoff.
- `!card interactions on|off [slug]` (the kill switch, `interaction_settings`): off removes all reactions of the wake,
  follow, play-now, calendar and ballot-box emoji (`Seeder.unseed`: the bot's seeds and any users' left over) from every card message and
  active copy in scope; on re-seeds them. It never touches a poll message, and it closes no poll. It runs with
  no card lock held. Unseeding is the only place the bot removes its own seeded reactions.
- Press logs: a `press` log carries `event_type`, `card_id`, `action` and `surface`, and never a `user_id`. `user_id` is
  logged only on `press_absorbed`. Me Too on a play-now post is a state change, so it is never rate limited or removed.
- Play-now targets (`interactions._play_now_targets`): a press on the card posts once, in the card's target (its
  verified `play_now_channel`, else `PLAY_NOW_CHANNEL_ID`). A press on a home copy posts in the copy's channel and, when
  the card's target is another channel, in that too. Each post gets its own play-now row and seeds, and both count as
  one press for the cooldown. One failed post is logged (ids and status only) and the other is kept; the presser is
  DMed only when no post succeeds.
- Play-now posts are sent only by `interactions.send_play_now`, which refuses a games-channel target before any REST
  call (`GamesChannelRefused`). `PLAY_NOW_CHANNEL_ID` must differ from `GAMES_CHANNEL_ID`, or the bot refuses to start.
- A play-now post is the only send that may mention anyone: `interactions.play_now_mentions` allows the card's own
  follow role only, and only when it belongs to the target's community (`_role_in`); otherwise `{"parse": []}`. Never
  users, `@everyone` or another role. `REVIEW.md` anchors these lines.

## Database

- `db.py` owns the single SQLite connection. All DB work goes through `await db.run(fn)`: one transaction per call,
  on a single-thread executor.
- Startup order: read-only integrity and version check; quarantine on corruption (never reset to an empty DB);
  refuse a newer schema without touching the file; otherwise back up to `<db>.pre-v<N>` and apply pending migrations.
  The directory and file pre-checks (`_check_directory`, `_check_writable`) run before any lookup or `_inspect`,
  and the write probe in `_connect` stays authoritative.
- Primary codes READONLY, CANTOPEN, BUSY and PERM map to `DatabaseRefused("unavailable")` and leave the file alone.
  Only corruption codes quarantine. Quarantine moves files aside under new names and never overwrites.
- Card slots (migration 0007): `card_voice`, `card_text_channel` and `card_address` are each keyed by
  `(card_id, slot)`, slot 1-3, and each row has an optional `suffix` with a CHECK that mirrors `card_fields.suffix`.
  The 0007 rebuild moved each v6 voice and address row to slot 1 with no suffix. The field names are
  `card_fields.SLOT_FIELDS` (voice, voice2, voice3, text_channel, text_channel2, text_channel3, address, address2,
  address3), plus `SUFFIX_FIELDS` and `PUBLIC_FIELDS`. `text_channel` was `text_channel1`, which is refused with
  `text.FIELD_RENAMED` and never read as an alias. A suffix or public flag is written only on a slot with a value
  (`cards.set_suffix`, `set_address_public` report a miss); clearing a slot deletes its row, suffix and flag included,
  and never moves another slot. Each slot renders as its own embed field, labelled `render.slot_label`, in the order
  voice, text channel, address. A card embed stays within `render.EMBED_CHAR_LIMIT` (6000 units) and
  `EMBED_MAX_FIELDS`; at the worst case `render._fit_budget` shortens first_time, then description, then join.
- Card tables (migrations 0002, 0003; `card_seats` is from 0001): `cards` gains `slug`, `state` and the base fields;
  one slot table per optional field (`card_voice`, `card_text_channel` keyed by slot 1-3, `card_address` with
  `public`, `card_location`, `card_seats`, `card_manager`, `card_probe`) and `card_runtime` (rendered hash, probe
  alert state). Slot table names are fixed in `cards.SLOT_TABLES` and `cards.SLOT_KIND_TABLES`, never built from input.
  Hosts are never logged,
  and the outside text shows an address only when it is public (`render.outside_address`, the one helper for all
  three address slots).
- `card_subscription` (migration 0005) holds one row per card and channel, keyed by `(card_id, channel_id)`, and cascades
  with its card. `message_id` and `content_hash` are set together or not at all, and a hash only with a message.
  `paused` is 0 or 1. Its writes are keyed by card and channel: `set_hash_if` and `clear_message_if` are compare-and-set,
  and `pause` and `resume` change a row only in the state they expect. The cap of five is app-level, not a CHECK.
- `board` (migration 0004) holds one row, `id = 1`, seeded by the migration. Its `channel_id` and `message_id` are set
  together or not at all, and `content_hash` is set only with a message. The board's writes are compare-and-set, as
  the cards' are: `clear_message_if`, and `set_hash_if` for the hash.
- Reaction tables (migration 0006): `requests` is rebuilt with statuses `open`, `ready`, `expired` and `cleared`, and a
  partial unique index `requests_open_once` keeps one open row per card and requester. New tables: `wake_request_dm`
  (the admin DMs about a request), `card_follow_role` (`role_id` unique: a role follows one card), `card_play_now_channel`,
  `play_now`, `interactions` (one row, the global switch), `card_interactions_off` and `reaction_degraded`.
- Session tables (migration 0008): `sessions` is rebuilt, since migration 0001 created it with `group_role_id` and no state
  columns. Its `state` is `upcoming`, `ended` or `cancelled`, and `closed_at` is set exactly when it is not upcoming.
  It also has `recurrence`, `series_id`, `next_posted` and `moved`. `rsvp_reaction` holds the presence rows (`implicit = 1`
  for the proposer's going and a scheduled poll's voters' maybes), and `rsvps` is rebuilt as the computed state (`waitlist_pos` for a waiting user).
  Both `rsvps` and `rsvp_reaction` cascade with their session.
- Schema: every table is `STRICT`. Ids are `TEXT` with CHECKs that they are digits only and at most 25 long; names are
  not stored, except a verified voice or text channel's display name (`card_voice.channel_name`,
  `card_text_channel.channel_name`). The snowflake bound is 25 in code and schema; Fluxer documents at most 19, so do
  not tighten without a new migration.
- Poll tables (migration 0009, schema 9): `polls` (`kind` is `approval` or `runoff`, `state` is `open` or `closed`,
  `outcome` is `winner`, `none` or `removed`; `parent_poll_id` links a runoff; `session_id` is `ON DELETE SET NULL`),
  `poll_options` (positions and `starts_at` unique per poll; `origin` is `offered` or `typed`), `poll_ballots` (one per
  recipient, with `answered`, `reminded` and the DM ids), and `poll_votes` (one row per pick, with `rank` for a runoff).
  The indexes are `polls_open_once` (one open poll per card), `polls_message`, `polls_state_closes`, `polls_parent`,
  `poll_votes_rank`, `poll_votes_option` and `poll_ballots_user`. Every table is `STRICT`, and the ids are checked as
  digits, as elsewhere.
- Migrations: `src/fluxer_panel/migrations/NNNN_name.sql`, numbered and contiguous from `0001`. Never edit a migration
  after release; add a new file instead. Do not put `BEGIN`, `COMMIT` or `PRAGMA user_version` in a file: the runner adds them.
- `secure_delete = ON` is set, so purged rows do not survive in free pages. `.pre-v<N>` backups are not covered by that
  and keep rows purged before they were taken.

## Retention purge (`purge.py`)

- A session is due when it has been ended or cancelled (`closed_at`) for more than `SESSION_RETENTION_SECONDS` (default
  7 days), except a weekly occurrence whose successor isn't posted yet. In `run`, each due session's message is first
  edited to its closed form without the location (the scrub). Then the rows are deleted, even if the edit failed
  (`session_scrub_failed` is logged with the session id). The `purge` subcommand has no network, so it leaves due
  sessions in place and reports `sessions_deferred` (status file schema 2). Upcoming sessions are never purged.
  RSVP rows go with their session by `ON DELETE CASCADE`.
- A request expires when `created_at` is more than 30 days ago, whatever its status.
- A play-now post expires when `created_at` is more than 30 days ago, in either state.
- A closed poll is deleted when it was closed more than `SESSION_RETENTION_SECONDS` ago, in the same transaction as the
  sessions. Its options, ballots and votes go by cascade, with no REST call, so the `purge` subcommand deletes them too.
  The count is `deleted.polls` in the status file.
- Cutoffs are strict: a row exactly at a cutoff is kept. The due sessions are read first, in their own transaction. Each
  due session is then scrubbed, outside any transaction. Every delete runs in one transaction that re-checks the due
  rule (`sessions.PURGE_DUE_SQL`), and a WAL checkpoint follows.
- `run` purges at startup and then every `PURGE_INTERVAL_SECONDS`. A failed purge logs `purge_failed`, is recorded
  in the status file, and the bot keeps running.
- The status file (`PURGE_STATUS_PATH`, default `DATA_DIR/purge-status.json`) is written atomically, mode 0644, and
  holds only timestamps and counts. A checkpoint failure after the commit is reported as `ok: false` with zero counts,
  though the rows may already be gone.
- `purge` subcommand exit codes: 0 success, 1 purge failed, 2 config error, 3 database refused.

## Live tools (`tools/live/`)

- Hand-run only. CI never runs them, and they need a local `.env`.
- The scripts that use `live()` may only use the private guild. `check_safety` refuses unless the channel is in the guild and the user is a member.
  It also refuses if the guild reports more than two members (no refusal when the count is not reported).
- Never print the token or raw ids. `Redactor` replaces the token with `[REDACTED]`, labels known ids and replaces any
  other 15+ digit run with `<id>`. Extra secrets (a Panel URL, a probe host) are replaced too, whatever the case of the match, and `label_text` gives any
  other value a stable label. `out()` is the only print path. Error output is the exception type and redacted message.
- Exit codes: 0 pass; 1 any FAIL or error; 2 refusal; 130 Ctrl+C.
- Scripts wait with `wait_for_enter()` (a daemon-thread read), never `input()`, so Ctrl+C still exits 130.
  Run them in a real terminal, not a non-interactive prompt.
- Scripts: `reactions.py`, `edit.py`, `reaction_remove.py`, `roles.py`, `dm.py`. Removal is exercised only in a guild channel;
  DM removal is untested (see `docs/fluxer-notes.md`).
- Reaction scripts, run by hand against the private guild: `seed_reactions.py` (the wake, bell and raised-hand seeds,
  their echo and the stored names; it does not cover the calendar), `role_ping.py` (a test role's `mention_roles`, the role's mentionable flag and the above-the-bot 403,
  which needs `LIVE_HIGH_ROLE_ID`; it never deletes a role), `board_last.py` (`last_message_id` and `messages?limit=1` after
  deletes; it deletes only its own test messages) and `reaction_bulk.py` (reactor paging, bulk removal and a rate probe).
- Card and Panel scripts, run by hand. Each prints only redacted labels:
  - `ptero_live.py` (needs `PTERODACTYL_URL`, `PTERODACTYL_API_KEY` and `LIVE_PTERO_SERVERS`): for each server, the bot
    user's permissions, the power state and the filtered schedules. A schedule name is printed as its length only.
    It flags any permission other than `schedule.read` or `websocket.connect`. It does not connect to Fluxer.
  - `probes_live.py` (needs `LIVE_PROBES`, a comma-separated list of `kind:host:port`): each target's counts, or its
    failure reason. Hosts and ports are never printed, and a map name is printed as its length only (`map_len=N`),
    since map names are operator-chosen. Version and game mode are printed as given. It does not connect to Fluxer.
  - `markup_live.py` (needs `LIVE_VOICE_CHANNEL_ID` and `LIVE_OTHER_GUILD_ID`, a community the bot is not in): posts
    test messages to `LIVE_CHANNEL_ID` for the timestamp, channel-link, masked-link, jump-link, board-sample, mention,
    edit-burst and escaping checks (the jump check posts a target and a masked link to it; the board sample posts six
    placeholder cards with fake statuses, and the operator judges it on desktop and web), reads the voice channel type, the posting text channel's type and the other community's status, then
    deletes its messages. The channel-link check builds its bare link, masked link and `<#...>` mention for `LIVE_VOICE_CHANNEL_ID`, the channel the card's
    Voice field links to, because a link to the posting channel does nothing when clicked. The instance URL's forms
    (the URL as given and as normalised, each host in its decoded and punycode spellings, with host:port and
    [host]:port, from `endpoint_secrets`) are redacted from every printed line, the operator's typed NOTE included,
    whatever the case of the match. The operator judges the rendering on the web and mobile clients.
- The `LIVE_*` variables are listed in `.env.example`, with `LIVE_PTERO_SERVERS` (comma-separated 8-hex server ids),
  `LIVE_PROBES`, `LIVE_VOICE_CHANNEL_ID` and `LIVE_OTHER_GUILD_ID` commented out.
