<!-- SPDX-FileCopyrightText: 2026 K. Chen -->
<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

# Fluxer activity panel

This project holds two Python packages under `src/`:

- `fluxer_client`: a panel-agnostic Fluxer client with REST, gateway, role cache
  and snowflake id helpers.
- `fluxer_panel`: the activity panel bot built on `fluxer_client`.

Configuration comes from environment variables. See `.env.example` for the names. The code
never loads `.env`; the operator sources it in the shell.

## Configuration

Every setting is an environment variable. A setting marked "default" can be left unset.
Positive-integer settings must be whole numbers above zero.

Core:

- `FLUXER_TOKEN` (required): the bot token. It is never logged, and `repr` does not show it.
- `FLUXER_INSTANCE_URL` (required unless `FLUXER_API_URL` is set): the instance's base URL, at most
  128 UTF-16 code units (an emoji or other character outside the Basic Multilingual Plane counts two). The
  API and gateway addresses are discovered from it.
- `FLUXER_API_URL` (optional): the API base URL. When it is set, no discovery is needed.
- `FLUXER_GATEWAY_URL` (optional): the gateway URL (`ws` or `wss`). When it is unset, the API
  reports the gateway address.
- `ADMIN_USER_IDS` (required): comma-separated user ids that may run admin and `!card` commands
  in direct messages.
- `COMMAND_PREFIX` (default `!`).
- `DATA_DIR` (default `data`): the directory for the database and the purge status file.
- `DB_PATH` (default `DATA_DIR/panel.db`).
- `PURGE_STATUS_PATH` (default `DATA_DIR/purge-status.json`).
- `PURGE_INTERVAL_SECONDS` (default `3600`).
- `LOG_LEVEL` (default `INFO`): one of `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`.

Cards and sources:

- `GAMES_CHANNEL_ID` (optional): the channel where published cards and the summary board are posted.
  Without it, no card or board is posted, and the log says `games_channel_unset`.
  Changing it does not move anything already posted. A card or the board stays in the channel it was
  posted in, and its edits go there. New cards go to the new channel. To move the board or a card,
  delete its message by hand, and the bot posts it again in the current channel.
- `PTERODACTYL_URL` and `PTERODACTYL_API_KEY`: the Panel's base URL and a client API key. Set both
  or neither. Setting only one refuses to start with exit code 2. Without both, no Panel host
  manager is registered.
- `POLL_INTERVAL_SECONDS` (default `60`): how often the card poller runs when nothing kicks it.
- `SCHEDULE_REFRESH_SECONDS` (default `600`): how often a card re-reads its Panel schedules and the
  names of its verified voice and text channels.
- `POLL_CONCURRENCY` (default `8`): how many cards are polled at once.
- `PROBE_TIMEOUT_SECONDS` (default `3`): the timeout for one probe.
- `PROBE_GRACE_SECONDS` (default `600`): how long a running server's failing probe is tolerated
  before its card shows Ready without counts. The admins then get one direct message.

Hand-run tools only (`tools/live/`, see `CLAUDE.md`): `LIVE_GUILD_ID`, `LIVE_CHANNEL_ID`,
`LIVE_USER_ID`, `LIVE_ROLE_ID`, `LIVE_HIGH_ROLE_ID`, `LIVE_PTERO_SERVERS`, `LIVE_PROBES`,
`LIVE_VOICE_CHANNEL_ID` and `LIVE_OTHER_GUILD_ID`. The
bot never reads them. `.env.example` lists them all.

## Commands

- `python -m fluxer_panel run` (the default) connects to Fluxer and runs the bot.
- `python -m fluxer_panel purge` runs one retention purge, writes the status file and exits.
  It needs the same configuration as the bot (`FLUXER_TOKEN`, the instance URL and
  `ADMIN_USER_IDS` are all required), but it never connects to Fluxer. It exits 0 when the
  purge succeeded, 1 when it failed (the log has `purge_failed`), 2 on a configuration error
  and 3 when the database is refused.

## Retention purge

`run` purges once at startup, after it has resolved its endpoints, and then every
`PURGE_INTERVAL_SECONDS` (default 3600). A purge that fails is logged and recorded in the
status file, and the bot keeps running.

The purge deletes:

- a session that does not recur and ended more than 7 days ago. Its RSVPs go with it.
- a recurring session whose series ended (`recurrence_until`) more than 7 days ago. A series
  with no end date is never purged.
- a request created more than 30 days ago, whatever its status.

The cutoffs are strict: a row exactly at a cutoff is kept.

### Status file

The status file is `purge-status.json` in the data directory, or the path in
`PURGE_STATUS_PATH`. It is rewritten after every attempt. It is world-readable (mode 0644)
so a monitor running as another user can read it. It holds only timestamps and counts:

```json
{
 "last_success_at": 1767225600,
 "last_attempt_at": 1767225600,
 "ok": true,
 "deleted": {"sessions": 0, "rsvps": 0, "requests": 0},
 "schema_version": 1
}
```

- `last_success_at`: when the last successful purge ran. It is `null` until the first success,
  and a failed attempt keeps the previous value.
- `last_attempt_at`: when the latest attempt ran, successful or not.
- `ok`: whether the latest attempt succeeded. `deleted` holds that attempt's counts (zeros on failure).
  A checkpoint failure after the commit is reported as `ok: false` with zero `deleted`, but the rows may already be
  gone. The next run checkpoints again.

Alert when `last_success_at` is `null` or older than twice `PURGE_INTERVAL_SECONDS`. A missing
file means the bot has never run a purge.

## Cards

Admins manage cards by direct message with `!card` commands. A card is one message in the games
channel. It shows how to join, what a first-timer should know, and optional extras: a voice channel,
up to three text channels, a server address, an in-person location and seats. A card for a game
server can also show a status line, such as Asleep, Waking up or Ready with a player count.

### Lifecycle

- **Draft:** `!card new` creates a card as a draft. A draft is never posted.
- **Live:** `!card publish` makes the card live. The poller posts it in the games channel on its
  next tick, and edits the message when its content changes.
- **Archived:** `!card archive` deletes the card's message and keeps the card. Publishing it again
  makes it live.

A card message that is deleted by hand is posted again. The delete kicks the poller, so the
repost does not wait for the next poll interval.

### Summary board

When `GAMES_CHANNEL_ID` is set, the bot keeps one more message in the games channel: the summary
board. It is always on, with no setting of its own. It lists every live card, in the same order as
`!card list`, as one inline cell: the card's title as the name and its status line as the value. A
card with no manager and no probe shows a dash, and so does a card whose status check failed in the
last poll. When the card's message is in the board's channel, its status is a link to the card's
message. The board is laid out for the desktop and web clients. Phones show the full cards as before.

- **Posting:** when no board is stored, the board is posted before the cards on a fresh start, so it sits
  above them. The first post may show plain status text for cards that are not posted yet. The next edit
  adds the links.
- **The board's channel:** the board is posted in the games channel and then stays there. A stored board
  is edited where it was posted, whatever `GAMES_CHANNEL_ID` is now. Its links go to the channel's own
  community, and a card is linked only when its message is in the same channel as the board. To move the
  board, delete its message by hand. The bot then posts a new board in the current games channel.
- **Edits:** the board is edited when its content changes, for example when a status or a title changes.
  When a board is stored, each card is posted as soon as its own check ends, so a slow check does not hold
  back the other cards. The board is brought up to date after all the cards.
- **Pinning is manual.** When the bot posts a new board message, it sends each admin one direct message:
  "I posted the summary board in the games channel. Pin it so it stays at the top." Pin it in the channel
  where the board was posted. The bot does not pin it, and it sends that message once per new board message.
- **Size:** the board shows at most 240 cards. The bot keeps the whole board message under 6000 characters of
  text, which is Fluxer's limit on embed text. The count is in UTF-16 code units, so an emoji counts two. A
  board with long titles, long links or many cards shows only the cards that fit, and a note at the bottom
  says that some games are not shown.
- **Failed checks:** a card whose status check fails in a poll stays on the board, with the dash. Its
  message is not edited in that poll.
- **Deleted by hand:** a board message that is deleted is posted again, the same way a card is.
- **No live cards:** the board still posts, with "No games are live."

### Syntax

```
!card new <slug> <title…>                  creates a draft
!card set <slug> <field> <value…>          sets one field
!card clear <slug> <field>                 clears one field
!card show <slug>                          the inside embed and the outside text
!card list                                 slug, state and title, by sort order
!card publish <slug>                       draft or archived -> live
!card archive <slug>                       deletes the message, keeps the card
!card help                                 the subcommands and the field names
!card help fields                          each field, its format and its limits
```

The last argument takes the rest of the line, so no quoting is needed. `!card help` and
`!card help fields` are the replies to read when you forget a format or a limit.

### Fields

- `title` (up to 200 characters), `description`, `join` (how to join) and `first_time` (up to 1000
  characters each): text. Control characters and bidirectional overrides are refused.
- `sort`: an integer from 0 to 9999. `seats`: an integer from 1 to 999.
- `location`: text up to 200 characters. Outside Fluxer it shows only a fixed placeholder.
- `voice <guild_id> <channel_id>`, or a pasted channel link. When the bot is in that community,
  the channel must be a voice channel there, and its name is stored. When the bot is not in it,
  the channel is stored unverified and the reply says so.
- `text_channel1`, `text_channel2` and `text_channel3`: three slots for text channels. Each takes the same input
  as `voice`, and each is checked the same way, except that the channel must be a text channel there. Each
  slot is separate: `!card clear <slug> text_channel2` clears only the second. The channels are listed in one
  `Channels` field, in slot order.
- `address <host>:<port>`: the server address. `address_public yes` or `no` (the default `no`)
  decides whether the outside text shows it.
- `manager <kind> <ref>`: a configured host manager and its reference. Only `pterodactyl` exists.
- `probe <kind> <host> <port>`: a probe kind (`minecraft_bedrock`, `minecraft_java` or `a2s`) with
  its host and port.
- `show map,version,game_mode`: any subset of those three, or `none`. These are the probe details
  a card shows.

Every reply names the field and the rule, never the value you sent. Replies use the word
"community".

## Adding a host manager or probe

Plugins are modules in `src/fluxer_panel/sources/`.

1. Write a module that implements a protocol from `sources/base.py`: `HostManager` (`kind`,
   `validate_ref`, `power_state`, `next_power_action`) or `Probe` (`kind`, `probe`).
2. Add one line to `MANAGER_PLUGINS` or `PROBES` in `sources/registry.py`. A manager plugin is
   `ManagerPlugin(create, ref_hint)`: `create` returns `None` when its settings are absent, and `ref_hint` is the
   ref's shape for the card help (for example `<8-character server id>`). A manager module therefore also exposes a
   `REF_HINT` string. A blank hint is refused.
3. Add a fake under `tests/` and tests for the plugin, as the existing probe tests do.
4. Return `ProbeFailed` or `PowerState.UNKNOWN` instead of raising. `validate_ref` raises
   `CardFieldError`, and `next_power_action` may raise `SourceError`; the caller handles both.

A result or an error must never carry a library message, a Panel response body, a player name or
a command payload. Errors leave a plugin as fixed reason keys.

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
  its integrity check). The panel moved `panel.db` (and any `-wal`, `-shm` or `-journal` files) to
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
- **`db_unavailable`**: the database, or the data directory it lives in, cannot be read or
  written by this process, or the database could not be used. The file was not touched.
  The panel checks that it can write the database before it connects, so this covers:
  - the data directory is not readable, writable and searchable by this process. The panel
    needs to read it to look for quarantined files, and to write and search it to create
    `panel.db`, `panel.db-wal` and `panel.db-shm`. This includes a missing database in a
    read-only data directory, and a data directory that cannot be created because a parent
    directory is not writable. Fix the permissions or ownership of the data directory, then
    start again. A refused start creates no new side files.
  - a database file that is not readable or writable. Fix the permissions or ownership of
    `panel.db`, then start again.
  - another connection holding the write lock for longer than 5 seconds. Stop the other
    process, then start again.
  - a `panel.db-journal` file next to `panel.db`. This is a pre-WAL rollback journal left
    behind by an interrupted transaction, and the panel refuses to read past it. The panel
    does not roll it back itself. Open the database once with a read-write SQLite client (for
    example the `sqlite3` shell, where it is installed, running `PRAGMA user_version;`). That
    open rolls the journal back and removes it. Do not delete the journal by hand.

The panel does not prevent a second instance from running against the same database.
Running two panels against one database is unsupported.

Exit 1 with a `fatal_error` event means something else failed while opening the database,
such as the data directory could not be created (for example, its parent is a regular file).
That is not a refusal and does not quarantine anything.
