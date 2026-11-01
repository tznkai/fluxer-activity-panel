<!-- SPDX-FileCopyrightText: 2026 K. Chen -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Fluxer activity panel

This project holds two Python packages under `src/`:

- `fluxer_client`: a panel-agnostic Fluxer client with REST, gateway, role cache
  and snowflake id helpers.
- `fluxer_panel`: the activity panel bot built on `fluxer_client`. It is added in a
  later phase.

Configuration comes from environment variables. See `.env.example` for the names.

## Development

```sh
uv venv -p 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
```

Run the three checks:

```sh
.venv/bin/python -m pytest -q -W error
.venv/bin/ruff check src tests tools
.venv/bin/reuse --root . lint
```

## Exit 3: database refused

The panel opens its SQLite database before it connects to Fluxer. If the database
cannot be used safely, it exits with code 3 and logs one of these events. Nothing is
connected and the file is not written to except as described.

- **`db_quarantined` or `db_quarantine_pending`**: the database was corrupt (or failed
  its integrity check). The panel moved `panel.db` (and any `-wal`/`-shm` files) to
  `panel.db.corrupt`, or `.corrupt.1` and so on on repeat. The panel refuses every
  start until the operator acts. Either restore `panel.db` from `panel.db.corrupt` if
  that file is recoverable, from a `panel.db.pre-v<N>` backup, or from an external
  backup; or move the `panel.db.corrupt*` files out of the data directory. Starting
  with an empty database is always a deliberate operator choice: the panel never
  creates one on its own while quarantined files are present.
- **`db_schema_newer`**: the database was written by a newer version of the code (its
  schema version is higher than this build knows). The file is left unchanged. Deploy
  the code that knows that schema version (for example, the code was rolled back, and
  the newer release should be redeployed).
- **`db_unavailable`**: the database exists but could not be opened (permissions, or a
  lock held by another process). The file was not touched. Fix the file permissions or
  release the lock, then start again.

Exit 1 with a `fatal_error` event means something else failed while opening the database,
such as an unwritable data directory. That is not a refusal and does not quarantine anything.
