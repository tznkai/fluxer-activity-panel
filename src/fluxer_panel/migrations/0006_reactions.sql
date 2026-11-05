-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- v6: reactions, wake requests, play-now and follow roles (fluxer-panel-reactions).
-- Snowflake check used throughout: x <> '' AND x NOT GLOB '*[^0-9]*' AND length(x) <= 25

-- requests: rebuilt. Nothing in the code has ever written to it. Open rows are kept once per card and requester,
-- done and declined rows become cleared, and rows with no card are dropped.
CREATE TABLE requests_new (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind = 'wake'),
    card_id      INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    requester_id TEXT NOT NULL CHECK (requester_id <> '' AND requester_id NOT GLOB '*[^0-9]*' AND length(requester_id) <= 25),
    status       TEXT NOT NULL CHECK (status IN ('open', 'ready', 'expired', 'cleared')),
    created_at   INTEGER NOT NULL,
    closed_at    INTEGER,
    CHECK ((status = 'open') = (closed_at IS NULL))
) STRICT;

INSERT INTO requests_new (id, kind, card_id, requester_id, status, created_at, closed_at)
SELECT r.id, r.kind, r.card_id, r.requester_id,
       CASE r.status WHEN 'open' THEN 'open' ELSE 'cleared' END,
       r.created_at,
       CASE r.status WHEN 'open' THEN NULL ELSE r.created_at END
FROM requests AS r
WHERE r.card_id IN (SELECT id FROM cards)
  AND (r.status <> 'open'
       OR r.id = (SELECT min(r2.id) FROM requests AS r2
                  WHERE r2.card_id = r.card_id AND r2.requester_id = r.requester_id AND r2.status = 'open'));

DROP TABLE requests;
ALTER TABLE requests_new RENAME TO requests;
CREATE UNIQUE INDEX requests_open_once ON requests (card_id, requester_id) WHERE status = 'open';

-- The DMs sent to admins about a wake request, one per admin and card, so an edit can find and update them.
CREATE TABLE wake_request_dm (
    card_id    INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    admin_id   TEXT NOT NULL CHECK (admin_id <> '' AND admin_id NOT GLOB '*[^0-9]*' AND length(admin_id) <= 25),
    kind       TEXT NOT NULL CHECK (kind IN ('request', 'signal')),
    channel_id TEXT NOT NULL CHECK (channel_id <> '' AND channel_id NOT GLOB '*[^0-9]*' AND length(channel_id) <= 25),
    message_id TEXT NOT NULL CHECK (message_id <> '' AND message_id NOT GLOB '*[^0-9]*' AND length(message_id) <= 25),
    opened_at  INTEGER NOT NULL,
    PRIMARY KEY (card_id, admin_id)
) STRICT;

-- One Fluxer role per card that a member can take to follow it. role_id is unique: a role follows one card only.
CREATE TABLE card_follow_role (
    card_id  INTEGER PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
    guild_id TEXT NOT NULL CHECK (guild_id <> '' AND guild_id NOT GLOB '*[^0-9]*' AND length(guild_id) <= 25),
    role_id  TEXT NOT NULL UNIQUE CHECK (role_id <> '' AND role_id NOT GLOB '*[^0-9]*' AND length(role_id) <= 25)
) STRICT;

-- Where a card's play-now posts go. Same CHECKs as card_text_channel.
CREATE TABLE card_play_now_channel (
    card_id      INTEGER PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
    guild_id     TEXT NOT NULL CHECK (guild_id <> '' AND guild_id NOT GLOB '*[^0-9]*' AND length(guild_id) <= 25),
    channel_id   TEXT NOT NULL CHECK (channel_id <> '' AND channel_id NOT GLOB '*[^0-9]*' AND length(channel_id) <= 25),
    channel_name TEXT CHECK (channel_name IS NULL OR length(channel_name) BETWEEN 1 AND 100),
    verified     INTEGER NOT NULL CHECK (verified IN (0, 1)),
    CHECK (verified = 1 OR channel_name IS NULL)
) STRICT;

-- One "play now" post in a card's play-now channel, from the press until it expires.
CREATE TABLE play_now (
    id          INTEGER PRIMARY KEY,
    card_id     INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    guild_id    TEXT NOT NULL CHECK (guild_id <> '' AND guild_id NOT GLOB '*[^0-9]*' AND length(guild_id) <= 25),
    channel_id  TEXT NOT NULL CHECK (channel_id <> '' AND channel_id NOT GLOB '*[^0-9]*' AND length(channel_id) <= 25),
    message_id  TEXT NOT NULL UNIQUE CHECK (message_id <> '' AND message_id NOT GLOB '*[^0-9]*' AND length(message_id) <= 25),
    presser_id  TEXT NOT NULL CHECK (presser_id <> '' AND presser_id NOT GLOB '*[^0-9]*' AND length(presser_id) <= 25),
    offers_wake INTEGER NOT NULL CHECK (offers_wake IN (0, 1)),
    state       TEXT NOT NULL CHECK (state IN ('live', 'expired')),
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL CHECK (expires_at >= created_at)
) STRICT;
CREATE INDEX play_now_card ON play_now (card_id, created_at);

-- The one global switch for reactions. The CHECK keeps it to a single row.
CREATE TABLE interactions (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
) STRICT;
INSERT INTO interactions (id) VALUES (1);

-- Cards whose reactions are switched off. A row is present when reactions are off for the card.
CREATE TABLE card_interactions_off (
    card_id INTEGER PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE
) STRICT;

-- Channels where adding a reaction failed, so the bot has alerted the admins and retries on a timer.
CREATE TABLE reaction_degraded (
    channel_id    TEXT NOT NULL PRIMARY KEY CHECK (channel_id <> '' AND channel_id NOT GLOB '*[^0-9]*' AND length(channel_id) <= 25),
    alerted_at    INTEGER NOT NULL,
    last_retry_at INTEGER NOT NULL
) STRICT;

CREATE INDEX cards_message ON cards (message_id) WHERE message_id IS NOT NULL;
CREATE INDEX card_subscription_message ON card_subscription (message_id) WHERE message_id IS NOT NULL;
