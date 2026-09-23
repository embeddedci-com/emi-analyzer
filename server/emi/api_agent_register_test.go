package emi

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// registerStore records the Worker handed to UpsertWorker and does nothing else.
//
// Store is a wide interface and this test needs one method of it, so the rest is an embedded
// nil interface: any other call would panic, which is the correct outcome for a test that is
// asserting a handler touches nothing else.
type registerStore struct {
	Store
	got *Worker
}

func (s *registerStore) UpsertWorker(_ context.Context, w *Worker) error {
	s.got = w
	return nil
}

// The register handler touches neither of these; they exist because New requires them.
type stubKeys struct{}

func (stubKeys) VerifyAgentKey(context.Context, string) (AgentKey, error) {
	return AgentKey{}, ErrKeyNotFound
}

type stubBlob struct{}

func (stubBlob) PresignGet(context.Context, string, time.Duration) (string, error) { return "", nil }
func (stubBlob) PresignPut(context.Context, string, string, time.Duration) (string, error) {
	return "", nil
}
func (stubBlob) Stat(context.Context, string) (int64, string, error) { return 0, "", nil }

// A worker's organisation must come from the key that was verified, not from a lookup the
// store performs for itself.
//
// This is a regression test for a live 404. UpsertWorker used to resolve the organisation by
// querying emi.emi_dev_api_keys -- the table the standalone DevKeyVerifier writes. Mounted
// into a host with its own key table, keys live there instead, so the lookup
// matched nothing, returned ErrNotFound, and every worker registration answered
// 404 {"error":"not found"}. Only the host knows where its keys live; the organisation has to
// travel with the request.
func TestWorkerRegisterCarriesTheKeysOrganisation(t *testing.T) {
	store := &registerStore{}
	svc, err := New(Deps{
		Store:       store,
		Keys:        stubKeys{},
		Blob:        stubBlob{},
		TokenSecret: bytes.Repeat([]byte("k"), 32),
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	body, _ := json.Marshal(map[string]any{
		"name":         "small-ingest",
		"capabilities": map[string]any{"cores": 1, "ram_gb": 0.5, "kinds": []string{"ingest"}},
	})
	req := httptest.NewRequest(http.MethodPost, "/emi-agent/register", bytes.NewReader(body))
	req = req.WithContext(context.WithValue(req.Context(), ctxKeyAgent,
		AgentKey{Kid: "kid-1", OrganizationID: "org-1", AgentType: AgentTypeEMI}))
	w := httptest.NewRecorder()
	svc.handleWorkerRegister(w, req)

	if w.Code != http.StatusOK && w.Code != http.StatusCreated {
		t.Fatalf("register: got %d body=%s", w.Code, w.Body.String())
	}
	if store.got == nil {
		t.Fatal("the handler never reached the store")
	}
	if store.got.OrganizationID != "org-1" {
		t.Fatalf("organisation: got %q want org-1 -- the worker would serve the wrong org, or none",
			store.got.OrganizationID)
	}
	if store.got.APIKeyKid != "kid-1" {
		t.Fatalf("kid: got %q want kid-1", store.got.APIKeyKid)
	}
}

// A key with no organisation is a shared worker serving everyone; that must survive as empty
// rather than being turned into a lookup failure.
func TestWorkerRegisterAllowsAnUnscopedKey(t *testing.T) {
	store := &registerStore{}
	svc, err := New(Deps{
		Store: store, Keys: stubKeys{}, Blob: stubBlob{},
		TokenSecret: bytes.Repeat([]byte("k"), 32),
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	body, _ := json.Marshal(map[string]any{
		"name":         "shared",
		"capabilities": map[string]any{"cores": 8, "ram_gb": 32},
	})
	req := httptest.NewRequest(http.MethodPost, "/emi-agent/register", bytes.NewReader(body))
	req = req.WithContext(context.WithValue(req.Context(), ctxKeyAgent,
		AgentKey{Kid: "kid-2", AgentType: AgentTypeEMI}))
	w := httptest.NewRecorder()
	svc.handleWorkerRegister(w, req)

	if store.got == nil {
		t.Fatal("the handler never reached the store")
	}
	if store.got.OrganizationID != "" {
		t.Fatalf("organisation: got %q want empty (serves every organisation)", store.got.OrganizationID)
	}
}
