<!-- SPDX-FileCopyrightText: 2026 K. Chen -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Review guide

Each security- and privacy-sensitive spot is listed as a reference: the path, the line, the text expected on that line, and why it matters. `tests/test_review_refs.py` checks every reference against the code, so a reference that no longer points at its anchor fails the test. Moving code means updating this file in the same change.

Paths are relative to this directory.

## Token handling

- `src/fluxer_panel/config.py:17` `_TOKEN = re.compile(` — the token must be printable ASCII with no spaces, because it is placed in an `Authorization` header.
- `src/fluxer_panel/config.py:22` `Message names the variable, never its value.` — configuration errors name the variable, so a bad token is reported without being echoed. This is the docstring's intent; the enforcement is in the raise lines below.
- `src/fluxer_panel/config.py:84` `raise ConfigError("FLUXER_TOKEN must be printable ASCII without spaces")` — the bad-token message states the rule and names the variable, and it never includes the token.
- `src/fluxer_panel/config.py:38` `raise ConfigError(message) from None` — a URL that fails to parse raises the generic message, and the parser's own text (which may echo the value) is dropped.
- `src/fluxer_panel/config.py:53` `token: str = field(repr=False)` — the token is left out of `repr(Config)`, so a logged or printed config does not show it.
- `src/fluxer_client/rest.py:102` `self._headers = {"Authorization": f"Bot {token}"}` — the only place REST sends the token. It goes in a header, never in a URL or a log line.
- `src/fluxer_client/gateway.py:135` `"token": self.token, "session_id"` — the resume frame (op 6) carries the token.
- `src/fluxer_client/gateway.py:173` `"token": self.token,` — the identify frame (op 2) carries the token.
- `src/fluxer_panel/__main__.py:124` `print(f"Configuration error: {exc}"` — one of the two panel prints (the other is the usage line below). It writes to stderr a configuration message, which names variables and never values.
- `src/fluxer_panel/__main__.py:119` `print(USAGE, file=sys.stderr)` — the second panel print, for a bad command line. It writes the fixed `USAGE` text to stderr, which names no values.
- `src/fluxer_panel/__main__.py:126` `setup_logging(config.log_level, [s for s in (config.token, config.pterodactyl_api_key) if s])` — the token and the Pterodactyl key are handed to the redaction filter before anything can log.
- `src/fluxer_panel/__main__.py:141` `last resort: log (redacted) and exit 1` — the last-resort catch-all logs through the redacting handler and returns 1. No raw traceback reaches stderr.

## Logging

- `src/fluxer_panel/logs.py:18` `ID_KEYS = frozenset(` — the only keys whose values may be logged as ids. `clean_fields` keeps one only if it is a snowflake.
- `src/fluxer_panel/logs.py:19` `OTHER_KEYS = frozenset(` — the other keys that may be logged. Numbers and bools pass. Strings must match `_SAFE_TOKEN`.
- `src/fluxer_panel/logs.py:24` `_SAFE_TOKEN = re.compile(` — string values for other keys must look like identifiers, so free text such as names or message content cannot be logged through them.
- `src/fluxer_panel/logs.py:28` `def clean_fields(` — the allowlist. Any key not listed in `ID_KEYS` or `OTHER_KEYS` is dropped.
- `src/fluxer_panel/logs.py:65` `_ESCAPE_ROUNDS = 3` — the token is redacted when it is repr'd or json-escaped up to three nested levels. Deeper nesting is not covered.
- `src/fluxer_panel/logs.py:78` `def token_variants(` — builds the raw and escaped forms of the token that the filter replaces.
- `src/fluxer_panel/logs.py:97` `class TokenRedactionFilter` — the handler-level backstop. It replaces the token in the message, the args and the traceback before formatting.
- `src/fluxer_panel/logs.py:117` `a malformed call must not raise` — a malformed log call is caught inside the filter, so it cannot raise into the code that logged it.
- `src/fluxer_panel/logs.py:141` `handler.addFilter(redaction)` — the filter is installed on the one JSON handler that `setup_logging` creates.
- `src/fluxer_panel/logs.py:149` `logging.captureWarnings(True)` — Python warnings go through logging, so they are JSON lines and are redacted too.
- `src/fluxer_client/events.py:26` `session_id: str = field(repr=False)` — the gateway session id from `Ready`, which with the token enables a session resume, is kept out of reprs.
- `src/fluxer_client/events.py:42` `content: str = field(repr=False)` — message text is kept out of reprs and therefore out of debug output.
- `src/fluxer_client/events.py:250` `log.warning("malformed %s payload: field %s", t, exc.field_name)` — a malformed payload is logged with the field name only.
- `src/fluxer_client/events.py:254` `field "?"` — the catch-all logs a placeholder, not the exception text, which may carry the value.
- `src/fluxer_client/gateway.py:76` `log.warning("gateway connection failed: %r", exc)` — a plain-text `fluxer_client` record. Its repr can include the instance host in stdout logs. That is acceptable because logs are not tracked, but reviewers should know it is there. Token redaction still applies.
- `src/fluxer_panel/poller.py:574` `log_event(log, "subscription_failed", level=logging.WARNING, card_id=card_id, channel_id=channel_id,` — a failed copy is logged with its card id, channel id and exception type only. No title, slug, channel name or message text is logged.

## Admin authorization

- `src/fluxer_panel/admin.py:22` `def authorize(` — the single authorization check. Four conditions follow.
- `src/fluxer_panel/admin.py:25` `event.guild_id is None` — admin commands work in DMs only. A command in a guild channel is refused.
- `src/fluxer_panel/admin.py:26` `not event.author_is_bot` — bot authors are refused.
- `src/fluxer_panel/admin.py:27` `is_snowflake(event.author_id)` — the author id must be a valid snowflake before it is compared.
- `src/fluxer_panel/admin.py:28` `event.author_id in config.admin_user_ids` — exact membership in the configured admin list.
- `src/fluxer_panel/admin.py:56` `if not authorize(event, config):` — every admin subcommand passes through this check in `handle`.
- `src/fluxer_panel/admin.py:57` `log_event(log, "admin_denied"` — a refusal is logged with the user and channel ids only.
- `src/fluxer_panel/bot.py:50` `if msg.author_is_bot or msg.author_id == self.user_id:` — the bot ignores its own messages and other bots' messages before routing. This prevents loops.

## Role assignment

No code in `src/fluxer_panel` calls `add_member_role` or `remove_member_role` in phase 1. The only callers are the live tool.

- `src/fluxer_client/rest.py:66` `def _check_reason(` — the audit reason is fixed, printable ASCII, and at most 128 characters. Caller text must never reach it.
- `src/fluxer_client/rest.py:71` `audit reason must be 1-128 printable ASCII characters` — the refusal happens before any request is sent.
- `src/fluxer_client/rest.py:244` `def _role_path(` — builds the role URL from ids that have passed the snowflake check.
- `src/fluxer_client/rest.py:245` `/members/{require_snowflake(user_id)}"` — the user id is validated as a snowflake in the path.
- `src/fluxer_client/rest.py:248` `async def add_member_role(` — a PUT that grants a role. This is a privilege change, so the role allowlist and the role hierarchy must be checked before the call. Fluxer answers both a hierarchy refusal and a missing permission with the same 403 code.
- `src/fluxer_client/rest.py:252` `async def remove_member_role(` — a DELETE that removes a role. The same checks apply.
- `tools/live/roles.py:32` `await rest.add_member_role(guild, user, role, reason=REASON)` — the live tool's call, which runs only in the private guild.

## Reaction removal

- `src/fluxer_client/rest.py:233` `def _reaction_path(` — builds the path from snowflake-checked ids and an encoded emoji.
- `src/fluxer_client/rest.py:240` `async def remove_reaction(` — a DELETE of one user's reaction. Fluxer requires Manage Messages for it. Removal has been exercised only in a guild channel. The DM case is not covered (see the phase 1 section).
- `tools/live/reaction_remove.py:80` `await rest.remove_reaction(channel, message_id, emoji, user)` — the only live exercise of removal.
- `tools/live/_live.py:223` `LIVE_CHANNEL_ID is not in LIVE_GUILD_ID` — the safety check that makes that live removal guild-only.

## Purge deletion

- `src/fluxer_panel/purge.py:26` `_EXPIRED_SESSIONS = """(` — the expiry rule. A non-recurring session expires by `ends_at`, and a recurring one by `recurrence_until`.
- `src/fluxer_panel/purge.py:28` `recurrence_until IS NOT NULL` — an open-ended series (`recurrence_until` NULL) never matches, so it is never purged.
- `src/fluxer_panel/purge.py:39` `def _purge_tx(` — one transaction: count the RSVPs of expired sessions, delete the sessions, delete the old requests.
- `src/fluxer_panel/purge.py:44` `DELETE FROM sessions` — deletes expired sessions. Their RSVPs go with them by cascade.
- `src/fluxer_panel/purge.py:45` `DELETE FROM requests` — deletes requests older than 30 days, whatever their status.
- `src/fluxer_panel/purge.py:64` `def write_status(` — the status file is written atomically and holds only timestamps and counts.
- `src/fluxer_panel/purge.py:80` `await db.checkpoint()` — a WAL checkpoint after each run. A checkpoint failure after the commit is reported as `ok: false`, though the rows may already be gone.
- `src/fluxer_panel/purge.py:107` `log.exception("purge_loop_error")` — the per-iteration catch-all. A failing iteration is logged, and the loop goes on.
- `src/fluxer_panel/__main__.py:77` `purge_task = asyncio.create_task(purge_loop(` — the purge task and the poller start only once the gateway task exists, so a stop that wins discovery never starts either.
- `src/fluxer_panel/__main__.py:93` `gw_task.cancel()` — `run()`'s finally cancels the gateway task first, so the session never closes under a live reconnect.
- `src/fluxer_panel/__main__.py:98` `poller_task.cancel()` — the card poller is cancelled and awaited after the gateway task and before the purge task, so no card write is in flight when the session closes.
- `src/fluxer_panel/__main__.py:102` `purge_task.cancel()` — the purge task is cancelled and awaited on every exit, after the poller.
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

- `tools/live/_live.py:27` `LONG_NUMBER = re.compile(` — any 15-plus-digit run that is not a known id is replaced with `<id>`.
- `tools/live/_live.py:33` `_redactor: Redactor | None = None` — set as soon as the token is loaded. `main()` reads it when it reports an error.
- `tools/live/_live.py:91` `class Redactor:` — maps known ids to labels and replaces the token and each extra secret (a Panel URL, a probe host, an instance URL and its host forms) with `[REDACTED]`, matching regardless of case.
- `tools/live/_live.py:129` `return LONG_NUMBER.sub("<id>", s)` — the last step: any remaining long digit run.
- `tools/live/_live.py:153` `def log_handler(` — the stderr log handler formats the whole record, message and traceback, through the redactor.
- `tools/live/_live.py:168` `def out(` — the only print path for the live scripts.
- `tools/live/_live.py:216` `async def check_safety(` — refuses unless the channel is in the guild and the user is a member. It also refuses if the guild reports more than two members (no refusal when the count is not reported).
- `tools/live/_live.py:228` `LIVE_USER_ID is not a member of LIVE_GUILD_ID` — a 404 on the membership check refuses. A mistyped user id never gets a DM.
- `tools/live/_live.py:239` `"Authorization": f"Bot {env['FLUXER_TOKEN']}"` — the version capture sends the token in a header and prints nothing from it.
- `tools/live/_live.py:253` `def gateway_failure(` — a gateway failure reports only the exception's type name. Its message is never printed, since it may echo the token or a payload.
- `tools/live/_live.py:384` `def error_redactor(` — resolved at the moment an error is printed, so a redactor set up later in the run still applies.
- `tools/live/_live.py:403` `f"ERROR {type(exc).__name__}: {exc}"` — error output is the type and the redacted message only, with no traceback.

## .env handling

- `.gitignore:10` `.env` — ignores the real env file, so a local `.env` is not committed by a plain `git add`.
- `.gitignore:11` `.env.*` — ignores every env file variant.
- `.gitignore:12` `!.env.example` — the one tracked env file, the blank template.
- `tests/test_repo_hygiene.py:26` `SECRET_GLOBS = (` — the denylist of key and PEM file names. The hygiene test lists tracked names and never opens them.
- `tests/test_repo_hygiene.py:30` `def is_secret(` — the check that a tracked name is a secret.
- `tools/live/_live.py:40` `def read_env_file(` — the live tool's own `KEY=VALUE` parser. It does no interpolation. Only the live tool reads a `.env` file.
- `tools/live/_live.py:61` `**read_env_file(env_file)` — values from the real environment override values from the `.env` file.
- `src/fluxer_panel/config.py:78` `def from_env(` — the panel reads environment variables only. It never opens `.env`; the operator sources the file in their shell.

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
- `src/fluxer_panel/logs.py:23` `PINNED_LOGGERS = ("a2s", "mcstatus")` — the probe libraries' loggers are pinned to WARNING, because a2s logs raw packets at DEBUG.

## Plugin boundary

- `src/fluxer_panel/sources/registry.py:29` `MANAGER_PLUGINS: dict` — the host-manager kinds, each with its factory and the ref it expects. Adding a kind is one line here.
- `src/fluxer_panel/sources/registry.py:32` `PROBES: dict[str, Probe]` — the probe kinds, each mapped to one fixed instance.
- `src/fluxer_panel/sources/registry.py:49` `if manager is not None:` — a factory that returns None (the manager is not configured) registers nothing, so the plugin is unreachable.
- `src/fluxer_panel/sources/base.py:65` `class HostManager(Protocol):` — the host-manager contract. `validate_ref` raises only `CardFieldError`, and `power_state` never raises.
- `src/fluxer_panel/sources/base.py:81` `class Probe(Protocol):` — the probe contract. A probe returns a result or `ProbeFailed` and never raises.
- `src/fluxer_panel/sources/_probe_util.py:35` `async def _guard(` — every probe runs under this guard, which maps each failure to a fixed reason.
- `src/fluxer_panel/sources/_probe_util.py:48` `never let a library message escape` — the catch-all. A library's error text is never returned.

## Card rendering

- `src/fluxer_panel/render.py:68` `def neutralise(` — breaks the mention forms (`@everyone`, `<@id>`, `<#id>`) with a zero-width joiner, so card text cannot ping anyone or form a mention.
- `src/fluxer_panel/render.py:27` `_MD = re.compile(` — the markdown control characters that `escape` backslashes.
- `src/fluxer_panel/render.py:73` `def escape(` — the escape for all card text: neutralise first, then escape markdown. Every user text passes through it before it enters an embed.
- `src/fluxer_panel/render.py:103` `def code(` — a code span. Backticks are removed, because they cannot be escaped inside one.
- `src/fluxer_panel/render.py:84` `def fit(` — cuts escaped text to a limit in UTF-16 units, the unit Fluxer counts in, without leaving a dangling backslash that would escape the ellipsis.
- `src/fluxer_panel/render.py:248` `"allowed_mentions": {"parse": []}` — every card send and edit carries empty `allowed_mentions`.
- `src/fluxer_panel/render.py:274` `def outside_location(has_location: bool)` — takes a bool, so the location text cannot reach the outside line.
- `src/fluxer_panel/render.py:181` `fit(escape(channel.channel_name)` — a voice or text channel's name is escaped before it enters the field, so a name cannot form a link or a mention.
- `src/fluxer_panel/render.py:258` `def _outside_channel(` — a channel name on the outside line is mention-neutralised, and the line shows it only when the link is verified.
- `tests/test_render.py:698` `SENTINEL_LOCATION not in outside` — the test that the location text never appears in the outside rendering.
- `src/fluxer_panel/render.py:113` `def channel_link(` — the only place a channel link is built. Both ids pass the snowflake check first.
- `tests/test_text.py:27` `"guild" in node.value.lower()` — the scan that fails on any user-facing string that mentions a guild.

- `src/fluxer_panel/render.py:121` `def jump_link(` — the only place a masked jump link is built. Its ids pass the snowflake check first.
- `src/fluxer_panel/render.py:195` `fit(escape(card.title), LIMITS["board_title"])` — a board cell's title is escaped before it is fitted, so a title cannot ping anyone or form a link.
- `src/fluxer_panel/render.py:218` `card.channel_id == games_channel_id` — a board status is a jump link only for a card whose message is in the board's channel, so the link's community is the one looked up for that channel, and no link leaves it.
- `src/fluxer_panel/render.py:211` `return {"embeds": embeds, "allowed_mentions": {"parse": []}}` — the summary board message carries empty `allowed_mentions`.
- `src/fluxer_panel/poller.py:432` `payload = render.card_message(embed)` — a subscription copy is sent with the card's own payload, whose `allowed_mentions` is `{"parse": []}`. A copy mentions nobody, the same as the card.

## Card commands

- `src/fluxer_panel/admin.py:60` `if head == "card":` — the `!card` branch runs only after `authorize` has passed, so card commands share the DM-only, admin-only check.
- `src/fluxer_panel/cards.py:37` `super().__init__(f"{field}: {rule}")` — `CardFieldError` carries a field name and a rule key. The submitted value is never in the message.
- `src/fluxer_panel/card_commands.py:234` `exc.status in (403, 404)` — a 403 or a 404 from `get_guild` means the bot is not in that community. Both get the same "unverified" reply.
- `src/fluxer_panel/card_commands.py:247` `if channel.get("type") != channel_type:` — the channel must be the type its slot takes: a voice channel for the voice slot, a text channel for a text slot.
- `src/fluxer_panel/card_commands.py:30` `TEXT_CHANNEL_TYPE = 0` — the text channel type. It is Discord's value and is not yet confirmed on a live instance; `tools/live/markup_live.py` prints the posting channel's type so the operator can check it.
- `src/fluxer_panel/card_commands.py:249` `str(channel.get("guild_id")) != guild_id` — the channel must belong to the community the admin named.
- `src/fluxer_panel/card_commands.py:424` `cards.set_state(conn, card_id, "archived", now)` — archive keeps the card row and its slots. It clears the message and sets the state, so the card can be read again.
- `src/fluxer_panel/card_commands.py:491` `return text.SUBSCRIPTION_NOT_IN_COMMUNITY` — a subscription is refused unless the bot is in the community. `_check_channel` answers a 403 or 404 on the community lookup with an unverified link, and a subscription never stores one.

## Card poller

- `src/fluxer_panel/poller.py:202` `log_event(log, "card_poll_failed"` — one card's failure is logged with the card id and the exception type only. The other cards still run.
- `src/fluxer_panel/poller.py:504` `await self._rest.edit_message(channel_id, message_id, embeds=` — the edit sends embeds and `allowed_mentions` and never `content`, so an edit cannot erase the message text.
- `src/fluxer_panel/poller.py:238` `cards.set_probe_tracking(c, card.id` — the alert state is saved before the DM goes out. A crash or a failed DM cannot send the alert a second time.
- `src/fluxer_panel/poller.py:240` `await self._alert(card)` — the admin DM that follows that save.
- `src/fluxer_panel/poller.py:282` `cards.set_voice_name_if(c, card_id` — a refreshed voice name is written only if the voice still matches the one that was read, so an admin's change during the lookup wins.
- `src/fluxer_panel/poller.py:284` `cards.set_text_channel_name_if(c, card_id, slot` — the same compare-and-set for a text channel, keyed by slot: a name is written only while that slot still holds the link that was read.
- `src/fluxer_panel/poller.py:271` `event = "voice_refresh_failed" if slot is None` — a failed name check is logged with the card id and the exception type only. No channel name reaches the log.
- `src/fluxer_panel/poller.py:521` `cards.set_hash_if(c, card_id` — the rendered hash is stored only if the message is still the same one, so a delete during an edit is not written back.
- `src/fluxer_panel/poller.py:46` `_DELETED_CAP = 256` — the remembered deletes are bounded. The oldest entries are evicted, which covers the window between a send and its commit.
- `src/fluxer_panel/poller.py:535` `self._deleted[(channel_id, message_id)] = None` — a repeated delete keeps its place, so eviction is by insertion order.
- `src/fluxer_panel/__main__.py:98` `poller_task.cancel()` — the poller is cancelled and awaited before the purge task, so no card write is in flight when the session closes.

- `src/fluxer_panel/poller.py:349` `log_event(log, "board_post_failed"` — a failed board post is logged with the games channel id and the exception type only. No title or status is logged.
- `src/fluxer_panel/poller.py:426` `board.set_hash_if(c, channel_id` — the board hash is stored only while the board still holds that message, so a delete during an edit is not written back.
- `src/fluxer_panel/poller.py:557` `if cards.clear_message_if(conn, channel_id, message_id) is not None:` — a MESSAGE_DELETE forgets a card message, a card's copy or the board message, in one transaction. Message ids are unique, so only one of the three can match.
- `src/fluxer_panel/poller.py:413` `await self._dm_admins(text.BOARD_PIN_ALERT` — each admin gets the pin request once per new board message. A board message deleted before its id is stored sends none.
- `src/fluxer_panel/poller.py:367` `channel_id = state.channel_id if state.channel_id is not None else games_channel_id` — a stored board is edited in the channel it was posted in, so a change of `GAMES_CHANNEL_ID` never moves it. Its community is looked up for that channel.
- `src/fluxer_panel/poller.py:362` `if state.message_id is None and not may_post:` — when this tick's first read of the stored board failed, no new board is posted in that tick, so a board is never posted while the stored state is unknown.
- `src/fluxer_panel/poller.py:458` `await self._sync(card_id, post_channel=sub.channel_id, edit_channel=sub.channel_id,` — a copy's post and edit run the card's own sync path, so each send carries the card's `allowed_mentions`. One copy's failure is caught here and does not stop the others.
- `src/fluxer_panel/poller.py:478` `await self._dm_admins(alert.format(slug=slug, channel_id=channel_id),` — the pause DM takes a fixed text, the card's slug (a-z, 0-9 and -) and a channel id. No title or other card text reaches it, and the channel is shown as a mention.
- `src/fluxer_panel/text.py:144` `SUBSCRIPTION_PAUSED_ALERT = (` — the 403 pause DM's text. Its placeholders are the card's slug and the channel id, and nothing else.

## Private denylist

- `tests/test_repo_hygiene.py:484` `return Path(common).parent / DENYLIST_RELATIVE` — the denylist lives in the main checkout's `docs/private/`, found through the git common directory, so a worktree reads the same file. The path is computed and the file is not opened here.
- `tests/test_repo_hygiene.py:512` `offenders.append(f"{path.relative_to(root)}:{number}")` — a finding is the path and the line only. The denylisted literal is never printed. Secret files are skipped before any read.
- `tests/test_repo_hygiene.py:560` `pytest.skip("private denylist absent")` — where the denylist is absent, as in the published split repository, the test skips.

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

- **Brief assumed:** the brief's reaction buttons (`▶️ 🙋 📅 🔔`) are unicode emoji. It does not say what the events look like.
- **True:** a unicode emoji has no `id` key at all. It is not `id: null`.
- **Consequence:** the parser handles the missing key, and `encode_emoji` percent-encodes unicode (`src/fluxer_client/rest.py:63` `return quote(emoji, safe="")`). Matching a reaction to a button must compare names, not ids.

### DM reactions

- **Brief assumed:** the brief says reactions act as buttons and that DM flows stand in for modals. It does not say what DM reaction events carry.
- **True:** DM reactions have no `guild_id` and no `member`, so there are no roles. They carry an undocumented 32-character `session_id`.
- **Consequence:** DM reactions cannot be role-gated, so treat DMs as having no guild roles. The DM `session_id` is not parsed into any event. The reaction dataclasses have no field for it, and the reaction parser builds them from named fields only (`src/fluxer_client/events.py:171` `return ReactionAdd(`). Panel code never sees it. Any future parser that reads it must keep it `repr=False` and must not log it.

### Bulk reaction events

- **Brief assumed:** the phase 1 plan asked for the `MESSAGE_REACTION_ADD_MANY` shape.
- **True:** it was not observed. The live run also did not observe `MESSAGE_REACTION_REMOVE_ALL` or `MESSAGE_REACTION_REMOVE_EMOJI`.
- **Consequence:** `parse` returns `None` for `ADD_MANY` without logging, so a bulk add is silently dropped (`src/fluxer_client/events.py:243` `MESSAGE_REACTION_ADD_MANY`). Capture the shape before any feature depends on bulk reactions.

### Event parser

- **Brief assumed:** the phase 1 plan anticipated event shape corrections from the Phase 3 live review.
- **True:** the live run matched the parser, and no parser change was needed (`docs/fluxer-notes.md`).
- **Consequence:** the fixtures in `tests/test_events.py` reproduce the observed shapes and are the baseline. Re-run the live scripts whenever the server version changes.

### Interactive live scripts need a terminal

- **Brief assumed:** the phase 1 plan did not say how the live scripts would take input.
- **True:** `reaction_remove.py` waits for Enter on stdin. Run from Claude Code's `!` prompt, stdin is closed, and the script exits with `EOFError` before the permission step.
- **Consequence:** live scripts run in a real terminal. `wait_for_enter` in `tools/live/_live.py` reads stdin in a daemon thread, so Ctrl+C still exits with status 130.
