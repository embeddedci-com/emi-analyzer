package emi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func (s *featureStore) GetWorker(context.Context, string) (*Worker, error) { return nil, ErrNotFound }

// dialWorker connects to the worker socket the way a worker does.
func dialWorker(t *testing.T, mux *http.ServeMux) *websocket.Conn {
	t.Helper()
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	conn, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(srv.URL, "http")+"/api/emi-agent/ws",
		&websocket.DialOptions{HTTPHeader: http.Header{"Authorization": {"Bearer eci_k_s"}}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.CloseNow() })
	return conn
}

func readRunID(t *testing.T, conn *websocket.Conn) string {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_, data, err := conn.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var msg struct {
		Type string `json:"type"`
		Run  struct {
			ID string `json:"id"`
		} `json:"run"`
	}
	if err := json.Unmarshal(data, &msg); err != nil || msg.Type != "new_run" {
		t.Fatalf("message = %s, %v", data, err)
	}
	return msg.Run.ID
}

// The socket must not push what the REST listing hides. A solve queued before full-wave was
// switched off used to be pushed, refused with 403 at mint, and pushed again on every poll.
//
// The store lists the solve first, so an unfiltered push would send it before the ingest run.
func TestWorkerSocketSkipsGatedRunKinds(t *testing.T) {
	mux, _ := newFeatureService(t, Features{})
	conn := dialWorker(t, mux)

	if id := readRunID(t, conn); id != "ingest" {
		t.Fatalf("first push = %q, want ingest", id)
	}
	// A poll resends the queue. If the solve were still in it, it would come first again.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := conn.Write(ctx, websocket.MessageText, []byte(`{"type":"poll"}`)); err != nil {
		t.Fatal(err)
	}
	if id := readRunID(t, conn); id != "ingest" {
		t.Fatalf("push after poll = %q, want ingest", id)
	}
}

func TestPendingRunsFollowTheFeatureSwitch(t *testing.T) {
	for _, tc := range []struct {
		features Features
		want     string
	}{
		{Features{}, "ingest"},
		{Features{FullWave: true}, "solve,ingest"},
	} {
		store := &featureStore{runs: map[string]*Run{
			"solve":  {ID: "solve", Kind: RunKindSolve, Status: StatusNew},
			"ingest": {ID: "ingest", Kind: RunKindIngest, Status: StatusNew},
		}}
		svc := &Service{deps: Deps{Store: store, Features: tc.features}, hub: NewHub()}
		runs, err := svc.pendingFor(context.Background(), "", Capabilities{Cores: 1, RAMGB: 1})
		if err != nil {
			t.Fatal(err)
		}
		var ids []string
		for _, r := range runs {
			ids = append(ids, r.ID)
		}
		if got := strings.Join(ids, ","); got != tc.want {
			t.Errorf("features %+v: pending = %q, want %q", tc.features, got, tc.want)
		}
	}
}
