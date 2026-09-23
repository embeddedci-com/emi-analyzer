package emi

import (
	"bytes"
	"context"
	"net/http"
	"testing"
	"time"
)

func newSocketService(t *testing.T) (*http.ServeMux, *Service) {
	t.Helper()
	store := &featureStore{runs: map[string]*Run{
		"solve":  {ID: "solve", ProjectID: "p1", Kind: RunKindSolve, Status: StatusFailed},
		"ingest": {ID: "ingest", ProjectID: "p1", Kind: RunKindIngest, Status: StatusNew},
	}}
	svc, err := New(Deps{Store: store, Keys: allowKeys{}, Blob: stubBlob{},
		TokenSecret: bytes.Repeat([]byte("k"), 32)})
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	svc.Mount(mux, "/api", func(next http.HandlerFunc) http.HandlerFunc { return next })
	return mux, svc
}

func waitOnline(t *testing.T, svc *Service, want int) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for svc.hub.OnlineCount() != want {
		if time.Now().After(deadline) {
			t.Fatalf("online = %d, want %d", svc.hub.OnlineCount(), want)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// A reconnecting worker replaces its old socket. The old one has to be closed, not just
// dropped from the map: its read loop is blocked in Read and would otherwise hold a
// goroutine and a file descriptor for as long as the dead TCP connection lingers.
func TestAReplacedWorkerSocketIsClosed(t *testing.T) {
	mux, svc := newSocketService(t)
	old := dialWorker(t, mux)
	readRunID(t, old)
	waitOnline(t, svc, 1)

	fresh := dialWorker(t, mux)
	readRunID(t, fresh)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, _, err := old.Read(ctx); err == nil || ctx.Err() != nil {
		t.Fatalf("old socket still open after replacement (err %v, ctx %v)", err, ctx.Err())
	}
	waitOnline(t, svc, 1)
}

// A worker that stops answering pings is dropped. The client here never reads, so it never
// sends a pong, which is what a half-dead TCP connection looks like from the server.
func TestAWorkerThatStopsAnsweringPingsIsDropped(t *testing.T) {
	mux, svc := newSocketService(t)
	svc.hub.pingInterval, svc.hub.pingTimeout, svc.hub.idleTimeout = 50*time.Millisecond, 50*time.Millisecond, 300*time.Millisecond
	dialWorker(t, mux)
	waitOnline(t, svc, 1)
	waitOnline(t, svc, 0)
}

// The idle timer is the backstop when pings are not the thing failing.
func TestIdleForMeasuresFromTheLastSign(t *testing.T) {
	wc := &workerConn{closed: make(chan struct{})}
	if wc.idleFor() != 0 {
		t.Fatal("a connection never seen reports idle time")
	}
	wc.lastSeen.Store(time.Now().Add(-time.Minute).UnixNano())
	if d := wc.idleFor(); d < 59*time.Second {
		t.Fatalf("idleFor = %v, want about a minute", d)
	}
}
