package emi

import (
	"errors"
	"math"
)

// speedOfLight in m/s.
const speedOfLight = 299792458.0

// bytesPerCell is what openEMS's engine costs per Yee cell: six arrays (volt, curr and the
// four operator arrays vv, vi, iv, ii), three components each, float32.
//
//	6 * 3 * 4 = 72
const bytesPerCell = 72

// Defaults for the parts of the cost model the user does not usually set.
const (
	DefaultPeriods              = 3.0   // T_sim = periods / f_min
	DefaultThroughputMCellsPerS = 200.0 // measured openEMS range is roughly 150-250
)

// EstimateInput is everything the cost model needs.
//
// The same computation runs in three places: live in the browser as the user drags the
// region of interest, here when the server decides which workers could take the run, and
// on the worker at the mesh stage where it becomes authoritative. All three are checked
// against testdata/estimate_fixtures.json so they cannot drift apart.
type EstimateInput struct {
	// Region of interest, including the air box above and below the board, in millimetres.
	ROIXmm float64 `json:"roi_x_mm"`
	ROIYmm float64 `json:"roi_y_mm"`
	ROIZmm float64 `json:"roi_z_mm"`

	// Requested cell size per axis, in micrometres: the mesh preset.
	//
	// The Courant limit keys off the smallest cell in *any* axis, and in plane that is a
	// quarter of the request (InPlaneMinCellFraction), because copper edges put grid lines
	// that close on any routed board. So it is usually dx, not dz, that sets the timestep:
	// 12.5 um in plane against 25 um vertically at the fine preset.
	DXum float64 `json:"dx_um"`
	DYum float64 `json:"dy_um"`
	DZum float64 `json:"dz_um"`

	// Lowest frequency the run has to resolve, in Hz.
	FMinHz float64 `json:"f_min_hz"`

	// Number of ports to excite. openEMS runs one full pass per excited port.
	Ports int `json:"ports"`

	// FillFactor is the real mesh's cell count as a multiple of the uniform bounding-box
	// count. Zero means 1.0, i.e. assume a uniform mesh.
	//
	// It is usually GREATER than 1, which is the opposite of what this field originally
	// assumed and of what its name suggests. DXum and friends are a floor on cell size, not
	// the spacing: the mesher puts a line at every copper edge, and a routed board has edges
	// far closer together than any preset. Measured on four real boards
	// (worker/scripts/measure_fill_factor.py, September 2026), the in-plane axes come out
	// 2.0-4.0x denser than uniform while the vertical axis, whose air is graded coarsely,
	// comes out 0.26-0.55x. In-plane wins:
	//
	//	coarse  min 4.67  median 7.53  max 8.65
	//	normal  min 2.31  median 4.66  max 5.08
	//	fine    min 1.14  median 2.90  max 3.22
	//
	// The preset dependence is not noise: feature spacing is set by the board, so the coarser
	// the request, the more the copper dominates.
	//
	// The name is kept because it is a wire field; the meaning is what is written here.
	//
	// Note that grading changes the cell count but NOT the timestep: dt is pinned by the
	// smallest cell, and grading is precisely the technique of keeping some cells small.
	FillFactor float64 `json:"fill_factor,omitempty"`

	// Optional overrides. Zero means use the package default.
	Periods              float64 `json:"periods,omitempty"`
	ThroughputMCellsPerS float64 `json:"throughput_mcells_per_s,omitempty"`
}

var errBadEstimateInput = errors.New("emi: estimate input must have positive extents, resolutions, frequency and port count")

// Estimate computes the cost of a solve run.
//
// Every number here follows from four facts and nothing else:
//
//	dmin    = min(dx/4, dy/4, dz)    the smallest cell the mesher makes
//	dt      = dmin / (c * sqrt(3))   Courant limit, set by the SMALLEST cell in any axis
//	T_sim   = periods / f_min        long enough to resolve the lowest frequency
//	steps   = T_sim / dt
//	cells   = product of ceil(extent / resolution) over the three axes
//
// It is intentionally not clever. A user who is told "1.9 billion cells" should be able to
// reproduce that on paper, because the alternative is a black box telling them their board
// will take fifty days.
// InPlaneMinCellFraction is the smallest in-plane cell as a fraction of the requested one.
// The mesher merges lines closer than a quarter of dx, and copper puts lines that close
// everywhere on a routed board, so the cell that sets the timestep is dx/4, not dx. Taking dx
// put the step count 2.0-4.5x low on four real boards.
const InPlaneMinCellFraction = 0.25

// MaxFillFactor is an upper bound on FillFactor, to catch a caller passing a cell count by
// mistake rather than a ratio. The largest value measured on a real board is 8.75.
const MaxFillFactor = 50.0

func (in EstimateInput) Estimate() (Estimate, error) {
	if in.ROIXmm <= 0 || in.ROIYmm <= 0 || in.ROIZmm <= 0 ||
		in.DXum <= 0 || in.DYum <= 0 || in.DZum <= 0 ||
		in.FMinHz <= 0 || in.Ports <= 0 {
		return Estimate{}, errBadEstimateInput
	}

	periods := in.Periods
	if periods <= 0 {
		periods = DefaultPeriods
	}
	throughput := in.ThroughputMCellsPerS
	if throughput <= 0 {
		throughput = DefaultThroughputMCellsPerS
	}
	// Zero means unset: this is an omitempty JSON field, so a caller who did not send one
	// and a caller who sent 0 are indistinguishable here. A negative value is neither, and
	// is the shape a unit mix-up takes, so it is refused rather than quietly defaulted --
	// which is also what the Python half does.
	fill := in.FillFactor
	if fill < 0 {
		return Estimate{}, errBadEstimateInput
	}
	if fill == 0 {
		fill = 1.0
	}
	// The cap only catches a caller passing a cell count where a ratio belongs; the largest
	// value measured on a real board is 8.75.
	if fill > MaxFillFactor {
		return Estimate{}, errBadEstimateInput
	}

	// Cell counts. Extents are mm, resolutions um, so extent*1000/res is dimensionless.
	nx := int64(math.Ceil(in.ROIXmm * 1000.0 / in.DXum))
	ny := int64(math.Ceil(in.ROIYmm * 1000.0 / in.DYum))
	nz := int64(math.Ceil(in.ROIZmm * 1000.0 / in.DZum))
	cells := int64(math.Ceil(float64(nx) * float64(ny) * float64(nz) * fill))

	// Courant limit, from the smallest cell in any axis, converted um -> m. In plane that is a
	// quarter of the requested cell: see InPlaneMinCellFraction.
	dminM := math.Min(in.DXum*InPlaneMinCellFraction,
		math.Min(in.DYum*InPlaneMinCellFraction, in.DZum)) * 1e-6
	dt := dminM / (speedOfLight * math.Sqrt(3))

	simTime := periods / in.FMinHz
	steps := int64(math.Ceil(simTime / dt))

	// Wall clock. One full pass per excited port.
	cellUpdates := float64(cells) * float64(steps) * float64(in.Ports)
	eta := cellUpdates / (throughput * 1e6)

	return Estimate{
		Cells:          cells,
		RAMBytes:       cells * bytesPerCell,
		Timesteps:      steps,
		DTSeconds:      dt,
		SimTimeSeconds: simTime,
		ETASeconds:     eta,
	}, nil
}
