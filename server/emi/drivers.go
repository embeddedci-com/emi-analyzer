package emi

import (
	"encoding/json"
	"fmt"
	"strings"
)

// Driver document handling: the emi-driver format in docs/emi-driver-format.md.
//
// The full validator lives twice, in Python (worker/emi_worker/drivers/document.py) and in
// TypeScript (webapp/src/lib/driverDocument.ts), and both are checked against
// testdata/driver_document_fixtures.json. This is deliberately NOT a third copy.
//
// What the server owes the system is different from what those two owe it. The browser tells
// someone their numbers do not make sense while they are typing. The worker refuses a
// document that reaches it any other way, and it is the only place that can, since it is the
// one that turns the numbers into physics. The server's job is narrower: keep the store
// coherent. So it checks the envelope -- format, version, size, and the three fields it lifts
// into columns -- and leaves rise-time-against-period to the two implementations that already
// agree on it.
//
// Writing a third full validator here would be a third thing to keep in step, and the first
// to drift, because Go has no fixture for the physics and no reason to grow one.

const (
	driverFormat = "emi-driver"
	// driverVersion is the newest document this build stores. A newer one is refused rather
	// than accepted and half-understood.
	driverVersion = 1
	// maxDriverDocumentBytes matches docs/emi-driver-format.md and the two validators.
	maxDriverDocumentBytes = 2 << 20
)

var (
	driverKinds = map[string]bool{"trapezoid": true, "waveform": true, "spectrum": true}
	driverRoles = map[string]bool{"signal": true, "switching-regulator": true}
)

// driverEnvelope is the part of an emi-driver document the server reads.
type driverEnvelope struct {
	Format  string `json:"format"`
	Version int    `json:"version"`
	Name    string `json:"name"`
	Kind    string `json:"kind"`
	Role    string `json:"role"`
}

// validateDriverDocument checks the envelope of a driver document and returns the fields the
// row lifts into columns. raw is the exact bytes received, so the size cap applies to what
// arrived rather than to a re-encoding of it.
func validateDriverDocument(raw []byte) (driverEnvelope, error) {
	var env driverEnvelope
	if len(raw) > maxDriverDocumentBytes {
		return env, fmt.Errorf("driver document is %.1f MB, over the %d MB limit",
			float64(len(raw))/1e6, maxDriverDocumentBytes>>20)
	}
	if !json.Valid(raw) {
		return env, fmt.Errorf("driver document is not valid JSON")
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return env, fmt.Errorf("driver document is not an object: %w", err)
	}
	if env.Format != driverFormat {
		return env, fmt.Errorf("not a driver document: format=%q", env.Format)
	}
	if env.Version < 1 {
		return env, fmt.Errorf("version must be at least 1, got %d", env.Version)
	}
	if env.Version > driverVersion {
		return env, fmt.Errorf(
			"this driver is version %d and this build understands version %d. Update the "+
				"analyzer rather than storing something it cannot read back",
			env.Version, driverVersion)
	}
	if strings.TrimSpace(env.Name) == "" {
		return env, fmt.Errorf("a driver needs a name")
	}
	env.Name = strings.TrimSpace(env.Name)
	if !driverKinds[env.Kind] {
		return env, fmt.Errorf("unknown driver kind %q", env.Kind)
	}
	if env.Role == "" {
		env.Role = "signal"
	}
	if !driverRoles[env.Role] {
		return env, fmt.Errorf("unknown role %q", env.Role)
	}
	return env, nil
}
