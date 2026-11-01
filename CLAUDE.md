<!-- SPDX-FileCopyrightText: 2026 K. Chen -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Fluxer Activity Panel

Last verified: 2026-11-01

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
- `tests/`: pytest suite. `tests/fakes.py` holds the fake REST and gateway servers. `tests/test_review_refs.py`
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
  user or channel names, or URLs. `clean_fields` allowlists fields: id keys must be snowflakes, other keys must be
  numbers, bools or identifier-like strings. Known exception: `__main__.main` calls `log.exception("fatal_error")`,
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

## Database

- `db.py` owns the single SQLite connection. All DB work goes through `await db.run(fn)`: one transaction per call,
  on a single-thread executor.
- Startup order: read-only integrity and version check; quarantine on corruption (never reset to an empty DB);
  refuse a newer schema without touching the file; otherwise back up to `<db>.pre-v<N>` and apply pending migrations.
  The directory and file pre-checks (`_check_directory`, `_check_writable`) run before any lookup or `_inspect`,
  and the write probe in `_connect` stays authoritative.
- Primary codes READONLY, CANTOPEN, BUSY and PERM map to `DatabaseRefused("unavailable")` and leave the file alone.
  Only corruption codes quarantine. Quarantine moves files aside under new names and never overwrites.
- Schema: every table is `STRICT`. Ids are `TEXT` with CHECKs that they are digits only and at most 25 long; names are not
  stored. The snowflake bound is 25 in code and schema; Fluxer documents at most 19, so do not tighten without a new migration.
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
- Only the private guild may be used. `check_safety` refuses unless the channel is in the guild and the user is a member.
  It also refuses if the guild reports more than two members (no refusal when the count is not reported).
- Never print the token or raw ids. `Redactor` replaces the token with `[REDACTED]`, labels known ids and replaces any
  other 15+ digit run with `<id>`. `out()` is the only print path. Error output is the exception type and redacted message.
- Exit codes: 0 pass; 1 any FAIL or error; 2 refusal; 130 Ctrl+C.
- Scripts wait with `wait_for_enter()` (a daemon-thread read), never `input()`, so Ctrl+C still exits 130.
  Run them in a real terminal, not a non-interactive prompt.
- Scripts: `reactions.py`, `edit.py`, `reaction_remove.py`, `roles.py`, `dm.py`. Removal is exercised only in a guild channel;
  DM removal is untested (see `docs/fluxer-notes.md`).
