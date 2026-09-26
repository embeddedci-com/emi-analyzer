package emi

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strings"
)

// ConductedRegulator is what a user says about one switching regulator. Every field left out is
// an assumed default in the worker, and the result says so; nothing here is required.
type ConductedRegulator struct {
	FrequencyHz   *float64 `json:"frequency_hz,omitempty"`
	InputCurrentA *float64 `json:"input_current_a,omitempty"`
	Duty          *float64 `json:"duty,omitempty"`
	RiseS         *float64 `json:"rise_s,omitempty"`
}

// ConductedParams is what a conducted run accepts.
type ConductedParams struct {
	// Class is the FCC 15.107 device class: "A" or "B".
	Class string `json:"class"`
	// Entry picks the power input ("J1:+12V") when a board has more than one.
	Entry      string                        `json:"entry,omitempty"`
	Regulators map[string]ConductedRegulator `json:"regulators,omitempty"`
}

const maxConductedRegulators = 20

// conductedRanges are the worker's limits (worker/emi_worker/conducted/sources.py), checked here
// too so a typo is refused when the run is created rather than minutes later by the worker.
var conductedRanges = []struct {
	name   string
	lo, hi float64
	get    func(ConductedRegulator) *float64
}{
	{"frequency_hz", 10e3, 10e6, func(r ConductedRegulator) *float64 { return r.FrequencyHz }},
	{"input_current_a", 1e-3, 100, func(r ConductedRegulator) *float64 { return r.InputCurrentA }},
	{"duty", 0.02, 0.98, func(r ConductedRegulator) *float64 { return r.Duty }},
	{"rise_s", 0.1e-9, 1e-6, func(r ConductedRegulator) *float64 { return r.RiseS }},
}

// printable is true for a short reference or net name with nothing a netlist or a log could
// misread: the worker never writes these into a deck, but it does write them into its results.
func printable(s string, max int) bool {
	if s == "" || len(s) > max {
		return false
	}
	for _, r := range s {
		if r < 0x20 || r == 0x7f {
			return false
		}
	}
	return true
}

// validateConductedParams checks a conducted run's parameters and returns them normalised.
func validateConductedParams(raw json.RawMessage) (ConductedParams, error) {
	var p ConductedParams
	if len(raw) > 0 && string(raw) != "null" {
		dec := json.NewDecoder(strings.NewReader(string(raw)))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&p); err != nil {
			return p, fmt.Errorf("params are not valid: %v", err)
		}
	}
	p.Class = strings.ToUpper(strings.TrimSpace(p.Class))
	if p.Class == "" {
		p.Class = "B"
	}
	if p.Class != "A" && p.Class != "B" {
		return p, errors.New(`class must be "A" or "B"`)
	}
	if p.Entry != "" && !printable(p.Entry, 200) {
		return p, errors.New("entry must be a connector and net such as J1:+12V")
	}
	if len(p.Regulators) > maxConductedRegulators {
		return p, fmt.Errorf("at most %d regulators can be set", maxConductedRegulators)
	}
	for ref, r := range p.Regulators {
		if !printable(ref, 64) {
			return p, errors.New("regulators must be keyed by reference designator")
		}
		for _, rg := range conductedRanges {
			v := rg.get(r)
			if v == nil {
				continue
			}
			if math.IsNaN(*v) || *v < rg.lo || *v > rg.hi {
				return p, fmt.Errorf("%s: %s must be between %g and %g", ref, rg.name, rg.lo, rg.hi)
			}
		}
	}
	return p, nil
}
