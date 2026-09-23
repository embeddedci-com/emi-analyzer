-- EMI Analyzer schema.
--
-- PGStore creates these in a dedicated "emi" schema. A host that mounts this package with a
-- store of its own may keep the same tables elsewhere, and may keep workers in an agents
-- table of its own instead of emi_workers.
--
-- Status and kind are text with CHECK constraints rather than enums: adding a value to a
-- Postgres enum is a migration that cannot run inside a transaction with other DDL, and both
-- vocabularies are expected to grow.

CREATE SCHEMA IF NOT EXISTS emi;

CREATE TABLE IF NOT EXISTS emi.emi_projects (
    id              text PRIMARY KEY,
    organization_id text NOT NULL,
    name            text NOT NULL,
    source_kind     text NOT NULL DEFAULT 'kicad',
    created_by      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT emi_projects_source_kind_check
        CHECK (source_kind = ANY (ARRAY['kicad'::text, 'gerber'::text]))
);

CREATE INDEX IF NOT EXISTS emi_projects_org_created_idx
    ON emi.emi_projects (organization_id, created_at DESC);

CREATE TABLE IF NOT EXISTS emi.emi_boards (
    id            text PRIMARY KEY,
    project_id    text NOT NULL REFERENCES emi.emi_projects (id) ON DELETE CASCADE,
    ingest_run_id text,
    s3_input_key  text NOT NULL,
    s3_board_key  text,
    layer_count   integer NOT NULL DEFAULT 0,
    net_count     integer NOT NULL DEFAULT 0,
    outline_mm    jsonb,
    stackup       jsonb,
    -- SHA-256 of the uploaded bytes, so the same board is recognised when it is offered
    -- again. Nullable: boards created before this existed, and clients that cannot hash.
    content_sha256 text,
    size_bytes     bigint NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE emi.emi_boards ADD COLUMN IF NOT EXISTS content_sha256 text;
ALTER TABLE emi.emi_boards ADD COLUMN IF NOT EXISTS size_bytes bigint NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS emi_boards_project_created_idx
    ON emi.emi_boards (project_id, created_at DESC);

-- Finding "has this organisation already got this board?" is the whole point of the hash,
-- and it is asked once per file the user picks.
CREATE INDEX IF NOT EXISTS emi_boards_content_idx
    ON emi.emi_boards (content_sha256)
    WHERE content_sha256 IS NOT NULL;

CREATE TABLE IF NOT EXISTS emi.emi_runs (
    id         text PRIMARY KEY,
    project_id text NOT NULL REFERENCES emi.emi_projects (id) ON DELETE CASCADE,
    board_id   text REFERENCES emi.emi_boards (id) ON DELETE SET NULL,
    kind       text NOT NULL,
    status     text NOT NULL,
    params     jsonb,
    estimate   jsonb,

    -- Ownership: the kid says which worker holds the run, and the jti invalidates a
    -- superseded worker's token when a retry hands the run to somebody else.
    owner_api_key_kid text,
    jti_key           text,

    claimed_at  timestamptz,
    started_at  timestamptz,
    finished_at timestamptz,

    progress jsonb,
    summary  jsonb,
    error    text,

    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT emi_runs_kind_check
        CHECK (kind = ANY (ARRAY['ingest'::text, 'solve'::text, 'transient'::text,
                                 'cable'::text, 'compliance'::text])),
    CONSTRAINT emi_runs_status_check
        CHECK (status = ANY (ARRAY['new'::text, 'retry_pending'::text, 'in_progress'::text,
                                   'stopping'::text, 'done'::text, 'failed'::text,
                                   'timed_out'::text]))
);

-- Growing a CHECK vocabulary on a database that already exists. This file is applied on every
-- start, and CREATE TABLE IF NOT EXISTS leaves an existing table's constraints as they were, so
-- a new run kind in the definition above reaches only new databases: on an existing one the
-- insert fails and the API answers 500. The constraint is replaced only when it lacks the newest
-- value, so a normal start takes no lock and scans nothing.
--
-- **The probe has to name the newest kind, and adding one here is not optional.** 'cable' and
-- 'compliance' were both added to the table definition, the Go RunKind vocabulary and the worker
-- dispatch table without this block being updated, so both run kinds existed everywhere except
-- in the one place that decides whether an INSERT succeeds. On any database created before them
-- the API answered 500 with nothing in the log, and the Cables tab had never worked end to end.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'emi_runs_kind_check'
          AND conrelid = 'emi.emi_runs'::regclass
          AND pg_get_constraintdef(oid) LIKE '%compliance%'
    ) THEN
        ALTER TABLE emi.emi_runs DROP CONSTRAINT IF EXISTS emi_runs_kind_check;
        ALTER TABLE emi.emi_runs ADD CONSTRAINT emi_runs_kind_check
            CHECK (kind = ANY (ARRAY['ingest'::text, 'solve'::text, 'transient'::text,
                                     'cable'::text, 'compliance'::text]));
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS emi_runs_project_created_idx
    ON emi.emi_runs (project_id, created_at DESC);

-- The dispatch query is "claimable runs for this org, oldest first". A partial index keeps
-- it cheap as finished runs accumulate, since only two statuses are ever claimable.
CREATE INDEX IF NOT EXISTS emi_runs_claimable_idx
    ON emi.emi_runs (created_at)
    WHERE status IN ('new', 'retry_pending');

-- The sweeper scans in-progress runs by claim time.
CREATE INDEX IF NOT EXISTS emi_runs_inprogress_idx
    ON emi.emi_runs (claimed_at)
    WHERE status IN ('in_progress', 'stopping');

CREATE TABLE IF NOT EXISTS emi.emi_artifacts (
    id           text PRIMARY KEY,
    run_id       text NOT NULL REFERENCES emi.emi_runs (id) ON DELETE CASCADE,
    name         text NOT NULL,
    s3_key       text NOT NULL,
    content_type text NOT NULL DEFAULT 'application/octet-stream',
    size_bytes   bigint NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    -- A retried run re-uploads the same names; last write wins rather than accumulating
    -- duplicates the UI would have to de-duplicate.
    UNIQUE (run_id, name)
);

-- Registered workers. A host may keep these in an agents table of its own instead.
CREATE TABLE IF NOT EXISTS emi.emi_workers (
    id              text PRIMARY KEY,
    api_key_kid     text NOT NULL UNIQUE,
    organization_id text NOT NULL,
    name            text NOT NULL DEFAULT '',
    status          text NOT NULL DEFAULT 'active',
    capabilities    jsonb NOT NULL DEFAULT '{}'::jsonb,
    registered_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz
);

-- Worker keys for DevKeyVerifier. A host with a key table of its own does not use this; it
-- exists so the compose stack can issue a worker key without one.
CREATE TABLE IF NOT EXISTS emi.emi_dev_api_keys (
    kid             text PRIMARY KEY,
    hash            text NOT NULL,
    organization_id text NOT NULL,
    agent_type      text NOT NULL DEFAULT 'emi',
    name            text NOT NULL DEFAULT '',
    revoked_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Drivers: the measured or declared source attached to a port, as an emi-driver document
-- (docs/emi-driver-format.md). Project-scoped and open to visitors, exactly like boards -- the
-- sign-in gate is on the component library, not on this.
--
-- The document is stored whole rather than shredded into columns. It is a versioned format
-- that BenchPod also writes, and the parts the server needs to filter on (name, role, kind)
-- are the only ones lifted out.
CREATE TABLE IF NOT EXISTS emi.emi_drivers (
    id              text PRIMARY KEY,
    organization_id text NOT NULL,
    project_id      text NOT NULL REFERENCES emi.emi_projects (id) ON DELETE CASCADE,
    created_by      text,
    name            text NOT NULL,
    role            text NOT NULL DEFAULT 'signal',
    kind            text NOT NULL,
    document        jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT emi_drivers_kind_check
        CHECK (kind = ANY (ARRAY['trapezoid'::text, 'waveform'::text, 'spectrum'::text])),
    CONSTRAINT emi_drivers_role_check
        CHECK (role = ANY (ARRAY['signal'::text, 'switching-regulator'::text]))
);

CREATE INDEX IF NOT EXISTS emi_drivers_project_created_idx
    ON emi.emi_drivers (project_id, created_at DESC);

-- Components: a user's own models for parts the analyzer would otherwise treat as bare
-- copper (docs/implementation.md §3). Unlike boards and drivers, these are NOT open to
-- visitors: a signed-out visitor can build one and use it, but it lives in their browser and
-- is gone when they leave, because there is no account to file it under.
--
-- The CHECK is deliberate belt and braces. The API refuses a visitor with a 403, and the
-- table refuses to hold one even if that check is ever bypassed -- a component outlives the
-- session that made it, so an owner that cannot come back is a row nobody can ever reach.
CREATE TABLE IF NOT EXISTS emi.emi_components (
    id              text PRIMARY KEY,
    owner_user_id   text NOT NULL,
    organization_id text NOT NULL,
    shared          boolean NOT NULL DEFAULT false,
    kind            text NOT NULL,
    name            text NOT NULL,
    match           jsonb NOT NULL DEFAULT '{}'::jsonb,
    model           jsonb NOT NULL DEFAULT '{}'::jsonb,
    sources         jsonb NOT NULL DEFAULT '[]'::jsonb,
    version         integer NOT NULL DEFAULT 1,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz,
    -- Where the numbers came from, which is a different question from who typed them in.
    -- It decides what the UI may claim: a "vendor" component cites a datasheet, a "user" one
    -- was entered by hand. Without this column a cited part round-trips as hand-entered.
    provenance      text NOT NULL DEFAULT 'user',
    CONSTRAINT emi_components_owner_not_visitor
        CHECK (owner_user_id NOT LIKE 'anon-%'),
    CONSTRAINT emi_components_provenance_check
        CHECK (provenance = ANY (ARRAY['generic'::text, 'vendor'::text, 'measured'::text,
                                       'user'::text]))
);

ALTER TABLE emi.emi_components
    ADD COLUMN IF NOT EXISTS provenance text NOT NULL DEFAULT 'user';

-- The list query is "mine, plus anything shared with my organisation", and it never wants a
-- soft-deleted row.
CREATE INDEX IF NOT EXISTS emi_components_org_idx
    ON emi.emi_components (organization_id, updated_at DESC)
    WHERE deleted_at IS NULL;
