package emi

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"time"
)

// Service holds the EMI control plane's state. One per process.
type Service struct {
	deps Deps
	hub  *Hub
}

// New builds a Service.
func New(d Deps) (*Service, error) {
	if d.Store == nil {
		return nil, errors.New("emi: Deps.Store is required")
	}
	if d.Keys == nil {
		return nil, errors.New("emi: Deps.Keys is required")
	}
	if d.Blob == nil {
		return nil, errors.New("emi: Deps.Blob is required")
	}
	if len(d.TokenSecret) < 32 {
		return nil, errors.New("emi: Deps.TokenSecret must be at least 32 bytes")
	}
	return &Service{deps: d, hub: NewHub()}, nil
}

// Hub exposes the worker hub, for hosts that want to surface online workers in an admin UI.
func (s *Service) Hub() *Hub { return s.hub }

// Mount registers every EMI route on mux.
//
// This is the single wiring point. A host that embeds this package calls it once from its
// routing and implements the three interfaces in deps.go; no EMI concepts leak into the
// host's code beyond that.
//
// prefix is the path the host serves this package under, e.g. "/api". The user-facing
// routes below expect the host's own authentication middleware to have run and to have
// called WithUser; the agent routes authenticate themselves, because a worker's key is not
// a human session.
func (s *Service) Mount(mux *http.ServeMux, prefix string, userAuth func(http.HandlerFunc) http.HandlerFunc) {
	if userAuth == nil {
		userAuth = func(h http.HandlerFunc) http.HandlerFunc { return h }
	}
	p := prefix

	// Every route goes through noStore. Responses here are per-user or per-run, and several
	// carry presigned URLs that die after presignTTL, so no cache in front of the server may
	// keep one. That is not hypothetical: when the origin sends no Cache-Control, Cloudflare
	// decides by file extension, and "/artifacts/geometry.bin" ends in one it caches. The edge
	// kept serving a geometry URL long after it expired; storage rejects an expired signature
	// without CORS headers, so the browser reported it as a CORS failure on the bucket.
	handle := func(pattern string, h http.HandlerFunc) { mux.HandleFunc(pattern, noStore(h)) }

	// --- User-facing ---
	handle("POST "+p+"/emi/projects", userAuth(s.handleCreateProject))
	handle("GET "+p+"/emi/projects", userAuth(s.handleListProjects))
	handle("GET "+p+"/emi/projects/{project_id}", userAuth(s.handleGetProject))
	handle("PATCH "+p+"/emi/projects/{project_id}", userAuth(s.handleRenameProject))
	handle("DELETE "+p+"/emi/projects/{project_id}", userAuth(s.handleDeleteProject))
	handle("POST "+p+"/emi/projects/{project_id}/uploads", userAuth(s.handleCreateUpload))
	handle("POST "+p+"/emi/projects/{project_id}/boards", userAuth(s.handleCreateBoard))
	handle("GET "+p+"/emi/projects/{project_id}/boards", userAuth(s.handleListBoards))
	handle("GET "+p+"/emi/projects/{project_id}/drivers", userAuth(s.handleListDrivers))
	handle("POST "+p+"/emi/projects/{project_id}/drivers", userAuth(s.handleCreateDriver))
	handle("DELETE "+p+"/emi/projects/{project_id}/drivers/{driver_id}", userAuth(s.handleDeleteDriver))
	handle("POST "+p+"/emi/projects/{project_id}/runs", userAuth(s.handleCreateRun))
	handle("GET "+p+"/emi/projects/{project_id}/runs", userAuth(s.handleListRuns))
	handle("GET "+p+"/emi/runs/{run_id}", userAuth(s.handleGetRun))
	handle("POST "+p+"/emi/runs/{run_id}/stop", userAuth(s.handleStopRun))
	handle("POST "+p+"/emi/runs/{run_id}/retry", userAuth(s.handleRetryRun))
	handle("GET "+p+"/emi/runs/{run_id}/artifacts", userAuth(s.handleListArtifacts))
	// {name...} rather than {name}: artifact names are relative paths like
	// "nearfield/100000000.bin", and a single-segment wildcard would 404 on every one of
	// them. sanitiseArtifactName is what keeps the multi-segment form safe.
	handle("GET "+p+"/emi/runs/{run_id}/artifacts/{name...}", userAuth(s.handleGetArtifactURL))
	handle("GET "+p+"/emi/components", userAuth(s.handleListComponents))
	handle("POST "+p+"/emi/components", userAuth(s.handleCreateComponent))
	handle("PUT "+p+"/emi/components/{component_id}", userAuth(s.handleUpdateComponent))
	handle("POST "+p+"/emi/components/{component_id}/share", userAuth(s.handleShareComponent))
	handle("DELETE "+p+"/emi/components/{component_id}", userAuth(s.handleDeleteComponent))
	handle("GET "+p+"/emi/workers", userAuth(s.handleListWorkers))
	handle("GET "+p+"/emi/whoami", userAuth(s.handleWhoami))
	handle("GET "+p+"/emi/features", userAuth(s.handleFeatures))
	// Asked once per file the user picks, before any bytes move.
	handle("GET "+p+"/emi/boards/lookup", userAuth(s.handleLookupBoard))
	handle("POST "+p+"/emi/estimate", userAuth(s.handleEstimate))

	// --- Worker-facing (emi agent key auth) ---
	handle("GET "+p+"/emi-agent/ws", s.handleWorkerWS)
	handle("POST "+p+"/emi-agent/register", s.requireWorkerKey(s.handleWorkerRegister))
	handle("POST "+p+"/emi-agent/deregister", s.requireWorkerKey(s.handleWorkerDeregister))
	handle("GET "+p+"/emi-agent/runs", s.requireWorkerKey(s.handleWorkerListRuns))
	handle("POST "+p+"/emi-agent/runs/{run_id}/token", s.requireWorkerKey(s.handleMintRunToken))
	handle("POST "+p+"/emi-agent/runs/{run_id}/token/refresh", s.requireWorkerKey(s.handleRefreshRunToken))

	// --- Run-scoped (short-lived run token) ---
	handle("POST "+p+"/emi-agent/runs/{run_id}/claim", s.requireRunToken(s.handleClaimRun))
	handle("POST "+p+"/emi-agent/runs/{run_id}/progress", s.requireRunToken(s.handleRunProgress))
	handle("POST "+p+"/emi-agent/runs/{run_id}/artifacts", s.requireRunToken(s.handleArtifactUploadInit))
	handle("POST "+p+"/emi-agent/runs/{run_id}/complete", s.requireRunToken(s.handleCompleteRun))
	handle("GET "+p+"/emi-agent/runs/{run_id}/input", s.requireRunToken(s.handleRunInputURL))
}

// noStore forbids browsers and shared caches from keeping a response. It is applied outside
// the auth middleware, so a 401 is covered as well as a success.
func noStore(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "private, no-store")
		h(w, r)
	}
}

// StartSweeper runs the stale-run timeout loop until ctx is cancelled. It is the backstop
// for a worker that dies mid-solve without ever completing its run.
func (s *Service) StartSweeper(ctx context.Context, every time.Duration) {
	if every <= 0 {
		every = 5 * time.Minute
	}
	go func() {
		t := time.NewTicker(every)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				now := s.deps.now()
				n, err := s.deps.Store.SweepTimedOut(ctx, now.Add(-s.deps.runTimeout()), now)
				if err != nil {
					s.deps.log().Error("emi: sweep failed", "err", err)
					continue
				}
				if n > 0 {
					s.deps.log().Warn("emi: runs timed out", "count", n)
				}
			}
		}
	}()
}

// ---- small http helpers ----

func newID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeErr(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// writeStoreErr maps store errors onto status codes in one place, so no handler has to
// remember that a missing row is a 404 and a lost race is a 409.
func writeStoreErr(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, ErrNotFound):
		writeErr(w, http.StatusNotFound, "not found")
	case errors.Is(err, ErrConflict):
		writeErr(w, http.StatusConflict, "conflicting state")
	default:
		writeErr(w, http.StatusInternalServerError, "internal error")
	}
}

func decodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	// 1 MiB is plenty for any control-plane body. Board data goes to object storage, not
	// through here, and this cap is part of how that stays true.
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		writeErr(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return false
	}
	return true
}

func queryInt(r *http.Request, name string, def, max int) int {
	v := r.URL.Query().Get(name)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil || n <= 0 {
		return def
	}
	if n > max {
		return max
	}
	return n
}
