package local

import (
	"context"
	"net/http"
	"testing"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

// version records an upload at key as a new board of the project, finishes its ingest with a
// board.json of its own, and returns the board and run ids.
func (f *flow) version(org, projectID, key string) (boardID, runID string) {
	f.t.Helper()
	f.put(key, []byte("(kicad_pcb "+key+")"))
	code, out := f.user(org, "POST", "/emi/projects/"+projectID+"/boards", map[string]any{"input_key": key})
	if code != http.StatusCreated {
		f.t.Fatalf("create board: %d %v", code, out)
	}
	boardID = out["board"].(map[string]any)["id"].(string)
	runID = out["run"].(map[string]any)["id"].(string)

	tok := f.mint(f.workerKey(""), runID)
	own := "runs/" + runID + "/board.json"
	f.put(own, []byte(`{}`))
	if code, out := f.agent(tok, "POST", "/emi-agent/runs/"+runID+"/complete", map[string]any{
		"status":    "done",
		"artifacts": []map[string]any{{"name": "board.json", "key": own}},
		"board":     map[string]any{"board_key": own, "layer_count": 2},
	}); code != http.StatusOK {
		f.t.Fatalf("complete: %d %v", code, out)
	}
	// Versions are ordered by when they were uploaded.
	f.advance(time.Second)
	return boardID, runID
}

func (f *flow) exists(key string) bool {
	_, _, err := f.blob.Stat(context.Background(), key)
	return err == nil
}

// Every upload into a project is a version of its board. Each keeps its own runs, and one
// version can be deleted without touching the others or a file another version still uses.
func TestVersionsOfABoard(t *testing.T) {
	f := newFlow(t, emi.Features{})
	p := f.project("orgA")
	v1, run1 := f.version("orgA", p, "uploads/orgA/one/board.kicad_pcb")
	v2, run2 := f.version("orgA", p, "uploads/orgA/two/board.kicad_pcb")
	// A third version of the same bytes as the second: content-addressed, so one object.
	v3, run3 := f.version("orgA", p, "uploads/orgA/two/board.kicad_pcb")

	code, out := f.user("orgA", "GET", "/emi/projects/"+p+"/boards", nil)
	if code != http.StatusOK {
		t.Fatalf("list boards: %d %v", code, out)
	}
	var ids []string
	for _, b := range out["boards"].([]any) {
		ids = append(ids, b.(map[string]any)["id"].(string))
	}
	if len(ids) != 3 || ids[0] != v3 || ids[2] != v1 {
		t.Fatalf("boards %v, want newest first [%s %s %s]", ids, v3, v2, v1)
	}

	// Another organisation cannot see or delete a version.
	if code, _ := f.user("orgB", "DELETE", "/emi/projects/"+p+"/boards/"+v1, nil); code != http.StatusNotFound {
		t.Fatalf("other org delete: got %d, want 404", code)
	}
	// A version with a run still going is refused: its worker would report into nothing.
	code, out = f.user("orgA", "POST", "/emi/projects/"+p+"/runs", map[string]any{"board_id": v1, "kind": "ingest"})
	if code != http.StatusCreated {
		t.Fatalf("reanalyse: %d %v", code, out)
	}
	queued := out["id"].(string)
	if code, _ := f.user("orgA", "DELETE", "/emi/projects/"+p+"/boards/"+v1, nil); code != http.StatusConflict {
		t.Fatalf("delete with a queued run: got %d, want 409", code)
	}
	if code, _ := f.user("orgA", "POST", "/emi/runs/"+queued+"/stop", map[string]any{}); code != http.StatusOK {
		t.Fatalf("stop: %d", code)
	}

	// Deleting v1 takes its runs and its file with it.
	if code, out := f.user("orgA", "DELETE", "/emi/projects/"+p+"/boards/"+v1, nil); code != http.StatusNoContent {
		t.Fatalf("delete v1: %d %v", code, out)
	}
	if code, _ := f.user("orgA", "GET", "/emi/runs/"+run1, nil); code != http.StatusNotFound {
		t.Errorf("v1's ingest run survived: %d", code)
	}
	if f.exists("uploads/orgA/one/board.kicad_pcb") || f.exists("runs/" + run1 + "/board.json") {
		t.Error("v1's objects survived")
	}
	for _, run := range []string{run2, run3} {
		if code, _ := f.user("orgA", "GET", "/emi/runs/"+run, nil); code != http.StatusOK {
			t.Errorf("run %s of another version is gone: %d", run, code)
		}
	}

	// v2's file is v3's too, so deleting v2 keeps it.
	if code, _ := f.user("orgA", "DELETE", "/emi/projects/"+p+"/boards/"+v2, nil); code != http.StatusNoContent {
		t.Fatalf("delete v2: %d", code)
	}
	if !f.exists("uploads/orgA/two/board.kicad_pcb") {
		t.Error("deleting v2 removed the file v3 still uses")
	}
	if !f.exists("runs/" + run3 + "/board.json") {
		t.Error("deleting v2 removed v3's results")
	}

	// The last version stays: a project without a board is removed by deleting the project.
	if code, _ := f.user("orgA", "DELETE", "/emi/projects/"+p+"/boards/"+v3, nil); code != http.StatusConflict {
		t.Fatalf("delete the last version: got %d, want 409", code)
	}
	if code, _ := f.user("orgA", "DELETE", "/emi/projects/"+p+"/boards/"+v1, nil); code != http.StatusNotFound {
		t.Fatalf("delete a version twice: got %d, want 404", code)
	}
}
