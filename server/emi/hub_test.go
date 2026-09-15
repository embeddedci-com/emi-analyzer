package emi

import "testing"

// A worker's key decides which organisations it serves, and there are two legitimate
// answers. A shared worker -- the kind EmbeddedCI runs -- names no organisation and does
// the solving for everybody. A scoped worker is a customer's own hardware and must never be
// handed anyone else's board.
func TestSharedWorkersServeEveryOrgAndScopedOnesDoNot(t *testing.T) {
	shared := &workerConn{orgID: ""}
	scoped := &workerConn{orgID: "acme"}

	for _, org := range []string{"acme", "globex", "public", ""} {
		if !shared.serves(org) {
			t.Errorf("a shared worker refused organisation %q", org)
		}
	}
	if !scoped.serves("acme") {
		t.Error("a scoped worker refused its own organisation")
	}
	if scoped.serves("globex") {
		t.Error("a scoped worker accepted another organisation's work")
	}
}

func hubWith(conns ...*workerConn) *Hub {
	h := NewHub()
	for _, wc := range conns {
		h.conns[wc] = struct{}{}
	}
	return h
}

func TestEligibilityMixesSharedAndScopedWorkers(t *testing.T) {
	caps := Capabilities{Cores: 8, RAMGB: 32, MaxCells: 100_000_000, Kinds: []RunKind{RunKindIngest, RunKindSolve}}
	shared := &workerConn{orgID: "", caps: caps}
	acme := &workerConn{orgID: "acme", caps: caps}
	h := hubWith(shared, acme)

	// Acme's run may go to either; another org's may go only to the shared one.
	if got := len(h.eligible("acme", RunKindSolve, 1000)); got != 2 {
		t.Errorf("acme had %d eligible workers, want 2", got)
	}
	got := h.eligible("globex", RunKindSolve, 1000)
	if len(got) != 1 || got[0] != shared {
		t.Errorf("globex got %d workers, want only the shared one", len(got))
	}

	// And the same split has to hold for what the UI is told, or a user sees "0 workers
	// online" while a shared worker sits there ready to take their run.
	if n := len(h.OnlineWorkers("globex")); n != 1 {
		t.Errorf("OnlineWorkers(globex) = %d, want 1", n)
	}
	if _, _, online := h.CanAccept("globex", RunKindSolve, 1000); online != 1 {
		t.Errorf("CanAccept(globex) saw %d workers online, want 1", online)
	}
}

// Capability filtering still applies to a shared worker: serving every organisation does
// not mean accepting every run.
func TestASharedWorkerStillDeclinesWorkItCannotDo(t *testing.T) {
	small := &workerConn{orgID: "", caps: Capabilities{
		Cores: 2, RAMGB: 4, MaxCells: 1_000_000, Kinds: []RunKind{RunKindIngest},
	}}
	h := hubWith(small)

	if n := len(h.eligible("acme", RunKindIngest, 0)); n != 1 {
		t.Errorf("ingest: %d eligible, want 1", n)
	}
	if n := len(h.eligible("acme", RunKindSolve, 500_000)); n != 0 {
		t.Errorf("solve on an ingest-only worker: %d eligible, want 0", n)
	}
	if n := len(h.eligible("acme", RunKindSolve, 50_000_000)); n != 0 {
		t.Errorf("oversized solve: %d eligible, want 0", n)
	}
}
