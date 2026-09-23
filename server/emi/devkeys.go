package emi

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// DevKeyVerifier is the KeyVerifier for the standalone Postgres stack (cmd/emi-server).
//
// It exists so `make up` can issue a worker key without a host application's key table. A host
// that mounts this package passes its own KeyVerifier, and nothing here is used.
//
// The scheme is the one in agentkey.go: keys look like eci_<kid>_<secret>, only a keyed hash
// of the secret is stored, and lookup failure is indistinguishable from a wrong secret.
type DevKeyVerifier struct {
	pool   *pgxpool.Pool
	pepper []byte
}

func NewDevKeyVerifier(pool *pgxpool.Pool, pepper []byte) *DevKeyVerifier {
	return &DevKeyVerifier{pool: pool, pepper: pepper}
}

// IssueKey mints a new worker key and returns the raw string, which is the only time it is
// ever visible.
func (v *DevKeyVerifier) IssueKey(ctx context.Context, orgID, name string) (string, error) {
	kid, hash, raw, err := NewAgentKey(v.pepper)
	if err != nil {
		return "", err
	}
	_, err = v.pool.Exec(ctx, `
		INSERT INTO emi.emi_dev_api_keys (kid, hash, organization_id, agent_type, name)
		VALUES ($1,$2,$3,'emi',$4)`,
		kid, hash, orgID, name)
	if err != nil {
		return "", err
	}
	return raw, nil
}

// VerifyAgentKey implements KeyVerifier.
func (v *DevKeyVerifier) VerifyAgentKey(ctx context.Context, raw string) (AgentKey, error) {
	return VerifyAgentKeyWith(v.pepper, raw, func(kid string) (StoredAgentKey, error) {
		var row StoredAgentKey
		var revoked *string
		err := v.pool.QueryRow(ctx, `
			SELECT hash, organization_id, agent_type, name, revoked_at::text
			FROM emi.emi_dev_api_keys WHERE kid = $1`, kid).
			Scan(&row.Hash, &row.OrganizationID, &row.AgentType, &row.Name, &revoked)
		if errors.Is(err, pgx.ErrNoRows) {
			return row, ErrKeyNotFound
		}
		row.Revoked = revoked != nil
		return row, err
	})
}
