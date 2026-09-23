package local

import (
	"context"
	"encoding/json"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

func openTestStore(t *testing.T) *SQLiteStore {
	t.Helper()
	s, err := OpenSQLite(context.Background(), filepath.Join(t.TempDir(), "emi.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	return s
}

// seed creates a project with one board and one new ingest run.
func seed(t *testing.T, s *SQLiteStore, org string, at time.Time) (*emi.Project, *emi.Board, *emi.Run) {
	t.Helper()
	ctx := context.Background()
	id := func(p string) string { return p + at.Format("150405.000000000") + org }
	p := &emi.Project{ID: id("p"), OrganizationID: org, Name: "board", SourceKind: emi.SourceKiCad, CreatedAt: at}
	if err := s.CreateProject(ctx, p); err != nil {
		t.Fatal(err)
	}
	b := &emi.Board{ID: id("b"), ProjectID: p.ID, InputKey: "uploads/x", ContentSHA256: "abc" + org, SizeBytes: 10, CreatedAt: at}
	if err := s.CreateBoard(ctx, b); err != nil {
		t.Fatal(err)
	}
	r := &emi.Run{ID: id("r"), ProjectID: p.ID, BoardID: b.ID, Kind: emi.RunKindIngest,
		Status: emi.StatusNew, Params: json.RawMessage(`{"a":1}`), CreatedAt: at, UpdatedAt: at}
	if err := s.CreateRun(ctx, r); err != nil {
		t.Fatal(err)
	}
	return p, b, r
}

func TestRoundTripKeepsEveryField(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	at := time.Date(2026, 9, 14, 10, 0, 0, 123456789, time.UTC)
	p, b, r := seed(t, s, "org", at)

	gotP, err := s.GetProject(ctx, p.ID)
	if err != nil || gotP.Name != "board" || !gotP.CreatedAt.Equal(at) || gotP.SourceKind != emi.SourceKiCad {
		t.Fatalf("project = %+v, %v", gotP, err)
	}

	if err := s.UpdateBoardParsed(ctx, b.ID, "runs/r/board.json", 4, 120, []byte(`[1,2]`), nil); err != nil {
		t.Fatal(err)
	}
	gotB, err := s.GetBoard(ctx, b.ID)
	if err != nil || gotB.BoardKey != "runs/r/board.json" || gotB.LayerCount != 4 || string(gotB.OutlineMM) != `[1,2]` || gotB.Stackup != nil {
		t.Fatalf("board = %+v, %v", gotB, err)
	}

	gotR, err := s.GetRun(ctx, r.ID)
	if err != nil || string(gotR.Params) != `{"a":1}` || gotR.ClaimedAt != nil || gotR.BoardID != b.ID {
		t.Fatalf("run = %+v, %v", gotR, err)
	}

	if _, err := s.GetRun(ctx, "missing"); !errors.Is(err, emi.ErrNotFound) {
		t.Fatalf("missing run: %v, want ErrNotFound", err)
	}
}

// Of many workers claiming one run, exactly one wins -- the property the Postgres store has
// and the handlers rely on.
func TestClaimIsAtomic(t *testing.T) {
	s := openTestStore(t)
	_, _, r := seed(t, s, "org", time.Now())

	var wg sync.WaitGroup
	var mu sync.Mutex
	wins, conflicts := 0, 0
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, err := s.ClaimRun(context.Background(), r.ID, "kid", "jti", time.Now())
			mu.Lock()
			defer mu.Unlock()
			switch {
			case err == nil:
				wins++
			case errors.Is(err, emi.ErrConflict):
				conflicts++
			default:
				t.Errorf("claim: %v", err)
			}
		}(i)
	}
	wg.Wait()
	if wins != 1 || conflicts != 15 {
		t.Fatalf("wins=%d conflicts=%d, want 1 and 15", wins, conflicts)
	}
}

func TestLifecycleTransitions(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	now := time.Now().UTC()
	_, _, r := seed(t, s, "org", now)

	if err := s.CompleteRun(ctx, r.ID, emi.StatusDone, nil, "", now); !errors.Is(err, emi.ErrConflict) {
		t.Fatalf("completing an unclaimed run: %v, want ErrConflict", err)
	}
	claimed, err := s.ClaimRun(ctx, r.ID, "kid", "jti-1", now)
	if err != nil || claimed.Status != emi.StatusInProgress || claimed.JTIKey != "jti-1" || claimed.ClaimedAt == nil {
		t.Fatalf("claim = %+v, %v", claimed, err)
	}
	if err := s.UpdateRunProgress(ctx, r.ID, &emi.Progress{Stage: "mesh", Pct: 40}, now); err != nil {
		t.Fatal(err)
	}
	if err := s.CompleteRun(ctx, r.ID, emi.StatusFailed, []byte(`{"x":1}`), "boom", now); err != nil {
		t.Fatal(err)
	}
	if err := s.RetryRun(ctx, r.ID, now); err != nil {
		t.Fatal(err)
	}
	got, _ := s.GetRun(ctx, r.ID)
	// Ownership must be cleared, or the superseded worker's token still names this run.
	if got.Status != emi.StatusRetryPending || got.JTIKey != "" || got.OwnerAPIKeyKid != "" || got.Error != "" || got.Progress != nil {
		t.Fatalf("after retry = %+v", got)
	}
	if string(got.Summary) != `{"x":1}` {
		t.Fatalf("summary lost: %s", got.Summary)
	}
}

// Timestamps are compared as strings in SQL, so the sweeper is only right if the stored
// format sorts chronologically -- including across a whole second with no fraction.
func TestSweepComparesTimesChronologically(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	base := time.Date(2026, 9, 14, 10, 0, 0, 0, time.UTC)

	_, _, old := seed(t, s, "a", base)
	_, _, fresh := seed(t, s, "b", base.Add(time.Second))
	if _, err := s.ClaimRun(ctx, old.ID, "k", "j", base); err != nil {
		t.Fatal(err)
	}
	if _, err := s.ClaimRun(ctx, fresh.ID, "k", "j", base.Add(900*time.Millisecond)); err != nil {
		t.Fatal(err)
	}
	n, err := s.SweepTimedOut(ctx, base.Add(500*time.Millisecond), base.Add(time.Hour))
	if err != nil || n != 1 {
		t.Fatalf("swept %d, %v; want 1", n, err)
	}
	if got, _ := s.GetRun(ctx, old.ID); got.Status != emi.StatusTimedOut {
		t.Fatalf("old run = %s, want timed_out", got.Status)
	}
	if got, _ := s.GetRun(ctx, fresh.ID); got.Status != emi.StatusInProgress {
		t.Fatalf("fresh run = %s, want in_progress", got.Status)
	}

	if n, err := s.FailInterrupted(ctx, base.Add(2*time.Hour)); err != nil || n != 1 {
		t.Fatalf("FailInterrupted = %d, %v; want 1", n, err)
	}
}

func TestClaimableRunsAndBoardLookupRespectOrganisations(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	now := time.Now()
	_, _, ra := seed(t, s, "a", now)
	_, _, rb := seed(t, s, "b", now.Add(time.Millisecond))

	runs, err := s.ListClaimableRuns(ctx, "a", 10)
	if err != nil || len(runs) != 1 || runs[0].ID != ra.ID {
		t.Fatalf("org a claimable = %v, %v", runs, err)
	}
	runs, err = s.ListClaimableRuns(ctx, "", 10)
	if err != nil || len(runs) != 2 || runs[0].ID != ra.ID || runs[1].ID != rb.ID {
		t.Fatalf("shared worker claimable = %v, %v", runs, err)
	}

	if _, _, err := s.FindBoardByContent(ctx, "a", "abcb"); !errors.Is(err, emi.ErrNotFound) {
		t.Fatalf("org a found org b's board: %v", err)
	}
	b, p, err := s.FindBoardByContent(ctx, "b", "abcb")
	if err != nil || p.OrganizationID != "b" || b.ProjectID != p.ID {
		t.Fatalf("lookup = %+v %+v %v", b, p, err)
	}
}

func TestComponentsAndDriversAndArtifacts(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	now := time.Now()
	p, _, r := seed(t, s, "org", now)

	c := &emi.Component{ID: "c1", OwnerUserID: "u1", OrganizationID: "org", Kind: "capacitor",
		Name: "C", Provenance: "user", Version: 1, CreatedAt: now}
	if err := s.CreateComponent(ctx, c); err != nil {
		t.Fatal(err)
	}
	// The visitor CHECK survives the port.
	visitor := *c
	visitor.ID, visitor.OwnerUserID = "c2", "anon-123"
	if err := s.CreateComponent(ctx, &visitor); err == nil {
		t.Fatal("a visitor-owned component was stored")
	}
	if list, _ := s.ListComponents(ctx, "org", "u2"); len(list) != 0 {
		t.Fatalf("unshared component visible to a colleague: %v", list)
	}
	if err := s.SetComponentShared(ctx, "c1", true); err != nil {
		t.Fatal(err)
	}
	c.Name = "C2"
	if err := s.UpdateComponent(ctx, c); err != nil {
		t.Fatal(err)
	}
	list, _ := s.ListComponents(ctx, "org", "u2")
	if len(list) != 1 || !list[0].Shared || list[0].Version != 2 || string(list[0].Match) != "{}" {
		t.Fatalf("shared list = %+v", list)
	}
	if err := s.SoftDeleteComponent(ctx, "c1"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.GetComponent(ctx, "c1"); !errors.Is(err, emi.ErrNotFound) {
		t.Fatalf("deleted component: %v", err)
	}

	d := &emi.Driver{ID: "d1", OrganizationID: "org", ProjectID: p.ID, Name: "clk", Role: "signal",
		Kind: "trapezoid", Document: json.RawMessage(`{"k":1}`), CreatedAt: now}
	if err := s.CreateDriver(ctx, d); err != nil {
		t.Fatal(err)
	}
	if err := s.DeleteDriver(ctx, "other-project", "d1"); !errors.Is(err, emi.ErrNotFound) {
		t.Fatalf("deleted across projects: %v", err)
	}
	if ds, _ := s.ListDrivers(ctx, p.ID); len(ds) != 1 || string(ds[0].Document) != `{"k":1}` {
		t.Fatalf("drivers = %+v", ds)
	}

	for _, size := range []int64{1, 2} {
		if err := s.CreateArtifact(ctx, &emi.Artifact{ID: "a" + string(rune('0'+size)), RunID: r.ID,
			Name: "board.json", Key: "k", ContentType: "application/json", SizeBytes: size, CreatedAt: now}); err != nil {
			t.Fatal(err)
		}
	}
	arts, _ := s.ListArtifacts(ctx, r.ID)
	if len(arts) != 1 || arts[0].SizeBytes != 2 {
		t.Fatalf("re-uploaded artifact did not replace the first: %+v", arts)
	}
}

func TestWorkersAndKeys(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	now := time.Now()
	keys := NewKeys(s.DB(), make([]byte, 32))

	raw, err := keys.Issue(ctx, "local", "auto")
	if err != nil {
		t.Fatal(err)
	}
	k, err := keys.VerifyAgentKey(ctx, raw)
	if err != nil || k.OrganizationID != "local" || k.AgentType != emi.AgentTypeEMI {
		t.Fatalf("verify = %+v, %v", k, err)
	}
	if _, err := keys.VerifyAgentKey(ctx, raw+"0"); !errors.Is(err, emi.ErrKeyNotFound) {
		t.Fatalf("wrong secret: %v", err)
	}
	manual, _ := keys.Issue(ctx, "local", "manual")
	if err := keys.RevokeNamed(ctx, "auto"); err != nil {
		t.Fatal(err)
	}
	if _, err := keys.VerifyAgentKey(ctx, raw); !errors.Is(err, emi.ErrKeyNotFound) {
		t.Fatalf("revoked key: %v", err)
	}
	if _, err := keys.VerifyAgentKey(ctx, manual); err != nil {
		t.Fatalf("a hand-issued key was revoked with the automatic ones: %v", err)
	}

	w := &emi.Worker{ID: "w", APIKeyKid: k.Kid, OrganizationID: "local", Name: "local", Status: "active",
		Capabilities: emi.Capabilities{Cores: 8}, RegisteredAt: now, LastSeenAt: &now}
	if err := s.UpsertWorker(ctx, w); err != nil {
		t.Fatal(err)
	}
	if ws, _ := s.ListWorkers(ctx, "local"); len(ws) != 1 || ws[0].Capabilities.Cores != 8 {
		t.Fatalf("workers = %+v", ws)
	}
	if err := s.DeregisterAll(ctx, now); err != nil {
		t.Fatal(err)
	}
	if ws, _ := s.ListWorkers(ctx, "local"); len(ws) != 0 {
		t.Fatalf("workers after DeregisterAll = %+v", ws)
	}
}

// Deleting a project takes its boards, runs and artifacts with it -- which only works because
// the connection turns foreign keys on.
func TestDeleteProjectCascades(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	now := time.Now()
	p, b, r := seed(t, s, "org", now)
	if err := s.CreateArtifact(ctx, &emi.Artifact{ID: "a1", RunID: r.ID, Name: "board.json",
		Key: "k", ContentType: "application/json", CreatedAt: now}); err != nil {
		t.Fatal(err)
	}

	if err := s.RenameProject(ctx, p.ID, "Rev B"); err != nil {
		t.Fatal(err)
	}
	if got, _ := s.GetProject(ctx, p.ID); got.Name != "Rev B" {
		t.Fatalf("name = %q", got.Name)
	}

	if err := s.DeleteProject(ctx, p.ID); err != nil {
		t.Fatal(err)
	}
	for _, check := range []struct {
		what string
		err  error
	}{
		{"project", func() error { _, e := s.GetProject(ctx, p.ID); return e }()},
		{"board", func() error { _, e := s.GetBoard(ctx, b.ID); return e }()},
		{"run", func() error { _, e := s.GetRun(ctx, r.ID); return e }()},
	} {
		if !errors.Is(check.err, emi.ErrNotFound) {
			t.Errorf("%s survived the delete: %v", check.what, check.err)
		}
	}
	if arts, _ := s.ListArtifacts(ctx, r.ID); len(arts) != 0 {
		t.Errorf("artifacts survived: %+v", arts)
	}
	if err := s.DeleteProject(ctx, p.ID); !errors.Is(err, emi.ErrNotFound) {
		t.Errorf("deleting twice: %v, want ErrNotFound", err)
	}
}

// An upload is content-addressed, so two projects can name the same object. Deleting one must
// not take the other's board file away.
func TestCountBoardsSharingInput(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	now := time.Now()
	p1, _, _ := seed(t, s, "a", now)
	p2, _, _ := seed(t, s, "b", now.Add(time.Millisecond))

	const key = "uploads/org/sha256/aa/board.kicad_pcb"
	for i, pid := range []string{p1.ID, p2.ID} {
		if err := s.CreateBoard(ctx, &emi.Board{ID: "shared" + string(rune('0'+i)),
			ProjectID: pid, InputKey: key, CreatedAt: now}); err != nil {
			t.Fatal(err)
		}
	}

	// One other project holds the same bytes, so deleting p1 must leave the object alone.
	n, err := s.CountBoardsSharingInput(ctx, key, p1.ID)
	if err != nil || n != 1 {
		t.Fatalf("shared count = %d, %v; want 1", n, err)
	}
	if n, _ := s.CountBoardsSharingInput(ctx, "uploads/nobody/else", p1.ID); n != 0 {
		t.Fatalf("unshared count = %d, want 0", n)
	}
}

// A queued run has no worker to wind it down, so stopping it ends it. It used to be left in
// "stopping" for good: nothing claims or completes a run in that state, the sweeper skips it
// for having no claimed_at, and retry and stop both refused it.
func TestRequestStop(t *testing.T) {
	s := openTestStore(t)
	ctx := context.Background()
	now := time.Now().UTC()

	for _, st := range []emi.RunStatus{emi.StatusNew, emi.StatusRetryPending} {
		_, _, r := seed(t, s, "org-"+string(st), now)
		if st == emi.StatusRetryPending {
			if _, err := s.db.ExecContext(ctx, `UPDATE emi_runs SET status = 'retry_pending' WHERE id = ?`, r.ID); err != nil {
				t.Fatal(err)
			}
		}
		if err := s.RequestStop(ctx, r.ID, now); err != nil {
			t.Fatalf("%s: %v", st, err)
		}
		got, _ := s.GetRun(ctx, r.ID)
		if got.Status != emi.StatusFailed || got.Error != emi.StoppedBeforeStartError || got.FinishedAt == nil {
			t.Fatalf("%s after stop = %+v", st, got)
		}
		if err := s.RetryRun(ctx, r.ID, now); err != nil {
			t.Fatalf("%s: a stopped queued run cannot be retried: %v", st, err)
		}
	}

	_, _, r := seed(t, s, "org-running", now)
	if _, err := s.ClaimRun(ctx, r.ID, "kid", "jti", now); err != nil {
		t.Fatal(err)
	}
	if err := s.RequestStop(ctx, r.ID, now); err != nil {
		t.Fatal(err)
	}
	got, _ := s.GetRun(ctx, r.ID)
	if got.Status != emi.StatusStopping || got.Error != "" || got.FinishedAt != nil {
		t.Fatalf("running run after stop = %+v", got)
	}
	if err := s.RequestStop(ctx, r.ID, now); !errors.Is(err, emi.ErrConflict) {
		t.Fatalf("stopping twice: %v, want ErrConflict", err)
	}
	if err := s.CompleteRun(ctx, r.ID, emi.StatusFailed, nil, "stopped on request", now); err != nil {
		t.Fatalf("the owner could not report back: %v", err)
	}
}
