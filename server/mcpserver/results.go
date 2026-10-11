package mcpserver

import (
	"context"
	_ "embed"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"
	"sync"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
	"github.com/embeddedci-com/emi-analyzer/server/emiclient"
)

// Everything an analysis found is in the ingest run's artifacts: rules.json (the findings and
// the decoupling view) and nets.json (lengths, delays, skew). The tools below read them and
// hand the agent a shaped, paginated slice: a whole rules.json is hundreds of kilobytes, far
// more than an agent should read to answer one question.

// target names the analysis a tool reads: a run, or a version of a project, or a project's
// newest analysed version.
type target struct {
	RunID     string `json:"run_id,omitempty" jsonschema:"an ingest run (analyze_board returns it)"`
	ProjectID string `json:"project_id,omitempty" jsonschema:"a project; without board_id its newest analysed version is read"`
	BoardID   string `json:"board_id,omitempty" jsonschema:"a version of the project (needs project_id)"`
}

// analysis is the ingest run a target resolved to.
type analysis struct {
	RunID     string `json:"run_id"`
	ProjectID string `json:"project_id"`
	BoardID   string `json:"board_id"`
	// Note says when a newer version exists that is not analysed yet.
	Note string `json:"note,omitempty"`
}

// resolve finds the finished ingest a target names.
func resolve(ctx context.Context, c *emiclient.Client, t target) (analysis, error) {
	if t.RunID != "" {
		r, err := c.GetRun(ctx, t.RunID)
		if err != nil {
			return analysis{}, err
		}
		if r.Kind != emi.RunKindIngest {
			return analysis{}, fmt.Errorf("run %s is a %s run; findings come from the board's analysis (an ingest run)", r.ID, r.Kind)
		}
		if err := finished(r); err != nil {
			return analysis{}, err
		}
		return analysis{RunID: r.ID, ProjectID: r.ProjectID, BoardID: r.BoardID}, nil
	}
	if t.ProjectID == "" {
		return analysis{}, fmt.Errorf("name the analysis: run_id, or project_id (with board_id for a specific version)")
	}
	runs, err := c.ListRuns(ctx, t.ProjectID)
	if err != nil {
		return analysis{}, err
	}
	sort.SliceStable(runs, func(i, j int) bool { return runs[i].CreatedAt.After(runs[j].CreatedAt) })
	var newest *emi.Run
	for i, r := range runs {
		if r.Kind != emi.RunKindIngest || (t.BoardID != "" && r.BoardID != t.BoardID) {
			continue
		}
		if newest == nil {
			newest = &runs[i]
		}
		if r.Status == emi.StatusDone {
			a := analysis{RunID: r.ID, ProjectID: r.ProjectID, BoardID: r.BoardID}
			if newest.ID != r.ID {
				a.Note = fmt.Sprintf("A newer analysis (run %s) is %s; these are the newest finished results.", newest.ID, newest.Status)
			}
			return a, nil
		}
	}
	if newest != nil {
		return analysis{}, finished(*newest)
	}
	if t.BoardID != "" {
		return analysis{}, fmt.Errorf("version %s of project %s has no analysis", t.BoardID, t.ProjectID)
	}
	return analysis{}, fmt.Errorf("project %s has no analysis yet: analyze_board first", t.ProjectID)
}

// finished is nil for a run whose results can be read, and says why not otherwise.
func finished(r emi.Run) error {
	switch {
	case r.Status == emi.StatusDone:
		return nil
	case emiclient.Pending(r.Status):
		return fmt.Errorf("run %s is still %s: call run_status with wait_s, then ask again", r.ID, r.Status)
	case r.Error != "":
		return fmt.Errorf("run %s %s: %s", r.ID, r.Status, r.Error)
	}
	return fmt.Errorf("run %s %s and has no results", r.ID, r.Status)
}

// ---- artifacts, cached ----

// artifactCache keeps the last few artifacts read. A finished run's artifacts never change,
// and an agent asks about the same analysis several times in a row.
type artifactCache struct {
	mu    sync.Mutex
	order []string
	items map[string][]byte
}

const cacheEntries = 8

func (a *artifactCache) get(ctx context.Context, c *emiclient.Client, runID, name string) ([]byte, error) {
	key := runID + "/" + name
	a.mu.Lock()
	if b, ok := a.items[key]; ok {
		a.mu.Unlock()
		return b, nil
	}
	a.mu.Unlock()
	b, err := c.Artifact(ctx, runID, name)
	if err != nil {
		return nil, fmt.Errorf("%s of run %s: %w", name, runID, err)
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.items == nil {
		a.items = map[string][]byte{}
	}
	if _, ok := a.items[key]; !ok {
		a.order = append(a.order, key)
		if len(a.order) > cacheEntries {
			delete(a.items, a.order[0])
			a.order = a.order[1:]
		}
	}
	a.items[key] = b
	return b, nil
}

// rulesDoc is rules.json, the parts the tools read.
type rulesDoc struct {
	Findings []finding `json:"findings"`
	Summary  struct {
		Critical       int     `json:"critical"`
		Warning        int     `json:"warning"`
		Info           int     `json:"info"`
		AssumedMaxFreq float64 `json:"assumed_max_frequency_hz"`
	} `json:"summary"`
	Notes              []string `json:"notes"`
	Suppressed         int      `json:"suppressed"`
	SuppressedFindings []struct {
		Rule   string `json:"rule"`
		Net    string `json:"net"`
		Title  string `json:"title"`
		Reason string `json:"reason"`
	} `json:"suppressed_findings"`
	SettingsWarnings []string `json:"settings_warnings"`
	Settings         *struct {
		File      string `json:"file"`
		FileError string `json:"file_error"`
	} `json:"settings"`
	Decoupling *decouplingDoc `json:"decoupling"`
}

type finding struct {
	ID       string   `json:"id"`
	Rule     string   `json:"rule"`
	Severity string   `json:"severity"`
	Title    string   `json:"title"`
	Detail   string   `json:"detail,omitempty"`
	Net      string   `json:"net,omitempty"`
	Layer    string   `json:"layer,omitempty"`
	X        *float64 `json:"x,omitempty"`
	Y        *float64 `json:"y,omitempty"`
	Action   string   `json:"action,omitempty"`
}

func (s *Server) rules(ctx context.Context, t target) (*emiclient.Client, analysis, *rulesDoc, error) {
	c, _, err := s.client(ctx)
	if err != nil {
		return nil, analysis{}, nil, err
	}
	a, err := resolve(ctx, c, t)
	if err != nil {
		return nil, analysis{}, nil, err
	}
	raw, err := s.cache.get(ctx, c, a.RunID, "rules.json")
	if err != nil {
		return nil, analysis{}, nil, err
	}
	var d rulesDoc
	if err := json.Unmarshal(raw, &d); err != nil {
		return nil, analysis{}, nil, fmt.Errorf("rules.json of run %s did not parse: %w", a.RunID, err)
	}
	return c, a, &d, nil
}

// ---- the rule catalogue ----

//go:embed ruleCatalogue.json
var catalogueJSON []byte

type catalogueRule struct {
	ID       string `json:"id"`
	Title    string `json:"title"`
	About    string `json:"about"`
	Category string `json:"category"`
	Params   []struct {
		Key     string  `json:"key"`
		Default float64 `json:"default"`
		PerNet  bool    `json:"per_net,omitempty"`
	} `json:"params"`
}

var catalogue = func() map[string]catalogueRule {
	var rules []catalogueRule
	if err := json.Unmarshal(catalogueJSON, &rules); err != nil {
		panic("ruleCatalogue.json: " + err.Error())
	}
	out := make(map[string]catalogueRule, len(rules))
	for _, r := range rules {
		out[r.ID] = r
	}
	return out
}()

func ruleTitle(id string) string {
	if r, ok := catalogue[id]; ok {
		return r.Title
	}
	return id
}

var severityRank = map[string]int{"critical": 0, "warning": 1, "info": 2}

// ---- findings ----

type findingsIn struct {
	target
	Severity string `json:"severity,omitempty" jsonschema:"only this severity and worse: critical, warning or info (default: all)"`
	Rule     string `json:"rule,omitempty" jsonschema:"only this check, by id (checks lists them)"`
	Net      string `json:"net,omitempty" jsonschema:"only findings on nets containing this text, case-insensitive"`
	Layer    string `json:"layer,omitempty" jsonschema:"only this layer, e.g. F.Cu"`
	Near     *near  `json:"near,omitempty" jsonschema:"only findings within radius_mm of a point on the board"`
	Detail   bool   `json:"detail,omitempty" jsonschema:"include each finding's full explanation (long; default off, use finding for one)"`
	Limit    int    `json:"limit,omitempty" jsonschema:"page size, default 25, max 200"`
	Offset   int    `json:"offset,omitempty"`
}

type near struct {
	X        float64 `json:"x" jsonschema:"board x in mm"`
	Y        float64 `json:"y" jsonschema:"board y in mm"`
	RadiusMM float64 `json:"radius_mm,omitempty" jsonschema:"default 5"`
}

type ruleCount struct {
	Rule     string `json:"rule"`
	Title    string `json:"title"`
	Critical int    `json:"critical,omitempty"`
	Warning  int    `json:"warning,omitempty"`
	Info     int    `json:"info,omitempty"`
}

type findingsOut struct {
	analysis
	Totals           map[string]int `json:"totals" jsonschema:"every finding of the analysis by severity"`
	ByRule           []ruleCount    `json:"by_rule" jsonschema:"every finding by check, worst first"`
	Matched          int            `json:"matched" jsonschema:"findings matching the filters"`
	Offset           int            `json:"offset"`
	NextOffset       int            `json:"next_offset,omitempty" jsonschema:"pass as offset for the next page; absent on the last"`
	Findings         []finding      `json:"findings" jsonschema:"worst first; x and y are board coordinates in mm"`
	Suppressed       int            `json:"suppressed,omitempty" jsonschema:"findings hidden by suppressions in emi.rules.yaml or the app"`
	RulesFile        string         `json:"rules_file,omitempty"`
	Notes            []string       `json:"notes,omitempty" jsonschema:"what came with the board and what was missing"`
	SettingsWarnings []string       `json:"settings_warnings,omitempty"`
	AssumedMaxFreqHz float64        `json:"assumed_max_frequency_hz,omitempty" jsonschema:"the frequency length-based checks assumed (board.max_frequency_hz in emi.rules.yaml)"`
}

func (s *Server) findings(ctx context.Context, _ *mcpRequest, in findingsIn) (*mcpResult, findingsOut, error) {
	_, a, d, err := s.rules(ctx, in.target)
	if err != nil {
		return nil, findingsOut{}, err
	}
	if in.Severity != "" {
		if _, ok := severityRank[in.Severity]; !ok {
			return nil, findingsOut{}, fmt.Errorf("severity must be critical, warning or info")
		}
	}
	if in.Rule != "" {
		if _, ok := catalogue[in.Rule]; !ok {
			return nil, findingsOut{}, fmt.Errorf("no check is called %q; checks lists them", in.Rule)
		}
	}
	limit := clamp(in.Limit, 25, 200)
	radius := 5.0
	if in.Near != nil && in.Near.RadiusMM > 0 {
		radius = in.Near.RadiusMM
	}

	out := findingsOut{analysis: a, Totals: map[string]int{"critical": d.Summary.Critical, "warning": d.Summary.Warning, "info": d.Summary.Info},
		Suppressed: d.Suppressed, Notes: d.Notes, SettingsWarnings: d.SettingsWarnings, AssumedMaxFreqHz: d.Summary.AssumedMaxFreq,
		Findings: []finding{}}
	if d.Settings != nil {
		out.RulesFile = d.Settings.File
	}
	counts := map[string]*ruleCount{}
	var matched []finding
	for _, f := range d.Findings {
		rc := counts[f.Rule]
		if rc == nil {
			rc = &ruleCount{Rule: f.Rule, Title: ruleTitle(f.Rule)}
			counts[f.Rule] = rc
		}
		switch f.Severity {
		case "critical":
			rc.Critical++
		case "warning":
			rc.Warning++
		case "info":
			rc.Info++
		}
		if in.Severity != "" && severityRank[f.Severity] > severityRank[in.Severity] {
			continue
		}
		if (in.Rule != "" && f.Rule != in.Rule) || (in.Layer != "" && f.Layer != in.Layer) ||
			(in.Net != "" && !strings.Contains(strings.ToLower(f.Net), strings.ToLower(in.Net))) {
			continue
		}
		if in.Near != nil && (f.X == nil || f.Y == nil || math.Hypot(*f.X-in.Near.X, *f.Y-in.Near.Y) > radius) {
			continue
		}
		if !in.Detail {
			f.Detail = ""
		}
		matched = append(matched, f)
	}
	for _, rc := range counts {
		out.ByRule = append(out.ByRule, *rc)
	}
	sort.Slice(out.ByRule, func(i, j int) bool {
		p, q := out.ByRule[i], out.ByRule[j]
		if p.Critical != q.Critical {
			return p.Critical > q.Critical
		}
		if p.Warning != q.Warning {
			return p.Warning > q.Warning
		}
		if p.Info != q.Info {
			return p.Info > q.Info
		}
		return p.Rule < q.Rule
	})
	sort.SliceStable(matched, func(i, j int) bool {
		if severityRank[matched[i].Severity] != severityRank[matched[j].Severity] {
			return severityRank[matched[i].Severity] < severityRank[matched[j].Severity]
		}
		return matched[i].Rule < matched[j].Rule
	})
	out.Matched, out.Offset = len(matched), in.Offset
	if in.Offset < len(matched) {
		end := min(in.Offset+limit, len(matched))
		out.Findings = append(out.Findings, matched[in.Offset:end]...)
		if end < len(matched) {
			out.NextOffset = end
		}
	}
	for i := range out.Findings {
		roundXY(&out.Findings[i])
	}
	return nil, out, nil
}

func roundXY(f *finding) { f.X, f.Y = round2(f.X), round2(f.Y) }

// round2 rounds a board coordinate to 0.01 mm: finer is noise to a reader.
func round2(p *float64) *float64 {
	if p == nil {
		return nil
	}
	v := math.Round(*p*100) / 100
	return &v
}

func clamp(v, def, max int) int {
	if v <= 0 {
		return def
	}
	if v > max {
		return max
	}
	return v
}

// ---- finding ----

type findingIn struct {
	target
	FindingID string `json:"finding_id" jsonschema:"the id from findings, e.g. plane-gap-3"`
}

type findingOut struct {
	analysis
	Finding finding `json:"finding"`
	Check   struct {
		Title    string `json:"title"`
		Category string `json:"category"`
		About    string `json:"about"`
	} `json:"check"`
	Suppress string `json:"suppress" jsonschema:"how to accept this finding on purpose, in emi.rules.yaml"`
}

func (s *Server) finding(ctx context.Context, _ *mcpRequest, in findingIn) (*mcpResult, findingOut, error) {
	if in.FindingID == "" {
		return nil, findingOut{}, fmt.Errorf("finding_id is required (findings lists them)")
	}
	_, a, d, err := s.rules(ctx, in.target)
	if err != nil {
		return nil, findingOut{}, err
	}
	for _, f := range d.Findings {
		if f.ID != in.FindingID {
			continue
		}
		out := findingOut{analysis: a, Finding: f}
		roundXY(&out.Finding)
		r := catalogue[f.Rule]
		out.Check.Title, out.Check.Category, out.Check.About = ruleTitle(f.Rule), r.Category, r.About
		net := f.Net
		if net == "" {
			net = "*"
		}
		out.Suppress = fmt.Sprintf("suppress:\n  - rule: %s\n    net: %q\n    reason: \"<why this is intentional>\"", f.Rule, net)
		return nil, out, nil
	}
	return nil, findingOut{}, fmt.Errorf("run %s has no finding %q; ids are per analysis, so take them from findings on the same run", a.RunID, in.FindingID)
}

// ---- checks ----

type checksIn struct {
	Rule string `json:"rule,omitempty" jsonschema:"one check by id; omit for all"`
}

type checkOut struct {
	ID       string           `json:"id"`
	Title    string           `json:"title"`
	Category string           `json:"category"`
	About    string           `json:"about,omitempty"`
	Params   []map[string]any `json:"params,omitempty" jsonschema:"settings and their defaults; per_net ones can be set for a group of nets"`
}

type checksOut struct {
	Checks    []checkOut `json:"checks"`
	RulesFile string     `json:"rules_file" jsonschema:"how to change them"`
}

const rulesFileHelp = `Put an emi.rules.yaml next to the .kicad_pcb; analyze_board sends it with the board. Format:
version: 1
board: { max_frequency_hz: 1.6e9 }
rules:
  radiator: false                  # turn a check off
  plane-gap: { severity: info }    # keep it, as advisory
  impedance: { params: { tolerance_pct: 10 } }
groups:                            # per_net settings for a set of nets (glob, or netclass:)
  - match: "DDR_DQ*"
    params: { byte_lane_ps: 6 }
suppress:                          # findings accepted on purpose; a reason is required
  - rule: edge-proximity
    net: GND
    reason: "guard ring, intentional"
Settings edited in the app's Checks tab apply on top of the file.`

func (s *Server) checks(_ context.Context, _ *mcpRequest, in checksIn) (*mcpResult, checksOut, error) {
	var rules []catalogueRule
	_ = json.Unmarshal(catalogueJSON, &rules)
	out := checksOut{Checks: []checkOut{}, RulesFile: rulesFileHelp}
	for _, r := range rules {
		if in.Rule != "" && r.ID != in.Rule {
			continue
		}
		c := checkOut{ID: r.ID, Title: r.Title, Category: r.Category}
		if in.Rule != "" || len(rules) <= 30 {
			c.About = r.About
		}
		for _, p := range r.Params {
			m := map[string]any{"key": p.Key, "default": p.Default}
			if p.PerNet {
				m["per_net"] = true
			}
			c.Params = append(c.Params, m)
		}
		out.Checks = append(out.Checks, c)
	}
	if in.Rule != "" && len(out.Checks) == 0 {
		return nil, checksOut{}, fmt.Errorf("no check is called %q", in.Rule)
	}
	return nil, out, nil
}
