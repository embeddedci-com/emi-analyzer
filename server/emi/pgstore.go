package emi

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

//go:embed schema.sql
var schemaSQL string

// PGStore implements Store over Postgres.
//
// embeddedci-server will supply its own implementation over its *sql.DB and its app schema;
// this one exists so the local stack and the tests have a real database with the real
// constraints rather than a map that silently permits states Postgres would reject.
type PGStore struct {
	pool   *pgxpool.Pool
	schema string
}

// NewPGStore connects and applies the schema.
func NewPGStore(ctx context.Context, dsn string) (*PGStore, error) {
	pool, err := pgxpool.New(ctx, dsn)
	if err != nil {
		return nil, fmt.Errorf("emi: connect: %w", err)
	}
	store, err := NewPGStoreFromPool(ctx, pool)
	if err != nil {
		pool.Close()
		return nil, err
	}
	return store, nil
}

// NewPGStoreFromPool is the same thing for a host that already has a pool, which is the
// mounted case: embeddedci-server opens one for these tables alongside its own connections
// to the same database. The schema is applied here rather than by the host's migrations,
// because it belongs to this package and moves with it.
//
// Ownership of the pool stays with the caller: Close on this store closes it, so do not
// hand it a pool something else is still using.
func NewPGStoreFromPool(ctx context.Context, pool *pgxpool.Pool) (*PGStore, error) {
	if err := pool.Ping(ctx); err != nil {
		return nil, fmt.Errorf("emi: ping: %w", err)
	}
	if _, err := pool.Exec(ctx, schemaSQL); err != nil {
		return nil, fmt.Errorf("emi: apply schema: %w", err)
	}
	return &PGStore{pool: pool, schema: "emi"}, nil
}

func (s *PGStore) Close() { s.pool.Close() }

// Pool exposes the connection pool for the dev server's own key issuance.
func (s *PGStore) Pool() *pgxpool.Pool { return s.pool }

func mapErr(err error) error {
	if errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	}
	return err
}

// ---- projects ----

func (s *PGStore) CreateProject(ctx context.Context, p *Project) error {
	_, err := s.pool.Exec(ctx, `
		INSERT INTO emi.emi_projects (id, organization_id, name, source_kind, created_by, created_at)
		VALUES ($1,$2,$3,$4,$5,$6)`,
		p.ID, p.OrganizationID, p.Name, string(p.SourceKind), nullStr(p.CreatedBy), p.CreatedAt)
	return mapErr(err)
}

func (s *PGStore) GetProject(ctx context.Context, id string) (*Project, error) {
	var p Project
	var createdBy *string
	var kind string
	err := s.pool.QueryRow(ctx, `
		SELECT id, organization_id, name, source_kind, created_by, created_at
		FROM emi.emi_projects WHERE id = $1`, id).
		Scan(&p.ID, &p.OrganizationID, &p.Name, &kind, &createdBy, &p.CreatedAt)
	if err != nil {
		return nil, mapErr(err)
	}
	p.SourceKind = SourceKind(kind)
	if createdBy != nil {
		p.CreatedBy = *createdBy
	}
	return &p, nil
}

func (s *PGStore) ListProjects(ctx context.Context, orgID string, limit int) ([]*Project, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, organization_id, name, source_kind, created_by, created_at
		FROM emi.emi_projects WHERE organization_id = $1
		ORDER BY created_at DESC LIMIT $2`, orgID, limit)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	out := []*Project{}
	for rows.Next() {
		var p Project
		var createdBy *string
		var kind string
		if err := rows.Scan(&p.ID, &p.OrganizationID, &p.Name, &kind, &createdBy, &p.CreatedAt); err != nil {
			return nil, err
		}
		p.SourceKind = SourceKind(kind)
		if createdBy != nil {
			p.CreatedBy = *createdBy
		}
		out = append(out, &p)
	}
	return out, rows.Err()
}

func (s *PGStore) RenameProject(ctx context.Context, id, name string) error {
	tag, err := s.pool.Exec(ctx, `UPDATE emi.emi_projects SET name = $2 WHERE id = $1`, id, name)
	if err != nil {
		return mapErr(err)
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// DeleteProject relies on the ON DELETE CASCADE from boards, runs, artifacts and drivers, so
// one statement removes the whole project rather than four that could half-succeed.
func (s *PGStore) DeleteProject(ctx context.Context, id string) error {
	tag, err := s.pool.Exec(ctx, `DELETE FROM emi.emi_projects WHERE id = $1`, id)
	if err != nil {
		return mapErr(err)
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *PGStore) CountBoardsSharingInput(ctx context.Context, inputKey, exceptProjectID string) (int, error) {
	var n int
	err := s.pool.QueryRow(ctx, `
		SELECT count(*) FROM emi.emi_boards
		WHERE s3_input_key = $1 AND project_id <> $2`, inputKey, exceptProjectID).Scan(&n)
	return n, mapErr(err)
}

// ---- boards ----

func (s *PGStore) CreateBoard(ctx context.Context, b *Board) error {
	_, err := s.pool.Exec(ctx, `
		INSERT INTO emi.emi_boards (id, project_id, s3_input_key, content_sha256, size_bytes, created_at)
		VALUES ($1,$2,$3,NULLIF($4,''),$5,$6)`,
		b.ID, b.ProjectID, b.InputKey, b.ContentSHA256, b.SizeBytes, b.CreatedAt)
	return mapErr(err)
}

func (s *PGStore) GetBoard(ctx context.Context, id string) (*Board, error) {
	return s.scanBoard(s.pool.QueryRow(ctx, `
		SELECT id, project_id, ingest_run_id, s3_input_key, s3_board_key,
		       layer_count, net_count, outline_mm, stackup, content_sha256, size_bytes, created_at
		FROM emi.emi_boards WHERE id = $1`, id))
}

type rowScanner interface {
	Scan(dest ...any) error
}

func (s *PGStore) scanBoard(row rowScanner) (*Board, error) {
	var b Board
	var ingestRun, boardKey, sha *string
	var outline, stackup []byte
	err := row.Scan(&b.ID, &b.ProjectID, &ingestRun, &b.InputKey, &boardKey,
		&b.LayerCount, &b.NetCount, &outline, &stackup, &sha, &b.SizeBytes, &b.CreatedAt)
	if err != nil {
		return nil, mapErr(err)
	}
	if ingestRun != nil {
		b.IngestRunID = *ingestRun
	}
	if boardKey != nil {
		b.BoardKey = *boardKey
	}
	if sha != nil {
		b.ContentSHA256 = *sha
	}
	b.OutlineMM = json.RawMessage(outline)
	b.Stackup = json.RawMessage(stackup)
	return &b, nil
}

// FindBoardByContent looks for a board this organisation has already uploaded, by hash.
//
// Scoped to the organisation on purpose: the hash is a global fact about the bytes, but
// "have *you* uploaded this" is not, and answering across organisations would tell one
// customer that another has the same board.
//
// The newest match wins, and a parsed one beats an unparsed one -- re-offering a board
// whose ingest failed should not send the user to a dead project.
func (s *PGStore) FindBoardByContent(ctx context.Context, orgID, sha string) (*Board, *Project, error) {
	row := s.pool.QueryRow(ctx, `
		SELECT b.id, b.project_id, b.ingest_run_id, b.s3_input_key, b.s3_board_key,
		       b.layer_count, b.net_count, b.outline_mm, b.stackup,
		       b.content_sha256, b.size_bytes, b.created_at,
		       p.id, p.organization_id, p.name, p.source_kind, p.created_by, p.created_at
		FROM emi.emi_boards b
		JOIN emi.emi_projects p ON p.id = b.project_id
		WHERE p.organization_id = $1 AND b.content_sha256 = $2
		ORDER BY (b.s3_board_key IS NOT NULL) DESC, b.created_at DESC
		LIMIT 1`, orgID, sha)

	var b Board
	var p Project
	var ingestRun, boardKey, bSha, createdBy *string
	var outline, stackup []byte
	err := row.Scan(&b.ID, &b.ProjectID, &ingestRun, &b.InputKey, &boardKey,
		&b.LayerCount, &b.NetCount, &outline, &stackup, &bSha, &b.SizeBytes, &b.CreatedAt,
		&p.ID, &p.OrganizationID, &p.Name, &p.SourceKind, &createdBy, &p.CreatedAt)
	if err != nil {
		return nil, nil, mapErr(err)
	}
	if ingestRun != nil {
		b.IngestRunID = *ingestRun
	}
	if boardKey != nil {
		b.BoardKey = *boardKey
	}
	if bSha != nil {
		b.ContentSHA256 = *bSha
	}
	if createdBy != nil {
		p.CreatedBy = *createdBy
	}
	b.OutlineMM = json.RawMessage(outline)
	b.Stackup = json.RawMessage(stackup)
	return &b, &p, nil
}

func (s *PGStore) UpdateBoardParsed(ctx context.Context, id, boardKey string, layers, nets int, outline, stackup []byte) error {
	_, err := s.pool.Exec(ctx, `
		UPDATE emi.emi_boards
		SET s3_board_key = COALESCE(NULLIF($2,''), s3_board_key),
		    layer_count  = $3,
		    net_count    = $4,
		    outline_mm   = COALESCE($5::jsonb, outline_mm),
		    stackup      = COALESCE($6::jsonb, stackup)
		WHERE id = $1`,
		id, boardKey, layers, nets, nullJSON(outline), nullJSON(stackup))
	return mapErr(err)
}

func (s *PGStore) ListBoards(ctx context.Context, projectID string) ([]*Board, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, project_id, ingest_run_id, s3_input_key, s3_board_key,
		       layer_count, net_count, outline_mm, stackup, content_sha256, size_bytes, created_at
		FROM emi.emi_boards WHERE project_id = $1 ORDER BY created_at DESC`, projectID)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	out := []*Board{}
	for rows.Next() {
		b, err := s.scanBoard(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, b)
	}
	return out, rows.Err()
}

// ---- drivers ----

func (s *PGStore) CreateDriver(ctx context.Context, d *Driver) error {
	_, err := s.pool.Exec(ctx, `
		INSERT INTO emi.emi_drivers
		    (id, organization_id, project_id, created_by, name, role, kind, document,
		     created_at, updated_at)
		VALUES ($1,$2,$3,NULLIF($4,''),$5,$6,$7,$8,$9,$9)`,
		d.ID, d.OrganizationID, d.ProjectID, d.CreatedBy, d.Name, d.Role, d.Kind,
		[]byte(d.Document), d.CreatedAt)
	return mapErr(err)
}

func (s *PGStore) ListDrivers(ctx context.Context, projectID string) ([]*Driver, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, organization_id, project_id, COALESCE(created_by,''), name, role, kind,
		       document, created_at, updated_at
		FROM emi.emi_drivers WHERE project_id = $1 ORDER BY created_at DESC`, projectID)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	out := []*Driver{}
	for rows.Next() {
		d := &Driver{}
		var doc []byte
		if err := rows.Scan(&d.ID, &d.OrganizationID, &d.ProjectID, &d.CreatedBy, &d.Name,
			&d.Role, &d.Kind, &doc, &d.CreatedAt, &d.UpdatedAt); err != nil {
			return nil, mapErr(err)
		}
		d.Document = doc
		out = append(out, d)
	}
	return out, rows.Err()
}

// DeleteDriver is scoped by project as well as id, so a caller who has passed the project
// check cannot reach a driver in somebody else's project by guessing an id.
func (s *PGStore) DeleteDriver(ctx context.Context, projectID, driverID string) error {
	tag, err := s.pool.Exec(ctx,
		`DELETE FROM emi.emi_drivers WHERE id = $1 AND project_id = $2`, driverID, projectID)
	if err != nil {
		return mapErr(err)
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// ---- components ----

func (s *PGStore) CreateComponent(ctx context.Context, c *Component) error {
	_, err := s.pool.Exec(ctx, `
		INSERT INTO emi.emi_components
		    (id, owner_user_id, organization_id, shared, kind, name, match, model, sources,
		     provenance, version, created_at, updated_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$12)`,
		c.ID, c.OwnerUserID, c.OrganizationID, c.Shared, c.Kind, c.Name,
		jsonOrEmpty(c.Match, "{}"), jsonOrEmpty(c.Model, "{}"),
		jsonOrEmpty(c.Sources, "[]"), c.Provenance, c.Version, c.CreatedAt)
	return mapErr(err)
}

// jsonOrEmpty keeps a nil RawMessage out of a NOT NULL jsonb column. A component with no
// match rules yet is an ordinary thing to save; a NULL there is not.
func jsonOrEmpty(raw json.RawMessage, fallback string) []byte {
	if len(raw) == 0 {
		return []byte(fallback)
	}
	return []byte(raw)
}

const componentColumns = `id, owner_user_id, organization_id, shared, kind, name,
	       match, model, sources, provenance, version, created_at, updated_at`

func (s *PGStore) scanComponent(row rowScanner) (*Component, error) {
	c := &Component{}
	var match, model, sources []byte
	if err := row.Scan(&c.ID, &c.OwnerUserID, &c.OrganizationID, &c.Shared, &c.Kind, &c.Name,
		&match, &model, &sources, &c.Provenance, &c.Version, &c.CreatedAt,
		&c.UpdatedAt); err != nil {
		return nil, mapErr(err)
	}
	c.Match, c.Model, c.Sources = match, model, sources
	return c, nil
}

func (s *PGStore) GetComponent(ctx context.Context, id string) (*Component, error) {
	return s.scanComponent(s.pool.QueryRow(ctx,
		`SELECT `+componentColumns+` FROM emi.emi_components
		 WHERE id = $1 AND deleted_at IS NULL`, id))
}

// ListComponents answers "mine, plus anything my organisation has shared". A component
// belonging to a colleague who has not shared it is not listed: sharing is the deliberate
// act, and an org-wide default would leak half-finished models between engineers.
func (s *PGStore) ListComponents(ctx context.Context, orgID, userID string) ([]*Component, error) {
	rows, err := s.pool.Query(ctx,
		`SELECT `+componentColumns+` FROM emi.emi_components
		 WHERE deleted_at IS NULL AND organization_id = $1 AND (owner_user_id = $2 OR shared)
		 ORDER BY updated_at DESC`, orgID, userID)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	out := []*Component{}
	for rows.Next() {
		c, err := s.scanComponent(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

// UpdateComponent bumps the version. Runs keep the resolved copy they were built with, so a
// later edit never changes a result that has already been reported (§12).
func (s *PGStore) UpdateComponent(ctx context.Context, c *Component) error {
	tag, err := s.pool.Exec(ctx, `
		UPDATE emi.emi_components
		SET kind = $2, name = $3, match = $4, model = $5, sources = $6, provenance = $7,
		    version = version + 1, updated_at = now()
		WHERE id = $1 AND deleted_at IS NULL`,
		c.ID, c.Kind, c.Name, jsonOrEmpty(c.Match, "{}"), jsonOrEmpty(c.Model, "{}"),
		jsonOrEmpty(c.Sources, "[]"), c.Provenance)
	if err != nil {
		return mapErr(err)
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

func (s *PGStore) SetComponentShared(ctx context.Context, id string, shared bool) error {
	tag, err := s.pool.Exec(ctx,
		`UPDATE emi.emi_components SET shared = $2, updated_at = now()
		 WHERE id = $1 AND deleted_at IS NULL`, id, shared)
	if err != nil {
		return mapErr(err)
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// SoftDeleteComponent keeps the row. A finished run may still name this component in its
// modelled-parts list, and a hard delete would turn that into a dangling reference.
func (s *PGStore) SoftDeleteComponent(ctx context.Context, id string) error {
	tag, err := s.pool.Exec(ctx,
		`UPDATE emi.emi_components SET deleted_at = now()
		 WHERE id = $1 AND deleted_at IS NULL`, id)
	if err != nil {
		return mapErr(err)
	}
	if tag.RowsAffected() == 0 {
		return ErrNotFound
	}
	return nil
}

// ---- runs ----

const runCols = `id, project_id, board_id, kind, status, params, estimate,
	owner_api_key_kid, jti_key, claimed_at, started_at, finished_at,
	progress, summary, error, created_at, updated_at`

func scanRun(row rowScanner) (*Run, error) {
	var r Run
	var boardID, owner, jti, errMsg *string
	var params, estimate, progress, summary []byte
	var kind, status string
	err := row.Scan(&r.ID, &r.ProjectID, &boardID, &kind, &status, &params, &estimate,
		&owner, &jti, &r.ClaimedAt, &r.StartedAt, &r.FinishedAt,
		&progress, &summary, &errMsg, &r.CreatedAt, &r.UpdatedAt)
	if err != nil {
		return nil, mapErr(err)
	}
	r.Kind, r.Status = RunKind(kind), RunStatus(status)
	if boardID != nil {
		r.BoardID = *boardID
	}
	if owner != nil {
		r.OwnerAPIKeyKid = *owner
	}
	if jti != nil {
		r.JTIKey = *jti
	}
	if errMsg != nil {
		r.Error = *errMsg
	}
	r.Params = json.RawMessage(params)
	r.Summary = json.RawMessage(summary)
	if len(estimate) > 0 {
		var e Estimate
		if json.Unmarshal(estimate, &e) == nil {
			r.Estimate = &e
		}
	}
	if len(progress) > 0 {
		var p Progress
		if json.Unmarshal(progress, &p) == nil {
			r.Progress = &p
		}
	}
	return &r, nil
}

func (s *PGStore) CreateRun(ctx context.Context, r *Run) error {
	var estimate []byte
	if r.Estimate != nil {
		estimate, _ = json.Marshal(r.Estimate)
	}
	_, err := s.pool.Exec(ctx, `
		INSERT INTO emi.emi_runs (id, project_id, board_id, kind, status, params, estimate,
		                          created_at, updated_at)
		VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9)`,
		r.ID, r.ProjectID, nullStr(r.BoardID), string(r.Kind), string(r.Status),
		nullJSON(r.Params), nullJSON(estimate), r.CreatedAt, r.UpdatedAt)
	return mapErr(err)
}

func (s *PGStore) GetRun(ctx context.Context, id string) (*Run, error) {
	return scanRun(s.pool.QueryRow(ctx, `SELECT `+runCols+` FROM emi.emi_runs WHERE id = $1`, id))
}

func (s *PGStore) ListRuns(ctx context.Context, projectID string, limit int) ([]*Run, error) {
	rows, err := s.pool.Query(ctx, `SELECT `+runCols+`
		FROM emi.emi_runs WHERE project_id = $1 ORDER BY created_at DESC LIMIT $2`,
		projectID, limit)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	return collectRuns(rows)
}

// ListClaimableRuns returns runs waiting for a worker.
//
// An empty orgID means a shared worker, which serves every organisation -- the $1 = ” test
// short-circuits the organisation filter rather than there being two queries to keep in
// step. Ordering is oldest-first across whatever set that leaves, so one organisation's
// backlog cannot starve another's beyond its place in the queue.
func (s *PGStore) ListClaimableRuns(ctx context.Context, orgID string, limit int) ([]*Run, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT r.id, r.project_id, r.board_id, r.kind, r.status, r.params, r.estimate,
		       r.owner_api_key_kid, r.jti_key, r.claimed_at, r.started_at, r.finished_at,
		       r.progress, r.summary, r.error, r.created_at, r.updated_at
		FROM emi.emi_runs r
		JOIN emi.emi_projects p ON p.id = r.project_id
		WHERE ($1 = '' OR p.organization_id = $1) AND r.status IN ('new','retry_pending')
		ORDER BY r.created_at ASC LIMIT $2`, orgID, limit)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	return collectRuns(rows)
}

func collectRuns(rows pgx.Rows) ([]*Run, error) {
	out := []*Run{}
	for rows.Next() {
		r, err := scanRun(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// ClaimRun is the one query where correctness under concurrency actually matters.
//
// The status predicate is in the UPDATE's WHERE clause, so Postgres does the compare and
// the write as one atomic statement. Two workers racing means one updates a row and the
// other updates none -- which is why the caller can treat "no rows" as ErrConflict rather
// than needing an advisory lock or a serializable transaction.
func (s *PGStore) ClaimRun(ctx context.Context, runID, keyKid, jti string, at time.Time) (*Run, error) {
	row := s.pool.QueryRow(ctx, `
		UPDATE emi.emi_runs
		SET status = 'in_progress',
		    owner_api_key_kid = $2,
		    jti_key = $3,
		    claimed_at = $4,
		    started_at = COALESCE(started_at, $4),
		    updated_at = $4
		WHERE id = $1 AND status IN ('new','retry_pending')
		RETURNING `+runCols, runID, keyKid, jti, at)
	r, err := scanRun(row)
	if errors.Is(err, ErrNotFound) {
		return nil, ErrConflict
	}
	return r, err
}

func (s *PGStore) UpdateRunProgress(ctx context.Context, runID string, p *Progress, at time.Time) error {
	b, err := json.Marshal(p)
	if err != nil {
		return err
	}
	tag, err := s.pool.Exec(ctx, `
		UPDATE emi.emi_runs SET progress = $2::jsonb, updated_at = $3
		WHERE id = $1 AND status IN ('in_progress','stopping')`, runID, b, at)
	if err != nil {
		return mapErr(err)
	}
	if tag.RowsAffected() == 0 {
		// Progress for a run that is not running is not an error worth failing the worker
		// over, but it must not silently look like success either.
		return ErrConflict
	}
	return nil
}

func (s *PGStore) SetRunEstimate(ctx context.Context, runID string, e *Estimate) error {
	b, err := json.Marshal(e)
	if err != nil {
		return err
	}
	_, err = s.pool.Exec(ctx, `UPDATE emi.emi_runs SET estimate = $2::jsonb WHERE id = $1`, runID, b)
	return mapErr(err)
}

func (s *PGStore) CompleteRun(ctx context.Context, runID string, status RunStatus, summary []byte, errMsg string, at time.Time) error {
	tag, err := s.pool.Exec(ctx, `
		UPDATE emi.emi_runs
		SET status = $2, summary = COALESCE($3::jsonb, summary), error = NULLIF($4,''),
		    finished_at = $5, updated_at = $5
		WHERE id = $1 AND status IN ('in_progress','stopping')`,
		runID, string(status), nullJSON(summary), errMsg, at)
	if err != nil {
		return mapErr(err)
	}
	if tag.RowsAffected() == 0 {
		return ErrConflict
	}
	return nil
}

// RequestStop ends a queued run at once and asks a running one to stop.
//
// Only a run a worker holds can be "stopping": that state means "waiting for the owner to
// report back". A queued run has no owner, so it used to sit in stopping forever -- nothing
// claims it, nothing completes it, the sweeper skips it for having no claimed_at, and retry
// and stop both refuse it. It goes straight to failed instead, which retry accepts.
func (s *PGStore) RequestStop(ctx context.Context, runID string, at time.Time) error {
	tag, err := s.pool.Exec(ctx, `
		UPDATE emi.emi_runs
		SET status      = CASE WHEN status = 'in_progress' THEN 'stopping' ELSE 'failed' END,
		    error       = CASE WHEN status = 'in_progress' THEN error ELSE $3 END,
		    finished_at = CASE WHEN status = 'in_progress' THEN finished_at ELSE $2 END,
		    updated_at  = $2
		WHERE id = $1 AND status IN ('new','retry_pending','in_progress')`,
		runID, at, StoppedBeforeStartError)
	if err != nil {
		return mapErr(err)
	}
	if tag.RowsAffected() == 0 {
		return ErrConflict
	}
	return nil
}

// RetryRun clears ownership as well as status. Leaving jti_key behind would let the old
// worker's still-valid token keep writing to a run that now belongs to somebody else.
func (s *PGStore) RetryRun(ctx context.Context, runID string, at time.Time) error {
	tag, err := s.pool.Exec(ctx, `
		UPDATE emi.emi_runs
		SET status = 'retry_pending', owner_api_key_kid = NULL, jti_key = NULL,
		    claimed_at = NULL, finished_at = NULL, error = NULL, progress = NULL,
		    updated_at = $2
		WHERE id = $1 AND status IN ('failed','timed_out')`, runID, at)
	if err != nil {
		return mapErr(err)
	}
	if tag.RowsAffected() == 0 {
		return ErrConflict
	}
	return nil
}

func (s *PGStore) SweepTimedOut(ctx context.Context, deadline, at time.Time) (int, error) {
	tag, err := s.pool.Exec(ctx, `
		UPDATE emi.emi_runs SET status = 'timed_out', finished_at = $2, updated_at = $2
		WHERE status IN ('in_progress','stopping') AND claimed_at < $1`, deadline, at)
	if err != nil {
		return 0, mapErr(err)
	}
	return int(tag.RowsAffected()), nil
}

// ---- artifacts ----

func (s *PGStore) CreateArtifact(ctx context.Context, a *Artifact) error {
	_, err := s.pool.Exec(ctx, `
		INSERT INTO emi.emi_artifacts (id, run_id, name, s3_key, content_type, size_bytes, created_at)
		VALUES ($1,$2,$3,$4,$5,$6,$7)
		ON CONFLICT (run_id, name) DO UPDATE
		SET s3_key = EXCLUDED.s3_key, content_type = EXCLUDED.content_type,
		    size_bytes = EXCLUDED.size_bytes, created_at = EXCLUDED.created_at`,
		a.ID, a.RunID, a.Name, a.Key, a.ContentType, a.SizeBytes, a.CreatedAt)
	return mapErr(err)
}

func (s *PGStore) ListArtifacts(ctx context.Context, runID string) ([]*Artifact, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, run_id, name, s3_key, content_type, size_bytes, created_at
		FROM emi.emi_artifacts WHERE run_id = $1 ORDER BY name`, runID)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	out := []*Artifact{}
	for rows.Next() {
		var a Artifact
		if err := rows.Scan(&a.ID, &a.RunID, &a.Name, &a.Key, &a.ContentType, &a.SizeBytes, &a.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, &a)
	}
	return out, rows.Err()
}

func (s *PGStore) GetArtifactByName(ctx context.Context, runID, name string) (*Artifact, error) {
	var a Artifact
	err := s.pool.QueryRow(ctx, `
		SELECT id, run_id, name, s3_key, content_type, size_bytes, created_at
		FROM emi.emi_artifacts WHERE run_id = $1 AND name = $2`, runID, name).
		Scan(&a.ID, &a.RunID, &a.Name, &a.Key, &a.ContentType, &a.SizeBytes, &a.CreatedAt)
	if err != nil {
		return nil, mapErr(err)
	}
	return &a, nil
}

// ---- workers ----

func (s *PGStore) UpsertWorker(ctx context.Context, w *Worker) error {
	caps, err := json.Marshal(w.Capabilities)
	if err != nil {
		return err
	}
	_, err = s.pool.Exec(ctx, `
		INSERT INTO emi.emi_workers (id, api_key_kid, organization_id, name, status,
		                             capabilities, registered_at, last_seen_at)
		VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8)
		ON CONFLICT (api_key_kid) DO UPDATE
		SET name = EXCLUDED.name, status = EXCLUDED.status,
		    capabilities = EXCLUDED.capabilities, last_seen_at = EXCLUDED.last_seen_at`,
		w.ID, w.APIKeyKid, w.OrganizationID, w.Name, w.Status, caps, w.RegisteredAt, w.LastSeenAt)
	return mapErr(err)
}

func (s *PGStore) GetWorker(ctx context.Context, keyKid string) (*Worker, error) {
	var w Worker
	var caps []byte
	err := s.pool.QueryRow(ctx, `
		SELECT id, api_key_kid, name, status, capabilities, registered_at, last_seen_at
		FROM emi.emi_workers WHERE api_key_kid = $1`, keyKid).
		Scan(&w.ID, &w.APIKeyKid, &w.Name, &w.Status, &caps, &w.RegisteredAt, &w.LastSeenAt)
	if err != nil {
		return nil, mapErr(err)
	}
	_ = json.Unmarshal(caps, &w.Capabilities)
	return &w, nil
}

// ListWorkers returns the workers that would take work for this organisation.
//
// An empty organization_id on the row is a shared worker -- one whose key names no
// organisation -- and it serves everybody, so it belongs in every organisation's list. It
// has to match Hub.serves exactly: the two are read together by the workers endpoint, and
// when they disagreed a shared worker was counted as online while being absent from the
// list, which reads as "0 workers" to anyone looking at the page.
func (s *PGStore) ListWorkers(ctx context.Context, orgID string) ([]*Worker, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, api_key_kid, name, status, capabilities, registered_at, last_seen_at
		FROM emi.emi_workers
		WHERE (organization_id = '' OR organization_id = $1) AND status = 'active'
		ORDER BY name`, orgID)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	out := []*Worker{}
	for rows.Next() {
		var w Worker
		var caps []byte
		if err := rows.Scan(&w.ID, &w.APIKeyKid, &w.Name, &w.Status, &caps, &w.RegisteredAt, &w.LastSeenAt); err != nil {
			return nil, err
		}
		_ = json.Unmarshal(caps, &w.Capabilities)
		out = append(out, &w)
	}
	return out, rows.Err()
}

func (s *PGStore) TouchWorker(ctx context.Context, keyKid string, at time.Time) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE emi.emi_workers SET last_seen_at = $2 WHERE api_key_kid = $1`, keyKid, at)
	return mapErr(err)
}

func (s *PGStore) DeregisterWorker(ctx context.Context, keyKid string, at time.Time) error {
	_, err := s.pool.Exec(ctx,
		`UPDATE emi.emi_workers SET status = 'deregistered', last_seen_at = $2 WHERE api_key_kid = $1`,
		keyKid, at)
	return mapErr(err)
}

// ---- small helpers ----

func nullStr(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func nullJSON(b []byte) any {
	if len(b) == 0 {
		return nil
	}
	return string(b)
}
