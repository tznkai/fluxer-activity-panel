<!-- SPDX-FileCopyrightText: 2026 K. Chen -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Fluxer Activity Panel

Last verified: 2026-10-31

Standalone project: `fluxer_client` (a reusable Fluxer REST and gateway client) and, from Phase 4,
`fluxer_panel` (the activity panel built on it). `README.md` has the user-facing description.

## Purpose and boundary

- `fluxer_client` (`src/fluxer_client/`) is the shared Fluxer layer. It never imports `fluxer_panel`.
- `fluxer_panel` (`src/fluxer_panel/`) is the activity panel. It imports only from `fluxer_client`'s public modules.
- No code here imports the source bot package. A test (`tests/test_repo_hygiene.py`) enforces this.

## Commands (run from this directory)

- Venv: `uv venv -p 3.12 .venv` then `uv pip install --python .venv/bin/python -e '.[dev]'`
- Tests: `.venv/bin/python -m pytest -q -W error` (warnings are errors)
- Lint: `.venv/bin/ruff check src tests tools`
- Licensing: `.venv/bin/reuse --root . lint`
- Run the panel: `.venv/bin/python -m fluxer_panel [run|purge]` (`run` is the default; `purge` is a stub until Phase 6).
  Config comes from the environment only. The code never loads `.env`; the operator sources it in their shell.
- CI (`.github/workflows/ci.yml`) runs these three on every push and pull request.

## Rules

- This directory is public: it is published by git subtree split. Keep it self-contained.
- No secrets, IDs, hostnames or instance details in tracked files. Test fixtures use fake values
  (`"1001"`, `"https://fluxer.invalid"`). Never write a run of 15+ digits.
- Never read or print `.env`. `.env.example` is the template and is the only tracked env file.
- Every new file needs an SPDX header or a `REUSE.toml` entry, or `reuse lint` fails.
- Do not modify `../green-oaks-bot`. It is the source of the copied client code and is read-only here.

## Layout

- `src/fluxer_client/ids.py`, `rest.py`, `gateway.py`, `roles.py`: the client modules.
- `src/fluxer_client/events.py`: parses gateway dispatches into frozen dataclasses; unknown or malformed payloads return `None` and log the field name only.
- `rest.py` `encode_emoji(emoji)`: validates a reaction emoji and percent-encodes unicode (or passes `name:id` custom emoji).
- `rest.py` `UNSET`: sentinel for "argument not given", distinct from `None` (which means JSON null).
- `rest.py` `FluxerREST.edit_message(channel_id, message_id, *, content, embeds)`: PATCH with only the fields given.
- `rest.py` `FluxerREST.add_reaction(channel_id, message_id, emoji)`: PUT the caller's reaction.
- `rest.py` `FluxerREST.remove_reaction(channel_id, message_id, emoji, user_id)`: DELETE one user's reaction.
- `rest.py` `FluxerREST.add_member_role(guild_id, user_id, role_id, *, reason)`: PUT a member role, with an optional audit reason.
- `rest.py` `FluxerREST.remove_member_role(guild_id, user_id, role_id, *, reason)`: DELETE a member role, with an optional audit reason.
- `rest.py` `FluxerREST.open_dm(user_id)`: POST to open or reuse a DM channel and return its id.
- `tests/fakes.py`: fake Fluxer REST and gateway servers used by the tests.
- `tools/live/_live.py`: the shared harness for the hand-run live scripts (never run by CI; they need a local `.env`).
  `load_env` refuses unless the required variables are set; `check_safety` refuses unless the channel is in the guild,
  the user is a member and the guild has at most 2 members. Refusals name the check, never a value. `Redactor` replaces
  the token with `[REDACTED]`, known ids with labels and other 15+ digit runs with `<id>`; `out()` is the only print path.
  `main()` exits 0 on pass, 1 on any FAIL or error (type and redacted message, no traceback), 2 on a refusal and 130 on Ctrl+C.
  The redactor is resolved when an error is printed, so it applies even when the error happens inside the run.
  Scripts wait for the operator with `wait_for_enter()` (stdin read in a daemon thread), never `input()` or
  `to_thread(input)`: a blocked non-daemon read keeps Ctrl+C from exiting 130 until Enter is pressed.
- `tools/live/` scripts: `reactions.py` (guild and DM reaction add and remove), `edit.py` (edit content and embeds),
  `reaction_remove.py` (removal with and without Manage Messages), `roles.py` (member role add and remove) and
  `dm.py` (open a DM, send, receive the reply and re-open).
- `src/fluxer_panel/config.py`: `Config.from_env()` reads `FLUXER_TOKEN`, `FLUXER_INSTANCE_URL` or `FLUXER_API_URL`,
  `FLUXER_GATEWAY_URL`, `ADMIN_USER_IDS` (required), `COMMAND_PREFIX`, `LOG_LEVEL`, `DATA_DIR`, `DB_PATH`,
  `PURGE_STATUS_PATH` and `PURGE_INTERVAL_SECONDS`. Errors name the variable, never its value. The token is not in `repr`.
- `src/fluxer_panel/logs.py`: `log_event`, `clean_fields`, `JsonFormatter` and `setup_logging`.
- `src/fluxer_panel/admin.py`: `authorize`, `parse_command` and `handle` for `<prefix>admin <sub>` (`ping` only).
- `src/fluxer_panel/bot.py`: `Bot.on_dispatch` routes typed gateway events. Guild messages are ignored; DMs go to `admin.handle`.
- `src/fluxer_panel/__main__.py`: `main()` parses `run|purge`, loads config, sets up logging and runs. `run()` resolves
  endpoints, then stops on SIGINT or SIGTERM and logs `shutting_down`.

## fluxer_panel rules

- Exit codes: 0 clean stop (a signal, or a gateway that returns without error); 1 fatal runtime or gateway error
  (`FatalGatewayError` or any uncaught exception); 2 config or usage error. Exit code 3 arrives in Phase 5.
- Logging: panel code logs only through `log_event(logger, name, level=..., **fields)`. It never logs message content,
  user or channel names, or URLs. `clean_fields` allowlists the fields: id keys must be snowflakes, and other keys must be
  numbers, bools or identifier-like strings. Known exception: the last-resort handler in `__main__.main` calls
  `log.exception("fatal_error")`. A traceback can carry any exception text, so no code enforces the no-URL rule there.
  `fluxer_client` records (gateway and rest) are plain-text messages that only get token redaction and may contain URLs and exception text.
- `setup_logging` replaces the root handlers with one JSON handler on stdout. It installs `TokenRedactionFilter`, which
  replaces the token, in raw and repr- or json-escaped form, in the message, args and traceback. A malformed format
  call does not raise. It also calls `logging.captureWarnings(True)`, so Python warnings are JSON lines too.
- Authorization: `admin.authorize` is the single authorization check. It requires a DM (no guild id), a non-bot author,
  and an author id in `ADMIN_USER_IDS`. Every admin command goes through `handle`, which calls `authorize` before acting.
  A denial logs `admin_denied`.
