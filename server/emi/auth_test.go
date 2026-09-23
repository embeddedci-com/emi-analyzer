package emi

import (
	"bytes"
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// tokenStore holds runs by id and nothing else.
type tokenStore struct {
	Store
	runs map[string]*Run
}

func (s *tokenStore) GetRun(_ context.Context, id string) (*Run, error) {
	if r, ok := s.runs[id]; ok {
		return r, nil
	}
	return nil, ErrNotFound
}

// keysFunc lets a test decide what a key lookup answers.
type keysFunc func(raw string) (AgentKey, error)

func (f keysFunc) VerifyAgentKey(_ context.Context, raw string) (AgentKey, error) { return f(raw) }

var testSecret = bytes.Repeat([]byte("k"), 32)

func tokenService(t *testing.T, keys KeyVerifier, now time.Time) (*Service, *http.ServeMux) {
	t.Helper()
	store := &tokenStore{runs: map[string]*Run{
		"r1":     {ID: "r1", Kind: RunKindIngest, Status: StatusInProgress, JTIKey: "jti-1", OwnerAPIKeyKid: "k"},
		"r2":     {ID: "r2", Kind: RunKindIngest, Status: StatusInProgress, JTIKey: "jti-2", OwnerAPIKeyKid: "k"},
		"orphan": {ID: "orphan", Kind: RunKindIngest, Status: StatusRetryPending},
	}}
	if keys == nil {
		keys = stubKeys{}
	}
	svc, err := New(Deps{Store: store, Keys: keys, Blob: stubBlob{}, TokenSecret: testSecret,
		Now: func() time.Time { return now }})
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	svc.Mount(mux, "/api", nil)
	return svc, mux
}

func call(mux *http.ServeMux, method, path, token string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, strings.NewReader("{}"))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, req)
	return w
}

func signed(t *testing.T, method jwt.SigningMethod, key any, c runClaims) string {
	t.Helper()
	s, err := jwt.NewWithClaims(method, c).SignedString(key)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

// Every way a run token can be wrong, and the one way it is right.
func TestRunTokenChecks(t *testing.T) {
	now := time.Date(2026, 9, 23, 12, 0, 0, 0, time.UTC)
	svc, mux := tokenService(t, nil, now)

	good, _, err := svc.mintRunToken("r1", "org", "jti-1")
	if err != nil {
		t.Fatal(err)
	}
	claims := func(run, jti string, exp time.Time) runClaims {
		return runClaims{TokenType: TokenTypeEMIRun, RunID: run, OrgID: "org",
			RegisteredClaims: jwt.RegisteredClaims{ID: jti, ExpiresAt: jwt.NewNumericDate(exp)}}
	}
	later := now.Add(time.Hour)
	noType := claims("r1", "jti-1", later)
	noType.TokenType = "agent_job"
	noExp := claims("r1", "jti-1", later)
	noExp.ExpiresAt = nil

	for _, tc := range []struct {
		name, path, token string
		want              int
	}{
		{"valid", "/api/emi-agent/runs/r1/claim", good, http.StatusOK},
		{"missing", "/api/emi-agent/runs/r1/claim", "", http.StatusUnauthorized},
		{"garbage", "/api/emi-agent/runs/r1/claim", "not-a-jwt", http.StatusUnauthorized},
		{"another run's token", "/api/emi-agent/runs/r2/claim", good, http.StatusForbidden},
		{"superseded jti", "/api/emi-agent/runs/r1/claim",
			signed(t, jwt.SigningMethodHS256, testSecret, claims("r1", "jti-old", later)), http.StatusConflict},
		{"run nobody holds", "/api/emi-agent/runs/orphan/claim",
			signed(t, jwt.SigningMethodHS256, testSecret, claims("orphan", "jti-1", later)), http.StatusConflict},
		{"expired", "/api/emi-agent/runs/r1/claim",
			signed(t, jwt.SigningMethodHS256, testSecret, claims("r1", "jti-1", now.Add(-time.Second))), http.StatusUnauthorized},
		{"no expiry", "/api/emi-agent/runs/r1/claim",
			signed(t, jwt.SigningMethodHS256, testSecret, noExp), http.StatusUnauthorized},
		{"HS512", "/api/emi-agent/runs/r1/claim",
			signed(t, jwt.SigningMethodHS512, testSecret, claims("r1", "jti-1", later)), http.StatusUnauthorized},
		{"alg none", "/api/emi-agent/runs/r1/claim",
			signed(t, jwt.SigningMethodNone, jwt.UnsafeAllowNoneSignatureType, claims("r1", "jti-1", later)), http.StatusUnauthorized},
		{"wrong secret", "/api/emi-agent/runs/r1/claim",
			signed(t, jwt.SigningMethodHS256, bytes.Repeat([]byte("x"), 32), claims("r1", "jti-1", later)), http.StatusUnauthorized},
		{"wrong token type", "/api/emi-agent/runs/r1/claim",
			signed(t, jwt.SigningMethodHS256, testSecret, noType), http.StatusUnauthorized},
	} {
		if w := call(mux, "POST", tc.path, tc.token); w.Code != tc.want {
			t.Errorf("%s: got %d %s, want %d", tc.name, w.Code, w.Body.String(), tc.want)
		}
	}
}

// A key that does not verify is 401 and a key lookup that fails is 500. The second used to be
// a 403 "invalid agent key" too, which sends an operator off to rotate a key that was fine.
func TestWorkerKeyErrors(t *testing.T) {
	for _, tc := range []struct {
		name string
		keys keysFunc
		want int
	}{
		{"unknown key", func(string) (AgentKey, error) { return AgentKey{}, ErrKeyNotFound }, http.StatusUnauthorized},
		{"store down", func(string) (AgentKey, error) { return AgentKey{}, errors.New("connection refused") }, http.StatusInternalServerError},
		{"build key", func(string) (AgentKey, error) { return AgentKey{Kid: "k", AgentType: "build"}, nil }, http.StatusForbidden},
	} {
		_, mux := tokenService(t, tc.keys, time.Now())
		for _, path := range []string{"/api/emi-agent/runs", "/api/emi-agent/ws"} {
			if w := call(mux, "GET", path, "eci_k_s"); w.Code != tc.want {
				t.Errorf("%s on %s: got %d %s, want %d", tc.name, path, w.Code, w.Body.String(), tc.want)
			}
		}
	}
	_, mux := tokenService(t, nil, time.Now())
	if w := call(mux, "GET", "/api/emi-agent/runs", ""); w.Code != http.StatusUnauthorized {
		t.Errorf("no key: got %d, want 401", w.Code)
	}
}
