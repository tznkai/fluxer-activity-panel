<!-- SPDX-FileCopyrightText: 2026 K. Chen -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Review guide

Each security- and privacy-sensitive spot is listed as a reference: the path, the line, the text expected on that line, and why it matters. `tests/test_review_refs.py` checks every reference against the code, so a reference that no longer points at its anchor fails the test. Moving code means updating this file in the same change.

Paths are relative to this directory.

## Token handling

- `src/fluxer_panel/config.py:17` `_TOKEN = re.compile(` — the token must be printable ASCII with no spaces, because it is placed in an `Authorization` header.
- `src/fluxer_panel/config.py:21` `Message names the variable, never its value.` — configuration errors name the variable, so a bad token is reported without being echoed.
- `src/fluxer_panel/config.py:37` `raise ConfigError(message) from None` — a URL that fails to parse raises the generic message, and the parser's own text (which may echo the value) is dropped.
- `src/fluxer_panel/config.py:45` `token: str = field(repr=False)` — the token is left out of `repr(Config)`, so a logged or printed config does not show it.
- `src/fluxer_client/rest.py:102` `self._headers = {"Authorization": f"Bot {token}"}` — the only place REST sends the token. It goes in a header, never in a URL or a log line.
- `src/fluxer_client/gateway.py:135` `"token": self.token, "session_id"` — the resume frame (op 6) carries the token.
- `src/fluxer_client/gateway.py:173` `"token": self.token,` — the identify frame (op 2) carries the token.
- `src/fluxer_panel/__main__.py:108` `print(f"Configuration error: {exc}"` — the only panel print. It writes a configuration message to stderr, which names variables and never values.
- `src/fluxer_panel/__main__.py:110` `setup_logging(config.log_level, config.token)` — the token is handed to the redaction filter before anything can log.
- `src/fluxer_panel/__main__.py:125` `last resort: log (redacted) and exit 1` — the last-resort catch-all logs through the redacting handler and returns 1. No raw traceback reaches stderr.

## Logging

- `src/fluxer_panel/logs.py:18` `ID_KEYS = frozenset(` — the only keys whose values may be logged as ids. `clean_fields` keeps one only if it is a snowflake.
- `src/fluxer_panel/logs.py:19` `OTHER_KEYS = frozenset(` — the other keys that may be logged. Numbers and bools pass. Strings must match `_SAFE_TOKEN`.
- `src/fluxer_panel/logs.py:20` `_SAFE_TOKEN = re.compile(` — string values for other keys must look like identifiers, so free text such as names or message content cannot be logged through them.
- `src/fluxer_panel/logs.py:24` `def clean_fields(` — the allowlist. Any key not listed in `ID_KEYS` or `OTHER_KEYS` is dropped.
- `src/fluxer_panel/logs.py:61` `_ESCAPE_ROUNDS = 3` — the token is redacted when it is repr'd or json-escaped up to three nested levels. Deeper nesting is not covered.
- `src/fluxer_panel/logs.py:74` `def token_variants(` — builds the raw and escaped forms of the token that the filter replaces.
- `src/fluxer_panel/logs.py:93` `class TokenRedactionFilter` — the handler-level backstop. It replaces the token in the message, the args and the traceback before formatting.
- `src/fluxer_panel/logs.py:108` `a malformed call must not raise` — a malformed log call is caught inside the filter, so it cannot raise into the code that logged it.
- `src/fluxer_panel/logs.py:130` `handler.addFilter(TokenRedactionFilter(token))` — the filter is installed on the one JSON handler that `setup_logging` creates.
- `src/fluxer_panel/logs.py:136` `logging.captureWarnings(True)` — Python warnings go through logging, so they are JSON lines and are redacted too.
- `src/fluxer_client/events.py:26` `session_id: str = field(repr=False)` — the undocumented session id that DM reactions carry is kept out of reprs.
- `src/fluxer_client/events.py:42` `content: str = field(repr=False)` — message text is kept out of reprs and therefore out of debug output.
- `src/fluxer_client/events.py:236` `log.warning("malformed %s payload: field %s", t, exc.field_name)` — a malformed payload is logged with the field name only.
- `src/fluxer_client/events.py:240` `field "?"` — the catch-all logs a placeholder, not the exception text, which may carry the value.
- `src/fluxer_client/gateway.py:76` `log.warning("gateway connection failed: %r", exc)` — a plain-text `fluxer_client` record. Its repr can include the instance host in stdout logs. That is acceptable because logs are not tracked, but reviewers should know it is there. Token redaction still applies.

## Admin authorization

- `src/fluxer_panel/admin.py:19` `def authorize(` — the single authorization check. Four conditions follow.
- `src/fluxer_panel/admin.py:22` `event.guild_id is None` — admin commands work in DMs only. A command in a guild channel is refused.
- `src/fluxer_panel/admin.py:23` `not event.author_is_bot` — bot authors are refused.
- `src/fluxer_panel/admin.py:24` `is_snowflake(event.author_id)` — the author id must be a valid snowflake before it is compared.
- `src/fluxer_panel/admin.py:25` `event.author_id in config.admin_user_ids` — exact membership in the configured admin list.
- `src/fluxer_panel/admin.py:43` `if not authorize(event, config):` — every admin subcommand passes through this check in `handle`.
- `src/fluxer_panel/admin.py:44` `log_event(log, "admin_denied"` — a refusal is logged with the user and channel ids only.
- `src/fluxer_panel/bot.py:38` `if msg.author_is_bot or msg.author_id == self.user_id:` — the bot ignores its own messages and other bots' messages before routing. This prevents loops.

## Role assignment

No code in `src/fluxer_panel` calls `add_member_role` or `remove_member_role` in phase 1. The only callers are the live tool.

- `src/fluxer_client/rest.py:66` `def _check_reason(` — the audit reason is fixed, printable ASCII, and at most 128 characters. Caller text must never reach it.
- `src/fluxer_client/rest.py:71` `audit reason must be 1-128 printable ASCII characters` — the refusal happens before any request is sent.
- `src/fluxer_client/rest.py:238` `def _role_path(` — builds the role URL from ids that have passed the snowflake check.
- `src/fluxer_client/rest.py:239` `/members/{require_snowflake(user_id)}"` — the user id is validated as a snowflake in the path.
- `src/fluxer_client/rest.py:242` `async def add_member_role(` — a PUT that grants a role. This is a privilege change, so the role allowlist and the role hierarchy must be checked before the call. Fluxer answers both a hierarchy refusal and a missing permission with the same 403 code.
- `src/fluxer_client/rest.py:246` `async def remove_member_role(` — a DELETE that removes a role. The same checks apply.
- `tools/live/roles.py:32` `await rest.add_member_role(guild, user, role, reason=REASON)` — the live tool's call, which runs only in the private guild.

## Reaction removal

- `src/fluxer_client/rest.py:227` `def _reaction_path(` — builds the path from snowflake-checked ids and an encoded emoji.
- `src/fluxer_client/rest.py:234` `async def remove_reaction(` — a DELETE of one user's reaction. Fluxer requires Manage Messages for it. Removal has been exercised only in a guild channel. The DM case is not covered (see the phase 1 section).
- `tools/live/reaction_remove.py:80` `await rest.remove_reaction(channel, message_id, emoji, user)` — the only live exercise of removal.
- `tools/live/_live.py:192` `LIVE_CHANNEL_ID is not in LIVE_GUILD_ID` — the safety check that makes that live removal guild-only.

## Purge deletion

- `src/fluxer_panel/purge.py:26` `_EXPIRED_SESSIONS = """(` — the expiry rule. A non-recurring session expires by `ends_at`, and a recurring one by `recurrence_until`.
- `src/fluxer_panel/purge.py:28` `recurrence_until IS NOT NULL` — an open-ended series (`recurrence_until` NULL) never matches, so it is never purged.
- `src/fluxer_panel/purge.py:39` `def _purge_tx(` — one transaction: count the RSVPs of expired sessions, delete the sessions, delete the old requests.
- `src/fluxer_panel/purge.py:44` `DELETE FROM sessions` — deletes expired sessions. Their RSVPs go with them by cascade.
- `src/fluxer_panel/purge.py:45` `DELETE FROM requests` — deletes requests older than 30 days, whatever their status.
- `src/fluxer_panel/purge.py:64` `def write_status(` — the status file is written atomically and holds only timestamps and counts.
- `src/fluxer_panel/purge.py:80` `await db.checkpoint()` — a WAL checkpoint after each run. A checkpoint failure after the commit is reported as `ok: false`, though the rows may already be gone.
- `src/fluxer_panel/purge.py:107` `log.exception("purge_loop_error")` — the per-iteration catch-all. A failing iteration is logged, and the loop goes on.
- `src/fluxer_panel/__main__.py:68` `purge_task = asyncio.create_task(purge_loop(` — the purge task starts only once the gateway task exists, so a stop that wins discovery never starts a purge.
- `src/fluxer_panel/__main__.py:82` `gw_task.cancel()` — `run()`'s finally cancels the gateway task first, so the session never closes under a live reconnect.
- `src/fluxer_panel/__main__.py:86` `purge_task.cancel()` — the purge task is cancelled and awaited on every exit, after the gateway task.
- `src/fluxer_panel/migrations/0001_init.sql:53` `REFERENCES cards(id) ON DELETE RESTRICT` — a card that still has sessions cannot be deleted.
- `src/fluxer_panel/migrations/0001_init.sql:69` `REFERENCES sessions(id) ON DELETE CASCADE` — RSVPs are removed with their session.

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

- `tools/live/_live.py:26` `LONG_NUMBER = re.compile(` — any 15-plus-digit run that is not a known id is replaced with `<id>`.
- `tools/live/_live.py:32` `_redactor: Redactor | None = None` — set as soon as the token is loaded. `main()` reads it when it reports an error.
- `tools/live/_live.py:78` `class Redactor:` — maps known ids to labels and replaces the token with `[REDACTED]`.
- `tools/live/_live.py:98` `return LONG_NUMBER.sub("<id>", s)` — the last step: any remaining long digit run.
- `tools/live/_live.py:122` `def log_handler(` — the stderr log handler formats the whole record, message and traceback, through the redactor.
- `tools/live/_live.py:137` `def out(` — the only print path for the live scripts.
- `tools/live/_live.py:185` `async def check_safety(` — refuses unless the channel is in the guild, the user is a member, and the guild has at most two members.
- `tools/live/_live.py:197` `LIVE_USER_ID is not a member of LIVE_GUILD_ID` — a 404 on the membership check refuses. A mistyped user id never gets a DM.
- `tools/live/_live.py:208` `"Authorization": f"Bot {env['FLUXER_TOKEN']}"` — the version capture sends the token in a header and prints nothing from it.
- `tools/live/_live.py:222` `def gateway_failure(` — a gateway failure reports only the exception's type name. Its message is never printed, since it may echo the token or a payload.
- `tools/live/_live.py:330` `def error_redactor(` — resolved at the moment an error is printed, so a redactor set up later in the run still applies.
- `tools/live/_live.py:349` `f"ERROR {type(exc).__name__}: {exc}"` — error output is the type and the redacted message only, with no traceback.

## .env handling

- `.gitignore:11` `.env.*` — ignores every env file variant.
- `.gitignore:12` `!.env.example` — the one tracked env file, the blank template.
- `tests/test_repo_hygiene.py:24` `SECRET_GLOBS = (` — the denylist of key and PEM file names. The hygiene test lists tracked names and never opens them.
- `tests/test_repo_hygiene.py:28` `def is_secret(` — the check that a tracked name is a secret.
- `tools/live/_live.py:39` `def read_env_file(` — the live tool's own `KEY=VALUE` parser. It does no interpolation. Only the live tool reads a `.env` file.
- `tools/live/_live.py:62` `**read_env_file(env_file)` — values from the real environment override values from the `.env` file.
- `src/fluxer_panel/config.py:58` `def from_env(` — the panel reads environment variables only. It never opens `.env`; the operator sources the file in their shell.

## What the phase 1 prompt got wrong

This section compares the phase 1 brief and plan with what the investigation and the live run showed. The sources are `docs/panel-brief.md` (the "Before any code" checks and build order step 1) and the phase 1 plan. The live results are in `docs/fluxer-notes.md`, recorded against server build 2026.1031.154542 and client build 2026.1017.133957. Where the brief is silent, the entry says so, and the assumption is attributed to the phase 1 plan's reading of Fluxer's documentation. That reading was not re-checked in this run.

### The green-oaks-bot client is not a clean base

- **Brief assumed:** the brief calls the client "working code" and says to extract its `fluxer/` layer into a shared package. The phase 1 plan treated it as a copy with small edits.
- **True:** green-oaks-bot's REST module imports its card-rendering helpers (`embed_colour` and `Reply`), and its render module imports Pillow. The layer was not self-contained.
- **Consequence:** phase 1 cut that seam. `reply_to_embed` went, `send_message` takes plain values, and `resolve_endpoints` takes plain values. Later phases must not re-add the source bot's imports. `tests/test_repo_hygiene.py` enforces this.

### Snowflake length

- **Brief assumed:** the brief lists snowflake validation as something to carry over, without a length. The phase 1 plan says Fluxer documents snowflakes as at most 19 digits.
- **True:** the code accepts 1 to 25 digits (`src/fluxer_client/ids.py:10`). The database CHECK constraints in migration 0001 and the admin-id pattern in `config.py` use the same 25 bound. The live run did not test id length.
- **Consequence and recommendation:** do not tighten to 19 now. A looser bound only admits more digit strings, and the rejected value never reaches an error message. Tightening would have to change three places: `ids.py`, `config.py`, and the CHECK constraints in `0001_init.sql`. Migration 0001 must not be edited after release, so the CHECKs would need a new migration that rebuilds the STRICT tables. Record 19 as the documented maximum, and revisit with a migration only if Fluxer issues longer ids.

### Edit rate limit

- **Brief assumed:** the brief does not mention a rate limit. The phase 1 plan says Fluxer's documented edit limit applies to published messages in shared (announcement) channels.
- **True:** six rapid edits (values 0 to 5) of an ordinary message, with client retries off, all succeeded with no 429 (`docs/fluxer-notes.md`). Six edits prove nothing about the limit's size.
- **Consequence:** the client keeps its generic 429 retry in `FluxerREST.request` (`src/fluxer_client/rest.py:109`). The panel must not depend on edits being unlimited.

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
- **Consequence:** the 128-character cap (`src/fluxer_client/rest.py:48`, `REASON_MAX`) is a local choice, and it is safe to keep. Every guild admin can read the reason, so reasons must be fixed text, which `_check_reason` enforces.

### PATCH with content null

- **Brief assumed:** the brief does not mention it. The phase 1 plan treated `content: null` as an edit option.
- **True:** the call was accepted. The returned content is an empty string, and the embed is kept. The behaviour is undocumented.
- **Consequence:** `rest.py` uses the `UNSET` sentinel (`src/fluxer_client/rest.py:36`), so "not given" is never sent as null. To change only the embed, omit `content`. Never send `content: null` by accident, because it erases the text.

### Hierarchy refusal and missing permission

- **Brief assumed:** the brief lists "role allowlist and hierarchy" as the highest-risk review item (`docs/panel-brief.md`, phase 3).
- **True:** a hierarchy refusal and a missing-permission refusal both return 403 with code `MISSING_PERMISSIONS`.
- **Consequence:** the client cannot tell them apart from the error. Phase 3 must check the role hierarchy before the call, not by reading the error.

### Unicode emoji shape

- **Brief assumed:** the brief's reaction buttons (`▶️ 🙋 📅 🔔`) are unicode emoji. It does not say what the events look like.
- **True:** a unicode emoji has no `id` key at all. It is not `id: null`.
- **Consequence:** the parser handles the missing key, and `encode_emoji` percent-encodes unicode (`src/fluxer_client/rest.py:63`). Matching a reaction to a button must compare names, not ids.

### DM reactions

- **Brief assumed:** the brief says reactions act as buttons and that DM flows stand in for modals. It does not say what DM reaction events carry.
- **True:** DM reactions have no `guild_id` and no `member`, so there are no roles. They carry an undocumented 32-character `session_id`.
- **Consequence:** DM reactions cannot be role-gated, so treat DMs as having no guild roles. The `session_id` is opaque and is kept out of reprs (`src/fluxer_client/events.py:26`). It must not be logged.

### Bulk reaction events

- **Brief assumed:** the phase 1 plan asked for the `MESSAGE_REACTION_ADD_MANY` shape.
- **True:** it was not observed. The live run also did not observe `MESSAGE_REACTION_REMOVE_ALL` or `MESSAGE_REACTION_REMOVE_EMOJI`.
- **Consequence:** `parse` returns `None` for `ADD_MANY` without logging, so a bulk add is silently dropped (`src/fluxer_client/events.py:229`). Capture the shape before any feature depends on bulk reactions.

### Event parser

- **Brief assumed:** the phase 1 plan anticipated event shape corrections from the Phase 3 live review.
- **True:** the live run matched the parser, and no parser change was needed (`docs/fluxer-notes.md`).
- **Consequence:** the fixtures in `tests/test_events.py` reproduce the observed shapes and are the baseline. Re-run the live scripts whenever the server version changes.

### Interactive live scripts need a terminal

- **Brief assumed:** the phase 1 plan did not say how the live scripts would take input.
- **True:** `reaction_remove.py` waits for Enter on stdin. Run from Claude Code's `!` prompt, stdin is closed, and the script exits with `EOFError` before the permission step.
- **Consequence:** live scripts run in a real terminal. `wait_for_enter` in `tools/live/_live.py` reads stdin in a daemon thread, so Ctrl+C still exits with status 130.
