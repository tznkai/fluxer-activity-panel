-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Schema 10: per-subscription modes (redirect, local, both), subscription follow roles, scoped sessions,
-- polls and play-now posts, and mirrors of home objects in "both" subscriptions.
-- card_subscription.id is AUTOINCREMENT so an id is never reused: poll_ballots.via_sub_id keeps a deleted
-- subscription's id with no foreign key, and must never come to mean another subscription.

CREATE TABLE card_subscription_new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id          INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    guild_id         TEXT NOT NULL CHECK (guild_id <> '' AND guild_id NOT GLOB '*[^0-9]*' AND length(guild_id) <= 25),
    channel_id       TEXT NOT NULL CHECK (channel_id <> '' AND channel_id NOT GLOB '*[^0-9]*' AND length(channel_id) <= 25),
    message_id       TEXT CHECK (message_id IS NULL OR (message_id <> '' AND message_id NOT GLOB '*[^0-9]*'
                                                       AND length(message_id) <= 25)),
    content_hash     TEXT CHECK (content_hash IS NULL OR (length(content_hash) = 64
                                                          AND content_hash NOT GLOB '*[^0-9a-f]*')),
    paused           INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    mode             TEXT NOT NULL DEFAULT 'redirect' CHECK (mode IN ('redirect', 'local', 'both')),
    local_channel_id TEXT CHECK (local_channel_id IS NULL OR (local_channel_id <> ''
                                 AND local_channel_id NOT GLOB '*[^0-9]*' AND length(local_channel_id) <= 25)),
    CHECK (message_id IS NOT NULL OR content_hash IS NULL),
    UNIQUE (card_id, channel_id)
) STRICT;
INSERT INTO card_subscription_new (card_id, guild_id, channel_id, message_id, content_hash, paused, mode)
    SELECT card_id, guild_id, channel_id, message_id, content_hash, paused, 'redirect'
    FROM card_subscription ORDER BY rowid;
DROP TABLE card_subscription;
ALTER TABLE card_subscription_new RENAME TO card_subscription;
CREATE INDEX card_subscription_message ON card_subscription (message_id) WHERE message_id IS NOT NULL;

-- One follow role per local/both subscription, in the subscription's own community (its guild_id).
CREATE TABLE subscription_follow_role (
    sub_id  INTEGER PRIMARY KEY REFERENCES card_subscription(id) ON DELETE CASCADE,
    role_id TEXT NOT NULL UNIQUE CHECK (role_id <> '' AND role_id NOT GLOB '*[^0-9]*' AND length(role_id) <= 25)
) STRICT;

-- NULL scope_sub_id is the home scope.
ALTER TABLE sessions ADD COLUMN scope_sub_id INTEGER REFERENCES card_subscription(id) ON DELETE RESTRICT;
ALTER TABLE polls    ADD COLUMN scope_sub_id INTEGER REFERENCES card_subscription(id) ON DELETE RESTRICT;
ALTER TABLE play_now ADD COLUMN scope_sub_id INTEGER REFERENCES card_subscription(id) ON DELETE RESTRICT;
CREATE INDEX sessions_scope ON sessions (scope_sub_id) WHERE scope_sub_id IS NOT NULL;
CREATE INDEX polls_scope    ON polls (scope_sub_id) WHERE scope_sub_id IS NOT NULL;
CREATE INDEX play_now_scope ON play_now (scope_sub_id) WHERE scope_sub_id IS NOT NULL;

-- One open poll per scope. ifnull keeps the home scope (NULL) unique too.
DROP INDEX polls_open_once;
CREATE UNIQUE INDEX polls_open_once ON polls (card_id, ifnull(scope_sub_id, 0)) WHERE state = 'open';

-- The both-subscription a far recipient was reached through. No foreign key on purpose (see header).
ALTER TABLE poll_ballots ADD COLUMN via_sub_id INTEGER CHECK (via_sub_id IS NULL OR via_sub_id > 0);
-- The both-subscription mirror an RSVP reaction came through (NULL: the home message). Same rule as above.
ALTER TABLE rsvp_reaction ADD COLUMN via_sub_id INTEGER CHECK (via_sub_id IS NULL OR via_sub_id > 0);

-- A home session, poll or play-now post re-posted in a both subscription's channel.
CREATE TABLE mirror_message (
    id           INTEGER PRIMARY KEY,
    sub_id       INTEGER NOT NULL REFERENCES card_subscription(id) ON DELETE CASCADE,
    channel_id   TEXT NOT NULL CHECK (channel_id <> '' AND channel_id NOT GLOB '*[^0-9]*' AND length(channel_id) <= 25),
    message_id   TEXT NOT NULL UNIQUE CHECK (message_id <> '' AND message_id NOT GLOB '*[^0-9]*'
                                             AND length(message_id) <= 25),
    content_hash TEXT CHECK (content_hash IS NULL OR (length(content_hash) = 64
                                                      AND content_hash NOT GLOB '*[^0-9a-f]*')),
    session_id   INTEGER REFERENCES sessions(id) ON DELETE CASCADE,
    poll_id      INTEGER REFERENCES polls(id) ON DELETE CASCADE,
    play_now_id  INTEGER REFERENCES play_now(id) ON DELETE CASCADE,
    CHECK ((session_id IS NOT NULL) + (poll_id IS NOT NULL) + (play_now_id IS NOT NULL) = 1)
) STRICT;
CREATE UNIQUE INDEX mirror_session  ON mirror_message (session_id, sub_id)  WHERE session_id IS NOT NULL;
CREATE UNIQUE INDEX mirror_poll     ON mirror_message (poll_id, sub_id)     WHERE poll_id IS NOT NULL;
CREATE UNIQUE INDEX mirror_play_now ON mirror_message (play_now_id, sub_id) WHERE play_now_id IS NOT NULL;
CREATE INDEX mirror_sub ON mirror_message (sub_id);
