-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- v3: up to three text channel links per card, by slot (fluxer-panel-cards).
-- Same CHECKs as card_voice. A verified channel's display name is the only name stored.

CREATE TABLE card_text_channel (
  card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  slot INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 3),
  guild_id TEXT NOT NULL CHECK (guild_id <> '' AND guild_id NOT GLOB '*[^0-9]*' AND length(guild_id) <= 25),
  channel_id TEXT NOT NULL CHECK (channel_id <> '' AND channel_id NOT GLOB '*[^0-9]*' AND length(channel_id) <= 25),
  channel_name TEXT CHECK (channel_name IS NULL OR length(channel_name) BETWEEN 1 AND 100),
  verified INTEGER NOT NULL CHECK (verified IN (0, 1)),
  CHECK (verified = 1 OR channel_name IS NULL),
  PRIMARY KEY (card_id, slot)
) STRICT;
