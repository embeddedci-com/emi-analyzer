package emi

import (
	"encoding/json"
	"math"
	"os"
	"testing"
)

// The cost model exists in Go, in Python on the worker, and (later) in TypeScript in the
// browser. Three implementations of the same arithmetic will drift unless something forces
// them not to, so all three read the same fixture file. If this test fails after an
// intentional change, regenerate the fixtures from the Python side and update all three.
func TestEstimateMatchesSharedFixtures(t *testing.T) {
	raw, err := os.ReadFile("testdata/estimate_fixtures.json")
	if err != nil {
		t.Fatalf("read fixtures: %v", err)
	}
	var doc struct {
		Cases []struct {
			Name     string        `json:"name"`
			Input    EstimateInput `json:"input"`
			Expected Estimate      `json:"expected"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("parse fixtures: %v", err)
	}
	if len(doc.Cases) == 0 {
		t.Fatal("fixture file has no cases")
	}

	for _, c := range doc.Cases {
		t.Run(c.Name, func(t *testing.T) {
			got, err := c.Input.Estimate()
			if err != nil {
				t.Fatalf("estimate: %v", err)
			}
			if got.Cells != c.Expected.Cells {
				t.Errorf("cells = %d, want %d", got.Cells, c.Expected.Cells)
			}
			if got.RAMBytes != c.Expected.RAMBytes {
				t.Errorf("ram_bytes = %d, want %d", got.RAMBytes, c.Expected.RAMBytes)
			}
			if got.Timesteps != c.Expected.Timesteps {
				t.Errorf("timesteps = %d, want %d", got.Timesteps, c.Expected.Timesteps)
			}
			// Floats compare relatively: Go and Python both use IEEE doubles here, but the
			// fixture round-trips through JSON decimal text.
			closeEnough(t, "dt_seconds", got.DTSeconds, c.Expected.DTSeconds)
			closeEnough(t, "sim_time_seconds", got.SimTimeSeconds, c.Expected.SimTimeSeconds)
			closeEnough(t, "eta_seconds", got.ETASeconds, c.Expected.ETASeconds)
		})
	}
}

func closeEnough(t *testing.T, name string, got, want float64) {
	t.Helper()
	if want == 0 {
		if got != 0 {
			t.Errorf("%s = %v, want 0", name, got)
		}
		return
	}
	if rel := math.Abs(got-want) / math.Abs(want); rel > 1e-12 {
		t.Errorf("%s = %v, want %v (relative error %g)", name, got, want, rel)
	}
}

// The single most surprising property of the cost model, and the one users get wrong: the
// timestep is pinned by the smallest cell in ANY axis. Halving only the vertical resolution
// doubles the number of timesteps even though the in-plane mesh never changed.
func TestVerticalMeshSetsTimestep(t *testing.T) {
	base := EstimateInput{
		ROIXmm: 20, ROIYmm: 20, ROIZmm: 5,
		DXum: 200, DYum: 200, DZum: 40,
		FMinHz: 300e6, Ports: 1,
	}
	finer := base
	finer.DZum = 20

	a, err := base.Estimate()
	if err != nil {
		t.Fatal(err)
	}
	b, err := finer.Estimate()
	if err != nil {
		t.Fatal(err)
	}

	if b.Timesteps < 2*a.Timesteps-1 {
		t.Errorf("halving dz should roughly double timesteps: %d -> %d", a.Timesteps, b.Timesteps)
	}
	if b.ETASeconds < 3.5*a.ETASeconds {
		// Cells double AND timesteps double, so wall clock goes up ~4x, not 2x. This is
		// the trap: a "small" mesh refinement is a 4x cost.
		t.Errorf("halving dz should roughly quadruple eta: %.0fs -> %.0fs", a.ETASeconds, b.ETASeconds)
	}
}

// Grading reduces the cell count but must NOT reduce the timestep, because grading is
// precisely the technique of keeping some cells small.
func TestFillFactorDoesNotChangeTimestep(t *testing.T) {
	uniform := EstimateInput{
		ROIXmm: 24, ROIYmm: 24, ROIZmm: 10,
		DXum: 50, DYum: 50, DZum: 25,
		FMinHz: 100e6, Ports: 1,
	}
	graded := uniform
	graded.FillFactor = 0.13

	u, err := uniform.Estimate()
	if err != nil {
		t.Fatal(err)
	}
	g, err := graded.Estimate()
	if err != nil {
		t.Fatal(err)
	}

	if g.Timesteps != u.Timesteps {
		t.Errorf("grading changed the timestep: %d != %d", g.Timesteps, u.Timesteps)
	}
	if g.Cells >= u.Cells {
		t.Errorf("grading did not reduce cells: %d >= %d", g.Cells, u.Cells)
	}
	if g.DTSeconds != u.DTSeconds {
		t.Errorf("grading changed dt: %v != %v", g.DTSeconds, u.DTSeconds)
	}
}

func TestEstimateRejectsNonsense(t *testing.T) {
	valid := EstimateInput{
		ROIXmm: 10, ROIYmm: 10, ROIZmm: 2,
		DXum: 50, DYum: 50, DZum: 25, FMinHz: 1e8, Ports: 1,
	}
	cases := map[string]func(*EstimateInput){
		"zero extent":     func(i *EstimateInput) { i.ROIXmm = 0 },
		"negative extent": func(i *EstimateInput) { i.ROIZmm = -1 },
		"zero resolution": func(i *EstimateInput) { i.DZum = 0 },
		"zero frequency":  func(i *EstimateInput) { i.FMinHz = 0 },
		"zero ports":      func(i *EstimateInput) { i.Ports = 0 },
		// 1.5 used to be rejected. It is a perfectly ordinary mesh multiplier: measured
		// values on real boards run from 0.70 to 8.75, because dx is a floor on cell size
		// and copper edges force lines much closer than that. Only a value that cannot be a
		// ratio at all is refused now.
		"fill absurdly high": func(i *EstimateInput) { i.FillFactor = MaxFillFactor + 1 },
		"fill negative":      func(i *EstimateInput) { i.FillFactor = -0.5 },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			in := valid
			mutate(&in)
			if _, err := in.Estimate(); err == nil {
				t.Error("expected an error, got none")
			}
		})
	}
}
