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
