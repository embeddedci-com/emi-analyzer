package local

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

// flow drives the emi control plane over HTTP against the real SQLite store and file blob, so
// a test sees what a browser and a worker see: status codes, JSON, and the rows behind them.
type flow struct {
	t     *testing.T
	srv   *httptest.Server
	store *SQLiteStore
	blob  *FileBlob
	keys  *Keys

	mu  sync.Mutex
	now time.Time
}

// newFlow mounts a Service under /api. The user middleware trusts an X-Test-Org header, which
// is how a test acts as two organisations against one server.
func newFlow(t *testing.T, features emi.Features, opts ...func(*emi.Deps)) *flow {
	t.Helper()
	ctx := context.Background()
	store, err := OpenSQLite(ctx, filepath.Join(t.TempDir(), "emi.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })

	mux := http.NewServeMux()
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)

	secret := bytes.Repeat([]byte("s"), 32)
	blob, err := NewFileBlob(filepath.Join(t.TempDir(), "blobs"), secret, "/blob", srv.URL)
	if err != nil {
		t.Fatal(err)
	}
	f := &flow{t: t, srv: srv, store: store, blob: blob, keys: NewKeys(store.DB(), secret),
		now: time.Date(2026, 9, 23, 12, 0, 0, 0, time.UTC)}

	deps := emi.Deps{
		Store: store, Keys: f.keys, Blob: blob, TokenSecret: secret,
		Features: features, Now: f.clock,
	}
	for _, o := range opts {
		o(&deps)
	}
	svc, err := emi.New(deps)
	if err != nil {
		t.Fatal(err)
	}
	svc.Mount(mux, "/api", func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			org := r.Header.Get("X-Test-Org")
			next(w, r.WithContext(emi.WithUser(r.Context(), emi.UserIdentity{UserID: "u-" + org, OrganizationID: org})))
		}
	})
	mux.Handle("/blob/", blob.Handler())
	return f
}

func (f *flow) clock() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

func (f *flow) advance(d time.Duration) {
	f.mu.Lock()
	f.now = f.now.Add(d)
	f.mu.Unlock()
}

func (f *flow) do(method, path string, header http.Header, body any) (int, map[string]any) {
	f.t.Helper()
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			f.t.Fatal(err)
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, f.srv.URL+path, rd)
	if err != nil {
		f.t.Fatal(err)
	}
	for k, v := range header {
		req.Header[k] = v
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		f.t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &out); err != nil {
			f.t.Fatalf("%s %s: status %d, body is not JSON: %s", method, path, resp.StatusCode, raw)
		}
	}
	return resp.StatusCode, out
}

// user calls a user-facing endpoint as a member of org.
func (f *flow) user(org, method, path string, body any) (int, map[string]any) {
	f.t.Helper()
	return f.do(method, "/api"+path, http.Header{"X-Test-Org": {org}}, body)
}

// agent calls a worker endpoint with a worker key or a run token.
func (f *flow) agent(token, method, path string, body any) (int, map[string]any) {
	f.t.Helper()
	return f.do(method, "/api"+path, http.Header{"Authorization": {"Bearer " + token}}, body)
}

// put writes an object the way a browser or worker does: through a presigned PUT.
func (f *flow) put(key string, data []byte) {
	f.t.Helper()
	u, err := f.blob.PresignPut(context.Background(), key, "application/octet-stream", 0, time.Hour)
	if err != nil {
		f.t.Fatal(err)
	}
	req, _ := http.NewRequest(http.MethodPut, u, bytes.NewReader(data))
	req.Header.Set("Content-Type", "application/octet-stream")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		f.t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		f.t.Fatalf("put %s: status %d", key, resp.StatusCode)
	}
}

func (f *flow) workerKey(org string) string {
	f.t.Helper()
	raw, err := f.keys.Issue(context.Background(), org, "test worker")
	if err != nil {
		f.t.Fatal(err)
	}
	return raw
}

// project creates a project for org and returns its id.
func (f *flow) project(org string) string {
	f.t.Helper()
	code, out := f.user(org, "POST", "/emi/projects", map[string]any{"name": "board"})
	if code != http.StatusCreated {
		f.t.Fatalf("create project: %d %v", code, out)
	}
	return out["id"].(string)
}

// board uploads a board for org and records it, returning the board id and its ingest run id.
func (f *flow) board(org, projectID string) (boardID, runID string) {
	f.t.Helper()
	key := "uploads/" + org + "/" + projectID + "/board.kicad_pcb"
	f.put(key, []byte("(kicad_pcb)"))
	code, out := f.user(org, "POST", "/emi/projects/"+projectID+"/boards", map[string]any{"input_key": key})
	if code != http.StatusCreated {
		f.t.Fatalf("create board: %d %v", code, out)
	}
	return out["board"].(map[string]any)["id"].(string), out["run"].(map[string]any)["id"].(string)
}

// mint takes a run as the holder of key and returns the run token.
func (f *flow) mint(key, runID string) string {
	f.t.Helper()
	code, out := f.agent(key, "POST", "/emi-agent/runs/"+runID+"/token", map[string]any{})
	if code != http.StatusOK {
		f.t.Fatalf("mint: %d %v", code, out)
	}
	return out["token"].(string)
}
