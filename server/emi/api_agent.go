package emi

import (
	"encoding/json"
	"net/http"
	"path"
	"strings"
	"time"
)

// Worker-facing endpoints. The flow is that of a CI agent:
//
//	dial in (ws) or poll  ->  mint run token  ->  claim  ->  progress*  ->  artifacts*  ->  complete
//
// Everything after the mint is authenticated by the short-lived run token, never by the
// long-lived worker key.

// presignTTL is short on purpose. A worker fetches its input once at the start of a run and
// uploads results at the end; it re-asks for a URL rather than holding one for hours.
const presignTTL = 15 * time.Minute

func (s *Service) handleWorkerRegister(w http.ResponseWriter, r *http.Request) {
	key, _ := agentFrom(r.Context())

	var body struct {
		Name         string       `json:"name"`
		Capabilities Capabilities `json:"capabilities"`
	}
	if !decodeJSON(w, r, &body) {
		return
	}
	if body.Capabilities.Cores <= 0 || body.Capabilities.RAMGB <= 0 {
		writeErr(w, http.StatusBadRequest, "capabilities must declare cores and ram_gb")
		return
	}
	for _, k := range body.Capabilities.Kinds {
		if !k.Valid() {
			writeErr(w, http.StatusBadRequest, "unknown run kind in capabilities.kinds: "+string(k))
			return
		}
	}

	now := s.deps.now()
	wk := &Worker{
		ID:             newID(),
		APIKeyKid:      key.Kid,
		OrganizationID: key.OrganizationID,
		Name:           body.Name,
		Status:         "active",
		Capabilities:   body.Capabilities,
		RegisteredAt:   now,
		LastSeenAt:     &now,
	}
	if err := s.deps.Store.UpsertWorker(r.Context(), wk); err != nil {
		writeStoreErr(w, err)
		return
	}
	scope := key.OrganizationID
	if scope == "" {
		scope = "all organisations"
	}
	s.deps.log().Info("emi: worker registered",
		"name", wk.Name, "kid", key.Kid, "serves", scope,
		"cores", wk.Capabilities.Cores, "max_cells", wk.Capabilities.MaxCells)

	writeJSON(w, http.StatusOK, map[string]any{
		"worker_id": wk.ID,
		"name":      wk.Name,
		// Empty means a shared worker: it takes runs from every organisation. Echoed so a
		// worker can log what it is actually serving rather than what was intended.
		"org_id": key.OrganizationID,
		"shared": key.OrganizationID == "",
		// Echoed back so a worker can log what the server believes about it. A mismatch
		// here has been the first symptom of every capability bug worth having.
		"capabilities": wk.Capabilities,
	})
}

func (s *Service) handleWorkerDeregister(w http.ResponseWriter, r *http.Request) {
	key, _ := agentFrom(r.Context())
	if err := s.deps.Store.DeregisterWorker(r.Context(), key.Kid, s.deps.now()); err != nil {
		writeStoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "deregistered"})
}

// handleWorkerListRuns is the REST fallback for the WebSocket push. Push is an optimisation;
// polling is the guarantee.
func (s *Service) handleWorkerListRuns(w http.ResponseWriter, r *http.Request) {
	key, _ := agentFrom(r.Context())
	runs, err := s.deps.Store.ListClaimableRuns(r.Context(), key.OrganizationID, queryInt(r, "limit", 25, 100))
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	_ = s.deps.Store.TouchWorker(r.Context(), key.Kid, s.deps.now())

	out := make([]map[string]any, 0, len(runs))
	for _, run := range runs {
		// A run queued while a feature was on is not handed out after it is switched off.
		if !s.deps.Features.allows(run.Kind) {
			continue
		}
		out = append(out, runEnvelope(run))
	}
	writeJSON(w, http.StatusOK, map[string]any{"runs": out})
}

// handleMintRunToken issues the short-lived run token and takes ownership of the run.
//
// Minting is what assigns ownership: it records the key id and the token's jti on the run.
// A second worker that mints for the same run after a retry gets a new jti, which
// invalidates the first worker's token — that is how a retry takes a run away from a hung
// worker without needing to reach that worker.
func (s *Service) handleMintRunToken(w http.ResponseWriter, r *http.Request) {
	key, _ := agentFrom(r.Context())
	runID := r.PathValue("run_id")

	run, err := s.deps.Store.GetRun(r.Context(), runID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	if !s.deps.Features.allows(run.Kind) {
		writeErr(w, http.StatusForbidden, s.deps.Features.refusal(run.Kind))
		return
	}
	proj, err := s.deps.Store.GetProject(r.Context(), run.ProjectID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	// A key naming no organisation is a shared worker and may take anyone's run; a scoped
	// key may take only its own organisation's.
	if key.OrganizationID != "" && proj.OrganizationID != key.OrganizationID {
		// 404 rather than 403: a worker scoped to another org should not learn that this
		// run id exists at all.
		writeErr(w, http.StatusNotFound, "not found")
		return
	}
	if run.Status.Terminal() {
		writeErr(w, http.StatusConflict, "run has already finished")
		return
	}
	// Minting again for a run this key already holds is a refresh, not a second claim.
	if heldBy(run, key.Kid) {
		s.writeRunToken(w, run.ID, proj.OrganizationID, run.JTIKey)
		return
	}
	if run.OwnerAPIKeyKid != "" && run.OwnerAPIKeyKid != key.Kid && !run.Status.Claimable() {
		writeErr(w, http.StatusConflict, "run is owned by another worker")
		return
	}

	// ClaimRun is the atomic step: of two workers minting for one run, exactly one changes
	// the row, and only that one is handed a token. The jti is chosen first because the row
	// records it, and a token is only worth anything once it matches the row.
	jti := newID()
	if _, err := s.deps.Store.ClaimRun(r.Context(), run.ID, key.Kid, jti, s.deps.now()); err != nil {
		writeStoreErr(w, err)
		return
	}
	s.writeRunToken(w, run.ID, proj.OrganizationID, jti)
}

// handleRefreshRunToken hands the worker that holds a run a fresh token for it.
//
// A run token lives RunTokenTTL and a solve can take longer. Minting again used to be the
// answer, and it never worked: ClaimRun refuses a run that is already in progress, and the
// token that came back carried a new jti the row did not have, so every call made with it was
// answered 409 as if the run had been reassigned.
//
// The new token carries the jti the run already has, and only its expiry is new. So the old
// token keeps working until it expires on its own: a progress post already in flight when the
// worker swaps tokens is not refused, which rotating the jti would do, and the worker would
// read that 409 as "reassigned" and abandon a solve hours in. Taking a run away from a worker
// is still what changes the jti (a retry clears it, the next claim sets a new one), and a
// refresh never succeeds for a run this key does not hold at that moment.
//
// Authenticated by the worker key, not the run token, so a worker whose token has already
// lapsed can still recover the run it is working on.
func (s *Service) handleRefreshRunToken(w http.ResponseWriter, r *http.Request) {
	key, _ := agentFrom(r.Context())
	run, err := s.deps.Store.GetRun(r.Context(), r.PathValue("run_id"))
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	proj, err := s.deps.Store.GetProject(r.Context(), run.ProjectID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	if key.OrganizationID != "" && proj.OrganizationID != key.OrganizationID {
		writeErr(w, http.StatusNotFound, "not found")
		return
	}
	if run.Status.Terminal() {
		writeErr(w, http.StatusConflict, "run has already finished")
		return
	}
	if !heldBy(run, key.Kid) {
		writeErr(w, http.StatusConflict, "run is not held by this worker")
		return
	}
	s.writeRunToken(w, run.ID, proj.OrganizationID, run.JTIKey)
}

// heldBy reports whether the worker key kid currently holds run: it claimed it, nothing has
// taken it away since, and the run is still being worked on.
func heldBy(run *Run, kid string) bool {
	return kid != "" && run.OwnerAPIKeyKid == kid && run.JTIKey != "" &&
		(run.Status == StatusInProgress || run.Status == StatusStopping)
}

// writeRunToken signs a run token and writes the response both mint and refresh return.
func (s *Service) writeRunToken(w http.ResponseWriter, runID, orgID, jti string) {
	tok, exp, err := s.mintRunToken(runID, orgID, jti)
	if err != nil {
		s.deps.log().Error("emi: signing a run token failed", "err", err)
		writeErr(w, http.StatusInternalServerError, "failed to mint run token")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"token":      tok,
		"run_id":     runID,
		"jti":        jti,
		"expires_at": exp.UTC().Format(time.RFC3339),
		"expires_in": int(exp.Sub(s.deps.now()).Seconds()),
	})
}

// handleClaimRun is a no-op confirmation in the current design, because minting already
// claimed. It exists so the worker's state machine has an explicit "I have started" step
// and so a future change can move claiming out of the mint without changing the worker.
func (s *Service) handleClaimRun(w http.ResponseWriter, r *http.Request) {
	runID, _ := runIDFrom(r.Context())
	run, err := s.deps.Store.GetRun(r.Context(), runID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"run": runEnvelope(run), "status": run.Status})
}

func (s *Service) handleRunProgress(w http.ResponseWriter, r *http.Request) {
	runID, _ := runIDFrom(r.Context())
	var p Progress
	if !decodeJSON(w, r, &p) {
		return
	}
	if p.Stage == "" {
		writeErr(w, http.StatusBadRequest, "progress must name a stage")
		return
	}
	if p.Pct < 0 {
		p.Pct = 0
	}
	if p.Pct > 100 {
		p.Pct = 100
	}
	now := s.deps.now()
	p.At = &now
	if err := s.deps.Store.UpdateRunProgress(r.Context(), runID, &p, now); err != nil {
		writeStoreErr(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// handleArtifactUploadInit hands back a presigned PUT.
//
// Artifact bytes never pass through the control plane. A result bundle is hundreds of
// megabytes, and a small server shared with other work cannot afford to proxy it. The bytes
// go worker -> object storage directly and the control plane only ever sees this small JSON.
func (s *Service) handleArtifactUploadInit(w http.ResponseWriter, r *http.Request) {
	runID, _ := runIDFrom(r.Context())

	var body struct {
		Name        string `json:"name"`
		ContentType string `json:"content_type"`
	}
	if !decodeJSON(w, r, &body) {
		return
	}
	name, ok := sanitiseArtifactName(body.Name)
	if !ok {
		writeErr(w, http.StatusBadRequest, "artifact name must be a relative path without .. segments")
		return
	}
	ct := body.ContentType
	if ct == "" {
		ct = "application/octet-stream"
	}

	key := artifactKey(runID, name)
	url, err := s.deps.Blob.PresignPut(r.Context(), key, ct, presignTTL)
	if err != nil {
		s.deps.log().Error("emi: presign put failed", "err", err, "key", key)
		writeErr(w, http.StatusInternalServerError, "failed to create upload url")
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"upload_url":   url,
		"method":       "PUT",
		"key":          key,
		"content_type": ct,
		"expires_in":   int(presignTTL.Seconds()),
	})
}

// handleRunInputURL gives the worker a presigned GET for the board bundle it needs.
func (s *Service) handleRunInputURL(w http.ResponseWriter, r *http.Request) {
	runID, _ := runIDFrom(r.Context())
	run, err := s.deps.Store.GetRun(r.Context(), runID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	if run.BoardID == "" {
		writeErr(w, http.StatusBadRequest, "run has no board")
		return
	}
	board, err := s.deps.Store.GetBoard(r.Context(), run.BoardID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}

	out := map[string]any{"board_id": board.ID}
	if board.InputKey != "" {
		u, err := s.deps.Blob.PresignGet(r.Context(), board.InputKey, presignTTL)
		if err != nil {
			writeErr(w, http.StatusInternalServerError, "failed to create download url")
			return
		}
		out["input_url"] = u
		out["input_key"] = board.InputKey
	}
	// A solve run also needs the normalised board produced by the earlier ingest run.
	if board.BoardKey != "" {
		u, err := s.deps.Blob.PresignGet(r.Context(), board.BoardKey, presignTTL)
		if err != nil {
			writeErr(w, http.StatusInternalServerError, "failed to create download url")
			return
		}
		out["board_url"] = u
		out["board_key"] = board.BoardKey
	}
	// Vendor SPICE models a transient run uses. The keys were checked against the organisation
	// when the run was created; they are checked again here, against the project the run belongs
	// to, because this is the moment a worker is actually handed something to download.
	if run.Kind == RunKindTransient && len(run.Params) > 0 {
		var tp TransientParams
		if err := json.Unmarshal(run.Params, &tp); err == nil && len(tp.Models) > 0 {
			project, err := s.deps.Store.GetProject(r.Context(), run.ProjectID)
			if err != nil {
				writeStoreErr(w, err)
				return
			}
			urls := make([]map[string]string, 0, len(tp.Models))
			for _, m := range tp.Models {
				if !modelKeyAllowed(m.Key, project.OrganizationID) {
					continue
				}
				u, err := s.deps.Blob.PresignGet(r.Context(), m.Key, presignTTL)
				if err != nil {
					writeErr(w, http.StatusInternalServerError, "failed to create download url")
					return
				}
				urls = append(urls, map[string]string{"key": m.Key, "url": u})
			}
			out["model_urls"] = urls
		}
	}
	out["expires_in"] = int(presignTTL.Seconds())
	writeJSON(w, http.StatusOK, out)
}

func (s *Service) handleCompleteRun(w http.ResponseWriter, r *http.Request) {
	runID, _ := runIDFrom(r.Context())

	var body struct {
		Status    RunStatus       `json:"status"`
		Error     string          `json:"error,omitempty"`
		Summary   json.RawMessage `json:"summary,omitempty"`
		Estimate  *Estimate       `json:"estimate,omitempty"`
		Artifacts []struct {
			Name        string `json:"name"`
			Key         string `json:"key"`
			ContentType string `json:"content_type"`
			SizeBytes   int64  `json:"size_bytes"`
		} `json:"artifacts,omitempty"`
		// Set by an ingest run once it has parsed the upload.
		Board *struct {
			BoardKey   string          `json:"board_key"`
			LayerCount int             `json:"layer_count"`
			NetCount   int             `json:"net_count"`
			OutlineMM  json.RawMessage `json:"outline_mm,omitempty"`
			Stackup    json.RawMessage `json:"stackup,omitempty"`
		} `json:"board,omitempty"`
	}
	if !decodeJSON(w, r, &body) {
		return
	}
	if body.Status != StatusDone && body.Status != StatusFailed {
		writeErr(w, http.StatusBadRequest, `status must be "done" or "failed"`)
		return
	}

	run, err := s.deps.Store.GetRun(r.Context(), runID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	now := s.deps.now()

	// The parsed board's key is presigned for every later solve and for the viewer, so like an
	// artifact key it must point into this run's own prefix. Checked before anything is written.
	if body.Board != nil && body.Board.BoardKey != "" && !runKeyAllowed(body.Board.BoardKey, runID) {
		writeErr(w, http.StatusBadRequest, "board_key must be under this run")
		return
	}

	// Names and keys are all checked before any row is written, so a bad entry late in the
	// list cannot leave the ones before it recorded.
	names := make([]string, len(body.Artifacts))
	keys := make([]string, len(body.Artifacts))
	for i, a := range body.Artifacts {
		name, ok := sanitiseArtifactName(a.Name)
		if !ok {
			writeErr(w, http.StatusBadRequest, "invalid artifact name: "+a.Name)
			return
		}
		key := a.Key
		if key == "" {
			key = artifactKey(runID, name)
		}
		// The key is presigned for any browser that can see this run, so a worker must not be
		// able to point it at another run's results, or at somebody's upload.
		if !runKeyAllowed(key, runID) {
			writeErr(w, http.StatusBadRequest, "artifact key must be under this run: "+name)
			return
		}
		names[i], keys[i] = name, key
	}

	// Register artifacts before flipping the run to done, so a client that reacts to the
	// status change never sees a finished run with a half-populated artifact list.
	for i, a := range body.Artifacts {
		name, key := names[i], keys[i]
		size, ct := a.SizeBytes, a.ContentType
		// Trust but verify: ask storage what actually landed. A worker that crashed
		// mid-upload should not be able to record a result that is not there.
		if realSize, realCT, sErr := s.deps.Blob.Stat(r.Context(), key); sErr == nil {
			size = realSize
			if realCT != "" {
				ct = realCT
			}
		} else if body.Status == StatusDone {
			writeErr(w, http.StatusBadRequest, "artifact was not uploaded: "+name)
			return
		}
		if ct == "" {
			ct = "application/octet-stream"
		}
		if err := s.deps.Store.CreateArtifact(r.Context(), &Artifact{
			ID: newID(), RunID: runID, Name: name, Key: key,
			ContentType: ct, SizeBytes: size, CreatedAt: now,
		}); err != nil {
			writeStoreErr(w, err)
			return
		}
	}

	if body.Estimate != nil {
		if err := s.deps.Store.SetRunEstimate(r.Context(), runID, body.Estimate); err != nil {
			writeStoreErr(w, err)
			return
		}
	}

	if body.Board != nil && run.BoardID != "" {
		if err := s.deps.Store.UpdateBoardParsed(r.Context(), run.BoardID,
			body.Board.BoardKey, body.Board.LayerCount, body.Board.NetCount,
			body.Board.OutlineMM, body.Board.Stackup); err != nil {
			writeStoreErr(w, err)
			return
		}
	}

	if err := s.deps.Store.CompleteRun(r.Context(), runID, body.Status, body.Summary, body.Error, now); err != nil {
		writeStoreErr(w, err)
		return
	}
	s.deps.log().Info("emi: run finished", "run", runID, "kind", run.Kind, "status", body.Status)
	writeJSON(w, http.StatusOK, map[string]any{"run_id": runID, "status": body.Status})
}

// artifactKey is the object-storage layout from the design doc: runs/<run_id>/<name>.
func artifactKey(runID, name string) string {
	return "runs/" + runID + "/" + name
}

// sanitiseArtifactName keeps names to safe relative paths. Artifact names come from the
// worker and end up as object keys, so "../" here would let a worker write outside its own
// run's prefix.
func sanitiseArtifactName(name string) (string, bool) {
	name = strings.TrimSpace(name)
	if name == "" || len(name) > 200 {
		return "", false
	}
	if strings.HasPrefix(name, "/") || strings.Contains(name, "\\") {
		return "", false
	}
	clean := path.Clean(name)
	if clean != name || clean == "." || strings.HasPrefix(clean, "..") {
		return "", false
	}
	for _, seg := range strings.Split(clean, "/") {
		if seg == "" || seg == "." || seg == ".." {
			return "", false
		}
	}
	return clean, true
}
