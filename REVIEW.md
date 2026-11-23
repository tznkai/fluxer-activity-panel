<!-- SPDX-FileCopyrightText: 2026 K. Chen -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Review guide

Each security- and privacy-sensitive spot is listed as a reference: the path, the line, the text expected on that line, and why it matters. `tests/test_review_refs.py` checks every reference against the code, so a reference that no longer points at its anchor fails the test. Moving code means updating this file in the same change.

Paths are relative to this directory.

## Token handling

- `src/fluxer_panel/config.py:21` `_TOKEN = re.compile(` — the token must be printable ASCII with no spaces, because it is placed in an `Authorization` header.
- `src/fluxer_panel/config.py:27` `Message names the variable, never its value.` — configuration errors name the variable, so a bad token is reported without being echoed. This is the docstring's intent; the enforcement is in the raise lines below.
- `src/fluxer_panel/config.py:142` `raise ConfigError("FLUXER_TOKEN must be printable ASCII without spaces")` — the bad-token message states the rule and names the variable, and it never includes the token.
- `src/fluxer_panel/config.py:43` `raise ConfigError(message) from None` — a URL that fails to parse raises the generic message, and the parser's own text (which may echo the value) is dropped.
- `src/fluxer_panel/config.py:91` `token: str = field(repr=False)` — the token is left out of `repr(Config)`, so a logged or printed config does not show it.
- `src/fluxer_client/rest.py:102` `self._headers = {"Authorization": f"Bot {token}"}` — the only place REST sends the token. It goes in a header, never in a URL or a log line.
- `src/fluxer_client/gateway.py:135` `"token": self.token, "session_id"` — the resume frame (op 6) carries the token.
- `src/fluxer_client/gateway.py:173` `"token": self.token,` — the identify frame (op 2) carries the token.
- `src/fluxer_panel/__main__.py:214` `print(f"Configuration error: {exc}"` — one of the two panel prints (the other is the usage line below). It writes to stderr a configuration message, which names variables and never values.
- `src/fluxer_panel/__main__.py:209` `print(USAGE, file=sys.stderr)` — the second panel print, for a bad command line. It writes the fixed `USAGE` text to stderr, which names no values.
- `src/fluxer_panel/__main__.py:216` `setup_logging(config.log_level, [s for s in (config.token, config.pterodactyl_api_key) if s])` — the token and the Pterodactyl key are handed to the redaction filter before anything can log.
- `src/fluxer_panel/__main__.py:231` `last resort: log (redacted) and exit 1` — the last-resort catch-all logs through the redacting handler and returns 1. No raw traceback reaches stderr.

## Logging

- `src/fluxer_panel/logs.py:18` `ID_KEYS = frozenset(` — the only keys whose values may be logged as ids. `clean_fields` keeps one only if it is a snowflake.
- `src/fluxer_panel/logs.py:19` `OTHER_KEYS = frozenset(` — the other keys that may be logged. Numbers and bools pass. Strings must match `_SAFE_TOKEN`.
- `src/fluxer_panel/logs.py:25` `_SAFE_TOKEN = re.compile(` — string values for other keys must look like identifiers, so free text such as names or message content cannot be logged through them.
- `src/fluxer_panel/logs.py:29` `def clean_fields(` — the allowlist. Any key not listed in `ID_KEYS` or `OTHER_KEYS` is dropped.
- `src/fluxer_panel/logs.py:66` `_ESCAPE_ROUNDS = 3` — the token is redacted when it is repr'd or json-escaped up to three nested levels. Deeper nesting is not covered.
- `src/fluxer_panel/logs.py:79` `def token_variants(` — builds the raw and escaped forms of the token that the filter replaces.
- `src/fluxer_panel/logs.py:98` `class TokenRedactionFilter` — the handler-level backstop. It replaces the token in the message, the args and the traceback before formatting.
- `src/fluxer_panel/logs.py:118` `a malformed call must not raise` — a malformed log call is caught inside the filter, so it cannot raise into the code that logged it.
- `src/fluxer_panel/logs.py:142` `handler.addFilter(redaction)` — the filter is installed on the one JSON handler that `setup_logging` creates.
- `src/fluxer_panel/logs.py:150` `logging.captureWarnings(True)` — Python warnings go through logging, so they are JSON lines and are redacted too.
- `src/fluxer_client/events.py:26` `session_id: str = field(repr=False)` — the gateway session id from `Ready`, which with the token enables a session resume, is kept out of reprs.
- `src/fluxer_client/events.py:42` `content: str = field(repr=False)` — message text is kept out of reprs and therefore out of debug output.
- `src/fluxer_client/events.py:263` `log.warning("malformed %s payload: field %s", t, exc.field_name)` — a malformed payload is logged with the field name only.
- `src/fluxer_client/events.py:267` `field "?"` — the catch-all logs a placeholder, not the exception text, which may carry the value.
- `src/fluxer_client/gateway.py:76` `log.warning("gateway connection failed: %r", exc)` — a plain-text `fluxer_client` record. Its repr can include the instance host in stdout logs. That is acceptable because logs are not tracked, but reviewers should know it is there. Token redaction still applies.
- `src/fluxer_panel/poller.py:856` `log_event(log, "subscription_failed", level=logging.WARNING, card_id=card_id, channel_id=channel_id,` — a failed copy is logged with its card id, channel id and exception type only. No title, slug, channel name or message text is logged.

## Admin authorization

- `src/fluxer_panel/admin.py:22` `def authorize(` — the single authorization check. Four conditions follow.
- `src/fluxer_panel/admin.py:25` `event.guild_id is None` — admin commands work in DMs only. A command in a guild channel is refused.
- `src/fluxer_panel/admin.py:26` `not event.author_is_bot` — bot authors are refused.
- `src/fluxer_panel/admin.py:27` `is_snowflake(event.author_id)` — the author id must be a valid snowflake before it is compared.
- `src/fluxer_panel/admin.py:28` `event.author_id in config.admin_user_ids` — exact membership in the configured admin list.
- `src/fluxer_panel/admin.py:32` `def is_admin_id(` — the admin check for a guild reaction, where the DM-only rule of `authorize` cannot apply: a snowflake id that is exactly in the configured admin list.
- `src/fluxer_panel/admin.py:61` `if not authorize(event, config):` — every admin subcommand passes through this check in `handle`.
- `src/fluxer_panel/admin.py:62` `log_event(log, "admin_denied"` — a refusal is logged with the user and channel ids only.
- `src/fluxer_panel/bot.py:65` `if msg.author_id == self.user_id:` — the bot skips its own messages, so its replies cannot loop. Every other message goes to `DmRouter.route`. Guild messages, bot or non-snowflake authors and admin heads still reach `admin.handle`, where `authorize` refuses a command from a bot or a guild channel.
- `src/fluxer_panel/poll_commands.py:165` `return event.author_id == poll.starter_id or admin.authorize(event, self._config)` — a poll change (sort, shown, extend, close) needs the poll's starter or an admin. A `!poll vote` is a ballot reply and needs neither.

## Role assignment

`src/fluxer_panel/follow_roles.py` is the only code in `fluxer_panel` that calls the role methods (`add_member_role`, `remove_member_role`, `create_role`, `set_role_mentionable`). A role change is a privilege change, so every check lives in that one module, and `tests/test_role_callers.py` fails if another module makes one of these calls. Fluxer answers a hierarchy refusal and a missing permission with the same 403, so the checks run before the call, never as a reaction to the 403.

- `src/fluxer_client/rest.py:66` `def _check_reason(` — the audit reason is fixed, printable ASCII, and at most 128 characters. Caller text must never reach it.
- `src/fluxer_client/rest.py:71` `audit reason must be 1-128 printable ASCII characters` — the refusal happens before any request is sent.
- `src/fluxer_client/rest.py:254` `def _role_path(` — builds the role URL from ids that have passed the snowflake check.
- `src/fluxer_client/rest.py:255` `/members/{require_snowflake(user_id)}"` — the user id is validated as a snowflake in the path.
- `src/fluxer_client/rest.py:258` `async def add_member_role(` — a PUT that grants a role. This is a privilege change, so the role allowlist and the role hierarchy must be checked before the call. Fluxer answers both a hierarchy refusal and a missing permission with the same 403 code.
- `src/fluxer_client/rest.py:262` `async def remove_member_role(` — a DELETE that removes a role. The same checks apply.
- `tools/live/roles.py:32` `await rest.add_member_role(guild, user, role, reason=REASON)` — the live tool's call, which runs only in the private guild.
- `src/fluxer_panel/follow_roles.py:113` `stored.role_id not in allow` — `toggle_follow` re-reads the card's stored role and the allowlist at press time. A role not in the allowlist is refused before any REST call.
- `src/fluxer_panel/follow_roles.py:129` `not manageable(` — the hierarchy check in `toggle_follow`, against freshly fetched roles and the bot's own member record, runs before the add or remove.
- `src/fluxer_panel/follow_roles.py:265` `if not zero_permissions(role):` — the adopt branch of `ensure_follow_role` refuses any role with permissions other than 0, so the bot never writes to a role that grants something.
- `src/fluxer_panel/follow_roles.py:273` `await deps.rest.set_role_mentionable(` — an adopted role is made mentionable only after every check passes, and nothing is stored if the call fails.
- `src/fluxer_panel/follow_roles.py:285` `deps.rest.create_role(` — the only role creation in the bot. The REST client fixes its permissions at 0.
- `src/fluxer_panel/follow_roles.py:295` `await deps.rest.set_role_mentionable(` — a created role is made mentionable and checked before its id is stored.
- `src/fluxer_client/rest.py:306` `"permissions": "0"` — every role the client creates has no permissions, and there is no parameter that changes it.
- `tests/test_role_callers.py:33` `assert files == {"follow_roles.py"}` — the structural test that fails when any module other than `follow_roles.py` uses a role method.

## Role creation

- `src/fluxer_panel/follow_roles.py:285` `created = await deps.rest.create_role(` — the only role creation in the bot. The role is created with the name, and its permissions are fixed at 0 by the REST client.
- `src/fluxer_client/rest.py:306` `"permissions": "0"` — every role the client creates has no permissions, and there is no parameter that changes it.
- `src/fluxer_panel/follow_roles.py:265` `if not zero_permissions(role):` — the adopt branch refuses any existing role with permissions other than 0 before it writes to it.
- `src/fluxer_panel/follow_roles.py:267` `if not manageable(role, roles, bot_roles, guild_id):` — the adopt branch's hierarchy check, against freshly fetched roles, runs before the PATCH.
- `src/fluxer_panel/follow_roles.py:310` `if role_id == guild_id:` — a manual set refuses the community's `@everyone` role, whose id is the guild id.
- `src/fluxer_panel/follow_roles.py:230` `async with deps.locks.get(card.id):` — the publish and the manual set take the card's own role lock, not the card lock. A second role step on the card waits and then reuses the role the first one stored, so it never creates a second role.

## Reaction removal

- `src/fluxer_client/rest.py:243` `def _reaction_path(` — builds the path from snowflake-checked ids and an encoded emoji.
- `src/fluxer_client/rest.py:250` `async def remove_reaction(` — a DELETE of one user's reaction. Fluxer requires Manage Messages for it. Removal has been exercised only in a guild channel. The DM case is not covered (see the phase 1 section).
- `tools/live/reaction_remove.py:80` `await rest.remove_reaction(channel, message_id, emoji, user)` — the only live exercise of removal.
- `tools/live/_live.py:224` `LIVE_CHANNEL_ID is not in LIVE_GUILD_ID` — the safety check that makes that live removal guild-only.

## Reactions and presses

- `src/fluxer_panel/presses.py:174` `if event.guild_id is None or event.user_id == self._identity.user_id or event.user_is_bot:` — DM reactions, the bot's own reactions and another bot's reactions never reach the press adapter.
- `src/fluxer_panel/presses.py:190` `in POLL_ACTION_EMOJI:` — 📅 and 🔁 on a poll message go to the poll flow before the kill switch and the rate limits, and only an add counts; a removal reaches nothing, because the poll flow removes the presser's reaction.
- `src/fluxer_panel/poll_flow.py:246` `if user_id != poll.starter_id and not admin.is_admin_id(user_id, self._config):` — a 📅 or 🔁 on a poll message acts only for the poll's starter or a listed admin; anyone else's is removed and does nothing.
- `src/fluxer_panel/poll_flow.py:283` `runoff = await self._db.run(lambda c: polls.child_of(c, poll_id))` — 📅 on a poll refuses when a runoff was started from it, checked under the poll's lock, so 📅 and 🔁 on one poll cannot both act.
- `src/fluxer_panel/poll_flow.py:325` `if view.poll.session_id is not None:` — 🔁 refuses when the winner is already scheduled, checked under the same lock as the 📅 path's check above it.
- `src/fluxer_panel/presses.py:207` `if found is None or not found.switched_on:` — the kill switch, the global one and the card's own, is read before any rate limit, so a switched-off surface absorbs nothing and counts nothing.
- `src/fluxer_panel/presses.py:254` `if last is not None and now - last < self._config.press_user_window_seconds:` — the per-user window. A repeated press from one user inside it is absorbed.
- `src/fluxer_panel/presses.py:262` `if len(times) >= self._config.press_card_burst:` — the per-card burst limit. A card's presses past the burst are absorbed.
- `src/fluxer_panel/presses.py:320` `await self._pacer.wait(event.channel_id)` — the reaction removal waits for the channel's shared budget before the call.
- `src/fluxer_panel/presses.py:321` `await self._rest.remove_reaction(event.channel_id, event.message_id, event.emoji.name, event.user_id)` — after a press is handled, the user's reaction is removed, so the button resets for the next tap.
- `src/fluxer_panel/presses.py:324` `newly = await self._db.run(lambda c: interaction_settings.mark_degraded(c, channel, int(now)))` — a removal refused with 403 marks its channel degraded, and the admins are DMed once. A later press retries the removal at most once a day, and a success clears the mark.

## Play-now mentions

- `src/fluxer_panel/interactions.py:57` `return {"parse": []}` — a play-now message with no follow role mentions nobody.
- `src/fluxer_panel/interactions.py:58` `return {"parse": [], "roles": [role_id], "replied_user": False}` — the play-now role ping. Only the card's own follow role can be mentioned, and only when `_role_in` has matched it.
- `src/fluxer_panel/interactions.py:64` `return follow.role_id if follow is not None and follow.guild_id == guild_id else None` — the follow-role community check: a role from another community is never mentioned.
- `src/fluxer_panel/interactions.py:342` `if channel_id == self._config.games_channel_id:` — the play-now guard in `send_play_now`. A play-now message is never sent to the games channel.
- `src/fluxer_panel/interactions.py:343` `raise GamesChannelRefused` — the refusal happens before any REST call.

## Purge deletion

- `src/fluxer_panel/sessions.py:222` `PURGE_DUE_SQL = ("state <> 'upcoming' AND closed_at < ?` — the expiry rule, shared by the due list and the delete's re-check. A session expires when it has been ended or cancelled (`closed_at`) for more than the session retention (7 days by default).
- `src/fluxer_panel/sessions.py:222` `state <> 'upcoming' AND closed_at < ?` — an upcoming session (`closed_at` NULL) never matches, so it is never purged, however old its end.
- `src/fluxer_panel/sessions.py:223` `AND NOT (recurrence IS 'weekly' AND next_posted = 0)` — keeps a weekly occurrence whose successor is not posted yet, so its purge cannot end the series. `IS` is NULL-safe, so a non-recurring session (recurrence NULL) is not kept by this test and is still purged.
- `src/fluxer_panel/purge.py:44` `def _purge_tx(` — one transaction: count the RSVPs of the due sessions, delete the due sessions, delete the old requests and play-now posts, and delete the closed polls past retention. The session delete re-checks the due rule, so a row that changed since it was read is kept.
- `src/fluxer_panel/purge.py:57` `DELETE FROM sessions` — deletes the due sessions. Their RSVPs go with them by cascade.
- `src/fluxer_panel/purge.py:58` `DELETE FROM requests` — deletes requests older than 30 days, whatever their status.
- `src/fluxer_panel/purge.py:81` `await scrub(session)` — the scrub runs before each due session's delete, so the edit is sent while the row exists. A failed scrub is logged with the session id, and the delete still runs.
- `src/fluxer_panel/purge.py:99` `def write_status(` — the status file is written atomically and holds only timestamps and counts, and the deferred count.
- `src/fluxer_panel/purge.py:120` `await db.checkpoint()` — a WAL checkpoint after each run. A checkpoint failure after the commit is reported as `ok: false`, though the rows may already be gone.
- `src/fluxer_panel/purge.py:148` `log.exception("purge_loop_error")` — the per-iteration catch-all. A failing iteration is logged, and the loop goes on.
- `src/fluxer_panel/__main__.py:159` `purge_task = asyncio.create_task(purge_loop(` — the purge task and the poller start only once the gateway task exists, so a stop that wins discovery never starts either.
- `src/fluxer_panel/__main__.py:160` `scrub=session_post.scrub,` — the `run` purge gets the session scrubber. The `purge` subcommand passes none, so it defers due sessions instead of deleting them.
- `src/fluxer_panel/__main__.py:178` `gw_task.cancel()` — `run()`'s finally cancels the gateway task first, so the session never closes under a live reconnect.
- `src/fluxer_panel/__main__.py:183` `poller_task.cancel()` — the card poller is cancelled and awaited after the gateway task and before the purge task, so no card write is in flight when the session closes.
- `src/fluxer_panel/__main__.py:190` `purge_task.cancel()` — the purge task is cancelled and awaited on every exit, after the poller.
- `src/fluxer_panel/migrations/0008_sessions.sql:11` `REFERENCES cards(id) ON DELETE RESTRICT` — a card that still has sessions cannot be deleted.
- `src/fluxer_panel/migrations/0008_sessions.sql:23` `state IN ('upcoming', 'ended', 'cancelled')` — the session states the purge reads.
- `src/fluxer_panel/migrations/0008_sessions.sql:29` `CHECK ((state = 'upcoming') = (closed_at IS NULL))` — a session has a close time exactly when it is no longer upcoming, so `closed_at` is the purge's clock.
- `src/fluxer_panel/migrations/0008_sessions.sql:47` `REFERENCES sessions(id) ON DELETE CASCADE` — RSVPs are removed with their session.

## Emoji and path encoding

- `src/fluxer_client/rest.py:49` `_CUSTOM_EMOJI = re.compile(` — custom emoji must be `name:id`. The name is letters, digits and underscore, and the id is numeric.
- `src/fluxer_client/rest.py:50` `_EMOJI_FORBIDDEN = (` — characters that could change the path are refused: slash, question mark, hash, dot-dot, percent and backslash.
- `src/fluxer_client/rest.py:53` `def encode_emoji(` — the only reaction path encoder. Unicode is percent-encoded. Custom emoji pass through after their check.
- `src/fluxer_client/rest.py:63` `return quote(emoji, safe="")` — `safe=""` encodes every reserved character, so a unicode emoji cannot add a path segment.
- `src/fluxer_client/ids.py:10` `_SNOWFLAKE = re.compile(` — snowflakes are 1 to 25 digits. The documented maximum is 19 (see the phase 1 section).
- `src/fluxer_client/ids.py:20` `raise ValueError("not a snowflake id")` — the rejected value never reaches the exception text, because it may be user-controlled.

## DB quarantine and backups

- `src/fluxer_panel/db.py:24` `SIDE_FILES = ("-wal", "-shm", "-journal")` — a quarantined database takes its side files with it, including a hot `-journal`.
- `src/fluxer_panel/db.py:60` `_UNAVAILABLE_CODES = frozenset(` — BUSY, READONLY, CANTOPEN and PERM mean this process cannot use the file right now. The panel refuses as `unavailable` and leaves the file alone. Corruption codes are the only ones that quarantine.
- `src/fluxer_panel/db.py:79` `def _check_directory(` — refuses unless the data directory is readable, searchable and writable. This runs before any lookup in the directory.
- `src/fluxer_panel/db.py:93` `def _check_writable(` — pre-checks the existing database file before SQLite creates any side file.
- `src/fluxer_panel/db.py:109` `def _inspect(` — the startup check, through a read-only connection. Any error that is not corruption is refused as `unavailable`.
- `src/fluxer_panel/db.py:117` `?mode=ro", uri=True)` — the inspection connection is opened read-only.
- `src/fluxer_panel/db.py:127` `def _write_probe(` — proves the file can be written and its lock taken. It writes the same `user_version` and always rolls back.
- `src/fluxer_panel/db.py:143` `def _quarantine(` — moves the damaged file and its side files aside under new names. Nothing is overwritten.
- `src/fluxer_panel/db.py:162` `except PermissionError:` — a data directory the process cannot create is `unavailable`. Other `mkdir` errors stay exit 1.
- `src/fluxer_panel/db.py:169` `path.parent.glob(glob.escape(path.name)` — after a quarantine, a missing database with a `.corrupt*` file beside it is refused. The panel never silently replaces it with an empty database.
- `src/fluxer_panel/db.py:171` `log_event(log, "db_quarantine_pending"` — the refusal for the case above.
- `src/fluxer_panel/db.py:182` `raise DatabaseRefused("schema_newer")` — a newer schema is refused, and the file is not touched.
- `src/fluxer_panel/db.py:188` `_primary_code(exc) in _UNAVAILABLE_CODES` — a connection failure with an unavailable code becomes the `unavailable` refusal.
- `src/fluxer_panel/db.py:208` `conn.execute("PRAGMA secure_delete = ON")` — deleted rows are overwritten on disk, so purged data does not survive in free pages. Backup copies are not covered (see the next reference).
- `src/fluxer_panel/db.py:220` `next_free_path(path, f".pre-v{pending[0][0]}")` — the pre-migration backup. A `.pre-v<N>` backup keeps the rows that were purged before it was taken. Rotating those backups is a deployment concern, not a code one.
- `src/fluxer_panel/db.py:226` `# the backup's directory entry must survive` — the backup's directory is fsynced before the migration commits.

## Live-tool redaction

- `tools/live/_live.py:27` `LONG_NUMBER = re.compile(` — any 15-plus-digit run that is not a known id is replaced with `<id>`.
- `tools/live/_live.py:33` `_redactor: Redactor | None = None` — set as soon as the token is loaded. `main()` reads it when it reports an error.
- `tools/live/_live.py:91` `class Redactor:` — maps known ids to labels and replaces the token and each extra secret (a Panel URL, a probe host, an instance URL and its host forms) with `[REDACTED]`, matching regardless of case.
- `tools/live/_live.py:129` `return LONG_NUMBER.sub("<id>", s)` — the last step: any remaining long digit run.
- `tools/live/_live.py:153` `def log_handler(` — the stderr log handler formats the whole record, message and traceback, through the redactor.
- `tools/live/_live.py:168` `def out(` — the only print path for the live scripts.
- `tools/live/_live.py:217` `async def check_safety(` — refuses unless the channel is in the guild and the user is a member. It also refuses if the guild reports more than two members (no refusal when the count is not reported).
- `tools/live/_live.py:229` `LIVE_USER_ID is not a member of LIVE_GUILD_ID` — a 404 on the membership check refuses. A mistyped user id never gets a DM.
- `tools/live/_live.py:240` `"Authorization": f"Bot {env['FLUXER_TOKEN']}"` — the version capture sends the token in a header and prints nothing from it.
- `tools/live/_live.py:254` `def gateway_failure(` — a gateway failure reports only the exception's type name. Its message is never printed, since it may echo the token or a payload.
- `tools/live/_live.py:386` `def error_redactor(` — resolved at the moment an error is printed, so a redactor set up later in the run still applies.
- `tools/live/_live.py:405` `f"ERROR {type(exc).__name__}: {exc}"` — error output is the type and the redacted message only, with no traceback.

## .env handling

- `.gitignore:10` `.env` — ignores the real env file, so a local `.env` is not committed by a plain `git add`.
- `.gitignore:11` `.env.*` — ignores every env file variant.
- `.gitignore:12` `!.env.example` — the one tracked env file, the blank template.
- `tests/test_repo_hygiene.py:26` `SECRET_GLOBS = (` — the denylist of key and PEM file names. The hygiene test lists tracked names and never opens them.
- `tests/test_repo_hygiene.py:30` `def is_secret(` — the check that a tracked name is a secret.
- `tools/live/_live.py:40` `def read_env_file(` — the live tool's own `KEY=VALUE` parser. It does no interpolation. Only the live tool reads a `.env` file.
- `tools/live/_live.py:61` `**read_env_file(env_file)` — values from the real environment override values from the `.env` file.
- `src/fluxer_panel/config.py:136` `def from_env(` — the panel reads environment variables only. It never opens `.env`; the operator sources the file in their shell.

## Published-tree hygiene

- `tests/test_repo_hygiene.py:149` `ALLOWED_IPS = frozenset(` — the only addresses a tracked file may contain are the loopback and unspecified ones.
- `tests/test_repo_hygiene.py:150` `ALLOWED_HOSTS = frozenset(` — the only named hosts allowed: reserved documentation names, the license host, Fluxer's public docs host and two test placeholders.
- `tests/test_repo_hygiene.py:168` `URL_AUTHORITY = re.compile(` — every network-scheme URL host in a tracked file is checked against the allowlist.
- `tests/test_repo_hygiene.py:171` `DOTTED_IPV4 = re.compile(` — every dotted IPv4 address, zero-padded or not, is checked against the allowlist.
- `tests/test_repo_hygiene.py:178` `BARE_NAME = re.compile(` — a bare dotted name is checked only when it ends in a listed TLD, so file names and Python attribute access are ignored.
- `tests/test_repo_hygiene.py:234` `def network_offenders(` — the scan. Secrets are skipped before any read, and a finding names the file, line and kind, never the value.

## Pterodactyl read-only access

- `src/fluxer_panel/sources/pterodactyl.py:33` `_ENDPOINTS = ("", "/resources", "/schedules")` — the only three paths the plugin reads: server details, `/resources` and `/schedules`.
- `src/fluxer_panel/sources/pterodactyl.py:32` `_SERVER_ID = re.compile(r"[0-9a-f]{8}")` — a server id must be eight lowercase hex digits before it reaches a URL.
- `src/fluxer_panel/sources/pterodactyl.py:97` `async with self._session.get(` — the one request call in the module, and it is a GET.
- `src/fluxer_panel/sources/pterodactyl.py:98` `allow_redirects=False` — a redirect is not followed, so the key is never sent to another host.
- `src/fluxer_panel/sources/pterodactyl.py:77` `"Authorization": f"Bearer {api_key}"` — the key goes only in this header, never in a URL or a log line.
- `src/fluxer_panel/sources/pterodactyl.py:126` `details = await self._get(ref, "")` — a 409 on `/resources` falls back to the details endpoint, the only read that reports suspension.
- `src/fluxer_panel/sources/pterodactyl.py:107` `status = resp.status` — only the status code is kept from an error reply. The error body is never read, so Panel text cannot reach an error message.
- `tests/test_pterodactyl_ast.py:43` `def test_no_write_or_socket_method_is_called` — the AST test that the module calls no write, socket or websocket method.
- `tests/test_pterodactyl_ast.py:73` `def test_endpoints_is_the_three_read_paths` — the allowlist test: `_ENDPOINTS` is exactly the three read paths, and every `_get` call uses one of them.

Residual risk: the Panel forces the `websocket.connect` permission onto the client key, and that permission allows reading the server console. The plugin never opens a websocket, and the AST test above keeps that true. The key itself still carries that scope, so it is accepted in the design and recorded here. Rotating the key is the remedy if it is ever exposed.

## Schedule payloads

- `src/fluxer_panel/sources/pterodactyl.py:184` `power_payload = (` — a task payload is kept only when the action is `power` and the payload is one of the four power kinds. Every other payload is dropped at parse time, so command text and backup names never leave the parser.
- `src/fluxer_panel/sources/pterodactyl.py:57` `name: str = field(repr=False)` — the schedule name is Panel text. It is kept out of `repr`, and nothing logs it.

## Probe details

- `src/fluxer_panel/sources/base.py:93` `def sanitize_detail(` — strips Minecraft colour codes and control characters, collapses whitespace and caps the result, so a detail is printable and short before a card shows it.
- `src/fluxer_panel/sources/base.py:89` `DETAIL_MAX = 48` — the length cap that `sanitize_detail` applies.
- `src/fluxer_panel/sources/_probe_util.py:30` `len(value) > RAW_MAX` — a string longer than `RAW_MAX` before sanitising is an oversized reply. It becomes `malformed`, and it is not truncated.
- `src/fluxer_panel/sources/minecraft.py:23` `async_status(tries=1)` — the Bedrock probe makes one request, so a silent server costs one request.
- `src/fluxer_panel/sources/minecraft.py:43` `async_status(tries=1)` — the same single request for the Java probe.
- `src/fluxer_panel/sources/minecraft.py:40` `mcstatus has already built the player sample` — mcstatus builds the Java player sample before this code runs. It is discarded right after parse and never read.
- `src/fluxer_panel/sources/a2s.py:30` `game_mode=None,` — python-a2s does not parse the game mode, so the A2S probe never reports one.
- `src/fluxer_panel/logs.py:24` `PINNED_LOGGERS = ("a2s", "mcstatus")` — the probe libraries' loggers are pinned to WARNING, because a2s logs raw packets at DEBUG.

## Plugin boundary

- `src/fluxer_panel/sources/registry.py:29` `MANAGER_PLUGINS: dict` — the host-manager kinds, each with its factory and the ref it expects. Adding a kind is one line here.
- `src/fluxer_panel/sources/registry.py:32` `PROBES: dict[str, Probe]` — the probe kinds, each mapped to one fixed instance.
- `src/fluxer_panel/sources/registry.py:49` `if manager is not None:` — a factory that returns None (the manager is not configured) registers nothing, so the plugin is unreachable.
- `src/fluxer_panel/sources/base.py:65` `class HostManager(Protocol):` — the host-manager contract. `validate_ref` raises only `CardFieldError`, and `power_state` never raises.
- `src/fluxer_panel/sources/base.py:81` `class Probe(Protocol):` — the probe contract. A probe returns a result or `ProbeFailed` and never raises.
- `src/fluxer_panel/sources/_probe_util.py:35` `async def _guard(` — every probe runs under this guard, which maps each failure to a fixed reason.
- `src/fluxer_panel/sources/_probe_util.py:48` `never let a library message escape` — the catch-all. A library's error text is never returned.

## Card rendering

- `src/fluxer_panel/render.py:87` `def neutralise(` — breaks the mention forms (`@everyone`, `<@id>`, `<#id>`) with a zero-width joiner, so card text cannot ping anyone or form a mention.
- `src/fluxer_panel/render.py:33` `_MD = re.compile(` — the markdown control characters that `escape` backslashes.
- `src/fluxer_panel/render.py:92` `def escape(` — the escape for all card text: neutralise first, then escape markdown. Every user text passes through it before it enters an embed.
- `src/fluxer_panel/render.py:122` `def code(` — a code span. Backticks are removed, because they cannot be escaped inside one.
- `src/fluxer_panel/render.py:103` `def fit(` — cuts escaped text to a limit in UTF-16 units, the unit Fluxer counts in, without leaving a dangling backslash that would escape the ellipsis.
- `src/fluxer_panel/render.py:340` `"allowed_mentions": {"parse": []}` — every card send and edit carries empty `allowed_mentions`.
- `src/fluxer_panel/render.py:374` `def outside_location(has_location: bool)` — takes a bool, so the location text cannot reach the outside line.
- `src/fluxer_panel/render.py:270` `fit(escape(channel.channel_name)` — a voice or text channel's name is escaped before it enters the field, so a name cannot form a link or a mention.
- `src/fluxer_panel/render.py:350` `def _outside_channel(` — a channel name on the outside line is mention-neutralised, and the line shows it only when the link is verified.
- `tests/test_render.py:705` `SENTINEL_LOCATION not in outside` — the test that the location text never appears in the outside rendering.
- `src/fluxer_panel/render.py:132` `def channel_link(` — the only place a channel link is built. Both ids pass the snowflake check first.
- `src/fluxer_panel/render.py:238` `suffix=escape(suffix)` — a slot's suffix is validated card text (`card_fields.suffix`), and it is escaped again before it enters the field name, as a second layer.
- `src/fluxer_panel/render.py:363` `def outside_address(address: Address, public: bool) -> str:` — the single helper that decides, for every address slot, whether the outside line shows the host or only says the address is in Fluxer. The host is shown only when `public` is true, with or without a suffix.
- `src/fluxer_panel/render.py:407` `lines.append(outside_address(address, address.public))` — the outside text sends each of the three address slots through that helper and has no other path to a host.
- `src/fluxer_panel/render.py:422` `if address is not None and address.public:` — the play-now and session messages (both through `_join_fields`) show an address slot only when that slot is public.
- `tests/test_render_slots.py:141` `assert LAN_HOST not in outside` — the leak test: a private address in any one of the three slots never appears outside Fluxer.
- `src/fluxer_panel/render.py:249` `def _fit_budget(` — keeps a card embed within 6000 UTF-16 units by shortening first_time, then the description, then join. No slot, label or link is cut.
- `tests/test_text.py:27` `"guild" in node.value.lower()` — the scan that fails on any user-facing string that mentions a guild.

- `src/fluxer_panel/render.py:140` `def jump_link(` — the only place a masked jump link is built. Its ids pass the snowflake check first.
- `src/fluxer_panel/render.py:286` `fit(escape(card.title), LIMITS["board_title"])` — a board cell's title is escaped before it is fitted, so a title cannot ping anyone or form a link.
- `src/fluxer_panel/render.py:310` `card.channel_id == games_channel_id` — a board status is a jump link only for a card whose message is in the board's channel, so the link's community is the one looked up for that channel, and no link leaves it.
- `src/fluxer_panel/render.py:303` `return {"embeds": embeds, "allowed_mentions": {"parse": []}}` — the summary board message carries empty `allowed_mentions`.
- `src/fluxer_panel/poller.py:638` `payload = render.card_message(embed)` — the card's own message and a home copy are sent with this near payload, whose `allowed_mentions` is `{"parse": []}`. A copy mentions nobody, the same as the card.
- `src/fluxer_panel/poller.py:640` `far_payload = render.card_message(far_embed)` — a far copy is sent with this payload instead. It is the same card with the next session's jump link left out, and it has the same `allowed_mentions`, so a far copy mentions nobody either.
- `src/fluxer_panel/poller.py:724` `home = games_guild is not None and sub.guild_id == games_guild` — the copy's community decides which payload it gets. A failed games-guild lookup (`None`) counts as far, so a copy gets the link-free rendering unless its community is confirmed to be the games community.

## Card commands

- `src/fluxer_panel/admin.py:65` `if head == "card":` — the `!card` branch runs only after `authorize` has passed, so card commands share the DM-only, admin-only check.
- `src/fluxer_panel/cards.py:42` `super().__init__(f"{field}: {rule}")` — `CardFieldError` carries a field name and a rule key. The submitted value is never in the message.
- `src/fluxer_panel/card_fields.py:63` `def suffix(field: str, value: str) -> str:` — the suffix validator. Suffixes are card text and will come from members later, so they are untrusted: composed to NFC, trimmed of spaces only (a newline is refused, not trimmed), runs of spaces collapsed, then 1 to 20 characters. A refusal names the field and the rule, never the value.
- `src/fluxer_panel/card_fields.py:79` `def _suffix_char(` — the allowlist: Unicode letters, decimal digits and `SUFFIX_MARKS` (space and - + . '). Everything else is refused, parentheses, backticks, markdown, `@`, `<`, `#`, controls, format characters, bidi controls and emoji included, and so are the Hangul filler letters (`_BLANK_LETTERS`), which render blank.
- `src/fluxer_panel/migrations/0007_card_slots.sql:19` `suffix NOT GLOB` — the schema's second layer for suffixes, on all three slot tables: length 1-20, trimmed, no double space, and no ASCII control or punctuation other than - + . '.
- `src/fluxer_panel/card_commands.py:243` `raise CardFieldError(field_name, "no_slot")` — a suffix is refused on an empty slot. The store's `set_suffix` writes only to a slot that still has a value, so a clear that lands first leaves no suffix behind.
- `src/fluxer_panel/cards.py:399` `table = SLOT_KIND_TABLES[kind]` — the suffix write's table name comes from a fixed map, never from input. An unknown kind raises before any SQL.
- `src/fluxer_panel/card_fields.py:36` `RENAMED_FIELDS = {"text_channel1": "text_channel"}` — the old field name is refused with a reply naming the new one. It is never read as an alias.
- `src/fluxer_panel/card_commands.py:353` `exc.status in (403, 404)` — a 403 or a 404 from `get_guild` means the bot is not in that community. Both get the same "unverified" reply.
- `src/fluxer_panel/card_commands.py:366` `if channel.get("type") != channel_type:` — the channel must be the type its slot takes: a voice channel for the voice slot, a text channel for a text slot.
- `src/fluxer_panel/card_commands.py:45` `TEXT_CHANNEL_TYPE = 0` — the text channel type, the same value as Discord's. It was confirmed on a live instance: `tools/live/markup_live.py` printed type 0 for the posting channel.
- `src/fluxer_panel/card_commands.py:368` `str(channel.get("guild_id")) != guild_id` — the channel must belong to the community the admin named.
- `src/fluxer_panel/card_commands.py:614` `cards.set_state(conn, card_id, "archived", now)` — archive keeps the card row and its slots. It clears the message and sets the state, so the card can be read again.
- `src/fluxer_panel/card_commands.py:719` `return text.SUBSCRIPTION_NOT_IN_COMMUNITY` — a subscription is refused unless the bot is in the community. `_check_channel` answers a 403 or 404 on the community lookup with an unverified link, and a subscription never stores one.

## Card poller

- `src/fluxer_panel/poller.py:335` `log_event(log, "card_poll_failed"` — one card's failure is logged with the card id and the exception type only. The other cards still run.
- `src/fluxer_panel/poller.py:785` `await self._rest.edit_message(channel_id, message_id, embeds=` — the edit sends embeds and `allowed_mentions` and never `content`, so an edit cannot erase the message text.
- `src/fluxer_panel/poller.py:375` `cards.set_probe_tracking(c, card.id` — the alert state is saved before the DM goes out. A crash or a failed DM cannot send the alert a second time.
- `src/fluxer_panel/poller.py:377` `await self._alert(card)` — the admin DM that follows that save.
- `src/fluxer_panel/poller.py:828` `for kind, slots, channel_type in (("voice", card.voices, VOICE_CHANNEL_TYPE),` — the name refresh covers every verified link in all three voice slots and all three text channel slots, each checked against the channel type its slot takes.
- `src/fluxer_panel/poller.py:425` `cards.set_voice_name_if(c, card_id` — a refreshed voice name is written only if that voice slot still holds the link that was read, so an admin's change during the lookup wins.
- `src/fluxer_panel/poller.py:427` `cards.set_text_channel_name_if(c, card_id, slot` — the same compare-and-set for a text channel, keyed by slot: a name is written only while that slot still holds the link that was read.
- `src/fluxer_panel/poller.py:414` `event = "voice_refresh_failed" if kind == "voice"` — a failed name check is logged with the card id and the exception type only. No channel name reaches the log.
- `src/fluxer_panel/poller.py:802` `cards.set_hash_if(c, card_id` — the rendered hash is stored only if the message is still the same one, so a delete during an edit is not written back.
- `src/fluxer_panel/poller.py:55` `_DELETED_CAP = 256` — the remembered deletes are bounded. The oldest entries are evicted, which covers the window between a send and its commit.
- `src/fluxer_panel/poller.py:816` `self._deleted[(channel_id, message_id)] = None` — a repeated delete keeps its place, so eviction is by insertion order.
- `src/fluxer_panel/__main__.py:183` `poller_task.cancel()` — the poller is cancelled and awaited before the purge task, so no card write is in flight when the session closes.

- `src/fluxer_panel/poller.py:599` `log_event(log, "board_sync_failed"` — a failed board post or edit is logged with the exception type only, and the next tick retries it. No title or status is logged.
- `src/fluxer_panel/poller.py:631` `board.set_hash_if(c, channel_id` — the board hash is stored only while the board still holds that message, so a delete during an edit is not written back.
- `src/fluxer_panel/poller.py:839` `if cards.clear_message_if(conn, channel_id, message_id) is not None:` — a MESSAGE_DELETE forgets a card message, a card's copy or the board message, in one transaction. Message ids are unique, so only one of the three can match.
- `src/fluxer_panel/poller.py:588` `channel_id = state.channel_id if state.channel_id is not None else games_channel_id` — a stored board is edited in the channel it was posted in, so a change of `GAMES_CHANNEL_ID` never moves it. Its community is looked up for that channel.
- `src/fluxer_panel/poller.py:583` `if state.message_id is None and not may_post:` — when this tick's first read of the stored board failed, no new board is posted in that tick, so a board is never posted while the stored state is unknown.
- `src/fluxer_panel/poller.py:727` `await self._sync(card_id, post_channel=sub.channel_id, edit_channel=sub.channel_id,` — a copy's post and edit run the card's own sync path, so each send carries the card's `allowed_mentions`. One copy's failure is caught here and does not stop the others.
- `src/fluxer_panel/poller.py:750` `await self._dm_admins(alert.format(slug=slug, channel_id=channel_id),` — the pause DM takes a fixed text, the card's slug (a-z, 0-9 and -) and a channel id. No title or other card text reaches it, and the channel is shown as a mention.
- `src/fluxer_panel/text.py:194` `SUBSCRIPTION_PAUSED_ALERT = (` — the 403 pause DM's text. Its placeholders are the card's slug and the channel id, and nothing else.
- `src/fluxer_panel/text.py:197` `SUBSCRIPTION_CHANNEL_GONE_ALERT = (` — the 404 pause DM's text. Its placeholders are the card's slug and the channel id, and nothing else.

## Private denylist

- `tests/test_repo_hygiene.py:484` `return Path(common).parent / DENYLIST_RELATIVE` — the denylist lives in the main checkout's `docs/private/`, found through the git common directory, so a worktree reads the same file. The path is computed and the file is not opened here.
- `tests/test_repo_hygiene.py:512` `offenders.append(f"{path.relative_to(root)}:{number}")` — a finding is the path and the line only. The denylisted literal is never printed. Secret files are skipped before any read.
- `tests/test_repo_hygiene.py:560` `pytest.skip("private denylist absent")` — where the denylist is absent, as in the published split repository, the test skips.

## Session time parser

- `src/fluxer_panel/session_time.py:56` `if len(parts) != 2:` — a typed time is exactly a day word or date and a clock, so free text cannot ride along inside the time.
- `src/fluxer_panel/session_time.py:18` `_TIME = re.compile(` — the clock is 24-hour, `H:MM` or `HH:MM`, and it must match the whole token (`fullmatch`).
- `src/fluxer_panel/session_time.py:70` `raise TimeRefused("weekday_today")` — a weekday name for today is refused rather than read as the next week.
- `src/fluxer_panel/session_time.py:76` `raise TimeRefused("date") from None` — an impossible date is refused, and the parser's own message is dropped.
- `src/fluxer_panel/session_time.py:40` `if back != wall:` — a wall time that does not exist (the spring-forward gap) is refused as `gap`.
- `src/fluxer_panel/session_time.py:42` `if aware.utcoffset() != aware.replace(fold=1).utcoffset():` — a wall time that occurs twice (the fall-back fold) is refused as `fold`, so a session never lands on the wrong hour.
- `src/fluxer_panel/session_time.py:33` `later.astimezone(timezone.utc) - earlier` — elapsed time is taken in UTC. Python subtracts two datetimes with the same zone by wall clock, which is an hour off across a clock change.
- `src/fluxer_panel/session_time.py:83` `if elapsed > HORIZON:` — a time more than 60 days out is refused. The comparison is strict, so a time exactly at the horizon is accepted.

## Session purge scrub

- `src/fluxer_panel/purge.py:79` `for session in due:` — the scrub loop in `run`. Every due session is visited before any row is deleted.
- `src/fluxer_panel/purge.py:81` `await scrub(session)` — the scrub edits the message to its closed form without the location, while the row still exists.
- `src/fluxer_panel/purge.py:83` `"session_scrub_failed"` — a failed scrub is logged with the session id and the exception type only.
- `src/fluxer_panel/purge.py:86` `_purge_tx(conn, now, session_ids=session_ids` — the delete runs for every due id after the loop, even when a scrub failed. The delete re-checks the due rule, so a row that changed since it was read is kept.
- `src/fluxer_panel/__main__.py:160` `scrub=session_post.scrub,` — the `run` purge is the only caller that gets the scrubber.
- `src/fluxer_panel/__main__.py:200` `scrub=None,` — the `purge` subcommand passes no scrubber, so due sessions stay in place and are reported as deferred.

## Waitlist promotion

- `src/fluxer_panel/rsvp.py:58` `and state[u] == "going"]` — a user who is already going stays going, so a later change in the list cannot move them.
- `src/fluxer_panel/rsvp.py:61` `newcomers = sorted(` — a new reaction is queued by the order it was added. A newcomer who finds the seats full joins the back of the waiting list.
- `src/fluxer_panel/rsvp.py:69` `while waiting and (seats is None or len(going) < seats):` — the promotion loop fills free seats from the front of the waiting list.
- `src/fluxer_panel/rsvp.py:72` `if state[u] == "waiting":` — `promoted` lists only users who were waiting before this run. A newcomer who takes a free seat is not reported as promoted.
- `src/fluxer_panel/sessions.py:344` `DELETE FROM rsvp_reaction WHERE session_id` — a user's first real reaction replaces their implicit row (the proposer's going, or a poll voter's maybe), so both rows never coexist.
- `src/fluxer_panel/session_post.py:147` `async def dm_promoted(` — one promotion DM per promoted user. It bypasses the DM backoff.

## Session channel guard

- `src/fluxer_panel/session_post.py:40` `if channel_id == self._config.games_channel_id:` — `send_session` checks the games channel before any REST call.
- `src/fluxer_panel/session_post.py:41` `raise GamesChannelRefused` — the refusal for a session post.
- `src/fluxer_panel/session_post.py:71` `if session.channel_id == self._config.games_channel_id:` — `edit_session` makes the same check against the session's stored channel.
- `src/fluxer_panel/session_post.py:72` `raise GamesChannelRefused` — the refusal for a session edit.
- `src/fluxer_panel/session_jobs.py:253` `send_session(session.channel_id, payload)` — a recurrence posts the next occurrence to the session's own channel, which the guard above keeps out of the games channel.

## Poll channel guard

- `src/fluxer_panel/poll_post.py:104` `if channel_id == self._config.games_channel_id:` — `send_poll` checks the games channel before any REST call.
- `src/fluxer_panel/poll_post.py:105` `raise GamesChannelRefused` — the refusal for a poll send.
- `src/fluxer_panel/poll_post.py:112` `if channel_id == self._config.games_channel_id:` — `delete_unstored` refuses the games channel before any REST call.
- `src/fluxer_panel/poll_post.py:249` `if view.poll.channel_id == self._config.games_channel_id:` — `_refused` makes the same check against a stored poll's channel, for every flush, closed-poll seed, unseed and reaction removal (`remove_reaction`).
- `src/fluxer_panel/poll_post.py:331` `if self._refused(view):` — `_edit_marked` (run by `_flush_locked`) refuses a stored games channel before any edit.
- `src/fluxer_panel/poll_post.py:130` `sent = await notify.dm_user(self._rest, user_id, content, backoff=self._backoff)` — a ballot DM goes through the DM helper, which sends empty mentions and honours the DM backoff.
- `src/fluxer_panel/poll_post.py:375` `allowed_mentions={"parse": []})` — the ballot edit carries empty `allowed_mentions`.

## Non-admin DM path

- `src/fluxer_panel/dm_router.py:138` `if event.guild_id is not None:` — a guild message goes to `admin.handle`, which refuses a command and gives no reply. Nothing from a guild channel reaches the session path.
- `src/fluxer_panel/dm_router.py:141` `if event.author_is_bot or not is_snowflake(event.author_id):` — a bot or non-snowflake author goes to `admin.handle`, which refuses it and logs it.
- `src/fluxer_panel/dm_router.py:144` `if _is_help(event.content, self._config.prefix):` — `help` and `!help` are answered only after the guild and bot checks, so the list is sent to a DM and never to a guild channel. A non-admin's help goes through the same limiter, and the admin heads are listed only when `admin.authorize` passes.
- `src/fluxer_panel/dm_router.py:64` `def admit(self, user_id: str) -> bool:` — `NonAdminLimiter.admit` allows 5 commands, replies or DM reactions per 60 seconds per non-admin user.
- `src/fluxer_panel/dm_router.py:65` `if user_id in self._config.admin_user_ids:` — admins are exempt from the limit.
- `src/fluxer_panel/dm_router.py:151` `if not self._limiter.admit(event.author_id):` — a `!session` command from a non-admin is rate limited before it is parsed.
- `src/fluxer_panel/dm_router.py:156` `poll_head = _head(event.content, self._config.prefix, "poll")` — a `!poll` command from a non-admin is admitted through the same non-admin limiter as `!session` (the admit call is the next branch), before it is parsed or reaches the poll commands.
- `src/fluxer_panel/dm_router.py:163` `if admin.parse_head(event.content, self._config.prefix) is not None:` — admin heads (`!admin`, `!card`, `!request`) go to `admin.handle` unchanged, and `authorize` checks them there.
- `src/fluxer_panel/session_flow.py:223` `if not self._limiter.admit(user):` — a 📅 reaction in a DM uses the same limiter.
- `src/fluxer_panel/session_commands.py:147` `return event.author_id == session.proposer_id or admin.authorize(event, self._config)` — a session change needs the proposer or an admin.

## DM backoff

- `src/fluxer_panel/dm_backoff.py:19` `def blocked(self, user_id: str) -> bool:` — the backoff is in memory. A user whose DM failed gets no further DM for the backoff window (`DM_BACKOFF_SECONDS`).
- `src/fluxer_panel/notify.py:34` `and not bypass and backoff.blocked(user_id):` — the skip. It is logged as `user_dm_skipped` with reason `backoff`, and no request is made.
- `src/fluxer_panel/notify.py:28` `bypass: bool = False)` — the promotion, cancel, move and series-stopped DMs, and the wake request close DMs (ready, expired and cleared), pass `bypass`, so they go out even while the user is in backoff.
- `src/fluxer_panel/notify.py:18` `async def dm_admins(` — admin DMs never take the backoff path, so an admin is always told.
- `src/fluxer_panel/session_flow.py:161` `if self._backoff.blocked(user):` — a 📅 start is refused while the proposer is in backoff, before any DM is sent.

## What the polls prompt got wrong

This section compares the polls brief and plan with what was built. Each item was checked against the code.

- **Session time parser:** the brief assumed the parser enforced the one-hour minimum. `session_time.parse` takes the
  zone, raises `TimeRefused`, and has no minimum. Only the offered slots and the weekly series apply one. Polls check the
  minimum themselves, for typed replies and before 📅 schedules a winner.
- **Follow role storage:** the follow role is the `card_follow_role` table, with `role_id` unique, not a column on the
  card. Polls read it through `cards.get_follow_role`.
- **Poll to session link:** `polls.session_id` needed `ON DELETE SET NULL`. Without it, purging a session that a poll
  points at would fail against that reference.
- **Session post hook:** `SessionPost.set_on_gone` holds one callback, and a second registration replaces the first.
  Polls do not register on it. A poll message has its own hook, `PollPost.set_on_gone`.
- **Admin check for reactions:** `admin.authorize` is DM only, so it cannot check a guild reaction. A 📅 or 🔁 on a poll
  message uses `admin.is_admin_id`, which has the same id rule without the DM rule.
- **Implicit RSVP rows:** the proposer's going row was the only implicit row. A scheduled poll also gives each voter a
  maybe row, so the reconcile removes a user's implicit row before it adds their real reaction, for any user who has one.
  The reconcile's own comment gives the reason: the two rows can share a key.
- **Runoff close:** a runoff gets a fresh poll length, which could outlast its candidates, and then an earlier candidate
  would no longer be usable when the runoff closed. Its close is capped at an hour and a margin before its earliest
  candidate, and a runoff with under 15 minutes left is refused.
- **Member list:** reading the community's member list to find the follow role's holders is new to this bot. Its response
  shape is checked only on a live server, in the member list check of the live poll checks.

## What the phase 4 prompt got wrong

This section compares the phase 4 brief and plan with the design and with what planning and execution found. The design's "Out of date in the prompt" list is the source for the first six items. The others come from planning and from the checks, and each one was checked against the code.

- **DM backoff and footer hint:** the prompt assumed an existing DM-failure backoff and footer hint. Neither existed. Both are new in this phase.
- **Sessions and RSVP tables:** phase 1's `sessions` and `rsvps` tables had `group_role_id` and no message or state columns. They were rebuilt, not extended.
- **The `purge` subcommand:** it has no REST, so it cannot scrub. It defers due sessions and reports them as `sessions_deferred`. Only the running bot scrubs and deletes them.
- **Failed scrubs:** a failed scrub edit is mostly a 404 (`UNKNOWN_CHANNEL` or `UNKNOWN_MESSAGE`), not a 403. A failure is logged, and the delete still runs.
- **Open-ended series:** the brief's rule that an open-ended series is never purged is replaced by a per-occurrence purge. A weekly occurrence whose successor is not posted yet is kept, so its purge cannot end the series.
- **Outcome section:** the reactions design has no "Outcome" section. Only the cards design does.
- **Reaction names:** `▶` (U+25B6) is wake and `🙋` (U+1F64B) is play-now. The 📅 seed is a fourth seeded emoji, after wake, bell and raised hand, and collides with neither.
- **Card status:** status is computed in memory from the stored fields and the probe results. It is never stored, so there is no status column to migrate.
- **Location limit:** the limit is 200 characters, from `card_fields.TEXT_LIMITS["location"]`.
- **The `moved` column:** migration 0008 adds `moved`, which records a time change, so the "Time changed" line survives later edits to the same session.
- **The purge keep rule:** the rule was first written as `recurrence = 'weekly'`. For a session with no recurrence that test is NULL, so `NOT (...)` was NULL and a non-recurring session could be kept past retention. The bug was found in Phase 7. The test uses the NULL-safe `IS`.
- **Elapsed time across a clock change:** two aware datetimes with the same zone subtract by wall clock, so an elapsed time across a DST change was an hour off. The time parser converts both values to UTC first.

## What the phase 2 prompt got wrong

This section compares the phase 2 brief and plan with what the design and the implementation decided. The design's "Deviations from the phase 2 prompt" section is the source for the first items. The others come from the implementation and its checks, and each one was checked against the code.

- **Source slots:** the prompt had one status source with an optional query. The design uses two independent slots, a host manager and a probe. `none` is no longer a query type. It means the probe slot is empty, and a probe-only card is allowed and shows "Not responding" on failure.
- **Query types:** the test servers are Bedrock, so a Java-only `minecraft` query was wrong for them. The query types are `minecraft_bedrock`, `minecraft_java` and `a2s`.
- **Probe details:** opt-in map, version and game mode details replace counts only.
- **Java names:** mcstatus builds the Java player sample into objects before panel code sees it, so the rule for Java is that the sample is discarded right after parse. For Bedrock and A2S the rule is that names are dropped at parse time.
- **Probe-failure fallback:** a running server whose probe fails past the grace period shows Ready without counts, and the admins get one DM.
- **Draft and publish:** cards start as drafts and appear in the games channel only when an admin publishes them.
- **Config names:** `PTERODACTYL_URL` and `PTERODACTYL_API_KEY` replace `PANEL_API_KEY`.
- **Key scope:** the key's real scope includes the Panel-forced `websocket.connect` permission, which allows reading the console. That was accepted, guarded by the path allowlist and its test, and is recorded as a residual risk.
- **Registry location:** the source registry is in its own module, `registry.py`, not in the base module. Placing it in the base module would make the base module import the plugins that import it back.
- **Suspended servers:** the Panel answers 409 on `/resources` for a suspended server, so the client falls back to the details endpoint to read the suspension flag. Any other 409 reads as unknown.
- **Name token:** the schema guard listed `name` as a forbidden column token. On 2026-11-02 the user decided that `name` alone is not personal data, because channel and map names use it. The token was removed, and every other token stays.
- **Community check:** a 403 and a 404 from `get_guild` both mean the bot is not in that community, and both get the same reply.
- **Alert flag:** `resolve` takes the persisted `alert_sent` flag, so the admin DM is sent at most once, even across a restart.
- **Oversized replies:** a probe reply longer than the raw limit is `malformed`. It is not truncated.
- **Location limit:** the location embed limit is 500 characters. A location is validated to 200 characters, and escaping can grow that text 2.5 times, so a 400-character limit would cut validated text.
- **Hosts:** an IPv6 zone id is refused in a host, and so is a host whose last label is all digits, because that label could be read as an IPv4 address.
- **Message deletes:** a MESSAGE_DELETE can race a post or an edit. The poller keeps a bounded set of remembered deletes, and it stores the rendered hash only if the message is still the same one.
- **Voice name refresh:** the refresh writes with a compare-and-set, so a concurrent admin change to the voice wins.

## What the phase 3 prompt got wrong

This section compares the phase 3 brief and prompt with the design and with the phase 1 live run recorded in `docs/fluxer-notes.md` under "Live reaction checks". The design's "Out of date in the brief and the prompt" list is the source for the first four items. The others come from the live run.

- **Mention Roles:** the brief names a "Mention Roles" permission. Fluxer has no such permission. A role ping needs the role to be mentionable, or the bot to hold Mention Everyone.
- **Managed roles:** Fluxer has no managed roles, so the brief's checks for them have nothing to test.
- **Status unknown and Unavailable:** the brief's states remain in the resolver, and both refuse open requests.
- **Scheduling emoji:** the brief's calendar emoji (U+1F4C5) belongs to a later phase. No code in phase 3 reacts with it.
- **Role creation:** the create call ignores `mentionable`, so a role is created as not mentionable. The bot changes it with a second call, and reads the change back.
- **allowed_mentions shape:** the docs implied that `parse` together with `roles` is refused. Live, `{"parse": [], "roles": [...]}` was accepted, and the role was mentioned.
- **Reactor list:** the `/users` reply is a paged object with `items`, `has_more` and `next_after`, not a plain list.
- **last_message_id:** it does not go back down after the newest message is deleted. The board check therefore reads the newest surviving message with `messages?limit=1` and does not trust `last_message_id`.

## What the phase 1 prompt got wrong

This section compares the phase 1 brief and plan with what the investigation and the live run showed. The sources are the panel brief (a private design document kept outside this repository; its "Before any code" checks and build order step 1) and the phase 1 plan. The live results are in `docs/fluxer-notes.md`, recorded against server build 2026.1031.154542 and client build 2026.1017.133957. Where the brief is silent, the entry says so, and the assumption is attributed to the phase 1 plan's reading of Fluxer's documentation. That reading was not re-checked in this run.

### The green-oaks-bot client is not a clean base

- **Brief assumed:** the brief calls the client "working code" and says to extract its `fluxer/` layer into a shared package. The phase 1 plan treated it as a copy with small edits.
- **True:** green-oaks-bot's REST module imports its card-rendering helpers (`embed_colour` and `Reply`), and its render module imports Pillow. The layer was not self-contained.
- **Consequence:** phase 1 cut that seam. `reply_to_embed` went, `send_message` takes plain values, and `resolve_endpoints` takes plain values. Later phases must not re-add the source bot's imports. `tests/test_repo_hygiene.py` enforces this.

### Snowflake length

- **Brief assumed:** the brief lists snowflake validation as something to carry over, without a length. The phase 1 plan says Fluxer documents snowflakes as at most 19 digits.
- **True:** the code accepts 1 to 25 digits (`src/fluxer_client/ids.py:10` `_SNOWFLAKE = re.compile(`). The database CHECK constraints in migration 0001 and the admin-id pattern in `config.py` use the same 25 bound. The live run did not test id length.
- **Consequence and recommendation:** do not tighten to 19 now. A looser bound only admits more digit strings, and the rejected value never reaches an error message. Tightening would have to change three places: `ids.py`, `config.py`, and the CHECK constraints in `0001_init.sql`. Migration 0001 must not be edited after release, so the CHECKs would need a new migration that rebuilds the STRICT tables. Record 19 as the documented maximum, and revisit with a migration only if Fluxer issues longer ids.

### Edit rate limit

- **Brief assumed:** the brief does not mention a rate limit. The phase 1 plan says Fluxer's documented edit limit applies to published messages in shared (announcement) channels.
- **True:** six rapid edits (values 0 to 5) of an ordinary message, with client retries off, all succeeded with no 429 (`docs/fluxer-notes.md`). Six edits prove nothing about the limit's size.
- **Consequence:** the client keeps its generic 429 retry in `FluxerREST.request` (`src/fluxer_client/rest.py:109` `async def request(`). The panel must not depend on edits being unlimited.

### Gateway intents

- **Brief assumed:** the brief lists reaction events among things to live-test. The phase 1 plan asked whether reaction events need intents.
- **True:** identify sends no intents, and reaction add and remove events arrived anyway, in guild and DM channels.
- **Consequence:** the gateway keeps identify without intents. The notes say guild `MESSAGE_CREATE` facts (`author.bot`, `member.roles`) were not re-observed in this run, so any phase that depends on them must test them live first.

### Removing another user's reaction

- **Brief assumed:** the brief lists removing a user's reaction as a live-test item. The phase 1 plan says the removal works only in guild channels, so DM flows cannot reset reactions.
- **True:** removal was exercised only in a guild channel. `tools/live/reaction_remove.py` uses `LIVE_CHANNEL_ID`, and `check_safety` requires that channel to be in the guild. The DM case was never run. It is untested, not disproved.
- **Consequence:** phases 3 and 4 must not depend on removing a reaction in a DM until a live test in a DM passes.

### Audit reason length

- **Brief assumed:** the brief does not mention audit reasons. The phase 1 plan says `X-Audit-Log-Reason` accepts 512 characters.
- **True:** the live Activity Log showed the reason text (`fluxer panel live test`) for both the add and the remove. The 512 limit was not tested.
- **Consequence:** the 128-character cap (`src/fluxer_client/rest.py:48` `REASON_MAX = 128`) is a local choice, and it is safe to keep. Every guild admin can read the reason, so reasons must be fixed text, which `_check_reason` enforces.

### PATCH with content null

- **Brief assumed:** the brief does not mention it. The phase 1 plan treated `content: null` as an edit option.
- **True:** the call was accepted. The returned content is an empty string, and the embed is kept. The behaviour is undocumented.
- **Consequence:** `rest.py` uses the `UNSET` sentinel (`src/fluxer_client/rest.py:36` `UNSET: Final = Unset.UNSET`), so "not given" is never sent as null. To change only the embed, omit `content`. Never send `content: null` by accident, because it erases the text.

### Hierarchy refusal and missing permission

- **Brief assumed:** the brief lists "role allowlist and hierarchy" as the highest-risk review item (the panel brief, phase 3).
- **True:** a hierarchy refusal and a missing-permission refusal both return 403 with code `MISSING_PERMISSIONS`.
- **Consequence:** the client cannot tell them apart from the error. Phase 3 must check the role hierarchy before the call, not by reading the error.

### Unicode emoji shape

- **Brief assumed:** the brief's reaction buttons (`U+25B6 U+FE0F`, `U+1F64B`, `U+1F4C5`, `U+1F514`) are unicode emoji. It does not say what the events look like.
- **True:** a unicode emoji has no `id` key at all. It is not `id: null`.
- **Consequence:** the parser handles the missing key, and `encode_emoji` percent-encodes unicode (`src/fluxer_client/rest.py:63` `return quote(emoji, safe="")`). Matching a reaction to a button must compare names, not ids.

### DM reactions

- **Brief assumed:** the brief says reactions act as buttons and that DM flows stand in for modals. It does not say what DM reaction events carry.
- **True:** DM reactions have no `guild_id` and no `member`, so there are no roles. They carry an undocumented 32-character `session_id`.
- **Consequence:** DM reactions cannot be role-gated, so treat DMs as having no guild roles. The DM `session_id` is not parsed into any event. The reaction dataclasses have no field for it, and the reaction parser builds them from named fields only (`src/fluxer_client/events.py:182` `return ReactionAdd(`). Panel code never sees it. Any future parser that reads it must keep it `repr=False` and must not log it.

### Bulk reaction events

- **Brief assumed:** the phase 1 plan asked for the `MESSAGE_REACTION_ADD_MANY` shape.
- **True:** it was not observed. The live run also did not observe `MESSAGE_REACTION_REMOVE_ALL` or `MESSAGE_REACTION_REMOVE_EMOJI`.
- **Consequence:** `parse` returns `None` for `ADD_MANY` without logging, so a bulk add is silently dropped (`src/fluxer_client/events.py:256` `MESSAGE_REACTION_ADD_MANY`). Capture the shape before any feature depends on bulk reactions.

### Event parser

- **Brief assumed:** the phase 1 plan anticipated event shape corrections from the Phase 3 live review.
- **True:** the live run matched the parser, and no parser change was needed (`docs/fluxer-notes.md`).
- **Consequence:** the fixtures in `tests/test_events.py` reproduce the observed shapes and are the baseline. Re-run the live scripts whenever the server version changes.

### Interactive live scripts need a terminal

- **Brief assumed:** the phase 1 plan did not say how the live scripts would take input.
- **True:** `reaction_remove.py` waits for Enter on stdin. Run from Claude Code's `!` prompt, stdin is closed, and the script exits with `EOFError` before the permission step.
- **Consequence:** live scripts run in a real terminal. `wait_for_enter` in `tools/live/_live.py` reads stdin in a daemon thread, so Ctrl+C still exits with status 130.
