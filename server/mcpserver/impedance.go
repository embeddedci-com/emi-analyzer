package mcpserver

import (
	"fmt"
	"math"
	"strconv"
)

// The supply impedance at an IC's pins, rebuilt from the branches the worker ships, with the
// same formulas as webapp/src/lib/decoupling.ts (branchesOf, applyChange, branchMag,
// networkMag, curve). Both are checked against webapp/src/lib/decouplingFixture.json, the
// worker's own result for a small board: the gaps, the worst point and the anti-resonances
// they rebuild must be the worker's.

type decBranch struct {
	ID   string  `json:"id"`
	CF   float64 `json:"c_f"`
	LH   float64 `json:"l_h"`
	ROhm float64 `json:"r_ohm"`
	Kind string  `json:"kind,omitempty"`
}

type decChange struct {
	Op     string     `json:"op"`
	Branch *decBranch `json:"branch,omitempty"`
	ID     string     `json:"id,omitempty"`
}

// frequencies is the worker's grid: log-spaced, the same points, so a gap edge here is a gap
// edge there.
func frequencies(fMin, fMax float64, perDecade int) []float64 {
	lo, hi := math.Log10(fMin), math.Log10(fMax)
	n := int(math.Round((hi-lo)*float64(perDecade))) + 1
	if n < 2 {
		return []float64{fMin, fMax}
	}
	out := make([]float64, n)
	for i := range out {
		out[i] = math.Pow(10, lo+float64(i)/float64(n-1)*(hi-lo))
	}
	return out
}

func branchesOf(ic decIC, parts map[string]decPart) []decBranch {
	out := make([]decBranch, 0, len(ic.Caps)+1)
	for _, c := range ic.Caps {
		p := parts[c.Ref]
		out = append(out, decBranch{ID: c.Ref, CF: p.CF, LH: c.LH, ROhm: p.ESROhm, Kind: "cap"})
	}
	if ic.PlaneBranch != nil {
		b := *ic.PlaneBranch
		b.Kind = "plane"
		out = append(out, b)
	}
	return out
}

func applyChange(bs []decBranch, c decChange) []decBranch {
	out := make([]decBranch, 0, len(bs)+1)
	switch c.Op {
	case "remove":
		for _, b := range bs {
			if b.ID != c.ID {
				out = append(out, b)
			}
		}
	case "add":
		out = append(out, bs...)
		if c.Branch != nil {
			out = append(out, *c.Branch)
		}
	default: // replace
		for _, b := range bs {
			if c.Branch != nil && b.ID == c.Branch.ID {
				nb := *c.Branch
				if nb.Kind == "" {
					nb.Kind = b.Kind
				}
				b = nb
			}
			out = append(out, b)
		}
	}
	return out
}

// branchMag is |Z| of one branch alone.
func branchMag(b decBranch, f float64) float64 {
	w := 2 * math.Pi * f
	return math.Hypot(b.ROhm, w*b.LH-1/(w*b.CF))
}

// networkMag is |Z| of the branches in parallel, behind a series inductance common to all.
func networkMag(bs []decBranch, seriesLH, f float64) float64 {
	if len(bs) == 0 {
		return math.Inf(1)
	}
	w := 2 * math.Pi * f
	var yr, yi float64
	for _, b := range bs {
		x := w*b.LH - 1/(w*b.CF)
		d := b.ROhm*b.ROhm + x*x
		yr += b.ROhm / d
		yi += -x / d
	}
	d := yr*yr + yi*yi
	return math.Hypot(yr/d, -yi/d+w*seriesLH)
}

func curve(bs []decBranch, seriesLH float64, freqs []float64) []float64 {
	out := make([]float64, len(freqs))
	for i, f := range freqs {
		out[i] = networkMag(bs, seriesLH, f)
	}
	return out
}

// ---- the decoupling tool's impedance output ----

const impedanceNote = "Experimental: a lumped estimate from the layout (each capacitor as series R-L-C with its " +
	"mounting and via inductance, the plane pair as one more branch), valid from 100 kHz to 1 GHz. It leaves out " +
	"the regulator's own output impedance, trace resistance and capacitor DC-bias derating. Ohms at the IC's supply pins."

// impedancePerDecade is the default grid when no frequencies are asked for: coarser than the
// worker's 40 per decade, enough to read the shape.
const impedancePerDecade = 10

type impedanceCurve struct {
	Change string    `json:"change"`
	Ohm    []float64 `json:"ohm"`
}

type impedanceOut struct {
	Note      string           `json:"note"`
	Warning   string           `json:"warning,omitempty" jsonschema:"set when some frequencies are below the model's range"`
	Hz        []float64        `json:"hz"`
	AsLaidOut []float64        `json:"as_laid_out_ohm"`
	Changes   []impedanceCurve `json:"with_change,omitempty" jsonschema:"the same frequencies with each recommended change applied, one at a time"`
}

// impedanceLowestHz is how far below the worker's grid the capacitor model is still worth
// reading: charge pumps and switching regulators run from a few tens of kHz.
const impedanceLowestHz = 10e3

const impedanceLowWarning = "Below %s the regulator's own output impedance, which this model leaves out, " +
	"usually sets the supply impedance: read these values as the capacitors alone."

// impedanceGrid checks the asked frequencies against the model's range, or returns the
// default grid. Frequencies down to impedanceLowestHz are accepted with a warning.
func impedanceGrid(d *decouplingDoc, asked []float64) ([]float64, string, error) {
	if len(asked) == 0 {
		return frequencies(d.FMinHz, d.FMaxHz, impedancePerDecade), "", nil
	}
	lowest := math.Min(impedanceLowestHz, d.FMinHz)
	warning := ""
	for _, f := range asked {
		if !(f >= lowest*(1-1e-9) && f <= d.FMaxHz*(1+1e-9)) {
			return nil, "", fmt.Errorf("impedance_hz %g is outside the range, %s to %s", f, hz(lowest), hz(d.FMaxHz))
		}
		if f < d.FMinHz*(1-1e-9) {
			warning = fmt.Sprintf(impedanceLowWarning, hz(d.FMinHz))
		}
	}
	return asked, warning, nil
}

func impedanceOf(ic decIC, parts map[string]decPart, freqs []float64, warning string, recs []decRecommendation) *impedanceOut {
	base := branchesOf(ic, parts)
	if len(base) == 0 {
		return nil
	}
	out := &impedanceOut{Note: impedanceNote, Warning: warning, Hz: roundAll(freqs),
		AsLaidOut: roundAll(curve(base, ic.SeriesLH, freqs))}
	for _, r := range recs {
		if r.Change == nil {
			continue
		}
		out.Changes = append(out.Changes, impedanceCurve{Change: r.Text,
			Ohm: roundAll(curve(applyChange(base, *r.Change), ic.SeriesLH, freqs))})
	}
	return out
}

// roundAll keeps four significant figures, as rules.json does.
func roundAll(v []float64) []float64 {
	out := make([]float64, len(v))
	for i, x := range v {
		out[i], _ = strconv.ParseFloat(strconv.FormatFloat(x, 'g', 4, 64), 64)
	}
	return out
}
