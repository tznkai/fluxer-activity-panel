-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Schema 8: sessions and RSVPs. Phase 1's sessions and rsvps were never written, so they are dropped and recreated.
-- The family tables are untouched.

DROP TABLE rsvps;
DROP TABLE sessions;

CREATE TABLE sessions (
    id            INTEGER PRIMARY KEY,
    card_id       INTEGER NOT NULL REFERENCES cards(id) ON DELETE RESTRICT,
    guild_id      TEXT NOT NULL CHECK (guild_id <> '' AND guild_id NOT GLOB '*[^0-9]*' AND length(guild_id) <= 25),
    channel_id    TEXT NOT NULL CHECK (channel_id <> '' AND channel_id NOT GLOB '*[^0-9]*' AND length(channel_id) <= 25),
    message_id    TEXT NOT NULL CHECK (message_id <> '' AND message_id NOT GLOB '*[^0-9]*' AND length(message_id) <= 25),
    starts_at     INTEGER NOT NULL,
    ends_at       INTEGER NOT NULL,
    proposer_id   TEXT NOT NULL CHECK (proposer_id <> '' AND proposer_id NOT GLOB '*[^0-9]*' AND length(proposer_id) <= 25),
    location_text TEXT CHECK (location_text IS NULL OR length(location_text) BETWEEN 1 AND 200),
    seats         INTEGER CHECK (seats IS NULL OR seats BETWEEN 1 AND 999),
    recurrence    TEXT CHECK (recurrence IS NULL OR recurrence = 'weekly'),
    series_id     INTEGER,
    next_posted   INTEGER NOT NULL DEFAULT 0 CHECK (next_posted IN (0, 1)),
    state         TEXT NOT NULL DEFAULT 'upcoming' CHECK (state IN ('upcoming', 'ended', 'cancelled')),
    closed_at     INTEGER,
    moved         INTEGER NOT NULL DEFAULT 0 CHECK (moved IN (0, 1)),
    headsup_sent  INTEGER NOT NULL DEFAULT 0 CHECK (headsup_sent IN (0, 1)),
    created_at    INTEGER NOT NULL,
    CHECK (ends_at > starts_at),
    CHECK ((state = 'upcoming') = (closed_at IS NULL))
) STRICT;
CREATE INDEX sessions_card_state ON sessions (card_id, state, starts_at);
CREATE UNIQUE INDEX sessions_message ON sessions (message_id);
CREATE INDEX sessions_series ON sessions (series_id);

CREATE TABLE rsvp_reaction (
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id    TEXT NOT NULL CHECK (user_id <> '' AND user_id NOT GLOB '*[^0-9]*' AND length(user_id) <= 25),
    choice     TEXT NOT NULL CHECK (choice IN ('going', 'maybe', 'no')),
    added_at   INTEGER NOT NULL,
    seq        INTEGER NOT NULL CHECK (seq > 0),
    implicit   INTEGER NOT NULL DEFAULT 0 CHECK (implicit IN (0, 1)),
    PRIMARY KEY (session_id, user_id, choice)
) STRICT;
CREATE UNIQUE INDEX rsvp_reaction_seq ON rsvp_reaction (session_id, seq);

CREATE TABLE rsvps (
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id      TEXT NOT NULL CHECK (user_id <> '' AND user_id NOT GLOB '*[^0-9]*' AND length(user_id) <= 25),
    choice       TEXT NOT NULL CHECK (choice IN ('going', 'waiting', 'maybe', 'no')),
    responded_at INTEGER NOT NULL,
    waitlist_pos INTEGER CHECK (waitlist_pos IS NULL OR waitlist_pos > 0),
    PRIMARY KEY (session_id, user_id),
    CHECK ((choice = 'waiting') = (waitlist_pos IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX rsvps_waitlist ON rsvps (session_id, waitlist_pos) WHERE waitlist_pos IS NOT NULL;
