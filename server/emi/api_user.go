package emi

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"
)

// User-facing endpoints. Every one of them checks that the caller's organisation owns the
// object being touched: an EMI project is somebody's unreleased PCB layout, and org
// scoping is the only thing standing between two customers.

func (s *Service) handleCreateProject(w http.ResponseWriter, r *http.Request) {
	u, ok := userFrom(r.Context())
	if !ok {
		writeErr(w, http.StatusUnauthorized, "not authenticated")
		return
	}
	var body struct {
		Name       string     `json:"name"`
		SourceKind SourceKind `json:"source_kind"`
	}
	if !decodeJSON(w, r, &body) {
		return
	}
	body.Name = strings.TrimSpace(body.Name)
	if body.Name == "" || len(body.Name) > 200 {
		writeErr(w, http.StatusBadRequest, "name is required (max 200 chars)")
		return
	}
	if body.SourceKind == "" {
		body.SourceKind = SourceKiCad
	}
	if !body.SourceKind.Valid() {
		writeErr(w, http.StatusBadRequest, `source_kind must be "kicad" or "gerber"`)
		return
	}

	p := &Project{
		ID: newID(), OrganizationID: u.OrganizationID, Name: body.Name,
		SourceKind: body.SourceKind, CreatedBy: u.UserID, CreatedAt: s.deps.now(),
	}
	if err := s.deps.Store.CreateProject(r.Context(), p); err != nil {
		writeStoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, p)
}

func (s *Service) handleListProjects(w http.ResponseWriter, r *http.Request) {
	u, ok := userFrom(r.Context())
	if !ok {
		writeErr(w, http.StatusUnauthorized, "not authenticated")
		return
	}
	ps, err := s.deps.Store.ListProjects(r.Context(), u.OrganizationID, queryInt(r, "limit", 50, 200))
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"projects": ps})
}

// project loads a project and checks org ownership in one step, so no handler can forget
// the second half.
func (s *Service) project(w http.ResponseWriter, r *http.Request) (*Project, bool) {
	u, ok := userFrom(r.Context())
	if !ok {
		writeErr(w, http.StatusUnauthorized, "not authenticated")
		return nil, false
	}
	p, err := s.deps.Store.GetProject(r.Context(), r.PathValue("project_id"))
	if err != nil {
		writeStoreErr(w, err)
		return nil, false
	}
	if p.OrganizationID != u.OrganizationID {
		writeErr(w, http.StatusNotFound, "not found")
		return nil, false
	}
	return p, true
}

// run loads a run and checks that the caller's org owns its project.
func (s *Service) run(w http.ResponseWriter, r *http.Request) (*Run, *Project, bool) {
	u, ok := userFrom(r.Context())
	if !ok {
		writeErr(w, http.StatusUnauthorized, "not authenticated")
		return nil, nil, false
	}
	run, err := s.deps.Store.GetRun(r.Context(), r.PathValue("run_id"))
	if err != nil {
		writeStoreErr(w, err)
		return nil, nil, false
	}
	p, err := s.deps.Store.GetProject(r.Context(), run.ProjectID)
	if err != nil {
		writeStoreErr(w, err)
		return nil, nil, false
	}
	if p.OrganizationID != u.OrganizationID {
		writeErr(w, http.StatusNotFound, "not found")
		return nil, nil, false
	}
	return run, p, true
}

func (s *Service) handleGetProject(w http.ResponseWriter, r *http.Request) {
	p, ok := s.project(w, r)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, p)
}

// handleCreateUpload gives the browser a presigned PUT so board files go straight to object
// storage. The server never sees the bytes.
func (s *Service) handleCreateUpload(w http.ResponseWriter, r *http.Request) {
	p, ok := s.project(w, r)
	if !ok {
		return
	}
	var body struct {
		Filename    string `json:"filename"`
		ContentType string `json:"content_type"`
		// SHA256 of the file the client is about to send, lowercase hex. Optional, but
		// sending it is what allows the upload to be skipped when we already hold those
		// bytes -- and a board file is tens of megabytes.
		SHA256 string `json:"sha256"`
	}
	if !decodeJSON(w, r, &body) {
		return
	}
	name, valid := sanitiseArtifactName(body.Filename)
	if !valid {
		writeErr(w, http.StatusBadRequest, "filename must be a simple relative name")
		return
	}
	ct := body.ContentType
	if ct == "" {
		ct = "application/octet-stream"
	}

	sha, err := normaliseSHA256(body.SHA256)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}

	// Content-addressed and scoped to the organisation, not the project: the same board
	// uploaded into two projects is one object, and a second upload of it is no upload at
	// all. Without a hash we fall back to a random key, which is simply the old behaviour.
	key := "uploads/" + p.OrganizationID + "/" + newID() + "/" + name
	if sha != "" {
		key = "uploads/" + p.OrganizationID + "/sha256/" + sha + "/" + name
		if size, _, sErr := s.deps.Blob.Stat(r.Context(), key); sErr == nil {
			writeJSON(w, http.StatusOK, map[string]any{
				"key": key, "already_uploaded": true,
				"size_bytes": size, "content_type": ct,
			})
			return
		}
	}

	url, err := s.deps.Blob.PresignPut(r.Context(), key, ct, presignTTL)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "failed to create upload url")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"upload_url": url, "method": "PUT", "key": key,
		"already_uploaded": false,
		"content_type":     ct, "expires_in": int(presignTTL.Seconds()),
	})
}

// normaliseSHA256 accepts an empty string or a 64-character hex digest, lowercased.
//
// It is strict because the digest becomes part of an object key: anything else here would
// be a client putting arbitrary text into the storage namespace.
func normaliseSHA256(in string) (string, error) {
	in = strings.ToLower(strings.TrimSpace(in))
	if in == "" {
		return "", nil
	}
	if len(in) != 64 {
		return "", errors.New("sha256 must be a 64-character hex digest")
	}
	for _, c := range in {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return "", errors.New("sha256 must be a 64-character hex digest")
		}
	}
	return in, nil
}

// handleCreateBoard records an upload and immediately queues the ingest run for it. One
// call, because a board the user cannot see is not useful and there is never a reason to
// upload without parsing.
func (s *Service) handleCreateBoard(w http.ResponseWriter, r *http.Request) {
	p, ok := s.project(w, r)
	if !ok {
		return
	}
	var body struct {
		InputKey string `json:"input_key"`
		SHA256   string `json:"sha256"`
	}
	if !decodeJSON(w, r, &body) {
		return
	}
	// Scoped to the organisation rather than the project, because an upload is now shared
	// across the projects of one organisation. It still stops a caller naming an upload
	// belonging to somebody else and having a worker parse it for them -- including by way of
	// "..", which a prefix check alone lets through (see objectkeys.go).
	if !uploadKeyAllowed(body.InputKey, p.OrganizationID) {
		writeErr(w, http.StatusBadRequest, "input_key must be an upload key for this organisation")
		return
	}
	size, _, err := s.deps.Blob.Stat(r.Context(), body.InputKey)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "no uploaded object at that key")
		return
	}
	sha, err := normaliseSHA256(body.SHA256)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}

	now := s.deps.now()
	b := &Board{
		ID: newID(), ProjectID: p.ID, InputKey: body.InputKey,
		ContentSHA256: sha, SizeBytes: size, CreatedAt: now,
	}
	if err := s.deps.Store.CreateBoard(r.Context(), b); err != nil {
		writeStoreErr(w, err)
		return
	}

	run := &Run{
		ID: newID(), ProjectID: p.ID, BoardID: b.ID,
		Kind: RunKindIngest, Status: StatusNew,
		CreatedAt: now, UpdatedAt: now,
	}
	if err := s.deps.Store.CreateRun(r.Context(), run); err != nil {
		writeStoreErr(w, err)
		return
	}
	b.IngestRunID = run.ID
	s.hub.DispatchRun(r.Context(), p.OrganizationID, run)

	writeJSON(w, http.StatusCreated, map[string]any{"board": b, "run": run})
}

// handleLookupBoard answers "have I uploaded this file before?" before the browser sends
// a single byte of it.
//
// The client hashes the file it just picked and asks. A hit means the board is already
// here, parsed, with its findings and any solves attached -- so the answer is a link to
// that project rather than a fresh upload of tens of megabytes the server already holds.
func (s *Service) handleLookupBoard(w http.ResponseWriter, r *http.Request) {
	u, ok := userFrom(r.Context())
	if !ok {
		writeErr(w, http.StatusUnauthorized, "not authenticated")
		return
	}
	sha, err := normaliseSHA256(r.URL.Query().Get("sha256"))
	if err != nil || sha == "" {
		writeErr(w, http.StatusBadRequest, "sha256 query parameter is required (64 hex characters)")
		return
	}

	board, project, err := s.deps.Store.FindBoardByContent(r.Context(), u.OrganizationID, sha)
	if errors.Is(err, ErrNotFound) {
		writeJSON(w, http.StatusOK, map[string]any{"found": false})
		return
	}
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"found": true, "board": board, "project": project,
		// Whether it is usable yet: a board whose ingest never finished has no geometry
		// to show, and the client should offer to re-run rather than link to a blank page.
		"parsed": board.BoardKey != "",
	})
}

// handleWhoami reports the identity the host's middleware resolved for this request.
//
// The page is public, so "who am I" has a real answer either way and the UI needs it: a
// signed-in user's boards are filed under their organisation and will still be there
// tomorrow, an anonymous visitor's are filed in a shared one. Telling them which is
// happening is the difference between a feature and a surprise.
func (s *Service) handleWhoami(w http.ResponseWriter, r *http.Request) {
	u, ok := userFrom(r.Context())
	if !ok {
		writeErr(w, http.StatusUnauthorized, "not authenticated")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"user_id":         u.UserID,
		"organization_id": u.OrganizationID,
		"login":           u.Login,
		"anonymous":       u.Anonymous,
	})
}

// handleRenameProject changes a project's name. The name is the only thing about a project a
// user can edit: everything else is a fact about the board they uploaded.
func (s *Service) handleRenameProject(w http.ResponseWriter, r *http.Request) {
	p, ok := s.project(w, r)
	if !ok {
		return
	}
	var body struct {
		Name string `json:"name"`
	}
	if !decodeJSON(w, r, &body) {
		return
	}
	name := strings.TrimSpace(body.Name)
	if name == "" || len(name) > 200 {
		writeErr(w, http.StatusBadRequest, "name must be between 1 and 200 characters")
		return
	}
	if err := s.deps.Store.RenameProject(r.Context(), p.ID, name); err != nil {
		writeStoreErr(w, err)
		return
	}
	p.Name = name
	writeJSON(w, http.StatusOK, p)
}

// handleDeleteProject removes a project, its boards, its runs and the objects they name.
//
// The objects matter as much as the rows. A board file is somebody's unreleased layout, and a
// delete that left it in storage would be a lie -- which is why this collects the keys first,
// deletes the rows, and then removes what nothing else refers to. An upload is
// content-addressed, so the same bytes can belong to two projects; those are kept.
func (s *Service) handleDeleteProject(w http.ResponseWriter, r *http.Request) {
	p, ok := s.project(w, r)
	if !ok {
		return
	}

	runs, err := s.deps.Store.ListRuns(r.Context(), p.ID, 1000)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	boards, err := s.deps.Store.ListBoards(r.Context(), p.ID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	// Asked before the rows go, while the other projects' boards are still comparable.
	var orphaned []string
	for _, b := range boards {
		for _, key := range []string{b.InputKey, b.BoardKey} {
			if key == "" {
				continue
			}
			n, err := s.deps.Store.CountBoardsSharingInput(r.Context(), key, p.ID)
			if err == nil && n == 0 {
				orphaned = append(orphaned, key)
			}
		}
	}

	if err := s.deps.Store.DeleteProject(r.Context(), p.ID); err != nil {
		writeStoreErr(w, err)
		return
	}

	// Best effort, and after the rows: a storage error must not leave a project the user was
	// told is gone, and an object nothing points at is recoverable from the logs.
	if del, canDelete := s.deps.Blob.(BlobDeleter); canDelete {
		for _, run := range runs {
			if err := del.DeletePrefix(r.Context(), "runs/"+run.ID+"/"); err != nil {
				s.deps.log().Warn("emi: could not delete a run's artifacts", "run", run.ID, "err", err)
			}
		}
		for _, key := range orphaned {
			if err := del.Delete(r.Context(), key); err != nil {
				s.deps.log().Warn("emi: could not delete an uploaded file", "key", key, "err", err)
			}
		}
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Service) handleListBoards(w http.ResponseWriter, r *http.Request) {
	p, ok := s.project(w, r)
	if !ok {
		return
	}
	bs, err := s.deps.Store.ListBoards(r.Context(), p.ID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"boards": bs})
}

func (s *Service) handleListDrivers(w http.ResponseWriter, r *http.Request) {
	p, ok := s.project(w, r)
	if !ok {
		return
	}
	ds, err := s.deps.Store.ListDrivers(r.Context(), p.ID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"drivers": ds})
}

func (s *Service) handleCreateDriver(w http.ResponseWriter, r *http.Request) {
	p, ok := s.project(w, r)
	if !ok {
		return
	}
	u, _ := userFrom(r.Context())

	// Read the document as raw bytes rather than through decodeJSON. Two reasons: the cap
	// here is §9.2's 2 MB rather than the 1 MB control-plane default, and the row stores the
	// bytes that arrived -- re-encoding a driver would quietly reorder or reformat a document
	// that BenchPod and a CI diff both treat as a file.
	r.Body = http.MaxBytesReader(w, r.Body, maxDriverDocumentBytes+1)
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		writeErr(w, http.StatusBadRequest, "could not read the driver document: "+err.Error())
		return
	}
	env, err := validateDriverDocument(raw)
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}

	now := s.deps.now()
	d := &Driver{
		ID: newID(), OrganizationID: p.OrganizationID, ProjectID: p.ID,
		CreatedBy: u.UserID, Name: env.Name, Role: env.Role, Kind: env.Kind,
		Document: json.RawMessage(raw), CreatedAt: now, UpdatedAt: now,
	}
	if err := s.deps.Store.CreateDriver(r.Context(), d); err != nil {
		writeStoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, d)
}

func (s *Service) handleDeleteDriver(w http.ResponseWriter, r *http.Request) {
	p, ok := s.project(w, r)
	if !ok {
		return
	}
	if err := s.deps.Store.DeleteDriver(r.Context(), p.ID, r.PathValue("driver_id")); err != nil {
		writeStoreErr(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s *Service) handleCreateRun(w http.ResponseWriter, r *http.Request) {
	p, ok := s.project(w, r)
	if !ok {
		return
	}
	var body struct {
		BoardID  string          `json:"board_id"`
		Kind     RunKind         `json:"kind"`
		Params   json.RawMessage `json:"params,omitempty"`
		Estimate *EstimateInput  `json:"estimate_input,omitempty"`
	}
	if !decodeJSON(w, r, &body) {
		return
	}
	if body.Kind == "" {
		body.Kind = RunKindSolve
	}
	if !body.Kind.Valid() {
		writeErr(w, http.StatusBadRequest,
			`kind must be "ingest", "solve", "transient", "cable" or "compliance"`)
		return
	}
	if !s.deps.Features.allows(body.Kind) {
		writeErr(w, http.StatusForbidden, s.deps.Features.refusal(body.Kind))
		return
	}
	if body.Kind == RunKindTransient {
		params, err := validateTransientParams(r.Context(), body.Params, p.OrganizationID, s.deps.Blob.Stat)
		if err != nil {
			writeErr(w, http.StatusBadRequest, err.Error())
			return
		}
		normalised, err := json.Marshal(params)
		if err != nil {
			writeErr(w, http.StatusInternalServerError, "failed to encode params")
			return
		}
		body.Params = normalised
	}
	if body.Kind == RunKindSolve {
		params, err := s.attachComponents(r, body.Params)
		if err != nil {
			writeErr(w, http.StatusBadRequest, err.Error())
			return
		}
		body.Params = params
	}
	if body.BoardID == "" {
		writeErr(w, http.StatusBadRequest, "board_id is required")
		return
	}
	board, err := s.deps.Store.GetBoard(r.Context(), body.BoardID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	if board.ProjectID != p.ID {
		writeErr(w, http.StatusNotFound, "not found")
		return
	}

	now := s.deps.now()
	run := &Run{
		ID: newID(), ProjectID: p.ID, BoardID: board.ID,
		Kind: body.Kind, Status: StatusNew, Params: body.Params,
		CreatedAt: now, UpdatedAt: now,
	}

	// Admission control. Rejecting an impossible run at submit is the whole point of
	// capability advertisement: the alternative is a user waiting six hours to discover
	// that no worker was ever going to finish it.
	if body.Estimate != nil {
		est, err := body.Estimate.Estimate()
		if err != nil {
			writeErr(w, http.StatusBadRequest, "invalid estimate input: "+err.Error())
			return
		}
		run.Estimate = &est

		if body.Kind == RunKindSolve {
			okAccept, bestMax, online := s.hub.CanAccept(p.OrganizationID, body.Kind, est.Cells)
			if online > 0 && !okAccept {
				writeJSON(w, http.StatusUnprocessableEntity, map[string]any{
					"error":             "no connected worker can run a job this size",
					"estimated_cells":   est.Cells,
					"largest_max_cells": bestMax,
					"workers_online":    online,
					"hint":              "shrink the region of interest, coarsen the mesh, or start a larger worker",
				})
				return
			}
		}
	}

	if err := s.deps.Store.CreateRun(r.Context(), run); err != nil {
		writeStoreErr(w, err)
		return
	}
	s.hub.DispatchRun(r.Context(), p.OrganizationID, run)
	writeJSON(w, http.StatusCreated, run)
}

func (s *Service) handleListRuns(w http.ResponseWriter, r *http.Request) {
	p, ok := s.project(w, r)
	if !ok {
		return
	}
	runs, err := s.deps.Store.ListRuns(r.Context(), p.ID, queryInt(r, "limit", 50, 200))
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"runs": runs})
}

func (s *Service) handleGetRun(w http.ResponseWriter, r *http.Request) {
	run, _, ok := s.run(w, r)
	if !ok {
		return
	}
	writeJSON(w, http.StatusOK, run)
}

func (s *Service) handleStopRun(w http.ResponseWriter, r *http.Request) {
	run, _, ok := s.run(w, r)
	if !ok {
		return
	}
	if run.Status.Terminal() {
		writeErr(w, http.StatusConflict, "run has already finished")
		return
	}
	if err := s.deps.Store.RequestStop(r.Context(), run.ID, s.deps.now()); err != nil {
		writeStoreErr(w, err)
		return
	}
	// Best effort. The run is already marked stopping in the database, so a worker that
	// misses this push still finds out at its next progress post.
	delivered := false
	if run.OwnerAPIKeyKid != "" {
		delivered = s.hub.PushStop(r.Context(), run.OwnerAPIKeyKid, run.ID)
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": StatusStopping, "pushed": delivered})
}

func (s *Service) handleRetryRun(w http.ResponseWriter, r *http.Request) {
	run, p, ok := s.run(w, r)
	if !ok {
		return
	}
	if run.Status != StatusFailed && run.Status != StatusTimedOut {
		writeErr(w, http.StatusConflict, "only failed or timed-out runs can be retried")
		return
	}
	if !s.deps.Features.allows(run.Kind) {
		writeErr(w, http.StatusForbidden, s.deps.Features.refusal(run.Kind))
		return
	}
	if err := s.deps.Store.RetryRun(r.Context(), run.ID, s.deps.now()); err != nil {
		writeStoreErr(w, err)
		return
	}
	fresh, err := s.deps.Store.GetRun(r.Context(), run.ID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	s.hub.DispatchRun(r.Context(), p.OrganizationID, fresh)
	writeJSON(w, http.StatusOK, fresh)
}

func (s *Service) handleListArtifacts(w http.ResponseWriter, r *http.Request) {
	run, _, ok := s.run(w, r)
	if !ok {
		return
	}
	as, err := s.deps.Store.ListArtifacts(r.Context(), run.ID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"artifacts": as})
}

// handleGetArtifactURL returns a presigned GET rather than the bytes, so the browser
// fetches exactly the one frequency grid it is displaying and the droplet stays out of it.
func (s *Service) handleGetArtifactURL(w http.ResponseWriter, r *http.Request) {
	run, _, ok := s.run(w, r)
	if !ok {
		return
	}
	a, err := s.deps.Store.GetArtifactByName(r.Context(), run.ID, r.PathValue("name"))
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	url, err := s.deps.Blob.PresignGet(r.Context(), a.Key, presignTTL)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, "failed to create download url")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"url": url, "name": a.Name, "content_type": a.ContentType,
		"size_bytes": a.SizeBytes, "expires_in": int(presignTTL.Seconds()),
	})
}

// handleListWorkers shows what the org currently has online, merged with what has ever
// registered. The UI needs the online set to tell a user whether their run will actually
// start or just sit there.
func (s *Service) handleListWorkers(w http.ResponseWriter, r *http.Request) {
	u, ok := userFrom(r.Context())
	if !ok {
		writeErr(w, http.StatusUnauthorized, "not authenticated")
		return
	}
	known, err := s.deps.Store.ListWorkers(r.Context(), u.OrganizationID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	online := map[string]bool{}
	for _, o := range s.hub.OnlineWorkers(u.OrganizationID) {
		online[o.APIKeyKid] = true
	}
	out := make([]map[string]any, 0, len(known))
	for _, k := range known {
		out = append(out, map[string]any{
			"name": k.Name, "capabilities": k.Capabilities,
			"registered_at": k.RegisteredAt, "last_seen_at": k.LastSeenAt,
			"online": online[k.APIKeyKid],
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"workers": out, "online_count": len(online)})
}

// handleEstimate is the authoritative cost model, for a UI that wants to confirm its own
// live client-side number before actually submitting.
func (s *Service) handleEstimate(w http.ResponseWriter, r *http.Request) {
	if _, ok := userFrom(r.Context()); !ok {
		writeErr(w, http.StatusUnauthorized, "not authenticated")
		return
	}
	var in EstimateInput
	if !decodeJSON(w, r, &in) {
		return
	}
	est, err := in.Estimate()
	if err != nil {
		writeErr(w, http.StatusBadRequest, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"estimate":  est,
		"eta_human": (time.Duration(est.ETASeconds) * time.Second).Round(time.Minute).String(),
		"ram_gb":    float64(est.RAMBytes) / 1e9,
	})
}
