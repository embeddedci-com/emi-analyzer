package mcpserver

import (
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
	"github.com/embeddedci-com/emi-analyzer/server/emiclient"
)

// The ESD simulation and the cable budget are runs of their own on a version of a board,
// started on request: seconds to minutes each, so an agent starts one, waits, and reads it.

// latestRun is the newest finished run of a kind on a board, or nil.
func latestRun(runs []emi.Run, boardID string, kind emi.RunKind) *emi.Run {
	var best *emi.Run
	for i, r := range runs {
		if r.BoardID != boardID || r.Kind != kind || r.Status != emi.StatusDone {
			continue
		}
		if best == nil || r.CreatedAt.After(best.CreatedAt) {
			best = &runs[i]
		}
	}
	return best
}

// runOf resolves a target to a finished run of a kind: run_id names it, otherwise the newest
// finished one on the version the target resolves to.
func (s *Server) runOf(ctx context.Context, c *emiclient.Client, t target, kind emi.RunKind, start string) (emi.Run, analysis, error) {
	if t.RunID != "" {
		r, err := c.GetRun(ctx, t.RunID)
		if err != nil {
			return emi.Run{}, analysis{}, err
		}
		if r.Kind == kind {
			if err := finished(r); err != nil {
				return emi.Run{}, analysis{}, err
			}
			return r, analysis{RunID: r.ID, ProjectID: r.ProjectID, BoardID: r.BoardID}, nil
		}
		// An ingest run names the version.
		t = target{ProjectID: r.ProjectID, BoardID: r.BoardID}
	}
	a, err := resolve(ctx, c, t)
	if err != nil {
		return emi.Run{}, analysis{}, err
	}
	runs, err := c.ListRuns(ctx, a.ProjectID)
	if err != nil {
		return emi.Run{}, analysis{}, err
	}
	r := latestRun(runs, a.BoardID, kind)
	if r == nil {
		return emi.Run{}, analysis{}, fmt.Errorf("version %s has no finished %s run: start one with %s", a.BoardID, kind, start)
	}
	return *r, analysis{RunID: r.ID, ProjectID: r.ProjectID, BoardID: r.BoardID}, nil
}

// ---- ESD ----

type runESDIn struct {
	target
	Level    int      `json:"level,omitempty" jsonschema:"IEC 61000-4-2 contact level: 1, 2, 3 or 4 (2, 4, 6, 8 kV); default 4"`
	Polarity string   `json:"polarity,omitempty" jsonschema:"both (default), positive or negative"`
	Lines    []string `json:"lines,omitempty" jsonschema:"only these nets; default every line that leaves the board through a connector"`
	WaitS    int      `json:"wait_s,omitempty" jsonschema:"seconds to wait for it to finish (max 600)"`
}

type startedOut struct {
	analysis
	Run *runOut `json:"run"`
}

func (s *Server) runESD(ctx context.Context, req *mcpRequest, in runESDIn) (*mcpResult, startedOut, error) {
	c, _, err := s.client(ctx)
	if err != nil {
		return nil, startedOut{}, err
	}
	a, err := resolve(ctx, c, in.target)
	if err != nil {
		return nil, startedOut{}, err
	}
	params := map[string]any{"level": in.Level, "polarity": in.Polarity, "lines": in.Lines}
	if in.Level == 0 {
		params["level"] = 4
	}
	r, err := c.CreateRun(ctx, a.ProjectID, a.BoardID, emi.RunKindTransient, params)
	if err != nil {
		return nil, startedOut{}, err
	}
	out, err := s.wait(ctx, req, c, r.ID, in.WaitS)
	if err != nil {
		return nil, startedOut{}, err
	}
	return nil, startedOut{analysis: a, Run: out}, nil
}

type transientDoc struct {
	Level       int      `json:"level"`
	KV          float64  `json:"kv"`
	Assumptions []string `json:"assumptions"`
	Notes       []string `json:"notes"`
	Lines       []struct {
		Net       string `json:"net"`
		Connector struct {
			Ref string `json:"ref"`
			Pad string `json:"pad"`
		} `json:"connector"`
		Clamp *struct {
			Ref   string `json:"ref"`
			Part  string `json:"part"`
			Model string `json:"model"`
		} `json:"clamp"`
		Series *struct {
			Ref string  `json:"ref"`
			Ohm float64 `json:"ohm"`
		} `json:"series_resistor"`
		IC *struct {
			Ref     string  `json:"ref"`
			Pad     string  `json:"pad"`
			SupplyV float64 `json:"supply_v"`
		} `json:"ic"`
		Geometry struct {
			Routed        bool    `json:"routed"`
			ClampGroundMM float64 `json:"clamp_ground_mm"`
			ClampGroundNH float64 `json:"clamp_ground_nh"`
			ClampStub     *struct {
				LengthMM float64 `json:"length_mm"`
			} `json:"clamp_stub"`
		} `json:"geometry"`
		Notes    []string `json:"notes"`
		Error    string   `json:"error"`
		Variants []struct {
			ID         string   `json:"id"`
			VPin       *float64 `json:"v_pin_peak_v"`
			VClamp     *float64 `json:"v_clamp_peak_v"`
			VConnector float64  `json:"v_connector_peak_v"`
			Unclamped  bool     `json:"unclamped"`
		} `json:"variants"`
	} `json:"lines"`
}

type esdLine struct {
	Connector string   `json:"connector" jsonschema:"connector ref.pad"`
	Net       string   `json:"net"`
	Clamp     string   `json:"clamp,omitempty" jsonschema:"the clamp on the line, and how good its model is (vendor, table or illustrative)"`
	IC        string   `json:"ic,omitempty" jsonschema:"the IC pin the line ends at, and its supply"`
	Peak      string   `json:"peak_as_laid_out" jsonschema:"the worst peak voltage, and where: at the IC pin, else the clamp, else the connector"`
	PeakV     float64  `json:"peak_v"`
	OverPinV  float64  `json:"over_supply_v,omitempty" jsonschema:"how far the IC pin peak exceeds its supply rail"`
	AtConn    string   `json:"clamp_at_connector,omitempty" jsonschema:"the same with the clamp moved to the connector: how much placement matters"`
	Layout    string   `json:"layout,omitempty" jsonschema:"clamp stub and ground path as laid out"`
	Unclamped bool     `json:"unclamped,omitempty"`
	Notes     []string `json:"notes,omitempty"`
	Error     string   `json:"error,omitempty"`
}

type esdIn struct {
	target
	Limit int `json:"limit,omitempty" jsonschema:"lines listed, worst first; default 30"`
}

type esdOut struct {
	analysis
	Level       string    `json:"level"`
	Lines       int       `json:"lines"`
	Unclamped   int       `json:"unclamped" jsonschema:"lines with no clamp at all"`
	Worst       []esdLine `json:"worst" jsonschema:"worst first: IC pins furthest over their supply, then the highest peaks"`
	Assumptions []string  `json:"assumptions,omitempty"`
	Caveat      string    `json:"caveat"`
}

const esdCaveat = "A circuit simulation of the discharge into each line with the routing as extracted and clamp models of the stated quality; illustrative models give a trend, not a number. It compares placements and versions; it does not predict an IEC 61000-4-2 test result."

func (s *Server) esd(ctx context.Context, _ *mcpRequest, in esdIn) (*mcpResult, esdOut, error) {
	c, _, err := s.client(ctx)
	if err != nil {
		return nil, esdOut{}, err
	}
	r, a, err := s.runOf(ctx, c, in.target, emi.RunKindTransient, "run_esd")
	if err != nil {
		return nil, esdOut{}, err
	}
	raw, err := s.cache.get(ctx, c, r.ID, "transient.json")
	if err != nil {
		return nil, esdOut{}, err
	}
	var d transientDoc
	if err := json.Unmarshal(raw, &d); err != nil {
		return nil, esdOut{}, fmt.Errorf("transient.json of run %s did not parse: %w", r.ID, err)
	}
	out := esdOut{analysis: a, Level: fmt.Sprintf("level %d, %g kV contact", d.Level, d.KV), Lines: len(d.Lines),
		Assumptions: d.Assumptions, Caveat: esdCaveat, Worst: []esdLine{}}
	type ranked struct {
		l    esdLine
		over float64
	}
	var all []ranked
	for _, l := range d.Lines {
		e := esdLine{Connector: l.Connector.Ref + "." + l.Connector.Pad, Net: l.Net, Notes: l.Notes, Error: l.Error}
		if l.Clamp != nil {
			e.Clamp = fmt.Sprintf("%s %s (%s model)", l.Clamp.Ref, l.Clamp.Part, l.Clamp.Model)
			parts := []string{fmt.Sprintf("ground path %.1f mm (%.2g nH)", l.Geometry.ClampGroundMM, l.Geometry.ClampGroundNH)}
			if l.Geometry.ClampStub != nil {
				parts = append([]string{fmt.Sprintf("clamp stub %.1f mm", l.Geometry.ClampStub.LengthMM)}, parts...)
			}
			e.Layout = strings.Join(parts, ", ")
		}
		if l.Series != nil {
			e.Clamp = strings.TrimSpace(e.Clamp + fmt.Sprintf("; series %s %g ohm", l.Series.Ref, l.Series.Ohm))
		}
		if l.IC != nil {
			e.IC = fmt.Sprintf("%s.%s (%g V supply)", l.IC.Ref, l.IC.Pad, l.IC.SupplyV)
		}
		over := math.Inf(-1)
		for _, v := range l.Variants {
			p, where := peak(v.VPin, v.VClamp, v.VConnector)
			switch v.ID {
			case "as_laid_out":
				e.PeakV = math.Round(p*10) / 10
				e.Peak = fmt.Sprintf("%.0f V at the %s", p, where)
				e.Unclamped = v.Unclamped
				if where == "pin" && l.IC != nil {
					over = p - l.IC.SupplyV
					e.OverPinV = math.Round(over*10) / 10
				}
			case "clamp_at_connector":
				e.AtConn = fmt.Sprintf("%.0f V at the %s", p, where)
			}
		}
		if e.Unclamped {
			out.Unclamped++
		}
		all = append(all, ranked{e, over})
	}
	sort.SliceStable(all, func(i, j int) bool {
		if all[i].over != all[j].over {
			return all[i].over > all[j].over
		}
		return all[i].l.PeakV > all[j].l.PeakV
	})
	limit := clamp(in.Limit, 30, 500)
	for i, x := range all {
		if i == limit {
			break
		}
		out.Worst = append(out.Worst, x.l)
	}
	return nil, out, nil
}

// peak is the voltage the ESD panel shows for a variant, and where it was measured (same
// choice as peakOf in webapp/src/lib/compare.ts).
func peak(pin, clamp *float64, connector float64) (float64, string) {
	if pin != nil {
		return *pin, "pin"
	}
	if clamp != nil {
		return *clamp, "clamp"
	}
	return connector, "connector"
}

// ---- cables ----

// cableLibrary.json is worker/emi_worker/cables/library.json, copied (Go embeds only files
// inside its module); a test fails when the two differ.
//
//go:embed cableLibrary.json
var cableLibraryJSON []byte

type cableType struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

var cableTypes = func() []cableType {
	var d struct {
		Cables []cableType `json:"cables"`
	}
	if err := json.Unmarshal(cableLibraryJSON, &d); err != nil {
		panic("cableLibrary.json: " + err.Error())
	}
	return d.Cables
}()

type cableSpec struct {
	Type    string  `json:"type" jsonschema:"a cable type id from cables (e.g. usb2-shielded), or none for a connector that is never cabled"`
	LengthM float64 `json:"length_m,omitempty" jsonschema:"cable length in meters; default the type's"`
}

type runCablesIn struct {
	target
	Connectors map[string]cableSpec `json:"connectors,omitempty" jsonschema:"connector ref to its cable, e.g. {\"J1\": {\"type\": \"usb2-shielded\", \"length_m\": 1}}; connectors left out are listed with a suggestion and not modeled"`
	Standard   string               `json:"standard_id,omitempty" jsonschema:"radiated limit to budget against; default fcc-15b-radiated-3m"`
	WaitS      int                  `json:"wait_s,omitempty"`
}

func (s *Server) runCables(ctx context.Context, req *mcpRequest, in runCablesIn) (*mcpResult, startedOut, error) {
	c, _, err := s.client(ctx)
	if err != nil {
		return nil, startedOut{}, err
	}
	a, err := resolve(ctx, c, in.target)
	if err != nil {
		return nil, startedOut{}, err
	}
	conns := map[string]any{}
	for ref, spec := range in.Connectors {
		if spec.Type == "" {
			return nil, startedOut{}, fmt.Errorf("connector %s needs a type (cables lists them, or none)", ref)
		}
		if spec.LengthM > 0 {
			conns[ref] = map[string]any{"type": spec.Type, "length_m": spec.LengthM}
		} else {
			conns[ref] = spec.Type
		}
	}
	std := in.Standard
	if std == "" {
		std = "fcc-15b-radiated-3m"
	}
	r, err := c.CreateRun(ctx, a.ProjectID, a.BoardID, emi.RunKindCable, map[string]any{"connectors": conns, "standard_id": std})
	if err != nil {
		return nil, startedOut{}, err
	}
	out, err := s.wait(ctx, req, c, r.ID, in.WaitS)
	if err != nil {
		return nil, startedOut{}, err
	}
	return nil, startedOut{analysis: a, Run: out}, nil
}

type cablesIn struct {
	target
}

type cableOut struct {
	Connector string  `json:"connector"`
	Cable     string  `json:"cable,omitempty"`
	Tightest  string  `json:"tightest,omitempty" jsonschema:"the frequency where the cable may carry the least common-mode current, and how much"`
	MaxDBuA   float64 `json:"max_common_mode_dbua,omitempty"`
	Peaks     string  `json:"radiates_best_at,omitempty"`
	Note      string  `json:"note,omitempty"`
}

type cablesOut struct {
	analysis
	Types      []cableType `json:"cable_types" jsonschema:"what run_cables accepts"`
	Cables     []cableOut  `json:"cables,omitempty" jsonschema:"tightest budget first"`
	Unassigned []string    `json:"unassigned,omitempty" jsonschema:"connectors with no cable declared: not modeled, with the suggested type"`
	Caveat     string      `json:"caveat"`
}

const cablesCaveat = "The common-mode current each cable can carry before its radiation alone reaches the limit, from an antenna model of the cable (nec2c). It ranks connectors and versions; how much current the board actually puts on a cable is not modeled here."

func (s *Server) cables(ctx context.Context, _ *mcpRequest, in cablesIn) (*mcpResult, cablesOut, error) {
	out := cablesOut{Types: cableTypes, Caveat: cablesCaveat}
	c, _, err := s.client(ctx)
	if err != nil {
		return nil, cablesOut{}, err
	}
	if in.RunID == "" && in.ProjectID == "" {
		return nil, out, nil // just the types
	}
	r, a, err := s.runOf(ctx, c, in.target, emi.RunKindCable, "run_cables")
	if err != nil {
		return nil, cablesOut{}, err
	}
	out.analysis = a
	raw, err := s.cache.get(ctx, c, r.ID, "cables.json")
	if err != nil {
		return nil, cablesOut{}, err
	}
	var d struct {
		Cables []struct {
			Ref          string    `json:"ref"`
			CableID      *string   `json:"cable_id"`
			CableName    string    `json:"cable_name"`
			LengthM      float64   `json:"length_m"`
			DeclaredNone bool      `json:"declared_none"`
			Peaks        []float64 `json:"radiation_peaks_hz"`
			Tightest     *struct {
				Hz    float64 `json:"frequency_hz"`
				DBuA  float64 `json:"max_current_dbua"`
				Limit float64 `json:"limit_dbuv_per_m"`
			} `json:"tightest"`
		} `json:"cables"`
		Unassigned []struct {
			Ref       string  `json:"ref"`
			Footprint string  `json:"footprint"`
			Suggested *string `json:"suggested"`
			Reason    string  `json:"reason"`
		} `json:"unassigned"`
	}
	if err := json.Unmarshal(raw, &d); err != nil {
		return nil, cablesOut{}, fmt.Errorf("cables.json of run %s did not parse: %w", r.ID, err)
	}
	for _, cb := range d.Cables {
		o := cableOut{Connector: cb.Ref}
		if cb.DeclaredNone {
			o.Note = "never cabled"
		} else {
			o.Cable = strings.TrimSpace(fmt.Sprintf("%s, %g m", cb.CableName, cb.LengthM))
			if cb.Tightest != nil {
				o.MaxDBuA = math.Round(cb.Tightest.DBuA*10) / 10
				o.Tightest = fmt.Sprintf("%.1f dBuA (%.3g uA) at %s", cb.Tightest.DBuA, math.Pow(10, cb.Tightest.DBuA/20), hz(cb.Tightest.Hz))
			}
			var ps []string
			for _, p := range cb.Peaks {
				ps = append(ps, hz(p))
			}
			o.Peaks = strings.Join(ps, ", ")
		}
		out.Cables = append(out.Cables, o)
	}
	sort.SliceStable(out.Cables, func(i, j int) bool {
		if (out.Cables[i].Tightest == "") != (out.Cables[j].Tightest == "") {
			return out.Cables[i].Tightest != ""
		}
		return out.Cables[i].MaxDBuA < out.Cables[j].MaxDBuA
	})
	for _, u := range d.Unassigned {
		line := u.Ref + " (" + u.Footprint + ")"
		if u.Suggested != nil {
			line += ": suggested " + *u.Suggested
		}
		if u.Reason != "" {
			line += ", " + u.Reason
		}
		out.Unassigned = append(out.Unassigned, line)
	}
	return nil, out, nil
}

func (s *Server) registerESDCables(srv *mcp.Server) {
	mcp.AddTool(srv, &mcp.Tool{
		Name: "run_esd",
		Description: "Start an ESD simulation on an analyzed version: an IEC 61000-4-2 contact discharge into every line " +
			"that leaves the board through a connector (or the named lines), in ngspice, with each clamp where it is and " +
			"moved to the connector. Takes seconds to minutes; wait_s waits. Read it with esd.",
		Annotations: &mcp.ToolAnnotations{Title: "Start ESD simulation", OpenWorldHint: &no, DestructiveHint: &no},
	}, s.runESD)
	mcp.AddTool(srv, &mcp.Tool{
		Name: "esd",
		Description: "The newest ESD simulation of a version: per connector line, the clamp, the peak voltage as laid out " +
			"(at the IC pin when there is one) and with the clamp at the connector, worst first.",
		Annotations: readOnly("ESD results"),
	}, s.esd)
	mcp.AddTool(srv, &mcp.Tool{
		Name: "run_cables",
		Description: "Start a cable budget on an analyzed version: for each connector you give a cable type and length, how " +
			"much common-mode current the cable can carry before it radiates past the limit. Call cables first for the types " +
			"and the connectors' suggestions.",
		Annotations: &mcp.ToolAnnotations{Title: "Start cable budget", OpenWorldHint: &no, DestructiveHint: &no},
	}, s.runCables)
	mcp.AddTool(srv, &mcp.Tool{
		Name: "cables",
		Description: "The cable types run_cables accepts and, for a version with a finished cable budget, each connector's " +
			"budget (tightest first) and the connectors no cable was declared for, with a suggested type.",
		Annotations: readOnly("Cable budgets"),
	}, s.cables)
}
