<!-- SPDX-FileCopyrightText: 2026 K. Chen -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Fluxer Activity Panel

Last verified: 2026-10-31

Standalone project: `fluxer_client` (a reusable Fluxer REST and gateway client) and, from Phase 4,
`fluxer_panel` (the activity panel built on it). `README.md` has the user-facing description.

## Purpose and boundary

- `fluxer_client` (`src/fluxer_client/`) is the shared Fluxer layer. It never imports `fluxer_panel`.
- `fluxer_panel` (planned, not yet in `src/`) imports only from `fluxer_client`'s public modules.
- No code here imports the source bot package. A test (`tests/test_repo_hygiene.py`) enforces this.

## Commands (run from this directory)

- Venv: `uv venv -p 3.12 .venv` then `uv pip install --python .venv/bin/python -e '.[dev]'`
- Tests: `.venv/bin/python -m pytest -q -W error` (warnings are errors)
- Lint: `.venv/bin/ruff check src tests tools`
- Licensing: `.venv/bin/reuse --root . lint`
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
- `tests/fakes.py`: fake Fluxer REST and gateway servers used by the tests.
- `tools/live/`: live-instance helper scripts (Phase 3). `.gitkeep` keeps the directory present until then.
