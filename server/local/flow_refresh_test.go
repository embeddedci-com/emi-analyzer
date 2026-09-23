package local

import (
	"net/http"
	"testing"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

// A solve longer than one run token has to be finishable. It was not: the token expired after
// RunTokenTTL, minting again failed to claim a run already in progress, and the token that
// came back anyway carried a jti the run did not have, so every call made with it got 409.
func TestLongRunRefreshesItsToken(t *testing.T) {
	f := newFlow(t, emi.Features{})
	p := f.project("orgA")
	_, runID := f.board("orgA", p)
	key := f.workerKey("")
	old := f.mint(key, runID)
	progress := map[string]any{"stage": "solve", "pct": 10}

	// Early refresh: the old token keeps working alongside the new one, so a request already
	// in flight when the worker swaps tokens is not mistaken for a reassignment.
	f.advance(time.Hour)
	code, out := f.agent(key, "POST", "/emi-agent/runs/"+runID+"/token/refresh", map[string]any{})
	if code != http.StatusOK || out["run_id"] != runID || out["token"] == "" {
		t.Fatalf("refresh: %d %v", code, out)
	}
	if exp, _ := out["expires_in"].(float64); time.Duration(exp)*time.Second != emi.RunTokenTTL {
		t.Fatalf("expires_in = %v, want %v", out["expires_in"], emi.RunTokenTTL)
	}
	fresh := out["token"].(string)
	for name, tok := range map[string]string{"old": old, "fresh": fresh} {
		if code, out := f.agent(tok, "POST", "/emi-agent/runs/"+runID+"/progress", progress); code != http.StatusNoContent {
			t.Fatalf("%s token after refresh: %d %v", name, code, out)
		}
	}

	// Past the first token's lifetime it is refused, and the refreshed one carries on.
	f.advance(emi.RunTokenTTL - time.Minute)
	if code, _ := f.agent(old, "POST", "/emi-agent/runs/"+runID+"/progress", progress); code != http.StatusUnauthorized {
		t.Fatalf("expired token: got %d, want 401", code)
	}
	if code, out := f.agent(fresh, "POST", "/emi-agent/runs/"+runID+"/progress", progress); code != http.StatusNoContent {
		t.Fatalf("refreshed token: %d %v", code, out)
	}

	// A worker whose token has already lapsed can still recover the run, by key. Minting again
	// for a run the key holds is the same refresh.
	f.advance(emi.RunTokenTTL)
	again := f.mint(key, runID)
	if code, out := f.agent(again, "POST", "/emi-agent/runs/"+runID+"/complete", map[string]any{"status": "done"}); code != http.StatusOK {
		t.Fatalf("complete with a re-minted token: %d %v", code, out)
	}

	// Nothing to refresh once the run has finished.
	if code, _ := f.agent(key, "POST", "/emi-agent/runs/"+runID+"/token/refresh", map[string]any{}); code != http.StatusConflict {
		t.Fatalf("refresh after completion: got %d, want 409", code)
	}
}

// Only the worker that holds the run gets a token for it.
func TestRefreshIsForTheHolderOnly(t *testing.T) {
	f := newFlow(t, emi.Features{})
	p := f.project("orgA")
	_, runID := f.board("orgA", p)
	holder, other, elsewhere := f.workerKey(""), f.workerKey(""), f.workerKey("orgB")
	refresh := func(key string) int {
		code, _ := f.agent(key, "POST", "/emi-agent/runs/"+runID+"/token/refresh", map[string]any{})
		return code
	}

	if code := refresh(holder); code != http.StatusConflict {
		t.Fatalf("refresh before anyone took the run: got %d, want 409", code)
	}
	f.mint(holder, runID)

	if code := refresh(other); code != http.StatusConflict {
		t.Fatalf("another worker's refresh: got %d, want 409", code)
	}
	if code, _ := f.agent(other, "POST", "/emi-agent/runs/"+runID+"/token", map[string]any{}); code != http.StatusConflict {
		t.Fatalf("another worker's mint: got %d, want 409", code)
	}
	if code := refresh(elsewhere); code != http.StatusNotFound {
		t.Fatalf("a worker scoped to another organisation: got %d, want 404", code)
	}
	if code := refresh("eci_nope_nope"); code != http.StatusUnauthorized {
		t.Fatalf("an unknown key: got %d, want 401", code)
	}

	// Still the holder's while it winds down after a stop.
	if code, out := f.user("orgA", "POST", "/emi/runs/"+runID+"/stop", map[string]any{}); code != http.StatusOK {
		t.Fatalf("stop: %d %v", code, out)
	}
	if code := refresh(holder); code != http.StatusOK {
		t.Fatalf("refresh while stopping: got %d, want 200", code)
	}
}

// After a retry hands the run to someone else, the first worker cannot refresh its way back.
func TestRefreshAfterRetryGoesToTheNewHolder(t *testing.T) {
	f := newFlow(t, emi.Features{})
	p := f.project("orgA")
	_, runID := f.board("orgA", p)
	first, second := f.workerKey(""), f.workerKey("")

	tok := f.mint(first, runID)
	if code, out := f.agent(tok, "POST", "/emi-agent/runs/"+runID+"/complete",
		map[string]any{"status": "failed", "error": "boom"}); code != http.StatusOK {
		t.Fatalf("fail: %d %v", code, out)
	}
	if code, out := f.user("orgA", "POST", "/emi/runs/"+runID+"/retry", map[string]any{}); code != http.StatusOK {
		t.Fatalf("retry: %d %v", code, out)
	}
	// Retry cleared the jti, so the first worker's token no longer names a holder.
	if code, _ := f.agent(tok, "POST", "/emi-agent/runs/"+runID+"/artifacts",
		map[string]any{"name": "x.bin"}); code != http.StatusConflict {
		t.Fatalf("superseded token on a retried run: got %d, want 409", code)
	}
	// Nor can it read the input or report a result while the run waits for a new worker.
	if code, _ := f.agent(tok, "GET", "/emi-agent/runs/"+runID+"/input", nil); code != http.StatusConflict {
		t.Fatalf("superseded token reading the input: got %d, want 409", code)
	}
	if code, _ := f.agent(tok, "POST", "/emi-agent/runs/"+runID+"/complete",
		map[string]any{"status": "done"}); code != http.StatusConflict {
		t.Fatalf("superseded token completing a retried run: got %d, want 409", code)
	}
	f.mint(second, runID)

	if code, _ := f.agent(first, "POST", "/emi-agent/runs/"+runID+"/token/refresh", map[string]any{}); code != http.StatusConflict {
		t.Fatalf("the superseded worker's refresh: got %d, want 409", code)
	}
	if code, _ := f.agent(second, "POST", "/emi-agent/runs/"+runID+"/token/refresh", map[string]any{}); code != http.StatusOK {
		t.Fatalf("the new holder's refresh: got %d, want 200", code)
	}
}
