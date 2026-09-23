package emi

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"strings"
)

// The worker key scheme both built-in key stores use: DevKeyVerifier on Postgres and the local
// app's on SQLite. Keys look like eci_<kid>_<secret>; only an HMAC of the secret under a
// server-side pepper is stored, so a copy of the table is not a copy of the keys.
//
// It lives here once because the comparison is the security-critical part, and two copies of
// it had already been written. A host with its own key table is free to ignore all of this and
// implement KeyVerifier however it likes.

// StoredAgentKey is the row a key store holds for one kid.
type StoredAgentKey struct {
	AgentKey
	// Hash is HashAgentKeySecret of the key's secret.
	Hash    string
	Revoked bool
}

// NewAgentKey generates a key. raw is what the worker is given, the only time it is ever
// visible; kid and hash are what the store keeps.
func NewAgentKey(pepper []byte) (kid, hash, raw string, err error) {
	kidBytes, secretBytes := make([]byte, 8), make([]byte, 32)
	if _, err := rand.Read(kidBytes); err != nil {
		return "", "", "", err
	}
	if _, err := rand.Read(secretBytes); err != nil {
		return "", "", "", err
	}
	kid, secret := hex.EncodeToString(kidBytes), hex.EncodeToString(secretBytes)
	return kid, HashAgentKeySecret(pepper, secret), "eci_" + kid + "_" + secret, nil
}

// HashAgentKeySecret is the keyed hash a store keeps in place of a key's secret.
func HashAgentKeySecret(pepper []byte, secret string) string {
	m := hmac.New(sha256.New, pepper)
	m.Write([]byte(secret))
	return hex.EncodeToString(m.Sum(nil))
}

// VerifyAgentKeyWith checks raw against the row lookup returns for its kid.
//
// lookup returns ErrKeyNotFound when there is no such kid; any other error is passed through
// as a failure of the store, not of the key. A malformed key, an unknown kid, a revoked key
// and a wrong secret all come back as ErrKeyNotFound, so a caller cannot tell them apart and
// cannot use the answer to find valid key ids.
func VerifyAgentKeyWith(pepper []byte, raw string, lookup func(kid string) (StoredAgentKey, error)) (AgentKey, error) {
	parts := strings.Split(strings.TrimSpace(raw), "_")
	if len(parts) != 3 || parts[0] != "eci" || parts[1] == "" || parts[2] == "" {
		return AgentKey{}, ErrKeyNotFound
	}
	kid, secret := parts[1], parts[2]
	row, err := lookup(kid)
	if err != nil {
		return AgentKey{}, err
	}
	if row.Revoked {
		return AgentKey{}, ErrKeyNotFound
	}
	// Constant time, so the comparison cannot be used as an oracle for guessing secrets.
	if !hmac.Equal([]byte(row.Hash), []byte(HashAgentKeySecret(pepper, secret))) {
		return AgentKey{}, ErrKeyNotFound
	}
	row.AgentKey.Kid = kid
	return row.AgentKey, nil
}
