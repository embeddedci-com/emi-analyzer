package emi

import (
	"context"
	"encoding/json"
	"errors"
)

// A compliance run is arithmetic on another run's results (§16, §17), so its worker needs that
// run's artifacts. It used to be handed nothing: the browser was meant to assemble the inputs,
// sent an empty list, and every estimate came back with nothing radiating.
//
// The worker now reads them itself, and this is the one place that decides which. The rule is
// narrow on purpose: the named solve must belong to the same project and the same board as the
// compliance run, and must have finished. A run token can therefore reach another run's
// results only within its own project, and a request cannot point the estimate at somebody
// else's solve or at a different board's.

// ComplianceParams is what a compliance run's params name. Everything else in them is the
// user's own declarations, which the worker reads directly.
type ComplianceParams struct {
	SolveRunID string `json:"solve_run_id"`
	DriverID   string `json:"driver_id"`
}

// complianceSolveArtifacts are the solve's results a compliance run reads. Nothing else is
// presigned: the field maps are hundreds of megabytes and the estimate never needs them.
var complianceSolveArtifacts = []string{
	"manifest.json", "ports.json", "farfield.json", "cable_ports.json", "cable_antenna.json",
}

// complianceInputs is what handleRunInputURL adds for a compliance run: presigned URLs for the
// solve's artifacts, and the attached driver's document.
func (s *Service) complianceInputs(ctx context.Context, run *Run) (map[string]any, error) {
	var p ComplianceParams
	if len(run.Params) > 0 {
		// Malformed params leave both fields empty, which the worker reports as gaps.
		_ = json.Unmarshal(run.Params, &p)
	}
	out := map[string]any{}

	solve, why, err := s.complianceSolve(ctx, run, p.SolveRunID)
	if err != nil {
		return nil, err
	}
	if solve == nil {
		out["solve"] = map[string]any{"error": why}
	} else {
		urls := map[string]string{}
		for _, name := range complianceSolveArtifacts {
			a, err := s.deps.Store.GetArtifactByName(ctx, solve.ID, name)
			if errors.Is(err, ErrNotFound) {
				continue
			}
			if err != nil {
				return nil, err
			}
			u, err := s.deps.Blob.PresignGet(ctx, a.Key, presignTTL)
			if err != nil {
				return nil, err
			}
			urls[name] = u
		}
		out["solve"] = map[string]any{"run_id": solve.ID, "artifacts": urls}
	}

	drivers := []map[string]any{}
	if p.DriverID != "" {
		list, err := s.deps.Store.ListDrivers(ctx, run.ProjectID)
		if err != nil {
			return nil, err
		}
		for _, d := range list {
			if d.ID == p.DriverID && d.ProjectID == run.ProjectID {
				drivers = append(drivers, map[string]any{"id": d.ID, "document": d.Document})
			}
		}
	}
	out["drivers"] = drivers
	return out, nil
}

// complianceSolve returns the solve a compliance run may read, or why it may not. The reasons
// are written for the user; a solve in another project reads exactly like a missing one, so
// the message says nothing about runs the caller cannot see.
func (s *Service) complianceSolve(ctx context.Context, run *Run, id string) (*Run, string, error) {
	if id == "" {
		return nil, "No solve was chosen. Pick a finished solve on the Results tab.", nil
	}
	solve, err := s.deps.Store.GetRun(ctx, id)
	if errors.Is(err, ErrNotFound) {
		return nil, "That solve is not in this project.", nil
	}
	if err != nil {
		return nil, "", err
	}
	if solve.ProjectID != run.ProjectID {
		return nil, "That solve is not in this project.", nil
	}
	if solve.Kind != RunKindSolve {
		return nil, "That run is not a solve.", nil
	}
	if solve.BoardID != run.BoardID {
		return nil, "That solve is of a different board. Estimate against a solve of this one.", nil
	}
	if solve.Status != StatusDone {
		return nil, "That solve has not finished.", nil
	}
	return solve, "", nil
}
