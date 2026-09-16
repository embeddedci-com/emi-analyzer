// Package local holds what the EMI Analyzer needs to run on one computer with nothing else
// installed: a SQLite Store, a Blob on the local filesystem, and a worker-key verifier.
//
// It is a separate package from emi on purpose. embeddedci-server mounts the emi package and
// never imports this one, so the SQLite driver and the file server are not linked into the
// hosted server. `make test-go` checks that emi never starts importing it.
package local

import (
	"context"
	"database/sql"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"time"

	_ "modernc.org/sqlite" // pure Go: no cgo, so the desktop app cross-compiles

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

//go:embed schema.sql
var schemaSQL string

// SQLiteStore implements emi.Store over a single SQLite file.
//
// It is a port of emi.PGStore and keeps its semantics exactly -- the atomic claim, the
// ownership reset on retry, the organisation scoping of lookups -- because the handlers were
// written against those semantics, not against Postgres.
type SQLiteStore struct {
	db *sql.DB
}

var _ emi.Store = (*SQLiteStore)(nil)

// OpenSQLite opens (creating if needed) the database at path and applies the schema.
func OpenSQLite(ctx context.Context, path string) (*SQLiteStore, error) {
	q := url.Values{}
	q.Add("_pragma", "foreign_keys(1)")
	q.Add("_pragma", "journal_mode(WAL)")
	q.Add("_pragma", "busy_timeout(10000)")
	db, err := sql.Open("sqlite", path+"?"+q.Encode())
	if err != nil {
		return nil, fmt.Errorf("local: open %s: %w", path, err)
	}
	// One connection. SQLite has one writer anyway, and the control plane's queries are a few
	// milliseconds each; serialising them here means no statement can ever see SQLITE_BUSY.
	db.SetMaxOpenConns(1)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, fmt.Errorf("local: open %s: %w", path, err)
	}
	if _, err := db.ExecContext(ctx, schemaSQL); err != nil {
		db.Close()
		return nil, fmt.Errorf("local: apply schema: %w", err)
	}
	return &SQLiteStore{db: db}, nil
}

func (s *SQLiteStore) Close() error { return s.db.Close() }

// DB exposes the handle for the key verifier, which shares the file.
func (s *SQLiteStore) DB() *sql.DB { return s.db }

// ---- time ----

// tsLayout is fixed width and always UTC, so that two stored timestamps compare as strings in
// the same order they compare as times. The sweeper's `claimed_at < ?` depends on that.
const tsLayout = "2006-01-02T15:04:05.000000000Z"

func ts(t time.Time) string { return t.UTC().Format(tsLayout) }

func nullTS(t *time.Time) any {
	if t == nil {
		return nil
	}
	return ts(*t)
}

func parseTS(s string) (time.Time, error) {
	t, err := time.Parse(tsLayout, s)
	if err != nil {
		// Be lenient on read: a row written by hand in the sqlite shell should not make a
		// project list fail.
		t, err = time.Parse(time.RFC3339Nano, s)
	}
	return t, err
}

// tsCol scans a TEXT timestamp column, NULL included.
type tsCol struct {
	t     *time.Time
	valid bool
}

func (c *tsCol) Scan(v any) error {
	var s string
	switch x := v.(type) {
	case nil:
		c.valid = false
		return nil
	case string:
		s = x
	case []byte:
		s = string(x)
	case time.Time:
		t := x.UTC()
		c.t, c.valid = &t, true
		return nil
	default:
		return fmt.Errorf("local: cannot scan %T as a timestamp", v)
	}
	t, err := parseTS(s)
	if err != nil {
		return err
	}
	c.t, c.valid = &t, true
	return nil
}

func (c tsCol) ptr() *time.Time {
	if !c.valid {
		return nil
	}
	return c.t
}

func (c tsCol) val() time.Time {
	if !c.valid {
		return time.Time{}
	}
	return *c.t
}

// ---- helpers ----

func mapErr(err error) error {
	if errors.Is(err, sql.ErrNoRows) {
		return emi.ErrNotFound
	}
	return err
}

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

func jsonOrEmpty(raw json.RawMessage, fallback string) string {
	if len(raw) == 0 {
		return fallback
	}
	return string(raw)
}

func str(ns sql.NullString) string {
	if ns.Valid {
		return ns.String
	}
	return ""
}

func raw(ns sql.NullString) json.RawMessage {
	if !ns.Valid || ns.String == "" {
		return nil
	}
	return json.RawMessage(ns.String)
}

func affected(res sql.Result, err error) error {
	if err != nil {
		return mapErr(err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return err
	}
	if n == 0 {
		return emi.ErrNotFound
	}
	return nil
}

type rowScanner interface {
	Scan(dest ...any) error
}

// ---- projects ----

const projectCols = `id, organization_id, name, source_kind, created_by, created_at`

func scanProject(row rowScanner) (*emi.Project, error) {
	var p emi.Project
	var kind string
	var createdBy sql.NullString
	var created tsCol
	if err := row.Scan(&p.ID, &p.OrganizationID, &p.Name, &kind, &createdBy, &created); err != nil {
		return nil, mapErr(err)
	}
	p.SourceKind = emi.SourceKind(kind)
	p.CreatedBy = str(createdBy)
	p.CreatedAt = created.val()
	return &p, nil
}

func (s *SQLiteStore) CreateProject(ctx context.Context, p *emi.Project) error {
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO emi_projects (id, organization_id, name, source_kind, created_by, created_at)
		VALUES (?,?,?,?,?,?)`,
		p.ID, p.OrganizationID, p.Name, string(p.SourceKind), nullStr(p.CreatedBy), ts(p.CreatedAt))
	return mapErr(err)
}

func (s *SQLiteStore) GetProject(ctx context.Context, id string) (*emi.Project, error) {
	return scanProject(s.db.QueryRowContext(ctx,
		`SELECT `+projectCols+` FROM emi_projects WHERE id = ?`, id))
}

func (s *SQLiteStore) ListProjects(ctx context.Context, orgID string, limit int) ([]*emi.Project, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT `+projectCols+` FROM emi_projects
		WHERE organization_id = ? ORDER BY created_at DESC LIMIT ?`, orgID, limit)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	out := []*emi.Project{}
	for rows.Next() {
		p, err := scanProject(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

func (s *SQLiteStore) RenameProject(ctx context.Context, id, name string) error {
	return affected(s.db.ExecContext(ctx, `UPDATE emi_projects SET name = ? WHERE id = ?`, name, id))
}

// DeleteProject leans on the schema's ON DELETE CASCADE, which is why the connection turns
// foreign_keys on: without that pragma SQLite would leave every board and run behind.
func (s *SQLiteStore) DeleteProject(ctx context.Context, id string) error {
	return affected(s.db.ExecContext(ctx, `DELETE FROM emi_projects WHERE id = ?`, id))
}

func (s *SQLiteStore) CountBoardsSharingInput(ctx context.Context, inputKey, exceptProjectID string) (int, error) {
	var n int
	err := s.db.QueryRowContext(ctx, `
		SELECT count(*) FROM emi_boards WHERE s3_input_key = ? AND project_id <> ?`,
		inputKey, exceptProjectID).Scan(&n)
	return n, mapErr(err)
}

// ---- boards ----

const boardCols = `id, project_id, ingest_run_id, s3_input_key, s3_board_key,
	layer_count, net_count, outline_mm, stackup, content_sha256, size_bytes, created_at`

func scanBoard(row rowScanner, extra ...any) (*emi.Board, error) {
	var b emi.Board
	var ingestRun, boardKey, outline, stackup, sha sql.NullString
	var created tsCol
	dest := []any{&b.ID, &b.ProjectID, &ingestRun, &b.InputKey, &boardKey,
		&b.LayerCount, &b.NetCount, &outline, &stackup, &sha, &b.SizeBytes, &created}
	if err := row.Scan(append(dest, extra...)...); err != nil {
		return nil, mapErr(err)
	}
	b.IngestRunID = str(ingestRun)
	b.BoardKey = str(boardKey)
	b.ContentSHA256 = str(sha)
	b.OutlineMM = raw(outline)
	b.Stackup = raw(stackup)
	b.CreatedAt = created.val()
	return &b, nil
}

func (s *SQLiteStore) CreateBoard(ctx context.Context, b *emi.Board) error {
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO emi_boards (id, project_id, s3_input_key, content_sha256, size_bytes, created_at)
		VALUES (?,?,?,?,?,?)`,
		b.ID, b.ProjectID, b.InputKey, nullStr(b.ContentSHA256), b.SizeBytes, ts(b.CreatedAt))
	return mapErr(err)
}

func (s *SQLiteStore) GetBoard(ctx context.Context, id string) (*emi.Board, error) {
	return scanBoard(s.db.QueryRowContext(ctx, `SELECT `+boardCols+` FROM emi_boards WHERE id = ?`, id))
}

// FindBoardByContent: see emi.PGStore.FindBoardByContent. Newest match wins, parsed first.
func (s *SQLiteStore) FindBoardByContent(ctx context.Context, orgID, sha string) (*emi.Board, *emi.Project, error) {
	row := s.db.QueryRowContext(ctx, `
		SELECT b.id, b.project_id, b.ingest_run_id, b.s3_input_key, b.s3_board_key,
		       b.layer_count, b.net_count, b.outline_mm, b.stackup,
		       b.content_sha256, b.size_bytes, b.created_at,
		       p.id, p.organization_id, p.name, p.source_kind, p.created_by, p.created_at
		FROM emi_boards b
		JOIN emi_projects p ON p.id = b.project_id
		WHERE p.organization_id = ? AND b.content_sha256 = ?
		ORDER BY (b.s3_board_key IS NOT NULL) DESC, b.created_at DESC
		LIMIT 1`, orgID, sha)

	var p emi.Project
	var kind string
	var createdBy sql.NullString
	var pCreated tsCol
	b, err := scanBoard(row, &p.ID, &p.OrganizationID, &p.Name, &kind, &createdBy, &pCreated)
	if err != nil {
		return nil, nil, err
	}
	p.SourceKind = emi.SourceKind(kind)
	p.CreatedBy = str(createdBy)
	p.CreatedAt = pCreated.val()
	return b, &p, nil
}

func (s *SQLiteStore) UpdateBoardParsed(ctx context.Context, id, boardKey string, layers, nets int, outline, stackup []byte) error {
	_, err := s.db.ExecContext(ctx, `
		UPDATE emi_boards
		SET s3_board_key = COALESCE(NULLIF(?,''), s3_board_key),
		    layer_count  = ?,
		    net_count    = ?,
		    outline_mm   = COALESCE(?, outline_mm),
		    stackup      = COALESCE(?, stackup)
		WHERE id = ?`,
		boardKey, layers, nets, nullJSON(outline), nullJSON(stackup), id)
	return mapErr(err)
}

func (s *SQLiteStore) ListBoards(ctx context.Context, projectID string) ([]*emi.Board, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT `+boardCols+` FROM emi_boards
		WHERE project_id = ? ORDER BY created_at DESC`, projectID)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	out := []*emi.Board{}
	for rows.Next() {
		b, err := scanBoard(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, b)
	}
	return out, rows.Err()
}

// ---- drivers ----

func (s *SQLiteStore) CreateDriver(ctx context.Context, d *emi.Driver) error {
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO emi_drivers
		    (id, organization_id, project_id, created_by, name, role, kind, document,
		     created_at, updated_at)
		VALUES (?,?,?,?,?,?,?,?,?,?)`,
		d.ID, d.OrganizationID, d.ProjectID, nullStr(d.CreatedBy), d.Name, d.Role, d.Kind,
		string(d.Document), ts(d.CreatedAt), ts(d.CreatedAt))
	return mapErr(err)
}

func (s *SQLiteStore) ListDrivers(ctx context.Context, projectID string) ([]*emi.Driver, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, organization_id, project_id, COALESCE(created_by,''), name, role, kind,
		       document, created_at, updated_at
		FROM emi_drivers WHERE project_id = ? ORDER BY created_at DESC`, projectID)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	out := []*emi.Driver{}
	for rows.Next() {
		d := &emi.Driver{}
		var doc string
		var created, updated tsCol
		if err := rows.Scan(&d.ID, &d.OrganizationID, &d.ProjectID, &d.CreatedBy, &d.Name,
			&d.Role, &d.Kind, &doc, &created, &updated); err != nil {
			return nil, mapErr(err)
		}
		d.Document = json.RawMessage(doc)
		d.CreatedAt, d.UpdatedAt = created.val(), updated.val()
		out = append(out, d)
	}
	return out, rows.Err()
}

func (s *SQLiteStore) DeleteDriver(ctx context.Context, projectID, driverID string) error {
	return affected(s.db.ExecContext(ctx,
		`DELETE FROM emi_drivers WHERE id = ? AND project_id = ?`, driverID, projectID))
}

// ---- components ----

const componentCols = `id, owner_user_id, organization_id, shared, kind, name,
	match, model, sources, provenance, version, created_at, updated_at`

func scanComponent(row rowScanner) (*emi.Component, error) {
	c := &emi.Component{}
	var match, model, sources string
	var created, updated tsCol
	if err := row.Scan(&c.ID, &c.OwnerUserID, &c.OrganizationID, &c.Shared, &c.Kind, &c.Name,
		&match, &model, &sources, &c.Provenance, &c.Version, &created, &updated); err != nil {
		return nil, mapErr(err)
	}
	c.Match, c.Model, c.Sources = json.RawMessage(match), json.RawMessage(model), json.RawMessage(sources)
	c.CreatedAt, c.UpdatedAt = created.val(), updated.val()
	return c, nil
}

func (s *SQLiteStore) CreateComponent(ctx context.Context, c *emi.Component) error {
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO emi_components
		    (id, owner_user_id, organization_id, shared, kind, name, match, model, sources,
		     provenance, version, created_at, updated_at)
		VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`,
		c.ID, c.OwnerUserID, c.OrganizationID, c.Shared, c.Kind, c.Name,
		jsonOrEmpty(c.Match, "{}"), jsonOrEmpty(c.Model, "{}"), jsonOrEmpty(c.Sources, "[]"),
		c.Provenance, c.Version, ts(c.CreatedAt), ts(c.CreatedAt))
	return mapErr(err)
}

func (s *SQLiteStore) GetComponent(ctx context.Context, id string) (*emi.Component, error) {
	return scanComponent(s.db.QueryRowContext(ctx,
		`SELECT `+componentCols+` FROM emi_components WHERE id = ? AND deleted_at IS NULL`, id))
}

func (s *SQLiteStore) ListComponents(ctx context.Context, orgID, userID string) ([]*emi.Component, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT `+componentCols+` FROM emi_components
		WHERE deleted_at IS NULL AND organization_id = ? AND (owner_user_id = ? OR shared)
		ORDER BY updated_at DESC`, orgID, userID)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	out := []*emi.Component{}
	for rows.Next() {
		c, err := scanComponent(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

func (s *SQLiteStore) UpdateComponent(ctx context.Context, c *emi.Component) error {
	return affected(s.db.ExecContext(ctx, `
		UPDATE emi_components
		SET kind = ?, name = ?, match = ?, model = ?, sources = ?, provenance = ?,
		    version = version + 1, updated_at = ?
		WHERE id = ? AND deleted_at IS NULL`,
		c.Kind, c.Name, jsonOrEmpty(c.Match, "{}"), jsonOrEmpty(c.Model, "{}"),
		jsonOrEmpty(c.Sources, "[]"), c.Provenance, ts(time.Now()), c.ID))
}

func (s *SQLiteStore) SetComponentShared(ctx context.Context, id string, shared bool) error {
	return affected(s.db.ExecContext(ctx,
		`UPDATE emi_components SET shared = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL`,
		shared, ts(time.Now()), id))
}

func (s *SQLiteStore) SoftDeleteComponent(ctx context.Context, id string) error {
	return affected(s.db.ExecContext(ctx,
		`UPDATE emi_components SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL`,
		ts(time.Now()), id))
}

// ---- runs ----

const runCols = `id, project_id, board_id, kind, status, params, estimate,
	owner_api_key_kid, jti_key, claimed_at, started_at, finished_at,
	progress, summary, error, created_at, updated_at`

func scanRun(row rowScanner) (*emi.Run, error) {
	var r emi.Run
	var boardID, owner, jti, errMsg, params, estimate, progress, summary sql.NullString
	var kind, status string
	var claimed, started, finished, created, updated tsCol
	err := row.Scan(&r.ID, &r.ProjectID, &boardID, &kind, &status, &params, &estimate,
		&owner, &jti, &claimed, &started, &finished,
		&progress, &summary, &errMsg, &created, &updated)
	if err != nil {
		return nil, mapErr(err)
	}
	r.Kind, r.Status = emi.RunKind(kind), emi.RunStatus(status)
	r.BoardID, r.OwnerAPIKeyKid, r.JTIKey, r.Error = str(boardID), str(owner), str(jti), str(errMsg)
	r.Params, r.Summary = raw(params), raw(summary)
	r.ClaimedAt, r.StartedAt, r.FinishedAt = claimed.ptr(), started.ptr(), finished.ptr()
	r.CreatedAt, r.UpdatedAt = created.val(), updated.val()
	if estimate.Valid && estimate.String != "" {
		var e emi.Estimate
		if json.Unmarshal([]byte(estimate.String), &e) == nil {
			r.Estimate = &e
		}
	}
	if progress.Valid && progress.String != "" {
		var p emi.Progress
		if json.Unmarshal([]byte(progress.String), &p) == nil {
			r.Progress = &p
		}
	}
	return &r, nil
}

func collectRuns(rows *sql.Rows) ([]*emi.Run, error) {
	defer rows.Close()
	out := []*emi.Run{}
	for rows.Next() {
		r, err := scanRun(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func (s *SQLiteStore) CreateRun(ctx context.Context, r *emi.Run) error {
	var estimate []byte
	if r.Estimate != nil {
		estimate, _ = json.Marshal(r.Estimate)
	}
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO emi_runs (id, project_id, board_id, kind, status, params, estimate,
		                      created_at, updated_at)
		VALUES (?,?,?,?,?,?,?,?,?)`,
		r.ID, r.ProjectID, nullStr(r.BoardID), string(r.Kind), string(r.Status),
		nullJSON(r.Params), nullJSON(estimate), ts(r.CreatedAt), ts(r.UpdatedAt))
	return mapErr(err)
}

func (s *SQLiteStore) GetRun(ctx context.Context, id string) (*emi.Run, error) {
	return scanRun(s.db.QueryRowContext(ctx, `SELECT `+runCols+` FROM emi_runs WHERE id = ?`, id))
}

func (s *SQLiteStore) ListRuns(ctx context.Context, projectID string, limit int) ([]*emi.Run, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT `+runCols+` FROM emi_runs
		WHERE project_id = ? ORDER BY created_at DESC LIMIT ?`, projectID, limit)
	if err != nil {
		return nil, mapErr(err)
	}
	return collectRuns(rows)
}

func (s *SQLiteStore) ListClaimableRuns(ctx context.Context, orgID string, limit int) ([]*emi.Run, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT r.id, r.project_id, r.board_id, r.kind, r.status, r.params, r.estimate,
		       r.owner_api_key_kid, r.jti_key, r.claimed_at, r.started_at, r.finished_at,
		       r.progress, r.summary, r.error, r.created_at, r.updated_at
		FROM emi_runs r
		JOIN emi_projects p ON p.id = r.project_id
		WHERE (? = '' OR p.organization_id = ?) AND r.status IN ('new','retry_pending')
		ORDER BY r.created_at ASC LIMIT ?`, orgID, orgID, limit)
	if err != nil {
		return nil, mapErr(err)
	}
	return collectRuns(rows)
}

// ClaimRun is atomic for the same reason the Postgres one is: the status predicate is in the
// UPDATE, so of two racing workers exactly one changes a row.
func (s *SQLiteStore) ClaimRun(ctx context.Context, runID, keyKid, jti string, at time.Time) (*emi.Run, error) {
	r, err := scanRun(s.db.QueryRowContext(ctx, `
		UPDATE emi_runs
		SET status = 'in_progress',
		    owner_api_key_kid = ?,
		    jti_key = ?,
		    claimed_at = ?,
		    started_at = COALESCE(started_at, ?),
		    updated_at = ?
		WHERE id = ? AND status IN ('new','retry_pending')
		RETURNING `+runCols, keyKid, jti, ts(at), ts(at), ts(at), runID))
	if errors.Is(err, emi.ErrNotFound) {
		return nil, emi.ErrConflict
	}
	return r, err
}

// conflictIfNone turns "no row matched the state predicate" into ErrConflict, as the Postgres
// store does for every state transition.
func conflictIfNone(res sql.Result, err error) error {
	if err := affected(res, err); errors.Is(err, emi.ErrNotFound) {
		return emi.ErrConflict
	} else {
		return err
	}
}

func (s *SQLiteStore) UpdateRunProgress(ctx context.Context, runID string, p *emi.Progress, at time.Time) error {
	b, err := json.Marshal(p)
	if err != nil {
		return err
	}
	return conflictIfNone(s.db.ExecContext(ctx, `
		UPDATE emi_runs SET progress = ?, updated_at = ?
		WHERE id = ? AND status IN ('in_progress','stopping')`, string(b), ts(at), runID))
}

func (s *SQLiteStore) SetRunEstimate(ctx context.Context, runID string, e *emi.Estimate) error {
	b, err := json.Marshal(e)
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx, `UPDATE emi_runs SET estimate = ? WHERE id = ?`, string(b), runID)
	return mapErr(err)
}

func (s *SQLiteStore) CompleteRun(ctx context.Context, runID string, status emi.RunStatus, summary []byte, errMsg string, at time.Time) error {
	return conflictIfNone(s.db.ExecContext(ctx, `
		UPDATE emi_runs
		SET status = ?, summary = COALESCE(?, summary), error = NULLIF(?,''),
		    finished_at = ?, updated_at = ?
		WHERE id = ? AND status IN ('in_progress','stopping')`,
		string(status), nullJSON(summary), errMsg, ts(at), ts(at), runID))
}

func (s *SQLiteStore) RequestStop(ctx context.Context, runID string, at time.Time) error {
	return conflictIfNone(s.db.ExecContext(ctx, `
		UPDATE emi_runs SET status = 'stopping', updated_at = ?
		WHERE id = ? AND status IN ('new','retry_pending','in_progress')`, ts(at), runID))
}

func (s *SQLiteStore) RetryRun(ctx context.Context, runID string, at time.Time) error {
	return conflictIfNone(s.db.ExecContext(ctx, `
		UPDATE emi_runs
		SET status = 'retry_pending', owner_api_key_kid = NULL, jti_key = NULL,
		    claimed_at = NULL, finished_at = NULL, error = NULL, progress = NULL,
		    updated_at = ?
		WHERE id = ? AND status IN ('failed','timed_out')`, ts(at), runID))
}

func (s *SQLiteStore) SweepTimedOut(ctx context.Context, deadline, at time.Time) (int, error) {
	res, err := s.db.ExecContext(ctx, `
		UPDATE emi_runs SET status = 'timed_out', finished_at = ?, updated_at = ?
		WHERE status IN ('in_progress','stopping') AND claimed_at < ?`, ts(at), ts(at), ts(deadline))
	if err != nil {
		return 0, mapErr(err)
	}
	n, err := res.RowsAffected()
	return int(n), err
}

// FailInterrupted fails every run that was being worked on when the app last stopped.
//
// Not part of emi.Store: a hosted server's workers outlive it, so a run in progress at
// startup is still being worked on. Here the worker is a container this process started and
// stopped, so a run left in progress can only be one that was cut off -- and without this it
// would sit at "in progress" for the sweeper's full 24 hours.
func (s *SQLiteStore) FailInterrupted(ctx context.Context, at time.Time) (int, error) {
	res, err := s.db.ExecContext(ctx, `
		UPDATE emi_runs
		SET status = 'failed', error = 'interrupted: the app was closed while this run was in progress',
		    finished_at = ?, updated_at = ?
		WHERE status IN ('in_progress','stopping')`, ts(at), ts(at))
	if err != nil {
		return 0, mapErr(err)
	}
	n, err := res.RowsAffected()
	return int(n), err
}

// ---- artifacts ----

func (s *SQLiteStore) CreateArtifact(ctx context.Context, a *emi.Artifact) error {
	_, err := s.db.ExecContext(ctx, `
		INSERT INTO emi_artifacts (id, run_id, name, s3_key, content_type, size_bytes, created_at)
		VALUES (?,?,?,?,?,?,?)
		ON CONFLICT (run_id, name) DO UPDATE
		SET s3_key = excluded.s3_key, content_type = excluded.content_type,
		    size_bytes = excluded.size_bytes, created_at = excluded.created_at`,
		a.ID, a.RunID, a.Name, a.Key, a.ContentType, a.SizeBytes, ts(a.CreatedAt))
	return mapErr(err)
}

func scanArtifact(row rowScanner) (*emi.Artifact, error) {
	var a emi.Artifact
	var created tsCol
	if err := row.Scan(&a.ID, &a.RunID, &a.Name, &a.Key, &a.ContentType, &a.SizeBytes, &created); err != nil {
		return nil, mapErr(err)
	}
	a.CreatedAt = created.val()
	return &a, nil
}

func (s *SQLiteStore) ListArtifacts(ctx context.Context, runID string) ([]*emi.Artifact, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, run_id, name, s3_key, content_type, size_bytes, created_at
		FROM emi_artifacts WHERE run_id = ? ORDER BY name`, runID)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	out := []*emi.Artifact{}
	for rows.Next() {
		a, err := scanArtifact(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, a)
	}
	return out, rows.Err()
}

func (s *SQLiteStore) GetArtifactByName(ctx context.Context, runID, name string) (*emi.Artifact, error) {
	return scanArtifact(s.db.QueryRowContext(ctx, `
		SELECT id, run_id, name, s3_key, content_type, size_bytes, created_at
		FROM emi_artifacts WHERE run_id = ? AND name = ?`, runID, name))
}

// ---- workers ----

func (s *SQLiteStore) UpsertWorker(ctx context.Context, w *emi.Worker) error {
	caps, err := json.Marshal(w.Capabilities)
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx, `
		INSERT INTO emi_workers (id, api_key_kid, organization_id, name, status,
		                         capabilities, registered_at, last_seen_at)
		VALUES (?,?,?,?,?,?,?,?)
		ON CONFLICT (api_key_kid) DO UPDATE
		SET name = excluded.name, status = excluded.status,
		    capabilities = excluded.capabilities, last_seen_at = excluded.last_seen_at`,
		w.ID, w.APIKeyKid, w.OrganizationID, w.Name, w.Status, string(caps),
		ts(w.RegisteredAt), nullTS(w.LastSeenAt))
	return mapErr(err)
}

func scanWorker(row rowScanner) (*emi.Worker, error) {
	var w emi.Worker
	var caps string
	var registered, lastSeen tsCol
	if err := row.Scan(&w.ID, &w.APIKeyKid, &w.Name, &w.Status, &caps, &registered, &lastSeen); err != nil {
		return nil, mapErr(err)
	}
	_ = json.Unmarshal([]byte(caps), &w.Capabilities)
	w.RegisteredAt, w.LastSeenAt = registered.val(), lastSeen.ptr()
	return &w, nil
}

func (s *SQLiteStore) GetWorker(ctx context.Context, keyKid string) (*emi.Worker, error) {
	return scanWorker(s.db.QueryRowContext(ctx, `
		SELECT id, api_key_kid, name, status, capabilities, registered_at, last_seen_at
		FROM emi_workers WHERE api_key_kid = ?`, keyKid))
}

func (s *SQLiteStore) ListWorkers(ctx context.Context, orgID string) ([]*emi.Worker, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT id, api_key_kid, name, status, capabilities, registered_at, last_seen_at
		FROM emi_workers
		WHERE (organization_id = '' OR organization_id = ?) AND status = 'active'
		ORDER BY name`, orgID)
	if err != nil {
		return nil, mapErr(err)
	}
	defer rows.Close()
	out := []*emi.Worker{}
	for rows.Next() {
		w, err := scanWorker(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, w)
	}
	return out, rows.Err()
}

func (s *SQLiteStore) TouchWorker(ctx context.Context, keyKid string, at time.Time) error {
	_, err := s.db.ExecContext(ctx,
		`UPDATE emi_workers SET last_seen_at = ? WHERE api_key_kid = ?`, ts(at), keyKid)
	return mapErr(err)
}

func (s *SQLiteStore) DeregisterWorker(ctx context.Context, keyKid string, at time.Time) error {
	_, err := s.db.ExecContext(ctx,
		`UPDATE emi_workers SET status = 'deregistered', last_seen_at = ? WHERE api_key_kid = ?`,
		ts(at), keyKid)
	return mapErr(err)
}

// DeregisterAll marks every worker gone. Called at startup: the only worker the local app
// knows is the container it starts, and a row left "active" from the last session would be
// counted as online until that container registers again.
func (s *SQLiteStore) DeregisterAll(ctx context.Context, at time.Time) error {
	_, err := s.db.ExecContext(ctx,
		`UPDATE emi_workers SET status = 'deregistered', last_seen_at = ? WHERE status = 'active'`, ts(at))
	return mapErr(err)
}
