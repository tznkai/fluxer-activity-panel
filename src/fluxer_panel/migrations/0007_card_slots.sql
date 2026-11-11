-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- v7: three slots each for voice channels, text channels and addresses, each with an optional suffix.
-- Snowflake check used throughout: x <> '' AND x NOT GLOB '*[^0-9]*' AND length(x) <= 25
-- Suffix check, the same on all three tables: 1-20 characters, trimmed, no double space, and none of the ASCII
-- characters the validator refuses (control characters, DEL and every ASCII punctuation mark except - + . ').
-- The validator (card_fields.suffix) is the real rule; this is the second layer.

-- card_voice: rebuilt with a slot. Each existing row becomes slot 1, with no suffix.
CREATE TABLE card_voice_new (
  card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  slot INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 3),
  guild_id TEXT NOT NULL CHECK (guild_id <> '' AND guild_id NOT GLOB '*[^0-9]*' AND length(guild_id) <= 25),
  channel_id TEXT NOT NULL CHECK (channel_id <> '' AND channel_id NOT GLOB '*[^0-9]*' AND length(channel_id) <= 25),
  channel_name TEXT CHECK (channel_name IS NULL OR length(channel_name) BETWEEN 1 AND 100),
  verified INTEGER NOT NULL CHECK (verified IN (0, 1)),
  suffix TEXT CHECK (suffix IS NULL OR (length(suffix) BETWEEN 1 AND 20 AND suffix = trim(suffix, ' ')
    AND instr(suffix, '  ') = 0
    AND suffix NOT GLOB '*[]' || char(1) || '-' || char(31) || char(127) || '!"#$%&()*,/:;<=>?@[\^_`{|}~]*')),
  CHECK (verified = 1 OR channel_name IS NULL),
  PRIMARY KEY (card_id, slot)
) STRICT;

INSERT INTO card_voice_new (card_id, slot, guild_id, channel_id, channel_name, verified, suffix)
SELECT card_id, 1, guild_id, channel_id, channel_name, verified, NULL FROM card_voice;

DROP TABLE card_voice;
ALTER TABLE card_voice_new RENAME TO card_voice;

-- card_address: rebuilt with a slot. Each existing row becomes slot 1 with its public flag, and no suffix.
CREATE TABLE card_address_new (
  card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  slot INTEGER NOT NULL CHECK (slot BETWEEN 1 AND 3),
  host TEXT NOT NULL CHECK (length(host) BETWEEN 1 AND 253),
  port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
  public INTEGER NOT NULL DEFAULT 0 CHECK (public IN (0, 1)),
  suffix TEXT CHECK (suffix IS NULL OR (length(suffix) BETWEEN 1 AND 20 AND suffix = trim(suffix, ' ')
    AND instr(suffix, '  ') = 0
    AND suffix NOT GLOB '*[]' || char(1) || '-' || char(31) || char(127) || '!"#$%&()*,/:;<=>?@[\^_`{|}~]*')),
  PRIMARY KEY (card_id, slot)
) STRICT;

INSERT INTO card_address_new (card_id, slot, host, port, public, suffix)
SELECT card_id, 1, host, port, public, NULL FROM card_address;

DROP TABLE card_address;
ALTER TABLE card_address_new RENAME TO card_address;

-- card_text_channel already has three slots. The field text_channel1 is now text_channel, and it is still slot 1, so
-- its rows stay where they are. Each slot gains a suffix.
ALTER TABLE card_text_channel ADD COLUMN suffix TEXT
  CHECK (suffix IS NULL OR (length(suffix) BETWEEN 1 AND 20 AND suffix = trim(suffix, ' ')
    AND instr(suffix, '  ') = 0
    AND suffix NOT GLOB '*[]' || char(1) || '-' || char(31) || char(127) || '!"#$%&()*,/:;<=>?@[\^_`{|}~]*'));
