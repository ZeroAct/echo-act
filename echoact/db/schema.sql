-- EchoAct schema, version 1.
--
-- Section 4.2 fixes what each subject holds; this file adds nothing to that
-- list except the three things a requirement forces:
--
--   * jobs.model_id, denormalised out of the settings JSON, because F-40
--     filters history by model and a filter over a JSON blob cannot use an
--     index.
--   * results.integrity_state / verified_at, because F-45 requires a missing
--     or corrupted result file to be *detected* and offered for delete or
--     regenerate; a finding that lives only in the reconciling process is
--     lost the moment the window closes.
--   * schema_version and backups, which N-15 needs so that a format change
--     can be preceded by a recoverable backup and an older build can tell
--     that it is looking at a newer format.
--
-- Read by echoact.db.migrations, which splits the file on the
-- "-- @section:" markers.  The fts5 section is applied only where the
-- SQLite build has FTS5; echoact.db.store falls back to LIKE where it does
-- not, so search degrades rather than disappearing (F-40).

-- @section: core

CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  REAL NOT NULL,
    description TEXT NOT NULL
);

-- ---------------------------------------------------------------- 4.2 Document
CREATE TABLE documents (
    document_id TEXT    PRIMARY KEY,
    title       TEXT    NOT NULL,
    body        TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    modified_at REAL    NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1
);

-- F-40 searches titles; NOCASE so a title search matches the way a reader
-- expects.  Korean is unaffected by case folding, Latin titles are not.
CREATE INDEX documents_by_title ON documents (title COLLATE NOCASE);
CREATE INDEX documents_by_modified ON documents (modified_at DESC);

-- ------------------------------------------------- 4.2 Integration permission
-- Deliberately holds no credential and no verifier.  N-17 keeps the verifier
-- in echoact.security and out of backups; this table is the permission
-- record F-71 displays and F-61 revokes.
CREATE TABLE clients (
    client_id    TEXT    PRIMARY KEY,
    label        TEXT    NOT NULL,
    capabilities TEXT    NOT NULL,          -- JSON array of domain.Capability values
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   REAL    NOT NULL,
    revoked_at   REAL,
    last_seen_at REAL
);

-- --------------------------------------------------------------------- 4.2 Job
-- source_text is nullable: a one-off job's snapshot is cleared when its
-- retention window closes (F-42), while the job row itself survives so that
-- 4.2's "the existing terminal job and its reason are returned" still holds.
CREATE TABLE jobs (
    job_id             TEXT    PRIMARY KEY,
    kind               TEXT    NOT NULL,
    request_path       TEXT    NOT NULL,
    owner_client_id    TEXT    NOT NULL,
    client_label       TEXT,
    state              TEXT    NOT NULL,
    retention          TEXT    NOT NULL,
    source_text        TEXT,
    model_id           TEXT    NOT NULL,
    settings_json      TEXT    NOT NULL,
    budget_json        TEXT,               -- the budget actually applied (F-39, F-78)
    created_at         REAL    NOT NULL,
    started_at         REAL,
    ended_at           REAL,
    error_code         TEXT,
    error_message      TEXT,
    idempotency_key    TEXT,
    generated_segments INTEGER NOT NULL DEFAULT 0,
    total_segments     INTEGER NOT NULL DEFAULT 0
);

-- There is no foreign key from jobs.owner_client_id to clients.client_id on
-- purpose: F-43 and F-61 both require a client's permissions to be revocable
-- and removable without touching the history of jobs it created, and 4.2
-- keeps the permission record separate from body-text history.
CREATE INDEX jobs_by_created ON jobs (created_at DESC);
CREATE INDEX jobs_by_model ON jobs (model_id, created_at DESC);
CREATE INDEX jobs_by_state ON jobs (state, created_at DESC);
CREATE INDEX jobs_by_owner ON jobs (owner_client_id, created_at DESC);

-- ----------------------------------------------------------------- 4.2 Segment
-- The two source columns are named for exactly what 4.2 standardises: offsets
-- in Unicode code points into the job's source text, start inclusive and end
-- exclusive.  A column called "source_start"/"source_end" invites someone to
-- put a UTF-16 index from Qt in it, which is the defect this naming exists to
-- prevent.
CREATE TABLE segments (
    segment_id                       TEXT    PRIMARY KEY,
    job_id                           TEXT    NOT NULL REFERENCES jobs (job_id) ON DELETE CASCADE,
    seq                              INTEGER NOT NULL,
    source_start_codepoint_inclusive INTEGER NOT NULL,
    source_end_codepoint_exclusive   INTEGER NOT NULL,
    spoken_text                      TEXT    NOT NULL,
    language                         TEXT    NOT NULL,
    audio_start_ms                   INTEGER,
    audio_end_ms                     INTEGER,
    trailing_silence_ms              INTEGER NOT NULL DEFAULT 0,
    audio_path                       TEXT,
    frame_count                      INTEGER NOT NULL DEFAULT 0,
    ready                            INTEGER NOT NULL DEFAULT 0,
    UNIQUE (job_id, seq),
    CHECK (source_start_codepoint_inclusive >= 0),
    CHECK (source_end_codepoint_exclusive >= source_start_codepoint_inclusive),
    CHECK (audio_end_ms IS NULL OR audio_start_ms IS NULL OR audio_end_ms >= audio_start_ms)
);

-- ------------------------------------------------------------------ 4.2 Result
-- Addressed by result_id, never by path: 4.2 requires a permission-checkable
-- identifier, and relative_path is resolved inside the app against the audio
-- directory and is never handed to a client.
CREATE TABLE results (
    result_id         TEXT    PRIMARY KEY,
    job_id            TEXT    NOT NULL UNIQUE REFERENCES jobs (job_id) ON DELETE CASCADE,
    sample_rate       INTEGER NOT NULL,
    channels          INTEGER NOT NULL,
    sample_width_bits INTEGER NOT NULL,
    frame_count       INTEGER NOT NULL,
    byte_size         INTEGER NOT NULL,
    digest            TEXT    NOT NULL,     -- SHA-256 of the WAV bytes
    relative_path     TEXT    NOT NULL,
    created_at        REAL    NOT NULL,
    expires_at        REAL,                 -- NULL means "kept until deleted" (4.1)
    integrity_state   TEXT    NOT NULL DEFAULT 'unverified',
    verified_at       REAL,
    CHECK (integrity_state IN ('unverified', 'ok', 'missing', 'corrupt'))
);

CREATE INDEX results_by_expiry ON results (expires_at);

-- --------------------------------------------------------- 4.2 Re-request record
-- Client and key, the request-match discriminator, the job, and an expiry.
-- No source text: 4.2 says only what duplicate prevention needs is kept, and
-- the discriminator is a digest supplied by the caller of the store.
CREATE TABLE idempotency (
    client_id      TEXT NOT NULL,
    key            TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    job_id         TEXT NOT NULL REFERENCES jobs (job_id) ON DELETE CASCADE,
    created_at     REAL NOT NULL,
    expires_at     REAL NOT NULL,
    PRIMARY KEY (client_id, key)
) WITHOUT ROWID;

CREATE INDEX idempotency_by_expiry ON idempotency (expires_at);

-- ------------------------------------------------------------- N-15 / F-44 Backups
-- Bookkeeping only.  Bundling, verification, and restore live in the backup
-- module; what belongs to the schema is the record that a recoverable copy
-- exists, which schema version it was taken at, and whether it was verified,
-- because 4.1 rotates scheduled backups only after a new one is verified.
CREATE TABLE backups (
    backup_id      TEXT    PRIMARY KEY,
    kind           TEXT    NOT NULL,        -- 'manual' | 'scheduled' | 'pre_migration'
    created_at     REAL    NOT NULL,
    location       TEXT    NOT NULL,
    byte_size      INTEGER NOT NULL DEFAULT 0,
    item_count     INTEGER NOT NULL DEFAULT 0,
    schema_version INTEGER NOT NULL,
    app_version    TEXT    NOT NULL,
    verified       INTEGER NOT NULL DEFAULT 0,
    note           TEXT,
    CHECK (kind IN ('manual', 'scheduled', 'pre_migration'))
);

CREATE INDEX backups_by_created ON backups (kind, created_at DESC);

-- @section: fts5
-- F-40's full-text index over document titles and retained body text.
--
-- The tokenizer is trigram rather than the default unicode61 because
-- unicode61 splits on whitespace, and Korean search terms are routinely a
-- substring of an eojeol rather than a whole one; with unicode61 a search for
-- a two-syllable stem inside a longer word finds nothing.  Trigram matches
-- substrings, at the cost of needing at least three characters -- the store
-- answers shorter queries with LIKE for that reason.
--
-- External content: the index stores no second copy of the body, so N-16's
-- retention accounting stays truthful about how much space a document costs.

CREATE VIRTUAL TABLE documents_fts USING fts5 (
    title,
    body,
    content = 'documents',
    content_rowid = 'rowid',
    tokenize = 'trigram'
);

CREATE TRIGGER documents_fts_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts (rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;

CREATE TRIGGER documents_fts_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts (documents_fts, rowid, title, body)
    VALUES ('delete', old.rowid, old.title, old.body);
END;

CREATE TRIGGER documents_fts_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts (documents_fts, rowid, title, body)
    VALUES ('delete', old.rowid, old.title, old.body);
    INSERT INTO documents_fts (rowid, title, body) VALUES (new.rowid, new.title, new.body);
END;
