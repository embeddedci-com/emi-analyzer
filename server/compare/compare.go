// Package compare says what changed between two versions of one board.
//
// It is the Go half of webapp/src/lib/compare.ts, for clients that are not the browser (the
// MCP server). Nothing here fetches anything: it takes the artifacts of two versions, which
// the caller downloads through the URLs the server mints, and diffs them. The server itself
// never reads artifact bytes, so the diff cannot live there.
//
// Both implementations must give the same answer: testdata/compare_fixtures.json is checked by
// compare_test.go and by compare.test.ts.
//
// Findings are the hard part. Their ids are positional (`plane-gap-3`), so the third
// plane-gap finding of one version has nothing to do with the third of the next, and their
// titles carry measurements that change with every edit. What survives an edit is the rule,
// the net, the layer and roughly where it is, so that is what a finding is matched on.
package compare

import (
	"math"
	"sort"
	"strconv"
	"strings"
)

// Finding is one entry of rules.json's findings. Only the fields the matcher reads are named;
// the caller keeps the full object if it needs the rest.
type Finding struct {
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

// MatchToleranceMM is how far a finding may move between versions and still be the same
// finding.
const MatchToleranceMM = 2.0

// Status of a finding between two versions.
const (
	Fixed     = "fixed"
	New       = "new"
	Unchanged = "unchanged"
)

// FindingChange is one finding of either version. Before is nil for a new finding, After for
// a fixed one.
type FindingChange struct {
	Status string   `json:"status"`
	Rule   string   `json:"rule"`
	Before *Finding `json:"before,omitempty"`
	After  *Finding `json:"after,omitempty"`
}

// Severity is the finding in the newer version when it has one.
func (c FindingChange) Severity() string {
	if c.After != nil {
		return c.After.Severity
	}
	return c.Before.Severity
}

// SeverityCounts counts changes of one status by severity.
type SeverityCounts struct {
	Critical int `json:"critical"`
	Warning  int `json:"warning"`
	Info     int `json:"info"`
	Total    int `json:"total"`
}

// FindingsDiff is every finding of both versions, paired.
type FindingsDiff struct {
	Changes []FindingChange           `json:"changes"`
	Counts  map[string]SeverityCounts `json:"counts"`
}

func located(f *Finding) bool { return f.X != nil && f.Y != nil }

// distance between two findings of one rule, net and layer, or +Inf when they cannot be the
// same finding. A finding with no place on the board (a summary) only matches another without
// one, and a matching title makes the pair closer than any other.
func distance(a, b *Finding, tol float64) float64 {
	if located(a) != located(b) {
		return math.Inf(1)
	}
	if !located(a) {
		if a.Title == b.Title {
			return 0
		}
		return tol
	}
	return math.Hypot(*a.X-*b.X, *a.Y-*b.Y)
}

// DiffFindings pairs the findings of two versions. Within each rule, net and layer, the
// closest pairs are taken first, so two findings that swapped order in the file still match
// their own partner. Then what is left pairs on an identical title however far it moved:
// "clamp D1's ground pad has no via" is the same finding when D1 moved across the board, and
// titles that carry a measurement only repeat when the measurement did. What pairs is
// unchanged, what is left over in the older version is fixed, and what is left over in the
// newer one is new.
func DiffFindings(before, after []Finding, tol float64) FindingsDiff {
	type group struct{ a, b []*Finding }
	var order []string
	groups := map[string]*group{}
	get := func(f *Finding) *group {
		k := f.Rule + "\x00" + f.Net + "\x00" + f.Layer
		g := groups[k]
		if g == nil {
			g = &group{}
			groups[k] = g
			order = append(order, k)
		}
		return g
	}
	for i := range before {
		g := get(&before[i])
		g.a = append(g.a, &before[i])
	}
	for i := range after {
		g := get(&after[i])
		g.b = append(g.b, &after[i])
	}

	changes := []FindingChange{}
	for _, k := range order {
		a, b := groups[k].a, groups[k].b
		type pair struct {
			d    float64
			i, j int
		}
		var pairs []pair
		for i, fa := range a {
			for j, fb := range b {
				if d := distance(fa, fb, tol); d <= tol {
					pairs = append(pairs, pair{d, i, j})
				}
			}
		}
		for i, fa := range a {
			for j, fb := range b {
				d := distance(fa, fb, tol)
				if d > tol && !math.IsInf(d, 1) && fa.Title == fb.Title {
					pairs = append(pairs, pair{tol + d, i, j})
				}
			}
		}
		sort.SliceStable(pairs, func(p, q int) bool {
			if pairs[p].d != pairs[q].d {
				return pairs[p].d < pairs[q].d
			}
			if pairs[p].i != pairs[q].i {
				return pairs[p].i < pairs[q].i
			}
			return pairs[p].j < pairs[q].j
		})
		usedA := make([]bool, len(a))
		usedB := make([]bool, len(b))
		for _, p := range pairs {
			if usedA[p.i] || usedB[p.j] {
				continue
			}
			usedA[p.i], usedB[p.j] = true, true
			changes = append(changes, FindingChange{Status: Unchanged, Rule: b[p.j].Rule, Before: a[p.i], After: b[p.j]})
		}
		for i, f := range a {
			if !usedA[i] {
				changes = append(changes, FindingChange{Status: Fixed, Rule: f.Rule, Before: f})
			}
		}
		for j, f := range b {
			if !usedB[j] {
				changes = append(changes, FindingChange{Status: New, Rule: f.Rule, After: f})
			}
		}
	}

	counts := map[string]SeverityCounts{Fixed: {}, New: {}, Unchanged: {}}
	for _, c := range changes {
		n := counts[c.Status]
		switch c.Severity() {
		case "critical":
			n.Critical++
		case "warning":
			n.Warning++
		case "info":
			n.Info++
		}
		n.Total++
		counts[c.Status] = n
	}
	return FindingsDiff{Changes: changes, Counts: counts}
}

var (
	statusOrder   = map[string]int{New: 0, Fixed: 1, Unchanged: 2}
	severityOrder = map[string]int{"critical": 0, "warning": 1, "info": 2}
)

// RuleGroup is the changes of one rule.
type RuleGroup struct {
	Rule    string          `json:"rule"`
	Changes []FindingChange `json:"changes"`
}

// GroupByRule groups changes by rule. Rules with something new come first, then something
// fixed; inside a rule, new before fixed before unchanged, worst first.
func GroupByRule(changes []FindingChange) []RuleGroup {
	idx := map[string]int{}
	var out []RuleGroup
	for _, c := range changes {
		i, ok := idx[c.Rule]
		if !ok {
			i = len(out)
			idx[c.Rule] = i
			out = append(out, RuleGroup{Rule: c.Rule})
		}
		out[i].Changes = append(out[i].Changes, c)
	}
	rank := make(map[string]int, len(out))
	for _, g := range out {
		sort.SliceStable(g.Changes, func(p, q int) bool {
			cp, cq := g.Changes[p], g.Changes[q]
			if statusOrder[cp.Status] != statusOrder[cq.Status] {
				return statusOrder[cp.Status] < statusOrder[cq.Status]
			}
			return severityOrder[cp.Severity()] < severityOrder[cq.Severity()]
		})
		rank[g.Rule] = statusOrder[g.Changes[0].Status]
	}
	sort.SliceStable(out, func(p, q int) bool {
		if rank[out[p].Rule] != rank[out[q].Rule] {
			return rank[out[p].Rule] < rank[out[q].Rule]
		}
		// Byte order, where the browser uses localeCompare. They can differ on mixed-case
		// names, which changes the order a list is shown in, never a status.
		return out[p].Rule < out[q].Rule
	})
	return out
}

// Headline is "3 fixed, 1 new", or "No change" when both are zero.
func Headline(counts map[string]SeverityCounts) string {
	var parts []string
	if n := counts[Fixed].Total; n > 0 {
		parts = append(parts, strconv.Itoa(n)+" fixed")
	}
	if n := counts[New].Total; n > 0 {
		parts = append(parts, strconv.Itoa(n)+" new")
	}
	if len(parts) == 0 {
		return "No change"
	}
	return strings.Join(parts, ", ")
}

// ---- nets ----

// NetRow is one row of nets.json, the fields the diff reads.
type NetRow struct {
	Net       string   `json:"net"`
	CopperMM  float64  `json:"copper_mm"`
	PathMM    *float64 `json:"path_mm"`
	PathVias  *int     `json:"path_vias"`
	ViasTotal *int     `json:"vias_total"`
}

// NetReport is nets.json.
type NetReport struct {
	Rows []NetRow `json:"rows"`
}

// NetChange is a net whose length or via count changed, or one only one version has.
type NetChange struct {
	Net          string   `json:"net"`
	Status       string   `json:"status"` // changed, added, removed
	LengthBefore *float64 `json:"length_before_mm,omitempty"`
	LengthAfter  *float64 `json:"length_after_mm,omitempty"`
	ViasBefore   *int     `json:"vias_before,omitempty"`
	ViasAfter    *int     `json:"vias_after,omitempty"`
}

// LengthEpsilonMM: below this a length is the same length, rounding in the report, not an
// edit.
const LengthEpsilonMM = 0.01

// The routed pad-to-pad length when there is one, otherwise all the copper on the net.
func netLength(r NetRow) float64 {
	if r.PathMM != nil {
		return *r.PathMM
	}
	return r.CopperMM
}

func netVias(r NetRow) int {
	if r.ViasTotal != nil {
		return *r.ViasTotal
	}
	if r.PathVias != nil {
		return *r.PathVias
	}
	return 0
}

// DiffNets lists the nets whose length or via count changed, and nets only one version has:
// changed first, the biggest length change first, then added, then removed.
func DiffNets(before, after NetReport) []NetChange {
	a := make(map[string]NetRow, len(before.Rows))
	for _, r := range before.Rows {
		a[r.Net] = r
	}
	b := make(map[string]bool, len(after.Rows))
	out := []NetChange{}
	for _, rb := range after.Rows {
		b[rb.Net] = true
		lb, vb := netLength(rb), netVias(rb)
		ra, ok := a[rb.Net]
		if !ok {
			out = append(out, NetChange{Net: rb.Net, Status: "added", LengthAfter: &lb, ViasAfter: &vb})
			continue
		}
		la, va := netLength(ra), netVias(ra)
		if math.Abs(la-lb) > LengthEpsilonMM || va != vb {
			out = append(out, NetChange{Net: rb.Net, Status: "changed",
				LengthBefore: &la, LengthAfter: &lb, ViasBefore: &va, ViasAfter: &vb})
		}
	}
	for _, ra := range before.Rows {
		if !b[ra.Net] {
			la, va := netLength(ra), netVias(ra)
			out = append(out, NetChange{Net: ra.Net, Status: "removed", LengthBefore: &la, ViasBefore: &va})
		}
	}
	order := map[string]int{"changed": 0, "added": 1, "removed": 2}
	val := func(p *float64) float64 {
		if p == nil {
			return 0
		}
		return *p
	}
	delta := func(c NetChange) float64 { return math.Abs(val(c.LengthAfter) - val(c.LengthBefore)) }
	sort.SliceStable(out, func(p, q int) bool {
		if order[out[p].Status] != order[out[q].Status] {
			return order[out[p].Status] < order[out[q].Status]
		}
		if dp, dq := delta(out[p]), delta(out[q]); dp != dq {
			return dp > dq
		}
		return out[p].Net < out[q].Net
	})
	return out
}
