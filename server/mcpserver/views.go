package mcpserver

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"os/exec"
	"runtime"
	"sort"
	"strings"

	"github.com/embeddedci-com/emi-analyzer/server/compare"
)

// ---- decoupling ----

// decouplingDoc is rules.json's decoupling view (webapp/src/lib/decoupling.ts). The worker
// has already judged it -- status, gaps, worst point, recommendations -- so nothing of that is
// recomputed here; only the impedance curves are rebuilt from its branches (impedance.go).
type decouplingDoc struct {
	FMinHz          float64   `json:"f_min_hz"`
	FMaxHz          float64   `json:"f_max_hz"`
	PointsPerDecade int       `json:"points_per_decade"`
	StackupAssumed  bool      `json:"stackup_assumed"`
	Note            string    `json:"note"`
	Rails           []decRail `json:"rails"`
}

type decRail struct {
	Net      string  `json:"net"`
	V        float64 `json:"v"`
	VAssumed bool    `json:"v_assumed"`
	Plane    *struct {
		Layer       string  `json:"layer"`
		GroundLayer string  `json:"ground_layer"`
		CF          float64 `json:"c_f"`
		ResonanceHz float64 `json:"resonance_hz"`
	} `json:"plane"`
	Status string             `json:"status"`
	Parts  map[string]decPart `json:"parts"`
	ICs    []decIC            `json:"ics"`
}

type decPart struct {
	Value   string  `json:"value"`
	Package string  `json:"package"`
	Basis   string  `json:"basis"`
	Source  string  `json:"source"`
	CF      float64 `json:"c_f"`
	ESROhm  float64 `json:"esr_ohm"`
}

type decIC struct {
	Ref         string     `json:"ref"`
	Pins        []string   `json:"pins"`
	TargetOhm   float64    `json:"target_ohm"`
	BandHz      float64    `json:"band_hz"`
	SeriesLH    float64    `json:"series_l_h"`
	PlaneBranch *decBranch `json:"plane_branch"`
	Caps        []struct {
		Ref          string  `json:"ref"`
		Pin          string  `json:"pin"`
		LH           float64 `json:"l_h"`
		DistanceMM   float64 `json:"distance_mm"`
		UsefulUpToHz float64 `json:"useful_up_to_hz"`
		GroundVias   int     `json:"ground_vias"`
	} `json:"caps"`
	Assumed         []string            `json:"assumed"`
	Recommendations []decRecommendation `json:"recommendations"`
	Status          string              `json:"status"`
	Gaps            [][]float64         `json:"gaps"`
	AntiResonances  []struct {
		Hz  float64 `json:"hz"`
		Ohm float64 `json:"ohm"`
	} `json:"anti_resonances"`
	Worst *struct {
		Hz       float64 `json:"hz"`
		Ohm      float64 `json:"ohm"`
		ExcessDB float64 `json:"excess_db"`
	} `json:"worst"`
}

type decRecommendation struct {
	Kind          string     `json:"kind"`
	Text          string     `json:"text"`
	Change        *decChange `json:"change"`
	ImprovementDB float64    `json:"improvement_db"`
	AtHz          float64    `json:"at_hz"`
}

type decouplingIn struct {
	target
	Rail string `json:"rail,omitempty" jsonschema:"one supply net, e.g. +3V3: lists each IC on it with its capacitors"`
	IC   string `json:"ic,omitempty" jsonschema:"one IC by reference, e.g. U3, on whichever rails it has"`
	// Experimental: see impedanceNote.
	Impedance   bool      `json:"impedance,omitempty" jsonschema:"experimental: with rail or ic, also return each IC's supply impedance |Z| in ohms at its pins, as laid out and with each recommended change applied; a lumped estimate from the layout, 100 kHz to 1 GHz"`
	ImpedanceHz []float64 `json:"impedance_hz,omitempty" jsonschema:"experimental: the frequencies in Hz for the impedance (implies impedance), 10 kHz to 1 GHz; below 100 kHz the result carries a warning; default a log grid, 10 points per decade over the model's range"`
}

type railBrief struct {
	Net         string  `json:"net"`
	V           float64 `json:"v"`
	VAssumed    bool    `json:"v_assumed,omitempty"`
	Status      string  `json:"status" jsonschema:"ok, or gaps when an IC's supply impedance exceeds its target somewhere"`
	Plane       string  `json:"plane,omitempty"`
	ICs         int     `json:"ics"`
	ICsWithGaps int     `json:"ics_with_gaps"`
	Worst       string  `json:"worst,omitempty" jsonschema:"the IC furthest over its target, and where"`
}

type icOut struct {
	Rail            string        `json:"rail"`
	Ref             string        `json:"ref"`
	Status          string        `json:"status"`
	TargetOhm       float64       `json:"target_ohm"`
	Worst           string        `json:"worst,omitempty"`
	Gaps            []string      `json:"gaps,omitempty" jsonschema:"frequency ranges above the target"`
	Caps            []string      `json:"caps" jsonschema:"each capacitor, how far from the pin, and up to where it helps"`
	Recommendations []string      `json:"recommendations,omitempty" jsonschema:"up to three changes that help, best first"`
	Assumed         []string      `json:"assumed,omitempty"`
	Impedance       *impedanceOut `json:"impedance,omitempty" jsonschema:"experimental: only when asked for"`
}

type decouplingOut struct {
	analysis
	Note    string      `json:"note,omitempty"`
	Rails   []railBrief `json:"rails,omitempty"`
	ICs     []icOut     `json:"ics,omitempty"`
	Missing string      `json:"missing,omitempty"`
}

func (s *Server) decoupling(ctx context.Context, _ *mcpRequest, in decouplingIn) (*mcpResult, decouplingOut, error) {
	_, a, d, err := s.rules(ctx, in.target)
	if err != nil {
		return nil, decouplingOut{}, err
	}
	out := decouplingOut{analysis: a}
	if d.Decoupling == nil || len(d.Decoupling.Rails) == 0 {
		out.Missing = "This analysis has no decoupling view: no supply rail with ICs and capacitors was recognized, or the board was analyzed by an older version."
		return nil, out, nil
	}
	out.Note = d.Decoupling.Note
	wantZ := in.Impedance || len(in.ImpedanceHz) > 0
	var freqs []float64
	var zWarning string
	if wantZ {
		if in.Rail == "" && in.IC == "" {
			return nil, decouplingOut{}, fmt.Errorf("impedance needs a rail or an ic")
		}
		if freqs, zWarning, err = impedanceGrid(d.Decoupling, in.ImpedanceHz); err != nil {
			return nil, decouplingOut{}, err
		}
	}
	if in.Rail == "" && in.IC == "" {
		for _, r := range d.Decoupling.Rails {
			b := railBrief{Net: r.Net, V: r.V, VAssumed: r.VAssumed, Status: r.Status, ICs: len(r.ICs)}
			if r.Plane != nil {
				b.Plane = fmt.Sprintf("%s over %s, %s, resonance %s", r.Plane.Layer, r.Plane.GroundLayer, farads(r.Plane.CF), hz(r.Plane.ResonanceHz))
			}
			worst := -1e9
			for _, ic := range r.ICs {
				if ic.Status == "gaps" {
					b.ICsWithGaps++
				}
				if ic.Worst != nil && ic.Worst.ExcessDB > worst {
					worst = ic.Worst.ExcessDB
					b.Worst = fmt.Sprintf("%s: %.1f dB over at %s", ic.Ref, ic.Worst.ExcessDB, hz(ic.Worst.Hz))
				}
			}
			out.Rails = append(out.Rails, b)
		}
		sort.SliceStable(out.Rails, func(i, j int) bool { return out.Rails[i].ICsWithGaps > out.Rails[j].ICsWithGaps })
		return nil, out, nil
	}
	for _, r := range d.Decoupling.Rails {
		if in.Rail != "" && !strings.EqualFold(r.Net, in.Rail) {
			continue
		}
		for _, ic := range r.ICs {
			if in.IC != "" && !strings.EqualFold(ic.Ref, in.IC) {
				continue
			}
			o := icOut{Rail: r.Net, Ref: ic.Ref, Status: ic.Status, TargetOhm: ic.TargetOhm, Assumed: ic.Assumed, Caps: []string{}}
			if ic.Worst != nil {
				o.Worst = fmt.Sprintf("%.3g ohm at %s, %.1f dB over", ic.Worst.Ohm, hz(ic.Worst.Hz), ic.Worst.ExcessDB)
			}
			for _, g := range ic.Gaps {
				if len(g) == 2 {
					o.Gaps = append(o.Gaps, hz(g[0])+" to "+hz(g[1]))
				}
			}
			for _, c := range ic.Caps {
				part := r.Parts[c.Ref]
				vias := "ground vias"
				if c.GroundVias == 1 {
					vias = "ground via"
				}
				o.Caps = append(o.Caps, fmt.Sprintf("%s %s %s at pin %s, %.1f mm, %d %s, useful up to %s",
					c.Ref, part.Value, part.Package, c.Pin, c.DistanceMM, c.GroundVias, vias, hz(c.UsefulUpToHz)))
			}
			// The worker ranks every change it tried, including ones that make it worse; only
			// the ones that help are advice.
			var helps []decRecommendation
			for _, rec := range ic.Recommendations {
				if rec.ImprovementDB < 0.1 || len(helps) == 3 {
					continue
				}
				helps = append(helps, rec)
				o.Recommendations = append(o.Recommendations, fmt.Sprintf("%s (%.1f dB better at %s)", rec.Text, rec.ImprovementDB, hz(rec.AtHz)))
			}
			if wantZ {
				o.Impedance = impedanceOf(ic, r.Parts, freqs, zWarning, helps)
			}
			out.ICs = append(out.ICs, o)
		}
	}
	if len(out.ICs) == 0 {
		return nil, decouplingOut{}, fmt.Errorf("no decoupling data for rail %q ic %q; call decoupling without them to list the rails", in.Rail, in.IC)
	}
	return nil, out, nil
}

func hz(f float64) string {
	switch {
	case f >= 1e9:
		return fmt.Sprintf("%.3g GHz", f/1e9)
	case f >= 1e6:
		return fmt.Sprintf("%.3g MHz", f/1e6)
	case f >= 1e3:
		return fmt.Sprintf("%.3g kHz", f/1e3)
	}
	return fmt.Sprintf("%.3g Hz", f)
}

func farads(c float64) string {
	switch {
	case c >= 1e-6:
		return fmt.Sprintf("%.3g uF", c*1e6)
	case c >= 1e-9:
		return fmt.Sprintf("%.3g nF", c*1e9)
	}
	return fmt.Sprintf("%.3g pF", c*1e12)
}

// ---- nets ----

type netRow struct {
	Net             string   `json:"net"`
	Netclass        string   `json:"netclass,omitempty"`
	Kind            string   `json:"kind,omitempty"`
	Topology        string   `json:"topology,omitempty"`
	Pads            *int     `json:"pads,omitempty"`
	CopperMM        float64  `json:"copper_mm"`
	PathMM          *float64 `json:"path_mm,omitempty"`
	PathFrom        string   `json:"path_from,omitempty"`
	PathTo          string   `json:"path_to,omitempty"`
	PathVias        *int     `json:"path_vias,omitempty"`
	ViasTotal       *int     `json:"vias_total,omitempty"`
	DelayPS         *float64 `json:"delay_ps,omitempty"`
	WidthMinMM      *float64 `json:"width_min_mm,omitempty"`
	WidthMaxMM      *float64 `json:"width_max_mm,omitempty"`
	DiffPairPartner string   `json:"diff_pair_partner,omitempty"`
	MatchGroup      string   `json:"match_group,omitempty"`
	MatchReference  string   `json:"match_reference,omitempty"`
	SkewPS          *float64 `json:"skew_ps,omitempty"`
	SkewMM          *float64 `json:"skew_mm,omitempty"`
	TolerancePS     *float64 `json:"tolerance_ps,omitempty"`
	WithinTolerance string   `json:"within_tolerance,omitempty"`
}

type netsIn struct {
	target
	Net        string `json:"net,omitempty" jsonschema:"only nets containing this text, case-insensitive"`
	MatchGroup string `json:"match_group,omitempty" jsonschema:"only one length-matching group"`
	OutOfTol   bool   `json:"out_of_tolerance,omitempty" jsonschema:"only nets whose skew is outside the group's tolerance"`
	Limit      int    `json:"limit,omitempty" jsonschema:"default 50, max 500"`
	Offset     int    `json:"offset,omitempty"`
}

type netsOut struct {
	analysis
	ClockNet   string   `json:"clock_net,omitempty"`
	Groups     []string `json:"match_groups,omitempty" jsonschema:"every length-matching group on the board"`
	Matched    int      `json:"matched"`
	NextOffset int      `json:"next_offset,omitempty"`
	Nets       []netRow `json:"nets" jsonschema:"lengths in mm, delays in ps"`
}

func (s *Server) nets(ctx context.Context, _ *mcpRequest, in netsIn) (*mcpResult, netsOut, error) {
	c, _, err := s.client(ctx)
	if err != nil {
		return nil, netsOut{}, err
	}
	a, err := resolve(ctx, c, in.target)
	if err != nil {
		return nil, netsOut{}, err
	}
	raw, err := s.cache.get(ctx, c, a.RunID, "nets.json")
	if err != nil {
		return nil, netsOut{}, err
	}
	var d struct {
		ClockNet string   `json:"clock_net"`
		Rows     []netRow `json:"rows"`
	}
	if err := json.Unmarshal(raw, &d); err != nil {
		return nil, netsOut{}, fmt.Errorf("nets.json of run %s did not parse: %w", a.RunID, err)
	}
	out := netsOut{analysis: a, ClockNet: d.ClockNet, Nets: []netRow{}}
	groups := map[string]bool{}
	var matched []netRow
	for _, r := range d.Rows {
		if r.MatchGroup != "" {
			groups[r.MatchGroup] = true
		}
		if in.Net != "" && !strings.Contains(strings.ToLower(r.Net), strings.ToLower(in.Net)) {
			continue
		}
		if in.MatchGroup != "" && r.MatchGroup != in.MatchGroup {
			continue
		}
		if in.OutOfTol && (r.WithinTolerance == "" || r.WithinTolerance == "yes") {
			continue
		}
		matched = append(matched, r)
	}
	for g := range groups {
		out.Groups = append(out.Groups, g)
	}
	sort.Strings(out.Groups)
	limit := clamp(in.Limit, 50, 500)
	out.Matched = len(matched)
	if in.Offset < len(matched) {
		end := min(in.Offset+limit, len(matched))
		out.Nets = append(out.Nets, matched[in.Offset:end]...)
		if end < len(matched) {
			out.NextOffset = end
		}
	}
	return nil, out, nil
}

// ---- compare ----

type compareIn struct {
	ProjectID string `json:"project_id"`
	Before    string `json:"before_board_id,omitempty" jsonschema:"the older version; default: the second newest analyzed one"`
	After     string `json:"after_board_id,omitempty" jsonschema:"the newer version; default: the newest analyzed one"`
	Unchanged bool   `json:"include_unchanged,omitempty" jsonschema:"also list findings present in both (long)"`
	Limit     int    `json:"limit,omitempty" jsonschema:"findings listed per status, default 50"`
}

type changeBrief struct {
	ID       string   `json:"id" jsonschema:"in the version it belongs to: before for fixed, after for new and unchanged"`
	Severity string   `json:"severity"`
	Title    string   `json:"title"`
	Net      string   `json:"net,omitempty"`
	Layer    string   `json:"layer,omitempty"`
	X        *float64 `json:"x,omitempty"`
	Y        *float64 `json:"y,omitempty"`
}

type ruleChanges struct {
	Rule      string        `json:"rule"`
	Title     string        `json:"title"`
	New       []changeBrief `json:"new,omitempty"`
	Fixed     []changeBrief `json:"fixed,omitempty"`
	Unchanged int           `json:"unchanged"`
	Still     []changeBrief `json:"still,omitempty"`
}

type versionRef struct {
	Version int    `json:"version"`
	BoardID string `json:"board_id"`
	RunID   string `json:"run_id"`
}

type compareOut struct {
	ProjectID string                            `json:"project_id"`
	Before    versionRef                        `json:"before"`
	After     versionRef                        `json:"after"`
	Headline  string                            `json:"headline"`
	Counts    map[string]compare.SeverityCounts `json:"counts"`
	Rules     []ruleChanges                     `json:"rules" jsonschema:"rules with something new first"`
	Nets      []compare.NetChange               `json:"nets,omitempty" jsonschema:"nets whose routed length or via count changed, biggest change first (top 30)"`
	NetsMore  int                               `json:"nets_more,omitempty"`
	Truncated bool                              `json:"truncated,omitempty"`
}

func (s *Server) compareVersions(ctx context.Context, _ *mcpRequest, in compareIn) (*mcpResult, compareOut, error) {
	if in.ProjectID == "" {
		return nil, compareOut{}, fmt.Errorf("project_id is required")
	}
	c, _, err := s.client(ctx)
	if err != nil {
		return nil, compareOut{}, err
	}
	p, err := c.GetProject(ctx, in.ProjectID)
	if err != nil {
		return nil, compareOut{}, err
	}
	boards, err := c.ListBoards(ctx, in.ProjectID)
	if err != nil {
		return nil, compareOut{}, err
	}
	runs, err := c.ListRuns(ctx, in.ProjectID)
	if err != nil {
		return nil, compareOut{}, err
	}
	vs := versions(p, boards, runs).Versions
	var analyzed []versionOut
	for _, v := range vs {
		if v.Analysis != nil {
			analyzed = append(analyzed, v)
		}
	}
	pick := func(id string) (versionOut, bool) {
		for _, v := range vs {
			if v.BoardID == id {
				return v, true
			}
		}
		return versionOut{}, false
	}
	var before, after versionOut
	switch {
	case in.After != "":
		v, ok := pick(in.After)
		if !ok {
			return nil, compareOut{}, fmt.Errorf("project %s has no version %s", in.ProjectID, in.After)
		}
		after = v
	case len(analyzed) > 0:
		after = analyzed[len(analyzed)-1]
	}
	switch {
	case in.Before != "":
		v, ok := pick(in.Before)
		if !ok {
			return nil, compareOut{}, fmt.Errorf("project %s has no version %s", in.ProjectID, in.Before)
		}
		before = v
	default:
		for i := len(analyzed) - 1; i >= 0; i-- {
			if analyzed[i].BoardID != after.BoardID && analyzed[i].Number < after.Number {
				before = analyzed[i]
				break
			}
		}
	}
	if before.BoardID == "" || after.BoardID == "" {
		return nil, compareOut{}, fmt.Errorf("project %s needs two analyzed versions to compare; it has %d", in.ProjectID, len(analyzed))
	}
	if before.Analysis == nil || after.Analysis == nil {
		return nil, compareOut{}, fmt.Errorf("both versions need a finished analysis; list_versions shows which do")
	}

	load := func(runID string) (*rulesDoc, error) {
		raw, err := s.cache.get(ctx, c, runID, "rules.json")
		if err != nil {
			return nil, err
		}
		var d rulesDoc
		return &d, json.Unmarshal(raw, &d)
	}
	rb, err := load(before.Analysis.ID)
	if err != nil {
		return nil, compareOut{}, err
	}
	ra, err := load(after.Analysis.ID)
	if err != nil {
		return nil, compareOut{}, err
	}
	diff := compare.DiffFindings(toCompare(rb.Findings), toCompare(ra.Findings), compare.MatchToleranceMM)
	out := compareOut{ProjectID: p.ID,
		Before:   versionRef{Version: before.Number, BoardID: before.BoardID, RunID: before.Analysis.ID},
		After:    versionRef{Version: after.Number, BoardID: after.BoardID, RunID: after.Analysis.ID},
		Headline: compare.Headline(diff.Counts), Counts: diff.Counts, Rules: []ruleChanges{}}
	limit := clamp(in.Limit, 50, 500)
	listed := map[string]int{}
	for _, g := range compare.GroupByRule(diff.Changes) {
		rc := ruleChanges{Rule: g.Rule, Title: ruleTitle(g.Rule)}
		for _, ch := range g.Changes {
			f := ch.After
			if f == nil {
				f = ch.Before
			}
			b := changeBrief{ID: f.ID, Severity: f.Severity, Title: f.Title, Net: f.Net, Layer: f.Layer, X: round2(f.X), Y: round2(f.Y)}
			if ch.Status == compare.Unchanged {
				rc.Unchanged++
				if !in.Unchanged {
					continue
				}
			}
			if listed[ch.Status] >= limit {
				out.Truncated = true
				continue
			}
			listed[ch.Status]++
			switch ch.Status {
			case compare.New:
				rc.New = append(rc.New, b)
			case compare.Fixed:
				rc.Fixed = append(rc.Fixed, b)
			default:
				rc.Still = append(rc.Still, b)
			}
		}
		out.Rules = append(out.Rules, rc)
	}

	// Lengths are a lesser question: a failure to read them leaves them out, not the answer.
	var nb, na compare.NetReport
	if raw, err := s.cache.get(ctx, c, before.Analysis.ID, "nets.json"); err == nil && json.Unmarshal(raw, &nb) == nil {
		if raw, err := s.cache.get(ctx, c, after.Analysis.ID, "nets.json"); err == nil && json.Unmarshal(raw, &na) == nil {
			nets := compare.DiffNets(nb, na)
			if len(nets) > 30 {
				out.NetsMore = len(nets) - 30
				nets = nets[:30]
			}
			out.Nets = nets
		}
	}
	return nil, out, nil
}

func toCompare(fs []finding) []compare.Finding {
	out := make([]compare.Finding, len(fs))
	for i, f := range fs {
		out[i] = compare.Finding{ID: f.ID, Rule: f.Rule, Severity: f.Severity, Title: f.Title, Net: f.Net,
			Layer: f.Layer, X: f.X, Y: f.Y, Action: f.Action}
	}
	return out
}

// ---- open_in_app ----

type openIn struct {
	ProjectID string `json:"project_id"`
	BoardID   string `json:"board_id,omitempty" jsonschema:"a version to show; default the newest"`
	Compare   bool   `json:"compare,omitempty" jsonschema:"open the comparison of two versions instead"`
	Before    string `json:"before_board_id,omitempty"`
	Open      bool   `json:"open,omitempty" jsonschema:"also open it in the user's browser"`
}

type openOut struct {
	URL    string `json:"url"`
	Opened bool   `json:"opened"`
}

// The local app serves the webapp under /tools/emi (webapp/app/main.tsx, src/routes.tsx).
const appBase = "/tools/emi"

func (s *Server) openInApp(ctx context.Context, _ *mcpRequest, in openIn) (*mcpResult, openOut, error) {
	if in.ProjectID == "" {
		return nil, openOut{}, fmt.Errorf("project_id is required")
	}
	c, e, err := s.client(ctx)
	if err != nil {
		return nil, openOut{}, err
	}
	if _, err := c.GetProject(ctx, in.ProjectID); err != nil {
		return nil, openOut{}, err
	}
	u := e.URL + appBase + "/" + url.PathEscape(in.ProjectID)
	q := url.Values{}
	if in.Compare {
		u += "/compare"
		if in.Before != "" {
			q.Set("before", in.Before)
		}
		if in.BoardID != "" {
			q.Set("after", in.BoardID)
		}
	} else if in.BoardID != "" {
		q.Set("version", in.BoardID)
	}
	if len(q) > 0 {
		u += "?" + q.Encode()
	}
	out := openOut{URL: u}
	if in.Open {
		out.Opened = openBrowser(u) == nil
	}
	return nil, out, nil
}

// openBrowser is a variable so tests do not open one.
var openBrowser = func(u string) error {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", u)
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", u)
	default:
		cmd = exec.Command("xdg-open", u)
	}
	return cmd.Start()
}
