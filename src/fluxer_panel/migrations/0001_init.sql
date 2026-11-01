-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Schema v1. Fluxer ids only; no names, nicknames, ages or group membership (those live in Fluxer roles).
-- Snowflake check: x <> '' AND x NOT GLOB '*[^0-9]*' AND length(x) <= 25

CREATE TABLE cards (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('server', 'in_person', 'online')),
    title       TEXT NOT NULL,
    description TEXT,
    channel_id  TEXT CHECK (channel_id <> '' AND channel_id NOT GLOB '*[^0-9]*' AND length(channel_id) <= 25),
    message_id  TEXT CHECK (message_id <> '' AND message_id NOT GLOB '*[^0-9]*' AND length(message_id) <= 25),
    created_by  TEXT NOT NULL CHECK (created_by <> '' AND created_by NOT GLOB '*[^0-9]*' AND length(created_by) <= 25),
    approved_by TEXT CHECK (approved_by <> '' AND approved_by NOT GLOB '*[^0-9]*' AND length(approved_by) <= 25),
    archived    INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
) STRICT;

CREATE TABLE card_server (
    card_id         INTEGER PRIMARY KEY REFERENCES cards(id),
    ptero_server_id TEXT,
    join_address    TEXT,
    query_kind      TEXT,
    query_port      INTEGER CHECK (query_port BETWEEN 1 AND 65535)
) STRICT;

CREATE TABLE card_location (
    card_id       INTEGER PRIMARY KEY REFERENCES cards(id),
    location_text TEXT NOT NULL,
    visibility    TEXT NOT NULL CHECK (visibility IN ('group', 'family'))
) STRICT;

CREATE TABLE card_seats (
    card_id INTEGER PRIMARY KEY REFERENCES cards(id),
    seats   INTEGER NOT NULL CHECK (seats > 0)
) STRICT;

CREATE TABLE families (
    id         INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL
) STRICT;

CREATE TABLE family_members (
    family_id INTEGER NOT NULL REFERENCES families(id) ON DELETE CASCADE,
    user_id   TEXT NOT NULL CHECK (user_id <> '' AND user_id NOT GLOB '*[^0-9]*' AND length(user_id) <= 25),
    relation  TEXT NOT NULL CHECK (relation IN ('parent', 'child')),
    PRIMARY KEY (family_id, user_id)
) STRICT;

CREATE TABLE sessions (
    id               INTEGER PRIMARY KEY,
    card_id          INTEGER NOT NULL REFERENCES cards(id) ON DELETE RESTRICT,
    group_role_id    TEXT NOT NULL CHECK (group_role_id <> '' AND group_role_id NOT GLOB '*[^0-9]*' AND length(group_role_id) <= 25),
    starts_at        INTEGER NOT NULL,
    ends_at          INTEGER NOT NULL,
    location_text    TEXT,
    seats            INTEGER CHECK (seats > 0),
    recurrence       TEXT,
    recurrence_until INTEGER,
    proposer_id      TEXT NOT NULL CHECK (proposer_id <> '' AND proposer_id NOT GLOB '*[^0-9]*' AND length(proposer_id) <= 25),
    created_at       INTEGER NOT NULL,
    CHECK (ends_at > starts_at)
) STRICT;

CREATE INDEX sessions_card ON sessions(card_id);

CREATE TABLE rsvps (
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id      TEXT NOT NULL CHECK (user_id <> '' AND user_id NOT GLOB '*[^0-9]*' AND length(user_id) <= 25),
    status       TEXT NOT NULL CHECK (status IN ('yes', 'maybe', 'no')),
    responded_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, user_id)
) STRICT;

CREATE TABLE requests (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('wake')),
    card_id      INTEGER REFERENCES cards(id),
    requester_id TEXT NOT NULL CHECK (requester_id <> '' AND requester_id NOT GLOB '*[^0-9]*' AND length(requester_id) <= 25),
    status       TEXT NOT NULL CHECK (status IN ('open', 'done', 'declined')),
    created_at   INTEGER NOT NULL
) STRICT;
