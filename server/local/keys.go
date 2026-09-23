package local

import (
	"context"
	"database/sql"
	"errors"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

// Keys issues and verifies worker keys against the local database.
//
// The scheme is emi's (agentkey.go) -- eci_<kid>_<secret>, only a keyed hash of the secret
// stored, and a missing key indistinguishable from a wrong secret -- so the worker is the same
// program whether it dials into this app or into a hosted server.
type Keys struct {
	db     *sql.DB
	pepper []byte
}

func NewKeys(db *sql.DB, pepper []byte) *Keys { return &Keys{db: db, pepper: pepper} }

var _ emi.KeyVerifier = (*Keys)(nil)

// Issue mints a key and returns the raw string, the only time it is visible.
func (k *Keys) Issue(ctx context.Context, orgID, name string) (string, error) {
	kid, hash, raw, err := emi.NewAgentKey(k.pepper)
	if err != nil {
		return "", err
	}
	_, err = k.db.ExecContext(ctx, `
		INSERT INTO emi_api_keys (kid, hash, organization_id, agent_type, name, created_at)
		VALUES (?,?,?,'emi',?,?)`, kid, hash, orgID, name, ts(time.Now()))
	if err != nil {
		return "", err
	}
	return raw, nil
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
	return emi.VerifyAgentKeyWith(k.pepper, raw, func(kid string) (emi.StoredAgentKey, error) {
		var row emi.StoredAgentKey
		var revoked sql.NullString
		err := k.db.QueryRowContext(ctx, `
			SELECT hash, organization_id, agent_type, name, revoked_at
			FROM emi_api_keys WHERE kid = ?`, kid).
			Scan(&row.Hash, &row.OrganizationID, &row.AgentType, &row.Name, &revoked)
		if errors.Is(err, sql.ErrNoRows) {
			return row, emi.ErrKeyNotFound
		}
		row.Revoked = revoked.Valid
		return row, err
	})
}
