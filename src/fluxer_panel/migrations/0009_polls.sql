-- SPDX-FileCopyrightText: 2026 K. Chen
-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Schema 9: time polls (approval polls and ranked runoffs), their options, ballots and votes.

CREATE TABLE polls (
    id                  INTEGER PRIMARY KEY,
    card_id             INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    guild_id            TEXT NOT NULL CHECK (guild_id <> '' AND guild_id NOT GLOB '*[^0-9]*' AND length(guild_id) <= 25),
    channel_id          TEXT NOT NULL CHECK (channel_id <> '' AND channel_id NOT GLOB '*[^0-9]*' AND length(channel_id) <= 25),
    message_id          TEXT NOT NULL CHECK (message_id <> '' AND message_id NOT GLOB '*[^0-9]*' AND length(message_id) <= 25),
    starter_id          TEXT NOT NULL CHECK (starter_id <> '' AND starter_id NOT GLOB '*[^0-9]*' AND length(starter_id) <= 25),
    kind                TEXT NOT NULL CHECK (kind IN ('approval', 'runoff')),
    parent_poll_id      INTEGER REFERENCES polls(id) ON DELETE SET NULL,
    state               TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'closed')),
    created_at          INTEGER NOT NULL,
    closes_at           INTEGER NOT NULL,
    closed_at           INTEGER,
    ballot_size         INTEGER NOT NULL CHECK (ballot_size BETWEEN 1 AND 20),
    pending_ballot_size INTEGER CHECK (pending_ballot_size IS NULL OR pending_ballot_size BETWEEN 1 AND 20),
    sorted_at           INTEGER,
    outcome             TEXT CHECK (outcome IS NULL OR outcome IN ('winner', 'none', 'removed')),
    winner_option_id    INTEGER,
    session_id          INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    CHECK (closes_at > created_at),
    CHECK ((state = 'open') = (closed_at IS NULL)),
    CHECK ((state = 'open') = (outcome IS NULL)),
    CHECK ((outcome IS 'winner') = (winner_option_id IS NOT NULL)),
    CHECK (session_id IS NULL OR outcome IS 'winner'),
    CHECK ((kind = 'runoff') = (parent_poll_id IS NOT NULL) OR parent_poll_id IS NULL)
) STRICT;
CREATE UNIQUE INDEX polls_open_once ON polls (card_id) WHERE state = 'open';
CREATE UNIQUE INDEX polls_message ON polls (message_id);
CREATE INDEX polls_state_closes ON polls (state, closes_at);
CREATE INDEX polls_parent ON polls (parent_poll_id);

CREATE TABLE poll_options (
    id        INTEGER PRIMARY KEY,
    poll_id   INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL CHECK (position >= 0),
    starts_at INTEGER NOT NULL,
    origin    TEXT NOT NULL CHECK (origin IN ('offered', 'typed')),
    UNIQUE (poll_id, position),
    UNIQUE (poll_id, starts_at),
    UNIQUE (id, poll_id)
) STRICT;

CREATE TABLE poll_ballots (
    poll_id       INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    user_id       TEXT NOT NULL CHECK (user_id <> '' AND user_id NOT GLOB '*[^0-9]*' AND length(user_id) <= 25),
    dm_channel_id TEXT CHECK (dm_channel_id IS NULL OR (dm_channel_id <> '' AND dm_channel_id NOT GLOB '*[^0-9]*' AND length(dm_channel_id) <= 25)),
    dm_message_id TEXT CHECK (dm_message_id IS NULL OR (dm_message_id <> '' AND dm_message_id NOT GLOB '*[^0-9]*' AND length(dm_message_id) <= 25)),
    dm_failed     INTEGER NOT NULL DEFAULT 0 CHECK (dm_failed IN (0, 1)),
    answered      INTEGER NOT NULL DEFAULT 0 CHECK (answered IN (0, 1)),
    reminded      INTEGER NOT NULL DEFAULT 0 CHECK (reminded IN (0, 1)),
    PRIMARY KEY (poll_id, user_id),
    CHECK ((dm_channel_id IS NULL) = (dm_message_id IS NULL))
) STRICT;

CREATE INDEX poll_ballots_user ON poll_ballots (user_id);

CREATE TABLE poll_votes (
    poll_id   INTEGER NOT NULL,
    user_id   TEXT NOT NULL,
    option_id INTEGER NOT NULL,
    rank      INTEGER CHECK (rank IS NULL OR rank >= 1),
    PRIMARY KEY (poll_id, user_id, option_id),
    FOREIGN KEY (poll_id, user_id) REFERENCES poll_ballots (poll_id, user_id) ON DELETE CASCADE,
    FOREIGN KEY (option_id, poll_id) REFERENCES poll_options (id, poll_id) ON DELETE CASCADE
) STRICT;
CREATE UNIQUE INDEX poll_votes_rank ON poll_votes (poll_id, user_id, rank) WHERE rank IS NOT NULL;
CREATE INDEX poll_votes_option ON poll_votes (option_id);
