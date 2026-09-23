package local

import (
	"net/http"
	"testing"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

// A board may only be recorded from an upload in the caller's own organisation, and ".." must
// not get around that. Both blob backends clean a key before using it, so
// "uploads/orgA/../orgB/..." used to read organisation B's file -- and deleting the project
// that recorded it deleted B's file too.
func TestCreateBoardRefusesKeysThatLeaveTheOrganisation(t *testing.T) {
	f := newFlow(t, emi.Features{})
	f.put("uploads/orgB/secret/board.kicad_pcb", []byte("(kicad_pcb B)"))
	f.put("uploads/orgA/mine/board.kicad_pcb", []byte("(kicad_pcb A)"))
	p := f.project("orgA")

	for _, key := range []string{
		"uploads/orgA/../orgB/secret/board.kicad_pcb",
		"uploads/orgA/mine/../../orgB/secret/board.kicad_pcb",
		"uploads/orgA/./mine/board.kicad_pcb",
		"uploads/orgA//mine/board.kicad_pcb",
		"/uploads/orgA/mine/board.kicad_pcb",
		"uploads/orgB/secret/board.kicad_pcb",
		"uploads/orgA",
		"",
	} {
		code, out := f.user("orgA", "POST", "/emi/projects/"+p+"/boards", map[string]any{"input_key": key})
		if code != http.StatusBadRequest {
			t.Errorf("input_key %q: got %d %v, want 400", key, code, out)
		}
	}

	code, out := f.user("orgA", "POST", "/emi/projects/"+p+"/boards",
		map[string]any{"input_key": "uploads/orgA/mine/board.kicad_pcb"})
	if code != http.StatusCreated {
		t.Fatalf("own clean key: got %d %v, want 201", code, out)
	}
}

// Every key a worker reports must be under its own run. The key is presigned for anyone who
// can see the run, so an unchecked one would hand out another run's results or an upload.
func TestCompleteRunKeepsKeysUnderTheRun(t *testing.T) {
	f := newFlow(t, emi.Features{})
	f.put("uploads/orgB/secret/board.kicad_pcb", []byte("(kicad_pcb B)"))
	p := f.project("orgA")
	_, runID := f.board("orgA", p)
	key := f.workerKey("")
	tok := f.mint(key, runID)

	own := "runs/" + runID + "/board.json"
	f.put(own, []byte(`{}`))

	bad := []map[string]any{
		{"status": "done", "artifacts": []map[string]any{{"name": "board.json", "key": "uploads/orgB/secret/board.kicad_pcb"}}},
		{"status": "done", "artifacts": []map[string]any{{"name": "board.json", "key": "runs/" + runID + "/../other/board.json"}}},
		{"status": "done", "artifacts": []map[string]any{{"name": "board.json", "key": "runs/other/board.json"}}},
		{"status": "done", "artifacts": []map[string]any{{"name": "board.json", "key": "/" + own}}},
		// One good entry before a bad one: nothing may be recorded.
		{"status": "done", "artifacts": []map[string]any{
			{"name": "board.json", "key": own},
			{"name": "x.bin", "key": "runs/other/x.bin"},
		}},
		{"status": "done", "board": map[string]any{"board_key": "uploads/orgB/secret/board.kicad_pcb"}},
		{"status": "done", "board": map[string]any{"board_key": "runs/" + runID + "/../other/board.json"}},
	}
	for i, body := range bad {
		if code, out := f.agent(tok, "POST", "/emi-agent/runs/"+runID+"/complete", body); code != http.StatusBadRequest {
			t.Errorf("case %d: got %d %v, want 400", i, code, out)
		}
	}
	if code, out := f.user("orgA", "GET", "/emi/runs/"+runID+"/artifacts", nil); code != http.StatusOK ||
		len(out["artifacts"].([]any)) != 0 {
		t.Fatalf("a rejected completion recorded artifacts: %d %v", code, out)
	}

	code, out := f.agent(tok, "POST", "/emi-agent/runs/"+runID+"/complete", map[string]any{
		"status":    "done",
		"artifacts": []map[string]any{{"name": "board.json", "key": own}},
		"board":     map[string]any{"board_key": own, "layer_count": 2},
	})
	if code != http.StatusOK {
		t.Fatalf("clean completion: got %d %v", code, out)
	}
	code, out = f.user("orgA", "GET", "/emi/runs/"+runID+"/artifacts/board.json", nil)
	if code != http.StatusOK || out["url"] == "" {
		t.Fatalf("artifact url: %d %v", code, out)
	}
}
