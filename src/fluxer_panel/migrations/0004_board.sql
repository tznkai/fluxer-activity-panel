-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- v4: the summary board, one message in the games channel (fluxer-panel-cards).
-- A single row. The channel and the message are set together or not at all, and a hash is kept only with a message.
-- Snowflake check used throughout: x <> '' AND x NOT GLOB '*[^0-9]*' AND length(x) <= 25

CREATE TABLE board (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  channel_id TEXT CHECK (channel_id IS NULL OR (channel_id <> '' AND channel_id NOT GLOB '*[^0-9]*'
                                                AND length(channel_id) <= 25)),
  message_id TEXT CHECK (message_id IS NULL OR (message_id <> '' AND message_id NOT GLOB '*[^0-9]*'
                                                AND length(message_id) <= 25)),
  content_hash TEXT CHECK (content_hash IS NULL OR (length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*')),
  CHECK ((channel_id IS NULL) = (message_id IS NULL)),
  CHECK (message_id IS NOT NULL OR content_hash IS NULL)
) STRICT;

INSERT INTO board (id) VALUES (1);
