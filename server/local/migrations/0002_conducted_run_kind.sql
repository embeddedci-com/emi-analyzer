-- Migration 2: the "conducted" run kind.
--
-- SQLite cannot change a CHECK constraint, so emi_runs is rebuilt. Two things make that harder
-- than the usual copy, drop and rename, both because foreign keys are on and cannot be switched
-- off inside the migration's transaction:
--
--   * DROP TABLE emi_runs is an implicit DELETE FROM emi_runs, and emi_artifacts references it
--     ON DELETE CASCADE: dropping the old table would delete every artifact. So emi_artifacts
--     is rebuilt first, pointing at the new runs table, and the old one is dropped before the
--     old runs table is.
--   * RENAME TO rewrites references in other tables to follow the renamed table, so once
--     emi_runs_new becomes emi_runs the new artifacts table points at emi_runs again.

CREATE TABLE emi_runs_new (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES emi_projects (id) ON DELETE CASCADE,
    board_id          TEXT REFERENCES emi_boards (id) ON DELETE SET NULL,
    kind              TEXT NOT NULL
        CHECK (kind IN ('ingest', 'solve', 'transient', 'cable', 'compliance', 'conducted')),
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

INSERT INTO emi_runs_new (id, project_id, board_id, kind, status, params, estimate,
                          owner_api_key_kid, jti_key, claimed_at, started_at, finished_at,
                          progress, summary, error, created_at, updated_at)
SELECT id, project_id, board_id, kind, status, params, estimate,
       owner_api_key_kid, jti_key, claimed_at, started_at, finished_at,
       progress, summary, error, created_at, updated_at
FROM emi_runs;

CREATE TABLE emi_artifacts_new (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES emi_runs_new (id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    s3_key       TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    UNIQUE (run_id, name)
);

INSERT INTO emi_artifacts_new (id, run_id, name, s3_key, content_type, size_bytes, created_at)
SELECT id, run_id, name, s3_key, content_type, size_bytes, created_at FROM emi_artifacts;

DROP TABLE emi_artifacts;
DROP TABLE emi_runs;
ALTER TABLE emi_runs_new RENAME TO emi_runs;
ALTER TABLE emi_artifacts_new RENAME TO emi_artifacts;

CREATE INDEX IF NOT EXISTS emi_runs_project_created_idx
    ON emi_runs (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS emi_runs_claimable_idx
    ON emi_runs (created_at)
    WHERE status IN ('new', 'retry_pending');

CREATE INDEX IF NOT EXISTS emi_runs_inprogress_idx
    ON emi_runs (claimed_at)
    WHERE status IN ('in_progress', 'stopping');
