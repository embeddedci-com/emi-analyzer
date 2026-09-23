package emi

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// complianceStore holds two projects: p1, whose board b1 has a finished solve with a far field,
// and p2, whose solve must never be reachable from a p1 compliance run.
type complianceStore struct {
	Store
	runs      map[string]*Run
	artifacts map[string]map[string]*Artifact
	drivers   map[string][]*Driver
}

func (s *complianceStore) GetRun(_ context.Context, id string) (*Run, error) {
	if r, ok := s.runs[id]; ok {
		return r, nil
	}
	return nil, ErrNotFound
}

func (s *complianceStore) GetArtifactByName(_ context.Context, runID, name string) (*Artifact, error) {
	if a, ok := s.artifacts[runID][name]; ok {
		return a, nil
	}
	return nil, ErrNotFound
}

func (s *complianceStore) ListDrivers(_ context.Context, projectID string) ([]*Driver, error) {
	return s.drivers[projectID], nil
}

// A Blob whose presigned URL names the key, so the test can see what was signed.
type keyBlob struct{ stubBlob }

func (keyBlob) PresignGet(_ context.Context, key string, _ time.Duration) (string, error) {
	return "signed:" + key, nil
}

func newComplianceService(t *testing.T) (*Service, *complianceStore) {
	t.Helper()
	art := func(run, name string) *Artifact {
		return &Artifact{ID: run + name, RunID: run, Name: name, Key: "runs/" + run + "/" + name}
	}
	store := &complianceStore{
		runs: map[string]*Run{
			"solve-1":   {ID: "solve-1", ProjectID: "p1", BoardID: "b1", Kind: RunKindSolve, Status: StatusDone},
			"solve-run": {ID: "solve-run", ProjectID: "p1", BoardID: "b1", Kind: RunKindSolve, Status: StatusInProgress},
			"solve-b2":  {ID: "solve-b2", ProjectID: "p1", BoardID: "b2", Kind: RunKindSolve, Status: StatusDone},
			"ingest-1":  {ID: "ingest-1", ProjectID: "p1", BoardID: "b1", Kind: RunKindIngest, Status: StatusDone},
			"solve-p2":  {ID: "solve-p2", ProjectID: "p2", BoardID: "b9", Kind: RunKindSolve, Status: StatusDone},
		},
		artifacts: map[string]map[string]*Artifact{
			"solve-1": {
				"manifest.json": art("solve-1", "manifest.json"),
				"farfield.json": art("solve-1", "farfield.json"),
				"ports.json":    art("solve-1", "ports.json"),
				// Never presigned: a compliance run has no use for the field maps.
				"nearfield/F_Cu/100000000.bin": art("solve-1", "nearfield/F_Cu/100000000.bin"),
			},
			"solve-p2": {"manifest.json": art("solve-p2", "manifest.json")},
		},
		drivers: map[string][]*Driver{
			"p1": {{ID: "drv-1", ProjectID: "p1", Document: json.RawMessage(`{"kind":"trapezoid"}`)}},
			"p2": {{ID: "drv-2", ProjectID: "p2", Document: json.RawMessage(`{"kind":"spectrum"}`)}},
		},
	}
	svc, err := New(Deps{
		Store: store, Keys: stubKeys{}, Blob: keyBlob{},
		TokenSecret: bytes.Repeat([]byte("k"), 32),
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return svc, store
}

func complianceRun(params string) *Run {
	return &Run{ID: "c1", ProjectID: "p1", BoardID: "b1", Kind: RunKindCompliance,
		Params: json.RawMessage(params)}
}

func TestComplianceInputsPresignTheSolvesResultsAndTheDriver(t *testing.T) {
	svc, _ := newComplianceService(t)
	out, err := svc.complianceInputs(context.Background(),
		complianceRun(`{"solve_run_id":"solve-1","driver_id":"drv-1"}`))
	if err != nil {
		t.Fatal(err)
	}
	solve := out["solve"].(map[string]any)
	urls := solve["artifacts"].(map[string]string)
	if urls["manifest.json"] != "signed:runs/solve-1/manifest.json" ||
		urls["farfield.json"] != "signed:runs/solve-1/farfield.json" {
		t.Fatalf("artifacts = %v", urls)
	}
	if len(urls) != 3 {
		t.Fatalf("only the compliance artifacts are signed, got %v", urls)
	}
	drivers := out["drivers"].([]map[string]any)
	if len(drivers) != 1 || drivers[0]["id"] != "drv-1" {
		t.Fatalf("drivers = %v", drivers)
	}
}

// The scope rule: another project's solve, another board's, an unfinished one and a run that is
// not a solve are all refused -- as a message the worker shows, never as a URL.
func TestComplianceInputsStayInsideTheProjectAndBoard(t *testing.T) {
	svc, _ := newComplianceService(t)
	for id, want := range map[string]string{
		"solve-p2":  "not in this project",
		"nope":      "not in this project",
		"solve-b2":  "different board",
		"solve-run": "not finished",
		"ingest-1":  "not a solve",
		"":          "No solve was chosen",
	} {
		out, err := svc.complianceInputs(context.Background(),
			complianceRun(`{"solve_run_id":"`+id+`"}`))
		if err != nil {
			t.Fatal(err)
		}
		solve := out["solve"].(map[string]any)
		if _, leaked := solve["artifacts"]; leaked {
			t.Errorf("%q: artifacts were signed", id)
		}
		if msg, _ := solve["error"].(string); !strings.Contains(msg, want) {
			t.Errorf("%q: error = %q, want it to mention %q", id, msg, want)
		}
	}
}

func TestComplianceInputsNeverHandOverAnotherProjectsDriver(t *testing.T) {
	svc, _ := newComplianceService(t)
	out, err := svc.complianceInputs(context.Background(),
		complianceRun(`{"solve_run_id":"solve-1","driver_id":"drv-2"}`))
	if err != nil {
		t.Fatal(err)
	}
	if d := out["drivers"].([]map[string]any); len(d) != 0 {
		t.Fatalf("a driver from another project was handed over: %v", d)
	}
}
