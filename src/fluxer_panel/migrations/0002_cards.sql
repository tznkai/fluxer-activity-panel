-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- v2: cards with slots and status sources (fluxer-panel-cards).
-- Snowflake check used throughout: x <> '' AND x NOT GLOB '*[^0-9]*' AND length(x) <= 25

ALTER TABLE cards ADD COLUMN slug TEXT
  CHECK (slug IS NULL OR (length(slug) BETWEEN 1 AND 32 AND slug NOT GLOB '*[^a-z0-9-]*'));
CREATE UNIQUE INDEX cards_slug ON cards(slug);
ALTER TABLE cards ADD COLUMN how_to_join TEXT
  CHECK (how_to_join IS NULL OR length(how_to_join) BETWEEN 1 AND 1000);
ALTER TABLE cards ADD COLUMN first_time TEXT
  CHECK (first_time IS NULL OR length(first_time) BETWEEN 1 AND 1000);
ALTER TABLE cards ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0
  CHECK (sort_order BETWEEN 0 AND 9999);
ALTER TABLE cards ADD COLUMN show_details TEXT NOT NULL DEFAULT ''
  CHECK (show_details IN ('', 'map', 'version', 'game_mode', 'map,version', 'map,game_mode',
                          'version,game_mode', 'map,version,game_mode'));
ALTER TABLE cards ADD COLUMN state TEXT NOT NULL DEFAULT 'draft'
  CHECK (state IN ('draft', 'live', 'archived'));
ALTER TABLE cards DROP COLUMN kind;
ALTER TABLE cards DROP COLUMN archived;

-- Never written by any code: replaced or recreated.
DROP TABLE card_server;
DROP TABLE card_location;
CREATE TABLE card_location (
  card_id INTEGER PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
  location_text TEXT NOT NULL CHECK (length(location_text) BETWEEN 1 AND 200)
) STRICT;

CREATE TABLE card_voice (
  card_id INTEGER PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
  guild_id TEXT NOT NULL CHECK (guild_id <> '' AND guild_id NOT GLOB '*[^0-9]*' AND length(guild_id) <= 25),
  channel_id TEXT NOT NULL CHECK (channel_id <> '' AND channel_id NOT GLOB '*[^0-9]*' AND length(channel_id) <= 25),
  channel_name TEXT CHECK (channel_name IS NULL OR length(channel_name) BETWEEN 1 AND 100),
  verified INTEGER NOT NULL CHECK (verified IN (0, 1)),
  CHECK (verified = 1 OR channel_name IS NULL)
) STRICT;

CREATE TABLE card_address (
  card_id INTEGER PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
  host TEXT NOT NULL CHECK (length(host) BETWEEN 1 AND 253),
  port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
  public INTEGER NOT NULL DEFAULT 0 CHECK (public IN (0, 1))
) STRICT;

CREATE TABLE card_manager (
  card_id INTEGER PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 32 AND kind NOT GLOB '*[^a-z0-9_]*'),
  ref TEXT NOT NULL CHECK (length(ref) BETWEEN 1 AND 64)
) STRICT;

CREATE TABLE card_probe (
  card_id INTEGER PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 32 AND kind NOT GLOB '*[^a-z0-9_]*'),
  host TEXT NOT NULL CHECK (length(host) BETWEEN 1 AND 253),
  port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535)
) STRICT;

CREATE TABLE card_runtime (
  card_id INTEGER PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
  rendered_hash TEXT
    CHECK (rendered_hash IS NULL OR (length(rendered_hash) = 64 AND rendered_hash NOT GLOB '*[^0-9a-f]*')),
  probe_failing_since INTEGER,
  probe_alert_sent INTEGER NOT NULL DEFAULT 0 CHECK (probe_alert_sent IN (0, 1))
) STRICT;

ALTER TABLE sessions ADD COLUMN channel_id TEXT
  CHECK (channel_id IS NULL OR (channel_id <> '' AND channel_id NOT GLOB '*[^0-9]*' AND length(channel_id) <= 25));
ALTER TABLE sessions ADD COLUMN message_id TEXT
  CHECK (message_id IS NULL OR (message_id <> '' AND message_id NOT GLOB '*[^0-9]*' AND length(message_id) <= 25));
