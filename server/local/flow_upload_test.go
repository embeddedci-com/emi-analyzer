package local

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

func capUploads(n int64) func(*emi.Deps) {
	return func(d *emi.Deps) { d.MaxUploadBytes = n }
}

// putURL sends data to a presigned URL and returns the status.
func putURL(t *testing.T, u string, data []byte) int {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPut, u, bytes.NewReader(data))
	req.Header.Set("Content-Type", "application/octet-stream")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	return resp.StatusCode
}

// A signed URL used to let its holder write any amount to disk. The size a client declares
// is checked up front and signed into the URL, and a body without one is still capped.
func TestUploadsAreCapped(t *testing.T) {
	f := newFlow(t, emi.Features{}, capUploads(100))
	f.blob.MaxBytes = 100
	p := f.project("orgA")

	code, _ := f.user("orgA", "POST", "/emi/projects/"+p+"/uploads",
		map[string]any{"filename": "board.kicad_pcb", "size_bytes": 101})
	if code != http.StatusRequestEntityTooLarge {
		t.Fatalf("declared oversize upload: got %d, want 413", code)
	}
	if code, _ := f.user("orgA", "POST", "/emi/projects/"+p+"/uploads",
		map[string]any{"filename": "board.kicad_pcb", "size_bytes": -1}); code != http.StatusBadRequest {
		t.Fatalf("negative size: got %d, want 400", code)
	}

	code, out := f.user("orgA", "POST", "/emi/projects/"+p+"/uploads",
		map[string]any{"filename": "board.kicad_pcb", "size_bytes": 10})
	if code != http.StatusOK {
		t.Fatalf("upload url: %d %v", code, out)
	}
	u := out["upload_url"].(string)
	if got := putURL(t, u, bytes.Repeat([]byte("x"), 11)); got != http.StatusBadRequest {
		t.Errorf("body longer than the signed size: got %d, want 400", got)
	}
	// The size is part of the signature: editing it in the URL breaks the URL.
	if got := putURL(t, strings.Replace(u, "n=10", "n=50", 1), bytes.Repeat([]byte("x"), 50)); got != http.StatusForbidden {
		t.Errorf("edited size: got %d, want 403", got)
	}
	if got := putURL(t, u, bytes.Repeat([]byte("x"), 10)); got != http.StatusOK {
		t.Errorf("body of the signed size: got %d, want 200", got)
	}

	// No declared size: the store still stops at the cap.
	unsized, _ := f.blob.PresignPut(context.Background(), "uploads/orgA/big/board.kicad_pcb", "", 0, time.Hour)
	if got := putURL(t, unsized, bytes.Repeat([]byte("x"), 101)); got != http.StatusRequestEntityTooLarge {
		t.Errorf("unsized oversize body: got %d, want 413", got)
	}
}

// Storage that cannot enforce a size (a presigned S3 PUT without one) is backed by a check
// on the object itself before a row names it.
func TestOversizeObjectsAreNotRecorded(t *testing.T) {
	f := newFlow(t, emi.Features{}, capUploads(100))
	p := f.project("orgA")
	big := "uploads/orgA/big/board.kicad_pcb"
	f.put(big, bytes.Repeat([]byte("x"), 200))
	if code, _ := f.user("orgA", "POST", "/emi/projects/"+p+"/boards",
		map[string]any{"input_key": big}); code != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversize board: got %d, want 413", code)
	}

	_, runID := f.board("orgA", p)
	tok := f.mint(f.workerKey(""), runID)
	if code, _ := f.agent(tok, "POST", "/emi-agent/runs/"+runID+"/artifacts",
		map[string]any{"name": "big.bin", "size_bytes": 101}); code != http.StatusRequestEntityTooLarge {
		t.Fatalf("declared oversize artifact: got %d, want 413", code)
	}
	key := "runs/" + runID + "/big.bin"
	f.put(key, bytes.Repeat([]byte("x"), 200))
	if code, _ := f.agent(tok, "POST", "/emi-agent/runs/"+runID+"/complete", map[string]any{
		"status": "done", "artifacts": []map[string]any{{"name": "big.bin", "key": key}},
	}); code != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversize artifact at completion: got %d, want 413", code)
	}
}

// A run that timed out while its worker was still uploading gains nothing when that worker
// reports back: the state check comes before any artifact is recorded.
func TestALateCompletionAttachesNothing(t *testing.T) {
	f := newFlow(t, emi.Features{})
	p := f.project("orgA")
	_, runID := f.board("orgA", p)
	tok := f.mint(f.workerKey(""), runID)
	key := "runs/" + runID + "/board.json"
	f.put(key, []byte(`{}`))

	if n, err := f.store.SweepTimedOut(context.Background(), f.clock().Add(time.Hour), f.clock()); err != nil || n != 1 {
		t.Fatalf("sweep: %d %v", n, err)
	}
	code, _ := f.agent(tok, "POST", "/emi-agent/runs/"+runID+"/complete", map[string]any{
		"status": "done", "artifacts": []map[string]any{{"name": "board.json", "key": key}},
		"board": map[string]any{"board_key": key, "layer_count": 4},
	})
	if code != http.StatusConflict {
		t.Fatalf("late completion: got %d, want 409", code)
	}
	if code, out := f.user("orgA", "GET", "/emi/runs/"+runID+"/artifacts", nil); code != http.StatusOK ||
		len(out["artifacts"].([]any)) != 0 {
		t.Fatalf("a timed-out run gained artifacts: %d %v", code, out)
	}
}

// Deduplication used to trust the hash the uploader claimed. In a shared organisation one
// user could put other bytes under the hash of a board somebody else was about to upload,
// and that upload would be skipped. Now only a hash the ingest worker computed counts.
func TestDedupeTrustsOnlyTheWorkersHash(t *testing.T) {
	f := newFlow(t, emi.Features{})
	p := f.project("orgA")

	planted := []byte("(kicad_pcb planted)")
	realSum := sha256.Sum256(planted)
	real := hex.EncodeToString(realSum[:])
	claimed := strings.Repeat("a", 64)

	code, out := f.user("orgA", "POST", "/emi/projects/"+p+"/uploads",
		map[string]any{"filename": "board.kicad_pcb", "sha256": claimed})
	if code != http.StatusOK || out["already_uploaded"] != false {
		t.Fatalf("first upload: %d %v", code, out)
	}
	key := out["key"].(string)
	f.put(key, planted)
	code, out = f.user("orgA", "POST", "/emi/projects/"+p+"/boards",
		map[string]any{"input_key": key, "sha256": claimed})
	if code != http.StatusCreated {
		t.Fatalf("create board: %d %v", code, out)
	}
	runID := out["run"].(map[string]any)["id"].(string)

	// Before ingest has hashed the bytes, the claim is worth nothing.
	lookup := func(sha string) bool {
		_, out := f.user("orgA", "GET", "/emi/boards/lookup?sha256="+sha, nil)
		return out["found"] == true
	}
	if lookup(claimed) {
		t.Fatal("an unverified claim was offered as a known board")
	}

	tok := f.mint(f.workerKey(""), runID)
	own := "runs/" + runID + "/board.json"
	f.put(own, []byte(`{}`))
	if code, out := f.agent(tok, "POST", "/emi-agent/runs/"+runID+"/complete", map[string]any{
		"status":    "done",
		"artifacts": []map[string]any{{"name": "board.json", "key": own}},
		"board":     map[string]any{"board_key": own, "layer_count": 2, "content_sha256": real},
	}); code != http.StatusOK {
		t.Fatalf("complete: %d %v", code, out)
	}

	// The claimed hash still matches nothing, so the next honest upload of that file goes
	// ahead instead of being pointed at the planted bytes.
	if lookup(claimed) {
		t.Error("the claimed hash matched after ingest")
	}
	_, out = f.user("orgA", "POST", "/emi/projects/"+p+"/uploads",
		map[string]any{"filename": "board.kicad_pcb", "sha256": claimed})
	if out["already_uploaded"] != false {
		t.Errorf("upload under the claimed hash was skipped: %v", out)
	}
	// Nor does it overwrite what is there, which may be somebody's upload awaiting ingest.
	if out["key"] == key {
		t.Errorf("upload under the claimed hash was handed the existing key %s", key)
	}
	// The hash the worker computed is the one that deduplicates.
	if !lookup(real) {
		t.Error("the verified hash did not match")
	}
	_, out = f.user("orgA", "POST", "/emi/projects/"+p+"/uploads",
		map[string]any{"filename": "board.kicad_pcb", "sha256": real})
	if out["already_uploaded"] != true || out["key"] != key {
		t.Errorf("upload under the verified hash: %v, want already_uploaded at %s", out, key)
	}

	// A malformed hash from the worker is refused rather than stored.
	_, run2 := f.board("orgA", p)
	tok2 := f.mint(f.workerKey(""), run2)
	if code, _ := f.agent(tok2, "POST", "/emi-agent/runs/"+run2+"/complete", map[string]any{
		"status": "done", "board": map[string]any{"content_sha256": "nope"},
	}); code != http.StatusBadRequest {
		t.Errorf("malformed worker hash: got %d, want 400", code)
	}
}
