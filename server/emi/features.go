package emi

import (
	"encoding/json"
	"net/http"
	"strings"
)

// Features switches off the parts of the analyzer that are built but not yet trustworthy.
//
// The zero value is the safe one: everything experimental is off, so a host that passes nothing
// ships only what has been verified. A host turns a feature on deliberately, typically from an
// EMI_EXPERIMENTAL environment variable (see ParseExperimental).
//
// Off means off at the server, not merely hidden in the UI: runs of a gated kind are refused at
// creation and on retry, and a worker is never handed one that was queued before the switch.
type Features struct {
	// FullWave enables the run kinds built on an openEMS full-wave solve: "solve" (hotspot
	// maps, the far field, and the cable emissions computed from a solve's cable ports) and
	// "compliance", which reads a solve's results. The "cable" run kind is not gated: it is a
	// method-of-moments budget on a wire alone and needs no solve.
	//
	// Off by default because none of it is verified, not because it is known to be broken. It
	// does solve end to end: the runs that used to be refused as unstable were a false positive
	// in the divergence check, which read the ripple on the excitation ramp as a blow-up. What
	// has not been done is a run at the record length a radiated result needs, or any check of
	// the far field, solve-based cable emissions or a compliance estimate against a real board.
	// See docs/known-issues.md.
	FullWave bool `json:"full_wave"`

	// SmallPartSolve enables one kind of solve on its own: a small part of a board (a net cut
	// out over its planes, or a small drawn region) with no far field, no cable ports and no
	// component models, a band that keeps the record short, and a cell and timestep budget the
	// worker enforces. Its outputs are the near-field map and the port impedance and
	// S-parameters. It is the part of full-wave solving that has been checked against closed
	// forms, for convergence and on real-board coupons (docs/verification/small-part-solve.md),
	// which is why it has its own switch. FullWave implies it.
	//
	// Off by default until the results in that document are accepted; see
	// SmallPartSolveByDefault.
	SmallPartSolve bool `json:"small_part_solve"`
}

// SmallPartSolveByDefault turns small-part solving on without EMI_EXPERIMENTAL. Flipping it is
// the whole change needed to ship it; it is false while the verification is being reviewed.
const SmallPartSolveByDefault = false

// FeatureFullWave is the EMI_EXPERIMENTAL name for Features.FullWave.
const FeatureFullWave = "full-wave"

// FeatureSmallPartSolve is the EMI_EXPERIMENTAL name for Features.SmallPartSolve.
const FeatureSmallPartSolve = "small-part-solve"

// KnownFeatures lists every EMI_EXPERIMENTAL name, for a host's error message.
var KnownFeatures = []string{FeatureFullWave, FeatureSmallPartSolve}

// SmallPartMode is the solve params' "mode" for a small-part solve.
const SmallPartMode = "small_part"

// ParseExperimental reads a comma-separated list such as "full-wave". Unknown names are returned
// so a host can warn about a typo instead of silently running without the feature it asked for.
func ParseExperimental(list string) (Features, []string) {
	f := Features{SmallPartSolve: SmallPartSolveByDefault}
	var unknown []string
	for _, name := range strings.Split(list, ",") {
		switch name = strings.ToLower(strings.TrimSpace(name)); name {
		case "":
		case FeatureFullWave:
			f.FullWave = true
		case FeatureSmallPartSolve:
			f.SmallPartSolve = true
		default:
			unknown = append(unknown, name)
		}
	}
	// Full-wave is the superset: a small-part solve is a solve with less switched on.
	if f.FullWave {
		f.SmallPartSolve = true
	}
	return f, unknown
}

// smallPartParams is what the gate reads from a solve's params. Only these fields: the rest is
// the worker's to validate.
type smallPartParams struct {
	Mode            string          `json:"mode"`
	FarField        bool            `json:"far_field"`
	CablePorts      json.RawMessage `json:"cable_ports"`
	ModelComponents bool            `json:"model_components"`
}

func readSmallPart(params json.RawMessage) (smallPartParams, bool) {
	var p smallPartParams
	if len(params) == 0 || json.Unmarshal(params, &p) != nil {
		return p, false
	}
	return p, p.Mode == SmallPartMode
}

// smallPartProblem is why a small-part solve's params ask for more than the mode does, or "".
func smallPartProblem(params json.RawMessage) string {
	p, ok := readSmallPart(params)
	if !ok {
		return ""
	}
	var what string
	switch {
	case p.FarField:
		what = "a far field"
	case len(p.CablePorts) > 0 && string(p.CablePorts) != "null" && string(p.CablePorts) != "{}":
		what = "cable ports"
	case p.ModelComponents:
		what = "component models"
	default:
		return ""
	}
	return "a small-part solve has no " + what + ". That needs a full-wave solve (" +
		FeatureFullWave + ")."
}

// allows reports whether a run of this kind, with these params, may be created or handed to a
// worker. The params matter only for a solve: a small-part one needs SmallPartSolve, any other
// needs FullWave.
func (f Features) allows(k RunKind, params json.RawMessage) bool {
	switch k {
	case RunKindCompliance:
		return f.FullWave
	case RunKindSolve:
		if f.FullWave {
			return true
		}
		_, small := readSmallPart(params)
		return f.SmallPartSolve && small && smallPartProblem(params) == ""
	}
	return true
}

// refusal is what a user is told when they ask for a gated run.
func (f Features) refusal(k RunKind, params json.RawMessage) string {
	if k == RunKindSolve && f.SmallPartSolve {
		if why := smallPartProblem(params); why != "" {
			return why
		}
		return `only small-part solves are turned on on this server. A solve of a whole region, ` +
			`with a far field or cable emissions, needs full-wave solving, which is experimental ` +
			`and can be enabled with EMI_EXPERIMENTAL=` + FeatureFullWave + `.`
	}
	if _, small := readSmallPart(params); k == RunKindSolve && small {
		return `small-part solves are experimental and turned off on this server. They can be ` +
			`enabled with EMI_EXPERIMENTAL=` + FeatureSmallPartSolve + `.`
	}
	return `"` + string(k) + `" runs need full-wave solving, which is experimental and turned off ` +
		`on this server: it has not been verified on real boards yet. It can be enabled with ` +
		`EMI_EXPERIMENTAL=` + FeatureFullWave + `.`
}

func (s *Service) handleFeatures(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.deps.Features)
}
