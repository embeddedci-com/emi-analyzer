package emi

import (
	"context"
	"encoding/json"
	"math/rand/v2"
	"sync"
	"time"

	"github.com/coder/websocket"
)

// Hub tracks the EMI workers currently dialled in.
//
// Dispatch is capability-filtered. A CI job can go to any connected agent because such jobs
// are roughly uniform. An EMI solve cannot, because a 12-million-cell run and a
// 1.9-billion-cell run are the same message.
type Hub struct {
	mu    sync.RWMutex
	conns map[*workerConn]struct{}
	byKid map[string]*workerConn

	log func() string
}

type workerConn struct {
	conn   *websocket.Conn
	keyKid string
	orgID  string
	name   string
	caps   Capabilities

	sendMu sync.Mutex
	closed chan struct{}
	once   sync.Once
}

// NewHub returns an empty hub.
func NewHub() *Hub {
	return &Hub{
		conns: make(map[*workerConn]struct{}),
		byKid: make(map[string]*workerConn),
	}
}

func (h *Hub) register(wc *workerConn) {
	h.mu.Lock()
	defer h.mu.Unlock()
	// One connection per key. A reconnecting worker replaces its old connection rather
	// than accumulating ghosts, which is what happens when a laptop lid closes and the
	// TCP connection dies without a close frame.
	if old, ok := h.byKid[wc.keyKid]; ok {
		delete(h.conns, old)
		old.shutdown()
	}
	h.conns[wc] = struct{}{}
	h.byKid[wc.keyKid] = wc
}

func (h *Hub) unregister(wc *workerConn) {
	h.mu.Lock()
	defer h.mu.Unlock()
	delete(h.conns, wc)
	if cur, ok := h.byKid[wc.keyKid]; ok && cur == wc {
		delete(h.byKid, wc.keyKid)
	}
	wc.shutdown()
}

func (wc *workerConn) shutdown() {
	wc.once.Do(func() { close(wc.closed) })
}

// send writes one JSON message. Writes are serialised per connection because
// coder/websocket permits only one concurrent writer.
func (wc *workerConn) send(ctx context.Context, msg any) error {
	b, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	wc.sendMu.Lock()
	defer wc.sendMu.Unlock()
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	return wc.conn.Write(ctx, websocket.MessageText, b)
}

// OnlineCount reports how many workers are connected.
func (h *Hub) OnlineCount() int {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return len(h.conns)
}

// serves reports whether a connected worker will take work for an organisation.
//
// A worker whose key names no organisation is a shared worker: it serves every one of them.
// That is the normal case for a hosted service: one pool doing the solving for everybody. A
// key scoped to an organisation is for the other case: a customer running a worker on their
// own hardware, which must only ever be handed their own boards.
func (wc *workerConn) serves(orgID string) bool {
	return wc.orgID == "" || wc.orgID == orgID
}

// OnlineWorkers returns a snapshot of the workers that would take work for one organisation.
func (h *Hub) OnlineWorkers(orgID string) []Worker {
	h.mu.RLock()
	defer h.mu.RUnlock()
	out := make([]Worker, 0, len(h.conns))
	for wc := range h.conns {
		if !wc.serves(orgID) {
			continue
		}
		out = append(out, Worker{
			APIKeyKid:    wc.keyKid,
			Name:         wc.name,
			Status:       "online",
			Capabilities: wc.caps,
		})
	}
	return out
}

// eligible returns the connected workers serving an org that would accept this run.
func (h *Hub) eligible(orgID string, kind RunKind, cells int64) []*workerConn {
	h.mu.RLock()
	defer h.mu.RUnlock()
	var out []*workerConn
	for wc := range h.conns {
		if wc.serves(orgID) && wc.caps.Accepts(kind, cells) {
			out = append(out, wc)
		}
	}
	return out
}

// CanAccept reports whether any connected worker in the org could take this run, and if
// not, the largest max_cells currently online. The API uses this to reject an oversized
// solve at submit time with a message naming the actual limit, rather than letting it sit
// claimable forever while the user wonders why nothing is happening.
func (h *Hub) CanAccept(orgID string, kind RunKind, cells int64) (ok bool, bestMaxCells int64, online int) {
	h.mu.RLock()
	defer h.mu.RUnlock()
	for wc := range h.conns {
		if !wc.serves(orgID) {
			continue
		}
		online++
		if wc.caps.MaxCells > bestMaxCells {
			bestMaxCells = wc.caps.MaxCells
		}
		if wc.caps.Accepts(kind, cells) {
			ok = true
		}
	}
	return ok, bestMaxCells, online
}

// DispatchRun pushes a new_run to one eligible worker chosen at random.
//
// If nobody is eligible this is a no-op and the run simply stays claimable: a worker that
// connects later is sent everything outstanding. Push is an optimisation, never the delivery
// guarantee.
func (h *Hub) DispatchRun(ctx context.Context, orgID string, r *Run) bool {
	var cells int64
	if r.Estimate != nil {
		cells = r.Estimate.Cells
	}
	cands := h.eligible(orgID, r.Kind, cells)
	if len(cands) == 0 {
		return false
	}
	wc := cands[rand.IntN(len(cands))]
	err := wc.send(ctx, map[string]any{
		"type": "new_run",
		"run":  runEnvelope(r),
	})
	return err == nil
}

// PushStop asks the worker that owns a run to stop it.
func (h *Hub) PushStop(ctx context.Context, keyKid, runID string) bool {
	h.mu.RLock()
	wc := h.byKid[keyKid]
	h.mu.RUnlock()
	if wc == nil {
		return false
	}
	return wc.send(ctx, map[string]any{"type": "stop_run", "run_id": runID}) == nil
}

// runEnvelope is the shape a worker receives. It is deliberately a subset of Run: a worker
// has no business knowing about owner_api_key_kid or jti_key.
func runEnvelope(r *Run) map[string]any {
	m := map[string]any{
		"id":         r.ID,
		"project_id": r.ProjectID,
		"kind":       r.Kind,
		"status":     r.Status,
		"created_at": r.CreatedAt,
	}
	if r.BoardID != "" {
		m["board_id"] = r.BoardID
	}
	if len(r.Params) > 0 {
		m["params"] = json.RawMessage(r.Params)
	}
	if r.Estimate != nil {
		m["estimate"] = r.Estimate
	}
	return m
}
