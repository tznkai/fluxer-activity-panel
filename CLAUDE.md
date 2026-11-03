<!-- SPDX-FileCopyrightText: 2026 K. Chen -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Fluxer Activity Panel

Last verified: 2026-11-03

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
  Cards: `cards.py` (store and dataclasses), `card_fields.py` (field validators), `card_commands.py` (`!card`
  subcommands), `resolver.py` (status from manager and probe results), `render.py` (embed, outside text and the
  summary board's payload), `text.py` (user-facing strings), `poller.py` (the single card poller, which also keeps
  the board). The summary board's single-row store is `board.py`.
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
- Admin: `admin.authorize` is the single check: DM only (no guild id), non-bot author, author id a valid snowflake, author id in
  `ADMIN_USER_IDS`. Every admin command goes through `handle`, which calls `authorize` first. Denials log `admin_denied`.
  The bot ignores its own and other bots' messages before routing.
- No code in `fluxer_panel` calls the role methods yet. Role changes are a privilege change: a future caller must
  check the role allowlist and hierarchy before the call, because Fluxer returns the same 403 for a hierarchy refusal
  and a missing permission.

- Card config: `GAMES_CHANNEL_ID` (a snowflake, optional). `PTERODACTYL_URL` and `PTERODACTYL_API_KEY` are both or
  neither, and setting one refuses to start with exit 2. `POLL_INTERVAL_SECONDS`, `SCHEDULE_REFRESH_SECONDS`,
  `POLL_CONCURRENCY`, `PROBE_TIMEOUT_SECONDS` and `PROBE_GRACE_SECONDS` are positive integers with defaults 60, 600,
  8, 3 and 600. `README.md` lists every variable.
  `FLUXER_INSTANCE_URL` is at most 128 characters (`config.INSTANCE_URL_MAX`), which keeps the card's Channels
  field inside Fluxer's 1024-character field limit. `FLUXER_API_URL` has no cap.

## Cards and sources rules

- A plugin's `power_state` and `probe` never raise. `validate_ref` raises only `CardFieldError`, and
  `next_power_action` may raise `SourceError`, which the caller hides.
- No library message, Panel body, player name or command payload leaves a plugin. Errors leave as fixed reason keys.
- The Pterodactyl module is GET-only over three read paths. `tests/test_pterodactyl_ast.py` enforces it.
- Every card send and edit uses `allowed_mentions={"parse": []}` and goes through `render`.
- `outside_location` takes a bool, never the location text.
- Card replies and card text live in `text.py`, which never says "guild". `tests/test_text.py` enforces it.
- Adding a plugin is one module plus one line in `sources/registry.py`.
- Summary board (`render.board_message`, `board.py`, `poller.py`): one message in `GAMES_CHANNEL_ID`, always on when
  that is set. Its payload comes only from `render.board_message`, which escapes each title with `escape`, fits it to
  `LIMITS["board_title"]` and keeps the whole message under `BOARD_CHAR_LIMIT`. Its statuses are the cards' own status
  lines from the resolver. A status is a link only through `jump_link`, and only for a card whose message is in the
  games channel. Board sends and edits use `allowed_mentions={"parse": []}`. The poller posts the board before the
  cards when none is stored, edits it after the cards when its hash changed, and sends the admins the pin request once
  per new message. The poller logs the board's ids and exception types only: never a title, a status or a link.

## Database

- `db.py` owns the single SQLite connection. All DB work goes through `await db.run(fn)`: one transaction per call,
  on a single-thread executor.
- Startup order: read-only integrity and version check; quarantine on corruption (never reset to an empty DB);
  refuse a newer schema without touching the file; otherwise back up to `<db>.pre-v<N>` and apply pending migrations.
  The directory and file pre-checks (`_check_directory`, `_check_writable`) run before any lookup or `_inspect`,
  and the write probe in `_connect` stays authoritative.
- Primary codes READONLY, CANTOPEN, BUSY and PERM map to `DatabaseRefused("unavailable")` and leave the file alone.
  Only corruption codes quarantine. Quarantine moves files aside under new names and never overwrites.
- `board` (migration 0004) holds one row, `id = 1`, seeded by the migration. Its `channel_id` and `message_id` are set
  together or not at all, and `content_hash` is set only with a message. The board's writes are compare-and-set, as
  the cards' are: `clear_message_if`, and `set_hash_if` for the hash.
- Schema: every table is `STRICT`. Ids are `TEXT` with CHECKs that they are digits only and at most 25 long; names are
  not stored, except a verified voice or text channel's display name (`card_voice.channel_name`,
  `card_text_channel.channel_name`). The snowflake bound is 25 in code and schema; Fluxer documents at most 19, so do
  not tighten without a new migration.
- Migrations: `src/fluxer_panel/migrations/NNNN_name.sql`, numbered and contiguous from `0001`. Never edit a migration
  after release; add a new file instead. Do not put `BEGIN`, `COMMIT` or `PRAGMA user_version` in a file: the runner adds them.
- `secure_delete = ON` is set, so purged rows do not survive in free pages. `.pre-v<N>` backups are not covered by that
  and keep rows purged before they were taken.

## Retention purge (`purge.py`)

- A non-recurring session expires when `ends_at` is more than 7 days ago. A recurring session expires when
  `recurrence_until` is more than 7 days ago. An open-ended series (`recurrence_until` NULL) is never purged.
  RSVPs go with their session by `ON DELETE CASCADE`.
- A request expires when `created_at` is more than 30 days ago, whatever its status.
- Cutoffs are strict: a row exactly at a cutoff is kept. One transaction per run, then a WAL checkpoint.
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
