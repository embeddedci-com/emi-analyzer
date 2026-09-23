package emi

import (
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
}

// FeatureFullWave is the EMI_EXPERIMENTAL name for Features.FullWave.
const FeatureFullWave = "full-wave"

// ParseExperimental reads a comma-separated list such as "full-wave". Unknown names are returned
// so a host can warn about a typo instead of silently running without the feature it asked for.
func ParseExperimental(list string) (Features, []string) {
	var f Features
	var unknown []string
	for _, name := range strings.Split(list, ",") {
		switch name = strings.ToLower(strings.TrimSpace(name)); name {
		case "":
		case FeatureFullWave:
			f.FullWave = true
		default:
			unknown = append(unknown, name)
		}
	}
	return f, unknown
}

// allows reports whether runs of this kind may be created or handed to a worker.
func (f Features) allows(k RunKind) bool {
	switch k {
	case RunKindSolve, RunKindCompliance:
		return f.FullWave
	}
	return true
}

// refusal is what a user is told when they ask for a gated run kind.
func (f Features) refusal(k RunKind) string {
	return `"` + string(k) + `" runs need full-wave solving, which is experimental and turned off ` +
		`on this server: it has not been verified on real boards yet. It can be enabled with ` +
		`EMI_EXPERIMENTAL=` + FeatureFullWave + `.`
}

func (s *Service) handleFeatures(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.deps.Features)
}
