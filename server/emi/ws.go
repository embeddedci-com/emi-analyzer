package emi

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"github.com/coder/websocket"
)

// The worker WebSocket. The worker always dials out, which is the property that lets it run
// on a laptop behind NAT or on an office workstation with no inbound firewall rule — the
// same reason build agents work that way.
//
// Push is an optimisation and never the delivery guarantee: everything sent here is also
// discoverable through GET /emi-agent/runs, so a worker that misses a message because it
// was reconnecting still picks the work up.

const (
	wsReadLimit    = 1 << 20 // 1 MiB; control messages only, results go to object storage
	wsPingInterval = 30 * time.Second
	wsPingTimeout  = 10 * time.Second
)

func (s *Service) handleWorkerWS(w http.ResponseWriter, r *http.Request) {
	// Authenticate before upgrading, so a bad key gets a plain 403 rather than a socket
	// that closes immediately for reasons the worker has to guess at.
	raw := bearer(r)
	if raw == "" {
		writeErr(w, http.StatusUnauthorized, "missing agent key")
		return
	}
	key, err := s.deps.Keys.VerifyAgentKey(r.Context(), raw)
	if err != nil {
		writeErr(w, http.StatusForbidden, "invalid agent key")
		return
	}
	if key.AgentType != AgentTypeEMI {
		writeErr(w, http.StatusForbidden, "key is not an emi worker key")
		return
	}

	conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{
		InsecureSkipVerify: true, // workers are not browsers; there is no Origin to check
	})
	if err != nil {
		s.deps.log().Warn("emi: ws accept failed", "err", err)
		return
	}
	conn.SetReadLimit(wsReadLimit)

	// The worker's declared capabilities come from its most recent register call. A worker
	// that connects without ever registering gets conservative defaults, which in practice
	// means it is offered ingest runs but no solves.
	caps := Capabilities{}
	if known, gErr := s.deps.Store.GetWorker(r.Context(), key.Kid); gErr == nil && known != nil {
		caps = known.Capabilities
	}

	wc := &workerConn{
		conn: conn, keyKid: key.Kid, orgID: key.OrganizationID,
		name: key.Name, caps: caps, closed: make(chan struct{}),
	}
	s.hub.register(wc)
	s.deps.log().Info("emi: worker connected", "kid", key.Kid, "org", key.OrganizationID,
		"max_cells", caps.MaxCells, "online", s.hub.OnlineCount())

	ctx, cancel := context.WithCancel(context.Background())
	defer func() {
		cancel()
		s.hub.unregister(wc)
		_ = conn.CloseNow()
		s.deps.log().Info("emi: worker disconnected", "kid", key.Kid, "online", s.hub.OnlineCount())
	}()

	_ = s.deps.Store.TouchWorker(ctx, key.Kid, s.deps.now())

	// Send whatever is already outstanding. This is what makes push-vs-poll invisible to
	// the worker: it always starts from a complete picture of its queue.
	s.sendPending(ctx, wc)

	go s.keepalive(ctx, wc)

	for {
		typ, data, rErr := conn.Read(ctx)
		if rErr != nil {
			if !errors.Is(rErr, context.Canceled) {
				s.deps.log().Debug("emi: ws read ended", "kid", key.Kid, "err", rErr)
			}
			return
		}
		if typ != websocket.MessageText {
			continue
		}
		s.handleWorkerMessage(ctx, wc, data)
	}
}

// sendPending pushes every claimable run for this worker's org that it could actually take.
func (s *Service) sendPending(ctx context.Context, wc *workerConn) {
	runs, err := s.deps.Store.ListClaimableRuns(ctx, wc.orgID, 50)
	if err != nil {
		s.deps.log().Error("emi: list claimable failed", "err", err)
		return
	}
	sent := 0
	for _, run := range runs {
		var cells int64
		if run.Estimate != nil {
			cells = run.Estimate.Cells
		}
		if !wc.caps.Accepts(run.Kind, cells) {
			continue
		}
		if err := wc.send(ctx, map[string]any{"type": "new_run", "run": runEnvelope(run)}); err != nil {
			return
		}
		sent++
	}
	if sent > 0 {
		s.deps.log().Info("emi: sent pending runs", "kid", wc.keyKid, "count", sent)
	}
}

// handleWorkerMessage handles the few things a worker says over the socket. Everything with
// a side effect — claiming, progress, completion — goes over REST with a run token instead,
// so that the audit trail and the authorisation checks live in one place.
func (s *Service) handleWorkerMessage(ctx context.Context, wc *workerConn, data []byte) {
	var msg struct {
		Type         string        `json:"type"`
		Capabilities *Capabilities `json:"capabilities,omitempty"`
	}
	if err := json.Unmarshal(data, &msg); err != nil {
		return
	}
	switch msg.Type {
	case "hello", "capabilities":
		// A worker may refresh its capabilities on an open socket, e.g. after the operator
		// changes its cgroup limits. Update both the live view used for dispatch and the
		// stored row used at reconnect.
		if msg.Capabilities != nil {
			s.hub.mu.Lock()
			wc.caps = *msg.Capabilities
			s.hub.mu.Unlock()
			if known, err := s.deps.Store.GetWorker(ctx, wc.keyKid); err == nil && known != nil {
				known.Capabilities = *msg.Capabilities
				_ = s.deps.Store.UpsertWorker(ctx, known)
			}
			// New capabilities may make previously ineligible work eligible.
			s.sendPending(ctx, wc)
		}
	case "poll":
		s.sendPending(ctx, wc)
	case "pong":
		_ = s.deps.Store.TouchWorker(ctx, wc.keyKid, s.deps.now())
	}
}

// keepalive pings the worker so a dead TCP connection is noticed rather than lingering as a
// phantom that dispatch keeps choosing.
func (s *Service) keepalive(ctx context.Context, wc *workerConn) {
	t := time.NewTicker(wsPingInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-wc.closed:
			return
		case <-t.C:
			pctx, cancel := context.WithTimeout(ctx, wsPingTimeout)
			err := wc.conn.Ping(pctx)
			cancel()
			if err != nil {
				wc.shutdown()
				return
			}
			_ = s.deps.Store.TouchWorker(ctx, wc.keyKid, s.deps.now())
		}
	}
}
