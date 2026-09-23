package emi

import (
	"errors"
	"strings"
	"testing"
)

func TestAgentKeyScheme(t *testing.T) {
	pepper := []byte(strings.Repeat("p", 32))
	kid, hash, raw, err := NewAgentKey(pepper)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(raw, "eci_"+kid+"_") || strings.Contains(hash, strings.TrimPrefix(raw, "eci_"+kid+"_")) {
		t.Fatalf("raw %q, hash %q", raw, hash)
	}
	stored := StoredAgentKey{Hash: hash, AgentKey: AgentKey{OrganizationID: "org", AgentType: AgentTypeEMI, Name: "w"}}
	lookup := func(row StoredAgentKey, err error) func(string) (StoredAgentKey, error) {
		return func(got string) (StoredAgentKey, error) {
			if got != kid {
				return StoredAgentKey{}, ErrKeyNotFound
			}
			return row, err
		}
	}

	key, err := VerifyAgentKeyWith(pepper, raw, lookup(stored, nil))
	if err != nil || key.Kid != kid || key.OrganizationID != "org" || key.AgentType != AgentTypeEMI {
		t.Fatalf("valid key = %+v, %v", key, err)
	}

	revoked := stored
	revoked.Revoked = true
	for name, tc := range map[string]struct {
		raw    string
		pepper []byte
		lookup func(string) (StoredAgentKey, error)
	}{
		"malformed":    {"eci_" + kid, pepper, lookup(stored, nil)},
		"empty secret": {"eci_" + kid + "_", pepper, lookup(stored, nil)},
		"wrong prefix": {strings.Replace(raw, "eci_", "abc_", 1), pepper, lookup(stored, nil)},
		"unknown kid":  {"eci_ffff_" + strings.Repeat("0", 64), pepper, lookup(stored, nil)},
		"wrong secret": {raw + "0", pepper, lookup(stored, nil)},
		"wrong pepper": {raw, []byte(strings.Repeat("q", 32)), lookup(stored, nil)},
		"revoked":      {raw, pepper, lookup(revoked, nil)},
	} {
		if _, err := VerifyAgentKeyWith(tc.pepper, tc.raw, tc.lookup); !errors.Is(err, ErrKeyNotFound) {
			t.Errorf("%s: got %v, want ErrKeyNotFound", name, err)
		}
	}

	// A store failure is not a bad key, and must not be reported as one.
	down := errors.New("connection refused")
	if _, err := VerifyAgentKeyWith(pepper, raw, lookup(StoredAgentKey{}, down)); !errors.Is(err, down) || errors.Is(err, ErrKeyNotFound) {
		t.Fatalf("store failure: got %v, want it passed through", err)
	}
}
