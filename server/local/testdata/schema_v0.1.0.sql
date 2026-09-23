-- EMI Analyzer schema for the local (desktop) app.
--
-- The same tables as server/emi/schema.sql, translated to SQLite. Keep the two in step: a column
-- added there and not here compiles, and then fails at the first INSERT of the local app.
--
-- Differences that are deliberate:
--   * no "emi." schema -- the database file is the analyzer's own
--   * jsonb is TEXT, booleans are INTEGER, timestamps are TEXT in one fixed-width UTC format
--     (see ts() in sqlitestore.go), so comparing two of them as strings compares them in time
--   * the CHECK vocabularies are repeated rather than migrated: this file is only ever applied
--     to a database this binary created, so there is no older constraint to replace yet

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS emi_projects (
    id              TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    name            TEXT NOT NULL,
    source_kind     TEXT NOT NULL DEFAULT 'kicad'
        CHECK (source_kind IN ('kicad', 'gerber')),
    created_by      TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS emi_projects_org_created_idx
    ON emi_projects (organization_id, created_at DESC);

CREATE TABLE IF NOT EXISTS emi_boards (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL REFERENCES emi_projects (id) ON DELETE CASCADE,
    ingest_run_id  TEXT,
    s3_input_key   TEXT NOT NULL,
    s3_board_key   TEXT,
    layer_count    INTEGER NOT NULL DEFAULT 0,
    net_count      INTEGER NOT NULL DEFAULT 0,
    outline_mm     TEXT,
    stackup        TEXT,
    content_sha256 TEXT,
    size_bytes     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS emi_boards_project_created_idx
    ON emi_boards (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS emi_boards_content_idx
    ON emi_boards (content_sha256)
    WHERE content_sha256 IS NOT NULL;

CREATE TABLE IF NOT EXISTS emi_runs (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES emi_projects (id) ON DELETE CASCADE,
    board_id          TEXT REFERENCES emi_boards (id) ON DELETE SET NULL,
    kind              TEXT NOT NULL
        CHECK (kind IN ('ingest', 'solve', 'transient', 'cable', 'compliance')),
    status            TEXT NOT NULL
        CHECK (status IN ('new', 'retry_pending', 'in_progress', 'stopping', 'done',
                          'failed', 'timed_out')),
    params            TEXT,
    estimate          TEXT,
    owner_api_key_kid TEXT,
    jti_key           TEXT,
    claimed_at        TEXT,
    started_at        TEXT,
    finished_at       TEXT,
    progress          TEXT,
    summary           TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS emi_runs_project_created_idx
    ON emi_runs (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS emi_runs_claimable_idx
    ON emi_runs (created_at)
    WHERE status IN ('new', 'retry_pending');

CREATE INDEX IF NOT EXISTS emi_runs_inprogress_idx
    ON emi_runs (claimed_at)
    WHERE status IN ('in_progress', 'stopping');

CREATE TABLE IF NOT EXISTS emi_artifacts (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES emi_runs (id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    s3_key       TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    UNIQUE (run_id, name)
);

CREATE TABLE IF NOT EXISTS emi_workers (
    id              TEXT PRIMARY KEY,
    api_key_kid     TEXT NOT NULL UNIQUE,
    organization_id TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'active',
    capabilities    TEXT NOT NULL DEFAULT '{}',
    registered_at   TEXT NOT NULL,
    last_seen_at    TEXT
);

-- Worker keys. The local app issues one for the worker container it starts; the scheme is the
-- same eci_<kid>_<secret> with only a keyed hash stored, as in the development harness.
CREATE TABLE IF NOT EXISTS emi_api_keys (
    kid             TEXT PRIMARY KEY,
    hash            TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    agent_type      TEXT NOT NULL DEFAULT 'emi',
    name            TEXT NOT NULL DEFAULT '',
    revoked_at      TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS emi_drivers (
    id              TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    project_id      TEXT NOT NULL REFERENCES emi_projects (id) ON DELETE CASCADE,
    created_by      TEXT,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'signal'
        CHECK (role IN ('signal', 'switching-regulator')),
    kind            TEXT NOT NULL
        CHECK (kind IN ('trapezoid', 'waveform', 'spectrum')),
    document        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS emi_drivers_project_created_idx
    ON emi_drivers (project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS emi_components (
    id              TEXT PRIMARY KEY,
    owner_user_id   TEXT NOT NULL CHECK (owner_user_id NOT LIKE 'anon-%'),
    organization_id TEXT NOT NULL,
    shared          INTEGER NOT NULL DEFAULT 0,
    kind            TEXT NOT NULL,
    name            TEXT NOT NULL,
    match           TEXT NOT NULL DEFAULT '{}',
    model           TEXT NOT NULL DEFAULT '{}',
    sources         TEXT NOT NULL DEFAULT '[]',
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    deleted_at      TEXT,
    provenance      TEXT NOT NULL DEFAULT 'user'
        CHECK (provenance IN ('generic', 'vendor', 'measured', 'user'))
);

CREATE INDEX IF NOT EXISTS emi_components_org_idx
    ON emi_components (organization_id, updated_at DESC)
    WHERE deleted_at IS NULL;
