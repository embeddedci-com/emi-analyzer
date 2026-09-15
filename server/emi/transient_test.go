package emi

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func fakeStat(sizes map[string]int64) statFunc {
	return func(_ context.Context, key string) (int64, string, error) {
		if n, ok := sizes[key]; ok {
			return n, "text/plain", nil
		}
		return 0, "", errors.New("not found")
	}
}

const orgKey = "uploads/org-1/sha256/abc/usblc6.lib"

func TestTransientParamsDefaults(t *testing.T) {
	p, err := validateTransientParams(context.Background(), nil, "org-1", fakeStat(nil))
	if err != nil {
		t.Fatalf("empty params: %v", err)
	}
	if p.Level != 4 || p.Polarity != "both" || p.Standard != "61000-4-2" {
		t.Fatalf("defaults: got %+v", p)
	}
}

func TestTransientParamsAcceptsAModelFromTheOrganisation(t *testing.T) {
	raw := json.RawMessage(`{"level":2,"models":[{"part":"USBLC6-2SC6","key":"` + orgKey +
		`","filename":"USBLC6.LIB","subckt":"USBLC6","pins":{"1":"IO1","2":"GND"}}]}`)
	p, err := validateTransientParams(context.Background(), raw, "org-1", fakeStat(map[string]int64{orgKey: 4096}))
	if err != nil {
		t.Fatalf("valid model rejected: %v", err)
	}
	if len(p.Models) != 1 || p.Level != 2 {
		t.Fatalf("got %+v", p)
	}
}

// A run must not be able to point a worker at another organisation's objects, or anywhere
// else in the bucket, by naming a key.
func TestTransientParamsRefusesKeysOutsideTheOrganisation(t *testing.T) {
	cases := map[string]string{
		"another org":      "uploads/org-2/sha256/abc/model.lib",
		"a board artifact": "runs/r1/board.json",
		"path traversal":   "uploads/org-1/../org-2/model.lib",
		"a bare prefix":    "uploads/org-10/model.lib",
	}
	for name, key := range cases {
		t.Run(name, func(t *testing.T) {
			raw := json.RawMessage(`{"models":[{"part":"X","key":"` + key + `","filename":"m.lib"}]}`)
			_, err := validateTransientParams(context.Background(), raw, "org-1", fakeStat(map[string]int64{key: 10}))
			if err == nil || !strings.Contains(err.Error(), "uploaded to this project") {
				t.Fatalf("key %q: got %v", key, err)
			}
		})
	}
}

func TestTransientParamsRejections(t *testing.T) {
	stat := fakeStat(map[string]int64{orgKey: 4096, "uploads/org-1/big.lib": 3 << 20})
	cases := []struct {
		name, raw, want string
	}{
		{"level", `{"level":5}`, "level must be"},
		{"polarity", `{"polarity":"sideways"}`, "polarity must be"},
		{"standard", `{"standard":"61000-4-4"}`, "standard must be"},
		{"not a library", `{"models":[{"part":"X","key":"` + orgKey + `","filename":"board.kicad_pcb"}]}`, "does not look like a SPICE library"},
		{"missing file", `{"models":[{"part":"X","key":"uploads/org-1/gone.lib","filename":"gone.lib"}]}`, "not found"},
		{"too large", `{"models":[{"part":"X","key":"uploads/org-1/big.lib","filename":"big.lib"}]}`, "between 1 byte"},
		{"no part", `{"models":[{"part":" ","key":"` + orgKey + `","filename":"m.lib"}]}`, "part must be"},
		{"duplicate part", `{"models":[{"part":"X","key":"` + orgKey + `","filename":"a.lib"},{"part":"x","key":"` + orgKey + `","filename":"b.lib"}]}`, "already has a model"},
		{"malformed", `{"level":"high"}`, "not valid"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := validateTransientParams(context.Background(), json.RawMessage(c.raw), "org-1", stat)
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("want error containing %q, got %v", c.want, err)
			}
		})
	}
}
