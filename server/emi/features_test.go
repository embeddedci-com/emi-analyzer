package emi

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// featureStore holds one project, one board and whatever runs a test puts in it.
type featureStore struct {
	Store
	runs    map[string]*Run
	created []*Run
}

func (s *featureStore) GetProject(_ context.Context, id string) (*Project, error) {
	return &Project{ID: id, OrganizationID: "org"}, nil
}
func (s *featureStore) GetBoard(_ context.Context, id string) (*Board, error) {
	return &Board{ID: id, ProjectID: "p1"}, nil
}
func (s *featureStore) CreateRun(_ context.Context, r *Run) error {
	s.created = append(s.created, r)
	return nil
}
func (s *featureStore) GetRun(_ context.Context, id string) (*Run, error) {
	if r, ok := s.runs[id]; ok {
		return r, nil
	}
	return nil, ErrNotFound
}
func (s *featureStore) RetryRun(context.Context, string, time.Time) error { return nil }
func (s *featureStore) ListClaimableRuns(context.Context, string, int) ([]*Run, error) {
	return []*Run{s.runs["solve"], s.runs["ingest"]}, nil
}
func (s *featureStore) TouchWorker(context.Context, string, time.Time) error { return nil }

type allowKeys struct{}

func (allowKeys) VerifyAgentKey(context.Context, string) (AgentKey, error) {
	return AgentKey{Kid: "k", AgentType: AgentTypeEMI}, nil
}

func newFeatureService(t *testing.T, f Features) (*http.ServeMux, *featureStore) {
	t.Helper()
	store := &featureStore{runs: map[string]*Run{
		"solve":  {ID: "solve", ProjectID: "p1", Kind: RunKindSolve, Status: StatusFailed},
		"ingest": {ID: "ingest", ProjectID: "p1", Kind: RunKindIngest, Status: StatusNew},
	}}
	svc, err := New(Deps{Store: store, Keys: allowKeys{}, Blob: stubBlob{},
		TokenSecret: bytes.Repeat([]byte("k"), 32), Features: f})
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	svc.Mount(mux, "/api", func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			next(w, r.WithContext(WithUser(r.Context(), UserIdentity{UserID: "u", OrganizationID: "org"})))
		}
	})
	return mux, store
}

func serve(mux *http.ServeMux, method, path, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer eci_k_s")
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)
	return w
}

// Off by default means off at the server: a client that ignores the UI still cannot start one.
func TestExperimentalRunKindsAreRefusedByDefault(t *testing.T) {
	mux, store := newFeatureService(t, Features{})

	for _, kind := range []string{"solve", "compliance", ""} { // "" defaults to solve
		w := serve(mux, "POST", "/api/emi/projects/p1/runs", `{"board_id":"b1","kind":"`+kind+`"}`)
		if w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), FeatureFullWave) {
			t.Errorf("kind %q: got %d %s, want 403 naming the feature", kind, w.Code, w.Body.String())
		}
	}
	if len(store.created) != 0 {
		t.Fatalf("a refused run was stored: %+v", store.created)
	}

	if w := serve(mux, "POST", "/api/emi/runs/solve/retry", ""); w.Code != http.StatusForbidden {
		t.Errorf("retrying a solve: got %d, want 403", w.Code)
	}

	// A solve queued before the switch is neither listed to a worker nor tokenised for one.
	w := serve(mux, "GET", "/api/emi-agent/runs", "")
	if strings.Contains(w.Body.String(), `"solve"`) || !strings.Contains(w.Body.String(), `"ingest"`) {
		t.Errorf("worker run list = %s, want the ingest run only", w.Body.String())
	}
	if w := serve(mux, "POST", "/api/emi-agent/runs/solve/token", ""); w.Code != http.StatusForbidden {
		t.Errorf("minting a token for a solve: got %d, want 403", w.Code)
	}

	if w := serve(mux, "GET", "/api/emi/features", ""); strings.TrimSpace(w.Body.String()) != `{"full_wave":false,"small_part_solve":false}` {
		t.Errorf("features = %s", w.Body.String())
	}
}

func TestVerifiedRunKindsAreNotGated(t *testing.T) {
	mux, store := newFeatureService(t, Features{})
	for _, kind := range []string{"ingest", "cable"} {
		if w := serve(mux, "POST", "/api/emi/projects/p1/runs", `{"board_id":"b1","kind":"`+kind+`"}`); w.Code == http.StatusForbidden {
			t.Errorf("kind %q was refused: %s", kind, w.Body.String())
		}
	}
	if len(store.created) != 2 {
		t.Fatalf("created %d runs, want 2", len(store.created))
	}
}

func TestFullWaveCanBeTurnedOn(t *testing.T) {
	mux, store := newFeatureService(t, Features{FullWave: true})
	if w := serve(mux, "POST", "/api/emi/projects/p1/runs", `{"board_id":"b1","kind":"solve"}`); w.Code == http.StatusForbidden {
		t.Fatalf("solve refused with the feature on: %s", w.Body.String())
	}
	if len(store.created) != 1 {
		t.Fatalf("created %d runs, want 1", len(store.created))
	}
	if w := serve(mux, "GET", "/api/emi-agent/runs", ""); !strings.Contains(w.Body.String(), `"solve"`) {
		t.Errorf("worker run list = %s, want the solve included", w.Body.String())
	}
}

func TestParseExperimental(t *testing.T) {
	f, unknown := ParseExperimental(" Full-Wave , typo,")
	if !f.FullWave || len(unknown) != 1 || unknown[0] != "typo" {
		t.Fatalf("got %+v %v", f, unknown)
	}
	if f, _ := ParseExperimental(""); f.FullWave || f.SmallPartSolve != SmallPartSolveByDefault {
		t.Fatal("empty list enabled a feature")
	}
	if f, _ := ParseExperimental("small-part-solve"); f.FullWave || !f.SmallPartSolve {
		t.Fatalf("small-part-solve alone: %+v", f)
	}
	// Full-wave is the superset: turning it on must not switch small-part solves off.
	if f, _ := ParseExperimental("full-wave"); !f.SmallPartSolve {
		t.Fatalf("full-wave alone: %+v", f)
	}
}

const smallPartBody = `{"board_id":"b1","kind":"solve","params":{"mode":"small_part","coupon":{"nets":["CLK"]}}}`

// The small-part switch lets exactly one kind of solve through, and nothing built on a solve.
func TestSmallPartSolveIsItsOwnFeature(t *testing.T) {
	mux, store := newFeatureService(t, Features{SmallPartSolve: true})

	if w := serve(mux, "POST", "/api/emi/projects/p1/runs", smallPartBody); w.Code == http.StatusForbidden {
		t.Fatalf("small-part solve refused with its feature on: %s", w.Body.String())
	}
	if len(store.created) != 1 {
		t.Fatalf("created %d runs, want 1", len(store.created))
	}

	for _, body := range []string{
		`{"board_id":"b1","kind":"solve"}`,
		`{"board_id":"b1","kind":"solve","params":{"far_field":true}}`,
		`{"board_id":"b1","kind":"compliance"}`,
	} {
		w := serve(mux, "POST", "/api/emi/projects/p1/runs", body)
		if w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), FeatureFullWave) {
			t.Errorf("%s: got %d %s, want 403 naming full-wave", body, w.Code, w.Body.String())
		}
	}

	// Asking a small-part solve for what the mode leaves out is a bad request, not a gate.
	for _, extra := range []string{`"far_field":true`, `"cable_ports":{"J1":{"type":"usb2-shielded"}}`, `"model_components":true`} {
		body := `{"board_id":"b1","kind":"solve","params":{"mode":"small_part",` + extra + `}}`
		if w := serve(mux, "POST", "/api/emi/projects/p1/runs", body); w.Code != http.StatusBadRequest {
			t.Errorf("%s: got %d %s, want 400", extra, w.Code, w.Body.String())
		}
	}
	if len(store.created) != 1 {
		t.Fatalf("a refused run was stored: %d runs", len(store.created))
	}

	// A plain solve queued while full-wave was on is not handed to a worker now, and cannot be
	// retried or tokenised; a small-part one is and can.
	store.runs["part"] = &Run{ID: "part", ProjectID: "p1", Kind: RunKindSolve, Status: StatusFailed,
		Params: json.RawMessage(`{"mode":"small_part"}`)}
	if w := serve(mux, "POST", "/api/emi/runs/solve/retry", ""); w.Code != http.StatusForbidden {
		t.Errorf("retrying a plain solve: got %d, want 403", w.Code)
	}
	if w := serve(mux, "POST", "/api/emi/runs/part/retry", ""); w.Code == http.StatusForbidden {
		t.Errorf("retrying a small-part solve was refused: %s", w.Body.String())
	}
	if w := serve(mux, "POST", "/api/emi-agent/runs/solve/token", ""); w.Code != http.StatusForbidden {
		t.Errorf("minting a token for a plain solve: got %d, want 403", w.Code)
	}
	if w := serve(mux, "GET", "/api/emi/features", ""); strings.TrimSpace(w.Body.String()) != `{"full_wave":false,"small_part_solve":true}` {
		t.Errorf("features = %s", w.Body.String())
	}
}

func TestSmallPartSolveIsOffByDefault(t *testing.T) {
	mux, store := newFeatureService(t, Features{})
	w := serve(mux, "POST", "/api/emi/projects/p1/runs", smallPartBody)
	if w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), FeatureSmallPartSolve) {
		t.Fatalf("got %d %s, want 403 naming small-part-solve", w.Code, w.Body.String())
	}
	if len(store.created) != 0 {
		t.Fatal("a refused run was stored")
	}
}

func TestWorkerListFollowsTheSmallPartGate(t *testing.T) {
	mux, store := newFeatureService(t, Features{SmallPartSolve: true})
	store.runs["solve"].Params = json.RawMessage(`{"mode":"small_part"}`)
	if w := serve(mux, "GET", "/api/emi-agent/runs", ""); !strings.Contains(w.Body.String(), `"solve"`) {
		t.Errorf("worker run list = %s, want the small-part solve", w.Body.String())
	}
	store.runs["solve"].Params = json.RawMessage(`{"far_field":true}`)
	if w := serve(mux, "GET", "/api/emi-agent/runs", ""); strings.Contains(w.Body.String(), `"solve"`) {
		t.Errorf("worker run list = %s, want the plain solve left out", w.Body.String())
	}
}
