package emi

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strings"
	"time"
)

// Signed-out visitors get an organisation of their own.
//
// The tool is public: you can open it, upload a board and read the findings without an
// account, and that is deliberate. What is not acceptable is doing that in a space shared
// with every other signed-out visitor, because a board is somebody's unreleased layout. So
// the first request without a credential is given a random visitor id in a cookie, and that
// id is the organisation.
//
// The cookie is signed. Unsigned it would be a header by another name: a visitor could type
// in somebody else's id and read their boards. Signed, the only ids that verify are ones
// this server minted.
//
// It is a browser-scoped identity, not an account. Clearing cookies loses the boards and
// another device is another visitor — which is what signing in fixes, and the reason the UI
// says which of the two is happening.
//
// This lives here rather than in a host because it is the analyzer's own policy about its
// own users. Both hosts — the mounted integration and the development harness — use it, so
// "signed out" means one thing.
const (
	VisitorCookieName = "emi_visitor"
	// VisitorOrgPrefix marks an organisation id as belonging to a signed-out visitor rather
	// than to a real organisation. Real ids are opaque, so the prefix is what makes the two
	// distinguishable in logs and in the database.
	VisitorOrgPrefix = "anon-"
	visitorTTL       = 365 * 24 * time.Hour
)

// Visitors mints and reads the signed-out visitor cookie.
type Visitors struct {
	// Secret signs the cookie. Any stable secret will do; changing it does not delete
	// anybody's boards, but it does make their cookie unverifiable, so they get a fresh
	// empty space and cannot reach the old one.
	Secret []byte
}

// Identify returns the identity for a request that carries no credential, plus a cookie to
// set when this is a first visit. The cookie is returned rather than written because the
// caller owns the ResponseWriter.
func (v Visitors) Identify(r *http.Request) (UserIdentity, *http.Cookie) {
	if id := v.read(r); id != "" {
		return v.identity(id), nil
	}
	id, cookie := v.mint(r)
	if id == "" {
		// Randomness failed. Serving one degraded request beats inventing a shared
		// identity, so this one gets a space nothing accumulates in.
		return v.identity("none"), nil
	}
	return v.identity(id), cookie
}

func (v Visitors) identity(id string) UserIdentity {
	return UserIdentity{
		UserID:         VisitorOrgPrefix + id,
		OrganizationID: VisitorOrgPrefix + id,
		Anonymous:      true,
	}
}

func (v Visitors) read(r *http.Request) string {
	c, err := r.Cookie(VisitorCookieName)
	if err != nil || c.Value == "" {
		return ""
	}
	id, sig, ok := strings.Cut(c.Value, ".")
	if !ok || id == "" {
		return ""
	}
	want, err := hex.DecodeString(sig)
	if err != nil {
		return ""
	}
	if !hmac.Equal(v.sign(id), want) {
		return ""
	}
	return id
}

func (v Visitors) mint(r *http.Request) (string, *http.Cookie) {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return "", nil
	}
	id := hex.EncodeToString(buf)
	return id, &http.Cookie{
		Name:     VisitorCookieName,
		Value:    id + "." + hex.EncodeToString(v.sign(id)),
		Path:     "/",
		MaxAge:   int(visitorTTL.Seconds()),
		HttpOnly: true,
		// Lax rather than Strict: the tool is reached by following a link, and Strict would
		// give that arrival a fresh identity and an empty project list.
		SameSite: http.SameSiteLaxMode,
		// A reverse proxy terminates TLS, so the request arriving here is plain HTTP.
		Secure: r.TLS != nil || strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https"),
	}
}

func (v Visitors) sign(id string) []byte {
	m := hmac.New(sha256.New, v.Secret)
	m.Write([]byte(id))
	return m.Sum(nil)
}
