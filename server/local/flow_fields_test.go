package local

import (
	"net/http"
	"testing"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

// Which worker key holds a run, and the jti its token must carry, stay on the server. They
// used to be in every Run the browser was sent.
func TestRunJSONLeavesOwnershipOut(t *testing.T) {
	f := newFlow(t, emi.Features{})
	p := f.project("orgA")
	_, runID := f.board("orgA", p)
	f.mint(f.workerKey(""), runID)

	check := func(what string, run map[string]any) {
		t.Helper()
		if run["id"] != runID {
			t.Fatalf("%s: not the run under test: %v", what, run)
		}
		for _, field := range []string{"owner_api_key_kid", "jti_key"} {
			if _, ok := run[field]; ok {
				t.Errorf("%s carries %s", what, field)
			}
		}
	}

	code, out := f.user("orgA", "GET", "/emi/runs/"+runID, nil)
	if code != http.StatusOK || out["status"] != string(emi.StatusInProgress) {
		t.Fatalf("get run: %d %v", code, out)
	}
	check("GET run", out)

	code, out = f.user("orgA", "GET", "/emi/projects/"+p+"/runs", nil)
	if code != http.StatusOK {
		t.Fatalf("list runs: %d %v", code, out)
	}
	check("run list", out["runs"].([]any)[0].(map[string]any))
}
