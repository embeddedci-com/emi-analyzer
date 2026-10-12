package mcpserver

import (
	"encoding/json"
	"math"
	"os"
	"strings"
	"testing"
)

// The worker's decoupling result for a small board, shared with webapp/src/lib/decoupling.test.ts:
// what the worker computed must come back out of the branches here, as it does in the browser.
func loadDecouplingFixture(t *testing.T) *decouplingDoc {
	t.Helper()
	raw, err := os.ReadFile("../../webapp/src/lib/decouplingFixture.json")
	if err != nil {
		t.Fatal(err)
	}
	var d decouplingDoc
	if err := json.Unmarshal(raw, &d); err != nil {
		t.Fatal(err)
	}
	return &d
}

func db(a, b float64) float64 { return 20 * math.Log10(a/b) }

func TestImpedanceGridIsTheWorkers(t *testing.T) {
	d := loadDecouplingFixture(t)
	f := frequencies(d.FMinHz, d.FMaxHz, d.PointsPerDecade)
	if len(f) != 161 || math.Abs(f[0]/1e5-1) > 1e-9 || math.Abs(f[160]/1e9-1) > 1e-9 {
		t.Fatalf("grid: %d points, %g to %g", len(f), f[0], f[len(f)-1])
	}
}

func TestImpedanceMatchesTheWorker(t *testing.T) {
	d := loadDecouplingFixture(t)
	rail := d.Rails[0]
	ic := rail.ICs[0]
	freqs := frequencies(d.FMinHz, d.FMaxHz, d.PointsPerDecade)
	base := branchesOf(ic, rail.Parts)
	z := curve(base, ic.SeriesLH, freqs)

	// The gaps: where |Z| is above the target, up to the band.
	var gaps [][2]float64
	start := -1
	k := 0
	for k < len(freqs) && freqs[k] <= ic.BandHz*(1+1e-9) {
		k++
	}
	for i := 0; i < k; i++ {
		over := z[i] > ic.TargetOhm
		if over && start < 0 {
			start = i
		}
		if !over && start >= 0 {
			gaps = append(gaps, [2]float64{freqs[start], freqs[i-1]})
			start = -1
		}
	}
	if start >= 0 {
		gaps = append(gaps, [2]float64{freqs[start], freqs[k-1]})
	}
	if len(gaps) != len(ic.Gaps) {
		t.Fatalf("gaps %v, worker %v", gaps, ic.Gaps)
	}
	for i, g := range gaps {
		if math.Abs(g[0]/ic.Gaps[i][0]-1) > 1e-3 || math.Abs(g[1]/ic.Gaps[i][1]-1) > 1e-3 {
			t.Errorf("gap %d: %v, worker %v", i, g, ic.Gaps[i])
		}
	}

	// The worst point, within the four significant figures rules.json keeps.
	worst := 0
	for i := 1; i < k; i++ {
		if z[i] > z[worst] {
			worst = i
		}
	}
	if math.Abs(freqs[worst]/ic.Worst.Hz-1) > 1e-3 || math.Abs(db(z[worst], ic.TargetOhm)-ic.Worst.ExcessDB) > 0.01 ||
		math.Abs(z[worst]/ic.Worst.Ohm-1) > 1e-3 {
		t.Errorf("worst %.4g ohm at %.4g Hz, worker %+v", z[worst], freqs[worst], *ic.Worst)
	}

	// Each anti-resonance the worker reports: the same ohms at that frequency, here computed
	// at the point itself rather than read off the grid.
	for _, ar := range ic.AntiResonances {
		if got := networkMag(base, ic.SeriesLH, ar.Hz); math.Abs(db(got, ar.Ohm)) > 0.05 {
			t.Errorf("anti-resonance at %g Hz: %.4g ohm, worker %.4g", ar.Hz, got, ar.Ohm)
		}
	}

	// Each recommended change: the worker's improvement at its worst point.
	for _, r := range ic.Recommendations {
		after := networkMag(applyChange(base, *r.Change), ic.SeriesLH, r.AtHz)
		before := networkMag(base, ic.SeriesLH, r.AtHz)
		if got := db(before, after); math.Abs(got-r.ImprovementDB) > 0.05 {
			t.Errorf("%s: %.2f dB, worker %.2f", r.Text, got, r.ImprovementDB)
		}
	}
}

func TestImpedanceClosedForms(t *testing.T) {
	// Same pair as the worker's and the browser's test: 8.55 MHz, 0.291 ohm.
	bulk := decBranch{ID: "C1", CF: 10e-6, LH: 2e-9, ROhm: 0.01}
	small := decBranch{ID: "C2", CF: 100e-9, LH: 1.5e-9, ROhm: 0.03}
	cs := bulk.CF * small.CF / (bulk.CF + small.CF)
	f := 1 / (2 * math.Pi * math.Sqrt((bulk.LH+small.LH)*cs))
	closed := branchMag(bulk, f) * branchMag(small, f) / (bulk.ROhm + small.ROhm)
	if math.Abs(db(networkMag([]decBranch{bulk, small}, 0, f), closed)) > 0.5 || math.Abs(closed-0.291) > 0.005 {
		t.Errorf("anti-resonance %.4g, closed form %.4g", networkMag([]decBranch{bulk, small}, 0, f), closed)
	}
	// Four identical parts at their resonance: a quarter of the ESR.
	one := decBranch{CF: 100e-9, LH: 1e-9, ROhm: 0.05}
	srf := 1 / (2 * math.Pi * math.Sqrt(one.LH*one.CF))
	if got := networkMag([]decBranch{one, one, one, one}, 0, srf); math.Abs(got-0.0125) > 1e-6 {
		t.Errorf("bank at resonance = %g", got)
	}
}

func TestApplyChangeLeavesTheRest(t *testing.T) {
	d := loadDecouplingFixture(t)
	base := branchesOf(d.Rails[0].ICs[0], d.Rails[0].Parts)
	removed := applyChange(base, decChange{Op: "remove", ID: "C2"})
	if len(removed) != 2 || removed[0].ID != "C1" || removed[1].ID != "C3" {
		t.Errorf("removed = %+v", removed)
	}
	if added := applyChange(base, decChange{Op: "add", Branch: &decBranch{ID: "new", CF: 1e-9, LH: 1e-9, ROhm: 0.2}}); len(added) != 4 {
		t.Errorf("added = %+v", added)
	}
	moved := applyChange(base, decChange{Op: "replace", Branch: &decBranch{ID: "C3", CF: 1e-5, LH: 3e-9, ROhm: 0.015}})
	if moved[2].LH != 3e-9 || moved[2].Kind != "cap" || moved[0] != base[0] || base[2].LH == 3e-9 {
		t.Errorf("moved = %+v, base = %+v", moved, base)
	}
}

func TestImpedanceGridChecksTheRange(t *testing.T) {
	d := loadDecouplingFixture(t)
	if g, w, err := impedanceGrid(d, nil); err != nil || len(g) != 41 || w != "" {
		t.Errorf("default grid: %d points, %q, %v", len(g), w, err)
	}
	if _, w, err := impedanceGrid(d, []float64{4e5, 1e6}); err != nil || w != "" {
		t.Errorf("in range: %q, %v", w, err)
	}
	// A charge pump at 92 kHz: accepted, with the warning that the regulator is left out.
	if _, w, err := impedanceGrid(d, []float64{92e3, 4e5}); err != nil || !strings.Contains(w, "regulator") {
		t.Errorf("92 kHz: %q, %v", w, err)
	}
	if _, _, err := impedanceGrid(d, []float64{5e3}); err == nil {
		t.Error("5 kHz is below the lowest frequency and was accepted")
	}
	if _, _, err := impedanceGrid(d, []float64{2e9}); err == nil {
		t.Error("2 GHz is above the model and was accepted")
	}
}
