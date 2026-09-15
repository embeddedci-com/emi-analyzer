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
	// FullWave enables openEMS full-wave solves and everything built on them: hotspot maps, the
	// far field, cable emissions and the compliance estimate.
	//
	// Off by default because long solves are not stable. Every FDTD run measured over a long
	// record (>=150,000 timesteps) has diverged, at every mesh preset, and the mesher does not
	// enforce its own grading bound -- see docs/known-issues.md. The worker detects divergence
	// and refuses to report the numbers, so a diverged run fails rather than lies; but a user
	// can wait many minutes to learn that, and a radiated compliance run is always a long one.
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
		`on this server: long solves are not yet numerically stable. It can be enabled with ` +
		`EMI_EXPERIMENTAL=` + FeatureFullWave + `.`
}

func (s *Service) handleFeatures(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, s.deps.Features)
}
