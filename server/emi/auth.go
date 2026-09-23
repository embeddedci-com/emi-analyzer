package emi

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

type ctxKey int

const (
	ctxKeyAgent ctxKey = iota
	ctxKeyRunID
	ctxKeyUser
)

// TokenTypeEMIRun is the token_type claim on a run-scoped worker token. It mirrors
// embeddedci-server's "agent_job" token type for build jobs.
const TokenTypeEMIRun = "emi_run"

// RunTokenTTL is how long one run token lasts. A solve can outlive it, which is intentional:
// the worker asks for a fresh one at POST .../token/refresh (see handleRefreshRunToken), and a
// stale token cannot be replayed for a day.
const RunTokenTTL = 6 * time.Hour

// UserIdentity is the authenticated human behind a request. The host supplies it; in
// embeddedci-server it comes from the existing JWT middleware.
type UserIdentity struct {
	UserID         string
	OrganizationID string
	// Login is a human-readable name for whoever this is, when the host has one. Shown in
	// the UI so a user can tell which account their boards are being filed under -- which
	// matters most when the answer is "none of yours".
	Login string
	// Anonymous marks a request that carried no credential. The host decides whether to
	// allow those at all; this package only reports it.
	Anonymous bool
}

// WithUser attaches a user identity to a request context. Hosts call this from their own
// auth middleware before delegating to the EMI handlers.
func WithUser(ctx context.Context, u UserIdentity) context.Context {
	return context.WithValue(ctx, ctxKeyUser, u)
}

// UserFrom returns the identity a host attached with WithUser. It is the counterpart to
// that call, for a host that wants to read back what its own middleware resolved.
func UserFrom(ctx context.Context) (UserIdentity, bool) {
	return userFrom(ctx)
}

func userFrom(ctx context.Context) (UserIdentity, bool) {
	u, ok := ctx.Value(ctxKeyUser).(UserIdentity)
	return u, ok
}

func agentFrom(ctx context.Context) (AgentKey, bool) {
	a, ok := ctx.Value(ctxKeyAgent).(AgentKey)
	return a, ok
}

func runIDFrom(ctx context.Context) (string, bool) {
	s, ok := ctx.Value(ctxKeyRunID).(string)
	return s, ok
}

// bearer extracts a token from the Authorization header, falling back to the ?token= query
// parameter. The fallback exists only because browsers cannot set headers on a WebSocket
// handshake; the same fallback is present for build agents.
func bearer(r *http.Request) string {
	if h := r.Header.Get("Authorization"); h != "" {
		if len(h) > 7 && strings.EqualFold(h[:7], "bearer ") {
			return strings.TrimSpace(h[7:])
		}
		return strings.TrimSpace(h)
	}
	return strings.TrimSpace(r.URL.Query().Get("token"))
}

// requireWorkerKey authenticates a long-lived EMI worker API key.
func (s *Service) requireWorkerKey(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		key, ok := s.workerKey(w, r)
		if !ok {
			return
		}
		next(w, r.WithContext(context.WithValue(r.Context(), ctxKeyAgent, key)))
	}
}

// workerKey verifies the worker key on a request, or writes the refusal and returns false.
//
// A key that does not verify is 401: the caller did not authenticate. A key that verifies but
// is not a worker key is 403. A store failure is 500 and logged, never a 401 or 403: telling a
// worker its key is bad because the database blinked makes an operator go and rotate a key
// that was fine. There is no anonymous fallback in any of these cases.
func (s *Service) workerKey(w http.ResponseWriter, r *http.Request) (AgentKey, bool) {
	raw := bearer(r)
	if raw == "" {
		writeErr(w, http.StatusUnauthorized, "missing agent key")
		return AgentKey{}, false
	}
	key, err := s.deps.Keys.VerifyAgentKey(r.Context(), raw)
	switch {
	case errors.Is(err, ErrKeyNotFound):
		// One answer for "no such key", "revoked" and "wrong secret" so the endpoint cannot
		// be used to enumerate valid key ids.
		writeErr(w, http.StatusUnauthorized, "invalid agent key")
		return AgentKey{}, false
	case err != nil:
		s.deps.log().Error("emi: agent key lookup failed", "err", err)
		writeErr(w, http.StatusInternalServerError, "internal error")
		return AgentKey{}, false
	}
	if key.AgentType != AgentTypeEMI {
		writeErr(w, http.StatusForbidden, "key is not an emi worker key")
		return AgentKey{}, false
	}
	return key, true
}

// runClaims is the payload of a short-lived run token.
type runClaims struct {
	TokenType string `json:"token_type"`
	RunID     string `json:"run_id"`
	OrgID     string `json:"org_id"`
	jwt.RegisteredClaims
}

// mintRunToken issues the short-lived token a worker uses for every run-scoped call.
//
// The token deliberately does not carry the worker's API key id. Ownership lives on the run
// row (owner_api_key_kid + jti_key), so a leaked token grants access to exactly one run and
// nothing else — the same property build-job tokens have.
func (s *Service) mintRunToken(runID, orgID, jti string) (string, time.Time, error) {
	now := s.deps.now()
	exp := now.Add(RunTokenTTL)
	tok := jwt.NewWithClaims(jwt.SigningMethodHS256, runClaims{
		TokenType: TokenTypeEMIRun,
		RunID:     runID,
		OrgID:     orgID,
		RegisteredClaims: jwt.RegisteredClaims{
			ID:        jti,
			IssuedAt:  jwt.NewNumericDate(now),
			ExpiresAt: jwt.NewNumericDate(exp),
		},
	})
	str, err := tok.SignedString(s.deps.TokenSecret)
	return str, exp, err
}

var errBadRunToken = errors.New("emi: invalid run token")

func (s *Service) parseRunToken(raw string) (*runClaims, error) {
	var c runClaims
	_, err := jwt.ParseWithClaims(raw, &c, func(t *jwt.Token) (any, error) {
		if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
			return nil, errBadRunToken
		}
		return s.deps.TokenSecret, nil
	}, jwt.WithValidMethods([]string{"HS256"}), jwt.WithTimeFunc(s.deps.now), jwt.WithExpirationRequired())
	if err != nil {
		return nil, err
	}
	if c.TokenType != TokenTypeEMIRun || c.RunID == "" {
		return nil, errBadRunToken
	}
	return &c, nil
}

// requireRunToken authenticates a run-scoped token and checks that it names the run in the
// URL path. Both halves matter: without the path check, a token for run A would work on
// run B.
func (s *Service) requireRunToken(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		raw := bearer(r)
		if raw == "" {
			writeErr(w, http.StatusUnauthorized, "missing run token")
			return
		}
		claims, err := s.parseRunToken(raw)
		if err != nil {
			writeErr(w, http.StatusUnauthorized, "invalid run token")
			return
		}
		pathRun := r.PathValue("run_id")
		if pathRun == "" || pathRun != claims.RunID {
			writeErr(w, http.StatusForbidden, "run token does not match this run")
			return
		}

		// The token proves "whoever minted this owns the run", but ownership can have
		// moved on since — a retry re-mints with a new jti. Checking jti_key here is what
		// stops a worker that was superseded from writing results over the new one's.
		// A run nobody holds (jti_key cleared by a retry) accepts no token at all, or the
		// superseded worker could still upload into a run that is waiting for someone else.
		run, err := s.deps.Store.GetRun(r.Context(), pathRun)
		if err != nil {
			writeStoreErr(w, err)
			return
		}
		if run.JTIKey == "" || run.JTIKey != claims.ID {
			writeErr(w, http.StatusConflict, "run has been reassigned to another worker")
			return
		}

		ctx := context.WithValue(r.Context(), ctxKeyRunID, pathRun)
		next(w, r.WithContext(ctx))
	}
}
