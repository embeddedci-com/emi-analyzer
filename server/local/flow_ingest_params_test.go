package local

import (
	"net/http"
	"testing"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

// Re-analysing with settings edited in the app sends them as the ingest run's params, and the
// worker reads them as its "run" settings layer. Nothing in between may drop them: a threshold
// changed in the app that silently did not reach the checks would read as the checks ignoring it.
func TestReanalyseCarriesSettingsToTheWorker(t *testing.T) {
	f := newFlow(t, emi.Features{})
	p := f.project("orgA")
	boardID, first := f.board("orgA", p)

	settings := map[string]any{"version": 1, "rules": map[string]any{
		"radiator": map[string]any{"enabled": false},
		"via-stub": map[string]any{"params": map[string]any{"resonance_margin": 6}},
	},
		// Net groups and suppressions ride in the same layer (the Checks view edits both).
		"groups":   []any{map[string]any{"match": "DQ*", "params": map[string]any{"byte_lane_ps": 4}}},
		"suppress": []any{map[string]any{"rule": "plane-gap", "net": "GND", "reason": "intentional"}},
	}
	code, out := f.user("orgA", "POST", "/emi/projects/"+p+"/runs", map[string]any{
		"board_id": boardID, "kind": "ingest", "params": map[string]any{"settings": settings},
	})
	if code != http.StatusCreated {
		t.Fatalf("reanalyse: %d %v", code, out)
	}
	runID := out["id"].(string)

	// The browser reads them back to show what the last analysis ran with.
	_, got := f.user("orgA", "GET", "/emi/runs/"+runID, nil)
	if !sameSettings(got["params"], settings) {
		t.Fatalf("run params: %v", got["params"])
	}

	code, list := f.agent(f.workerKey(""), "GET", "/emi-agent/runs", nil)
	if code != http.StatusOK {
		t.Fatalf("worker list: %d %v", code, list)
	}
	var seen bool
	for _, r := range list["runs"].([]any) {
		run := r.(map[string]any)
		switch run["id"] {
		case runID:
			seen = true
			if !sameSettings(run["params"], settings) {
				t.Fatalf("worker envelope params: %v", run["params"])
			}
		case first:
			// The upload's own ingest has no settings of its own, and says nothing about them.
			if _, ok := run["params"]; ok {
				t.Fatalf("first ingest grew params: %v", run["params"])
			}
		}
	}
	if !seen {
		t.Fatalf("worker did not see the re-analysis: %v", list)
	}
}

func sameSettings(params any, want map[string]any) bool {
	m, ok := params.(map[string]any)
	if !ok {
		return false
	}
	s, ok := m["settings"].(map[string]any)
	if !ok {
		return false
	}
	rules := s["rules"].(map[string]any)
	radiator := rules["radiator"].(map[string]any)
	stub := rules["via-stub"].(map[string]any)["params"].(map[string]any)
	groups, _ := s["groups"].([]any)
	suppress, _ := s["suppress"].([]any)
	if len(groups) != 1 || len(suppress) != 1 {
		return false
	}
	group := groups[0].(map[string]any)
	sup := suppress[0].(map[string]any)
	return radiator["enabled"] == false && stub["resonance_margin"] == float64(6) &&
		len(rules) == len(want["rules"].(map[string]any)) &&
		group["match"] == "DQ*" && group["params"].(map[string]any)["byte_lane_ps"] == float64(4) &&
		sup["reason"] == "intentional"
}
