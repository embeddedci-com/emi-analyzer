package emi

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The server is not a third copy of the driver validator (see drivers.go). It checks the
// envelope, so these tests say exactly that: every document the other two accept must be
// storable, and the envelope-level refusals must match.
//
// The rest of testdata/driver_document_fixtures.json -- rise time against period, samples
// against one full period, sources on every number -- is deliberately NOT asserted here. A
// document that is structurally sound and physically nonsense reaches the worker, which is
// the only place that can tell and the only place that has the fixture for it.

type driverFixtures struct {
	Valid []struct {
		Name     string          `json:"name"`
		Document json.RawMessage `json:"document"`
		Expected struct {
			Kind       string `json:"kind"`
			Role       string `json:"role"`
			DriverName string `json:"driver_name"`
		} `json:"expected"`
	} `json:"valid"`
	Invalid []struct {
		Name        string          `json:"name"`
		Document    json.RawMessage `json:"document"`
		MustMention string          `json:"must_mention"`
	} `json:"invalid"`
}

// envelopeCases are the invalid fixtures the server is responsible for catching. The others
// are physics, and naming them here rather than inferring them keeps the split explicit: if
// a new fixture appears, this list is what someone has to think about.
var envelopeCases = map[string]bool{
	"wrong-format":   true,
	"future-version": true,
	"no-name":        true,
	"unknown-kind":   true,
	"unknown-role":   true,
}

func loadDriverFixtures(t *testing.T) driverFixtures {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "driver_document_fixtures.json"))
	if err != nil {
		t.Fatalf("reading fixtures: %v", err)
	}
	var f driverFixtures
	if err := json.Unmarshal(raw, &f); err != nil {
		t.Fatalf("parsing fixtures: %v", err)
	}
	if len(f.Valid) == 0 || len(f.Invalid) == 0 {
		t.Fatal("fixtures carry no cases")
	}
	return f
}

func TestValidateDriverDocumentAcceptsEveryValidFixture(t *testing.T) {
	for _, c := range loadDriverFixtures(t).Valid {
		t.Run(c.Name, func(t *testing.T) {
			env, err := validateDriverDocument(c.Document)
			if err != nil {
				t.Fatalf("rejected a document the worker and the browser both accept: %v", err)
			}
			if env.Kind != c.Expected.Kind {
				t.Errorf("kind = %q, want %q", env.Kind, c.Expected.Kind)
			}
			if env.Role != c.Expected.Role {
				t.Errorf("role = %q, want %q", env.Role, c.Expected.Role)
			}
			if env.Name != c.Expected.DriverName {
				t.Errorf("name = %q, want %q", env.Name, c.Expected.DriverName)
			}
		})
	}
}

func TestValidateDriverDocumentRefusesTheEnvelopeCases(t *testing.T) {
	f := loadDriverFixtures(t)
	seen := map[string]bool{}
	for _, c := range f.Invalid {
		if !envelopeCases[c.Name] {
			continue
		}
		seen[c.Name] = true
		t.Run(c.Name, func(t *testing.T) {
			if _, err := validateDriverDocument(c.Document); err == nil {
				t.Fatal("accepted; want an error")
			}
		})
	}
	for name := range envelopeCases {
		if !seen[name] {
			t.Errorf("fixture %q has disappeared; the envelope split needs revisiting", name)
		}
	}
}

func TestValidateDriverDocumentLeavesPhysicsToTheWorker(t *testing.T) {
	// A document with an impossible edge is structurally fine. The server stores it and the
	// worker refuses it, which is the intended division -- asserted so that someone adding a
	// third validator here has to delete this test first and think about why.
	f := loadDriverFixtures(t)
	for _, c := range f.Invalid {
		if c.Name != "edges-do-not-fit" {
			continue
		}
		if _, err := validateDriverDocument(c.Document); err != nil {
			t.Fatalf("the server rejected physics it does not own: %v", err)
		}
		return
	}
	t.Fatal("the edges-do-not-fit fixture has gone")
}

func TestValidateDriverDocumentRefusesAnOversizedDocument(t *testing.T) {
	big := make([]byte, maxDriverDocumentBytes+1)
	for i := range big {
		big[i] = ' '
	}
	copy(big, []byte(`{"format":"emi-driver","version":1,"name":"x","kind":"trapezoid"}`))
	if _, err := validateDriverDocument(big); err == nil {
		t.Fatal("accepted an oversized document")
	} else if !strings.Contains(err.Error(), "over the") {
		t.Errorf("error = %q, want it to mention the limit", err)
	}
}

func TestValidateDriverDocumentRefusesNonJSON(t *testing.T) {
	if _, err := validateDriverDocument([]byte("not json at all")); err == nil {
		t.Fatal("accepted bytes that are not JSON")
	}
}

func TestValidateDriverDocumentDefaultsTheRole(t *testing.T) {
	// docs/emi-driver-format.md shows role as optional; a document without one is a signal driver.
	env, err := validateDriverDocument([]byte(
		`{"format":"emi-driver","version":1,"name":"clk","kind":"trapezoid"}`))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if env.Role != "signal" {
		t.Errorf("role = %q, want signal", env.Role)
	}
}
