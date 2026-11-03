-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- v5: card subscriptions, copies of a card in other text channels (fluxer-panel-cards).
-- One row per card and channel. A hash is kept only with a message. The cap of five per card is enforced by
-- subscriptions.add, in the same transaction as the insert, and not by this schema.
-- Snowflake check used throughout: x <> '' AND x NOT GLOB '*[^0-9]*' AND length(x) <= 25

CREATE TABLE card_subscription (
  card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  guild_id TEXT NOT NULL CHECK (guild_id <> '' AND guild_id NOT GLOB '*[^0-9]*' AND length(guild_id) <= 25),
  channel_id TEXT NOT NULL CHECK (channel_id <> '' AND channel_id NOT GLOB '*[^0-9]*' AND length(channel_id) <= 25),
  message_id TEXT CHECK (message_id IS NULL OR (message_id <> '' AND message_id NOT GLOB '*[^0-9]*'
                                                AND length(message_id) <= 25)),
  content_hash TEXT CHECK (content_hash IS NULL OR (length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*')),
  paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
  CHECK (message_id IS NOT NULL OR content_hash IS NULL),
  PRIMARY KEY (card_id, channel_id)
) STRICT;
