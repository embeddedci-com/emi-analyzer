// Package emi is the EMI Analyzer control plane.
//
// It is deliberately self-contained: everything it needs from a host application
// (API-key lookup, object storage, a clock) arrives through the small interfaces in
// deps.go, and every HTTP route it serves is registered by a single call to Mount.
// That keeps this package mountable into embeddedci-server without either codebase
// growing knowledge of the other.
package emi

import (
	"encoding/json"
	"time"
)

// RunKind separates the two very different workloads a worker can be asked to do.
//
// An ingest run is seconds of parsing and rule checks, and any worker can take one.
// A solve run is hours of FDTD and needs real cores and RAM. Keeping them apart is
// what lets a board appear on screen immediately while the heavy work stays optional.
type RunKind string

// A transient run simulates an ESD discharge on a board's exposed lines with ngspice: seconds of
// circuit solving, on demand, and like ingest any worker with the simulator can take it.
const (
	RunKindIngest    RunKind = "ingest"
	RunKindSolve     RunKind = "solve"
	RunKindTransient RunKind = "transient"
	// RunKindCable is the Tier A cable budget (docs/implementation.md §5.1): a few hundred
	// milliseconds of method-of-moments on a wire, needing no solve at all.
	RunKindCable RunKind = "cable"
	// RunKindCompliance combines what other runs already produced into a margin against a
	// limit (§16, §17). It runs no solver at all -- it reads a solve's far field, its cable
	// transfer functions and the rule findings -- so a user changing a cable length or
	// swapping a driver gets the answer back without queueing behind a solver worker.
	RunKindCompliance RunKind = "compliance"
)

func (k RunKind) Valid() bool {
	switch k {
	case RunKindIngest, RunKindSolve, RunKindTransient, RunKindCable, RunKindCompliance:
		return true
	}
	return false
}

// RunStatus mirrors the build-job vocabulary in embeddedci-server's docs/job-lifecycle.md
// exactly, including the states we do not yet emit, so that an operator reading the EMI
// tables sees the same words they already know from builds.
type RunStatus string

const (
	StatusNew          RunStatus = "new"
	StatusRetryPending RunStatus = "retry_pending"
	StatusInProgress   RunStatus = "in_progress"
	StatusStopping     RunStatus = "stopping"
	StatusDone         RunStatus = "done"
	StatusFailed       RunStatus = "failed"
	StatusTimedOut     RunStatus = "timed_out"
)

// StoppedBeforeStartError is the error recorded on a run stopped while it was still queued.
// It ends as failed, like a run its worker stopped, so it can be retried the same way.
const StoppedBeforeStartError = "stopped before a worker started it"

// Claimable reports whether a worker may take this run.
func (s RunStatus) Claimable() bool {
	return s == StatusNew || s == StatusRetryPending
}

// Terminal reports whether the run has finished and will not change again on its own.
func (s RunStatus) Terminal() bool {
	return s == StatusDone || s == StatusFailed || s == StatusTimedOut
}

func (s RunStatus) Valid() bool {
	switch s {
	case StatusNew, StatusRetryPending, StatusInProgress, StatusStopping,
		StatusDone, StatusFailed, StatusTimedOut:
		return true
	}
	return false
}

// SourceKind is the format the user uploaded. KiCad is the P1 path; Gerber arrives in P3.
type SourceKind string

const (
	SourceKiCad  SourceKind = "kicad"
	SourceGerber SourceKind = "gerber"
)

func (s SourceKind) Valid() bool {
	return s == SourceKiCad || s == SourceGerber
}

// AgentTypeEMI is the third value of embeddedci-server's api_keys.agent_type column,
// alongside the existing "build" and "hw".
const AgentTypeEMI = "emi"

// Capabilities is what a worker declares about itself at registration.
//
// Build agents need no equivalent because build jobs are roughly uniform. EMI runs span
// four orders of magnitude, so the server has to know whether a given worker can actually
// finish a given run before dispatching it — otherwise a laptop cheerfully claims a
// 40-hour solve and dies on it.
type Capabilities struct {
	Cores          int      `json:"cores"`
	RAMGB          float64  `json:"ram_gb"`
	MaxCells       int64    `json:"max_cells"`
	OpenEMSVersion string   `json:"openems_version,omitempty"`
	Tools          []string `json:"tools,omitempty"`
	// Kinds the worker is willing to run. Empty means both.
	Kinds []RunKind `json:"kinds,omitempty"`
}

// Accepts reports whether this worker can take a run of the given kind and size.
func (c Capabilities) Accepts(kind RunKind, cells int64) bool {
	if len(c.Kinds) > 0 {
		var ok bool
		for _, k := range c.Kinds {
			if k == kind {
				ok = true
				break
			}
		}
		if !ok {
			return false
		}
	}
	// Ingest and transient runs are cheap by construction and are not sized.
	// Cells only bound a solve. Ingest, transient and cable runs are seconds of work on any
	// worker, so a cell ceiling would refuse them for no reason.
	if kind == RunKindIngest || kind == RunKindTransient || kind == RunKindCable {
		return true
	}
	if c.MaxCells > 0 && cells > c.MaxCells {
		return false
	}
	return true
}

// Project groups the boards and runs for one PCB under one organisation.
type Project struct {
	ID             string     `json:"id"`
	OrganizationID string     `json:"organization_id"`
	Name           string     `json:"name"`
	SourceKind     SourceKind `json:"source_kind"`
	CreatedBy      string     `json:"created_by,omitempty"`
	CreatedAt      time.Time  `json:"created_at"`
}

// Driver is a measured or declared source attached to a port, stored as the emi-driver
// document of docs/emi-driver-format.md. The document travels whole; Name, Role and Kind are
// lifted out of it so a list can be rendered without parsing every row.
type Driver struct {
	ID             string          `json:"id"`
	OrganizationID string          `json:"organization_id"`
	ProjectID      string          `json:"project_id"`
	CreatedBy      string          `json:"created_by,omitempty"`
	Name           string          `json:"name"`
	Role           string          `json:"role"`
	Kind           string          `json:"kind"`
	Document       json.RawMessage `json:"document"`
	CreatedAt      time.Time       `json:"created_at"`
	UpdatedAt      time.Time       `json:"updated_at"`
}

// Component is a user's model for a part the analyzer would otherwise treat as bare copper
// (§11-13). Saving one needs an account: a visitor can build and use a component, but it
// lives in their browser and does not survive the session, because there is nothing to file
// it under that they could come back to.
type Component struct {
	ID             string          `json:"id"`
	OwnerUserID    string          `json:"owner_user_id"`
	OrganizationID string          `json:"organization_id"`
	Shared         bool            `json:"shared"`
	Kind           string          `json:"kind"`
	Name           string          `json:"name"`
	Match          json.RawMessage `json:"match,omitempty"`
	Model          json.RawMessage `json:"model,omitempty"`
	Sources        json.RawMessage `json:"sources,omitempty"`
	//: Where the numbers came from — generic, vendor, measured or user. Decides what the UI
	//: may claim about them; a generic figure is never attributed to a manufacturer.
	Provenance string    `json:"provenance"`
	Version    int       `json:"version"`
	CreatedAt  time.Time `json:"created_at"`
	UpdatedAt  time.Time `json:"updated_at"`
}

// Board is one normalised parse of an upload: the board.json + geometry.bin pair that
// everything downstream (viewer, rules, mesher) reads instead of the original format.
type Board struct {
	ID          string `json:"id"`
	ProjectID   string `json:"project_id"`
	IngestRunID string `json:"ingest_run_id,omitempty"`
	InputKey    string `json:"s3_input_key"`
	BoardKey    string `json:"s3_board_key,omitempty"`
	// ContentSHA256 is the hash of the uploaded bytes. It is what makes a re-upload of
	// the same file recognisable, and what content-addresses the object in storage.
	ContentSHA256 string          `json:"content_sha256,omitempty"`
	SizeBytes     int64           `json:"size_bytes,omitempty"`
	LayerCount    int             `json:"layer_count"`
	NetCount      int             `json:"net_count"`
	OutlineMM     json.RawMessage `json:"outline_mm,omitempty"`
	Stackup       json.RawMessage `json:"stackup,omitempty"`
	CreatedAt     time.Time       `json:"created_at"`
}

// Progress is what a worker streams while it works. Build agents stream log lines; an EMI
// solve needs numbers the UI can draw, and EnergyDB in particular is the single best
// signal of whether a run is converging or wasting hours.
type Progress struct {
	Stage          string     `json:"stage"`
	Pct            float64    `json:"pct"`
	Message        string     `json:"message,omitempty"`
	Cells          int64      `json:"cells,omitempty"`
	Timestep       int64      `json:"timestep,omitempty"`
	TotalTimesteps int64      `json:"total_timesteps,omitempty"`
	EnergyDB       *float64   `json:"energy_db,omitempty"`
	At             *time.Time `json:"at,omitempty"`
}

// Estimate is the cost model output. The client computes it live as the user drags the
// region of interest; the worker recomputes it authoritatively at the mesh stage and
// fails the run before solving if it no longer fits. See estimate.go.
type Estimate struct {
	Cells          int64   `json:"cells"`
	RAMBytes       int64   `json:"ram_bytes"`
	Timesteps      int64   `json:"timesteps"`
	DTSeconds      float64 `json:"dt_seconds"`
	SimTimeSeconds float64 `json:"sim_time_seconds"`
	ETASeconds     float64 `json:"eta_seconds"`
}

// Run is one unit of work handed to a worker.
type Run struct {
	ID        string          `json:"id"`
	ProjectID string          `json:"project_id"`
	BoardID   string          `json:"board_id,omitempty"`
	Kind      RunKind         `json:"kind"`
	Status    RunStatus       `json:"status"`
	Params    json.RawMessage `json:"params,omitempty"`
	Estimate  *Estimate       `json:"estimate,omitempty"`

	// Ownership, assigned when a worker mints a run token. Mirrors jobs.owner_api_key_kid
	// and jobs.jti_key in embeddedci-server.
	OwnerAPIKeyKid string `json:"owner_api_key_kid,omitempty"`
	JTIKey         string `json:"jti_key,omitempty"`

	ClaimedAt  *time.Time `json:"claimed_at,omitempty"`
	StartedAt  *time.Time `json:"started_at,omitempty"`
	FinishedAt *time.Time `json:"finished_at,omitempty"`

	Progress *Progress       `json:"progress,omitempty"`
	Summary  json.RawMessage `json:"summary,omitempty"`
	Error    string          `json:"error,omitempty"`

	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// Artifact is one file a run produced, stored in object storage and served to the browser
// through a short-lived presigned URL. The bytes never pass through the control plane.
type Artifact struct {
	ID          string    `json:"id"`
	RunID       string    `json:"run_id"`
	Name        string    `json:"name"`
	Key         string    `json:"s3_key"`
	ContentType string    `json:"content_type"`
	SizeBytes   int64     `json:"size_bytes"`
	CreatedAt   time.Time `json:"created_at"`
}

// Worker is a registered EMI worker. In embeddedci-server these rows live in app.agents
// with agent_type='emi'; the standalone dev server keeps its own equivalent table.
type Worker struct {
	ID        string `json:"id"`
	APIKeyKid string `json:"api_key_kid"`
	// OrganizationID is whose runs this worker may take, copied from the verified agent key.
	// Empty means a shared worker that serves every organisation.
	//
	// It travels on the Worker rather than being looked up in the store, because only the
	// host knows where keys live: the local stack keeps them in emi.emi_dev_api_keys, and a
	// mounted deployment in embeddedci-server's app.api_keys. A store that reaches for one
	// of those works in exactly one deployment and fails in the other.
	OrganizationID string       `json:"organization_id,omitempty"`
	Name           string       `json:"name"`
	Status         string       `json:"status"`
	Capabilities   Capabilities `json:"capabilities"`
	RegisteredAt   time.Time    `json:"registered_at"`
	LastSeenAt     *time.Time   `json:"last_seen_at,omitempty"`
}
