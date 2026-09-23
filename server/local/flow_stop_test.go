package local

import (
	"net/http"
	"testing"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

// Stopping a run no worker has taken ends it, and says so, instead of leaving it "stopping"
// with nobody to finish it.
func TestStopQueuedRunEndsIt(t *testing.T) {
	f := newFlow(t, emi.Features{})
	p := f.project("orgA")
	_, runID := f.board("orgA", p)

	code, out := f.user("orgA", "POST", "/emi/runs/"+runID+"/stop", map[string]any{})
	if code != http.StatusOK || out["status"] != string(emi.StatusFailed) || out["pushed"] != false {
		t.Fatalf("stop: %d %v", code, out)
	}
	code, out = f.user("orgA", "GET", "/emi/runs/"+runID, nil)
	if code != http.StatusOK || out["status"] != string(emi.StatusFailed) || out["error"] != emi.StoppedBeforeStartError {
		t.Fatalf("run after stop: %d %v", code, out)
	}
	// A worker that saw the run before it was stopped cannot take it now.
	key := f.workerKey("")
	if code, out := f.agent(key, "POST", "/emi-agent/runs/"+runID+"/token", map[string]any{}); code != http.StatusConflict {
		t.Fatalf("mint after stop: %d %v, want 409", code, out)
	}
	if code, out := f.user("orgA", "POST", "/emi/runs/"+runID+"/retry", map[string]any{}); code != http.StatusOK ||
		out["status"] != string(emi.StatusRetryPending) {
		t.Fatalf("retry after stop: %d %v", code, out)
	}
}

// A run a worker holds is asked to stop, and stays the worker's to finish.
func TestStopRunningRunWaitsForItsWorker(t *testing.T) {
	f := newFlow(t, emi.Features{})
	p := f.project("orgA")
	_, runID := f.board("orgA", p)
	tok := f.mint(f.workerKey(""), runID)

	code, out := f.user("orgA", "POST", "/emi/runs/"+runID+"/stop", map[string]any{})
	if code != http.StatusOK || out["status"] != string(emi.StatusStopping) {
		t.Fatalf("stop: %d %v", code, out)
	}
	code, out = f.agent(tok, "POST", "/emi-agent/runs/"+runID+"/complete",
		map[string]any{"status": "failed", "error": "stopped on request"})
	if code != http.StatusOK {
		t.Fatalf("complete after stop: %d %v", code, out)
	}
}
