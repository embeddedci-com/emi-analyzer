package emi

import (
	"context"
	"errors"
	"log/slog"
	"time"
)

// Everything this package needs from a host application lives behind the interfaces in
// this file. A host that embeds this package satisfies them with its own key table and
// storage client; cmd/emi-server satisfies them with Postgres and S3-compatible storage, and
// cmd/emi-local with SQLite and local files. Neither side needs to know about the other.

// AgentKey is the identity behind a verified agent API key.
type AgentKey struct {
	Kid            string
	OrganizationID string
	AgentType      string
	Name           string
}

var (
	// ErrKeyNotFound means the key id does not exist, is revoked, or the secret is wrong.
	// Deliberately one error for all three so callers cannot probe for valid key ids.
	ErrKeyNotFound = errors.New("emi: agent key not found")

	// ErrWrongAgentType means the key is valid but is not an EMI worker key.
	ErrWrongAgentType = errors.New("emi: key is not an emi agent key")

	// ErrNotFound is returned by Store for a missing row.
	ErrNotFound = errors.New("emi: not found")

	// ErrConflict is returned when a state transition is not legal from the current state,
	// for example two workers racing to claim the same run.
	ErrConflict = errors.New("emi: conflicting state")
)

// KeyVerifier turns a raw "eci_<kid>_<secret>" string into an identity.
//
// The host owns this deliberately: a host with agent keys of its own already has a hashing
// scheme for them, and duplicating that here would mean two implementations of the same
// security-critical comparison. The built-in stores share the one in agentkey.go.
type KeyVerifier interface {
	VerifyAgentKey(ctx context.Context, raw string) (AgentKey, error)
}

// Blob is the object storage this package hands out URLs for. Note that there is no Get or
// Put: the control plane must never carry bulk bytes, and leaving those methods off the
// interface makes that impossible rather than merely discouraged.
type Blob interface {
	PresignGet(ctx context.Context, key string, ttl time.Duration) (string, error)
	// PresignPut mints an upload URL. A size above zero is the exact length the upload must
	// have, and storage that can enforce it does; zero means the client did not say.
	PresignPut(ctx context.Context, key, contentType string, size int64, ttl time.Duration) (string, error)
	Stat(ctx context.Context, key string) (sizeBytes int64, contentType string, err error)
}

// BlobDeleter is an optional half of Blob: storage that can also remove objects.
//
// Optional because the control plane's normal life never deletes anything -- results are
// immutable and a run is kept for its history. It exists for the one thing a user can ask
// for explicitly: deleting a project, which has to take its board file and results with it,
// or "delete" would leave the design on disk.
type BlobDeleter interface {
	// Delete removes one object. A key that is already gone is not an error.
	Delete(ctx context.Context, key string) error
	// DeletePrefix removes every object under a prefix.
	DeletePrefix(ctx context.Context, prefix string) error
}

// Deps is the wiring a host passes to Mount.
type Deps struct {
	Store Store
	Keys  KeyVerifier
	Blob  Blob

	// TokenSecret signs the short-lived per-run JWTs a worker uses for run-scoped calls.
	TokenSecret []byte

	// Now is injectable so tests do not have to sleep.
	Now func() time.Time

	Logger *slog.Logger

	// Features switches experimental parts of the analyzer on. The zero value turns them all
	// off; see features.go.
	Features Features

	// RunTimeout is how long a run may sit in_progress before the sweeper marks it
	// timed_out. Zero means DefaultRunTimeout.
	RunTimeout time.Duration

	// MaxUploadBytes caps one board upload or one artifact. Zero means
	// DefaultMaxUploadBytes. Storage is told the declared size when the client gives one,
	// and every object is checked against the cap before a row names it.
	MaxUploadBytes int64
}

// DefaultMaxUploadBytes fits a zipped KiCad project with room to spare, and the largest
// result a solve writes (near-field dumps run to a few hundred megabytes).
const DefaultMaxUploadBytes int64 = 1 << 30

func (d *Deps) maxUploadBytes() int64 {
	if d.MaxUploadBytes > 0 {
		return d.MaxUploadBytes
	}
	return DefaultMaxUploadBytes
}

// DefaultRunTimeout is generous because a legitimate solve genuinely can run for many
// hours. The sweeper is a backstop against dead workers, not a scheduling policy.
const DefaultRunTimeout = 24 * time.Hour

func (d *Deps) now() time.Time {
	if d.Now != nil {
		return d.Now()
	}
	return time.Now().UTC()
}

func (d *Deps) log() *slog.Logger {
	if d.Logger != nil {
		return d.Logger
	}
	return slog.Default()
}

func (d *Deps) runTimeout() time.Duration {
	if d.RunTimeout > 0 {
		return d.RunTimeout
	}
	return DefaultRunTimeout
}

// Store is the persistence this package needs. PGStore implements it over pgx and
// local.SQLiteStore over SQLite; a host may implement it over its own database.
type Store interface {
	// Projects
	CreateProject(ctx context.Context, p *Project) error
	GetProject(ctx context.Context, id string) (*Project, error)
	ListProjects(ctx context.Context, orgID string, limit int) ([]*Project, error)
	RenameProject(ctx context.Context, id, name string) error
	// DeleteProject removes the project and everything filed under it -- boards, runs and
	// artifacts -- in one go. The rows go; removing the objects they name is the caller's job,
	// because only it knows whether another project still refers to them.
	DeleteProject(ctx context.Context, id string) error
	// CountBoardsSharingInput answers "does any project other than this one use these bytes?".
	// An upload is content-addressed, so two projects can name one object and deleting one
	// project must not take the other's board away.
	CountBoardsSharingInput(ctx context.Context, inputKey, exceptProjectID string) (int, error)

	// Boards
	CreateBoard(ctx context.Context, b *Board) error
	GetBoard(ctx context.Context, id string) (*Board, error)
	UpdateBoardParsed(ctx context.Context, id, boardKey string, layers, nets int, outline, stackup []byte) error
	ListBoards(ctx context.Context, projectID string) ([]*Board, error)

	// Drivers. Project-scoped and open to visitors, like boards.
	CreateDriver(ctx context.Context, d *Driver) error
	ListDrivers(ctx context.Context, projectID string) ([]*Driver, error)
	DeleteDriver(ctx context.Context, projectID, driverID string) error

	// Components. Owned by a signed-in user, visible to their organisation when shared.
	CreateComponent(ctx context.Context, c *Component) error
	GetComponent(ctx context.Context, id string) (*Component, error)
	ListComponents(ctx context.Context, orgID, userID string) ([]*Component, error)
	UpdateComponent(ctx context.Context, c *Component) error
	SetComponentShared(ctx context.Context, id string, shared bool) error
	SoftDeleteComponent(ctx context.Context, id string) error
	// FindBoardByContent answers "has this organisation already uploaded these bytes?".
	// Returns ErrNotFound when it has not. Scoped to the organisation deliberately: the
	// hash is a fact about the file, but who has it is not.
	FindBoardByContent(ctx context.Context, orgID, sha256 string) (*Board, *Project, error)

	// Runs
	CreateRun(ctx context.Context, r *Run) error
	GetRun(ctx context.Context, id string) (*Run, error)
	ListRuns(ctx context.Context, projectID string, limit int) ([]*Run, error)
	ListClaimableRuns(ctx context.Context, orgID string, limit int) ([]*Run, error)

	// ClaimRun moves a run from a claimable state to in_progress and records ownership.
	// It must be atomic: two workers racing for the same run means exactly one wins and
	// the other gets ErrConflict.
	ClaimRun(ctx context.Context, runID, keyKid, jti string, at time.Time) (*Run, error)

	UpdateRunProgress(ctx context.Context, runID string, p *Progress, at time.Time) error
	SetRunEstimate(ctx context.Context, runID string, e *Estimate) error
	// CompleteRun applies a worker's final report atomically. ErrConflict, and nothing
	// written, when the run is no longer in_progress or stopping.
	CompleteRun(ctx context.Context, runID string, c *Completion, at time.Time) error
	// RequestStop moves an in_progress run to stopping, for its worker to wind down, and a
	// queued (new or retry_pending) run straight to failed with StoppedBeforeStartError,
	// because no worker holds it to report back. ErrConflict for any other state.
	RequestStop(ctx context.Context, runID string, at time.Time) error
	RetryRun(ctx context.Context, runID string, at time.Time) error

	// SweepTimedOut marks runs that have been in_progress past the deadline as timed_out
	// and returns how many it changed.
	SweepTimedOut(ctx context.Context, deadline time.Time, at time.Time) (int, error)

	// Artifacts
	CreateArtifact(ctx context.Context, a *Artifact) error
	ListArtifacts(ctx context.Context, runID string) ([]*Artifact, error)
	GetArtifactByName(ctx context.Context, runID, name string) (*Artifact, error)

	// Workers. A host may map these onto an agents table of its own.
	UpsertWorker(ctx context.Context, w *Worker) error
	GetWorker(ctx context.Context, keyKid string) (*Worker, error)
	ListWorkers(ctx context.Context, orgID string) ([]*Worker, error)
	TouchWorker(ctx context.Context, keyKid string, at time.Time) error
	DeregisterWorker(ctx context.Context, keyKid string, at time.Time) error
}
