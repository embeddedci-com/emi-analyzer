package local

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"strings"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

// Keys issues and verifies worker keys against the local database.
//
// The scheme is the one every other host uses -- eci_<kid>_<secret>, only a keyed hash of the
// secret stored, and a missing key indistinguishable from a wrong secret -- so the worker is
// the same program whether it dials into this app or into a hosted server.
type Keys struct {
	db     *sql.DB
	pepper []byte
}

func NewKeys(db *sql.DB, pepper []byte) *Keys { return &Keys{db: db, pepper: pepper} }

var _ emi.KeyVerifier = (*Keys)(nil)

func (k *Keys) hash(secret string) string {
	m := hmac.New(sha256.New, k.pepper)
	m.Write([]byte(secret))
	return hex.EncodeToString(m.Sum(nil))
}

// Issue mints a key and returns the raw string, the only time it is visible.
func (k *Keys) Issue(ctx context.Context, orgID, name string) (string, error) {
	kidBytes, secretBytes := make([]byte, 8), make([]byte, 32)
	if _, err := rand.Read(kidBytes); err != nil {
		return "", err
	}
	if _, err := rand.Read(secretBytes); err != nil {
		return "", err
	}
	kid, secret := hex.EncodeToString(kidBytes), hex.EncodeToString(secretBytes)
	_, err := k.db.ExecContext(ctx, `
		INSERT INTO emi_api_keys (kid, hash, organization_id, agent_type, name, created_at)
		VALUES (?,?,?,'emi',?,?)`, kid, k.hash(secret), orgID, name, ts(time.Now()))
	if err != nil {
		return "", err
	}
	return "eci_" + kid + "_" + secret, nil
}

// RevokeNamed revokes every key issued under name. The app issues a fresh key for each worker
// container it starts, so those from an earlier session have nothing left to authenticate --
// while a key issued by hand for a worker the user runs themselves is left alone.
func (k *Keys) RevokeNamed(ctx context.Context, name string) error {
	_, err := k.db.ExecContext(ctx,
		`UPDATE emi_api_keys SET revoked_at = ? WHERE revoked_at IS NULL AND name = ?`, ts(time.Now()), name)
	return err
}

func (k *Keys) VerifyAgentKey(ctx context.Context, raw string) (emi.AgentKey, error) {
	parts := strings.Split(strings.TrimSpace(raw), "_")
	if len(parts) != 3 || parts[0] != "eci" {
		return emi.AgentKey{}, emi.ErrKeyNotFound
	}
	kid, secret := parts[1], parts[2]

	var stored, orgID, agentType, name string
	var revoked sql.NullString
	err := k.db.QueryRowContext(ctx, `
		SELECT hash, organization_id, agent_type, name, revoked_at
		FROM emi_api_keys WHERE kid = ?`, kid).Scan(&stored, &orgID, &agentType, &name, &revoked)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return emi.AgentKey{}, emi.ErrKeyNotFound
		}
		return emi.AgentKey{}, err
	}
	if revoked.Valid {
		return emi.AgentKey{}, emi.ErrKeyNotFound
	}
	if !hmac.Equal([]byte(stored), []byte(k.hash(secret))) {
		return emi.AgentKey{}, emi.ErrKeyNotFound
	}
	return emi.AgentKey{Kid: kid, OrganizationID: orgID, AgentType: agentType, Name: name}, nil
}
