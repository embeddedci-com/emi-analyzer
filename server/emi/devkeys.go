package emi

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// DevKeyVerifier is the KeyVerifier for the standalone local stack.
//
// It exists only so `make up` can issue a worker key without dragging in
// embeddedci-server's api_keys table and pepper handling. When this package is mounted into
// embeddedci-server, that server passes its own KeyVerifier and none of this compiles into
// the deployment.
//
// The scheme deliberately mirrors the real one: keys look like eci_<kid>_<secret>, only a
// keyed hash of the secret is stored, and lookup failure is indistinguishable from a wrong
// secret.
type DevKeyVerifier struct {
	pool   *pgxpool.Pool
	pepper []byte
}

func NewDevKeyVerifier(pool *pgxpool.Pool, pepper []byte) *DevKeyVerifier {
	return &DevKeyVerifier{pool: pool, pepper: pepper}
}

func hashSecret(pepper []byte, secret string) string {
	m := hmac.New(sha256.New, pepper)
	m.Write([]byte(secret))
	return hex.EncodeToString(m.Sum(nil))
}

// IssueKey mints a new worker key and returns the raw string, which is the only time it is
// ever visible.
func (v *DevKeyVerifier) IssueKey(ctx context.Context, orgID, name string) (string, error) {
	kidBytes := make([]byte, 8)
	secretBytes := make([]byte, 32)
	if _, err := rand.Read(kidBytes); err != nil {
		return "", err
	}
	if _, err := rand.Read(secretBytes); err != nil {
		return "", err
	}
	kid := hex.EncodeToString(kidBytes)
	secret := hex.EncodeToString(secretBytes)

	_, err := v.pool.Exec(ctx, `
		INSERT INTO emi.emi_dev_api_keys (kid, hash, organization_id, agent_type, name)
		VALUES ($1,$2,$3,'emi',$4)`,
		kid, hashSecret(v.pepper, secret), orgID, name)
	if err != nil {
		return "", err
	}
	return "eci_" + kid + "_" + secret, nil
}

// VerifyAgentKey implements KeyVerifier.
func (v *DevKeyVerifier) VerifyAgentKey(ctx context.Context, raw string) (AgentKey, error) {
	parts := strings.Split(strings.TrimSpace(raw), "_")
	if len(parts) != 3 || parts[0] != "eci" {
		return AgentKey{}, ErrKeyNotFound
	}
	kid, secret := parts[1], parts[2]

	var storedHash, orgID, agentType, name string
	var revoked *string
	err := v.pool.QueryRow(ctx, `
		SELECT hash, organization_id, agent_type, name, revoked_at::text
		FROM emi.emi_dev_api_keys WHERE kid = $1`, kid).
		Scan(&storedHash, &orgID, &agentType, &name, &revoked)
	if err != nil {
		if err == pgx.ErrNoRows {
			return AgentKey{}, ErrKeyNotFound
		}
		return AgentKey{}, err
	}
	if revoked != nil {
		return AgentKey{}, ErrKeyNotFound
	}
	// Constant time, so the comparison cannot be used as an oracle for guessing secrets.
	if !hmac.Equal([]byte(storedHash), []byte(hashSecret(v.pepper, secret))) {
		return AgentKey{}, ErrKeyNotFound
	}
	return AgentKey{Kid: kid, OrganizationID: orgID, AgentType: agentType, Name: name}, nil
}
