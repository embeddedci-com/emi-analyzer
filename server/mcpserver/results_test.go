package mcpserver

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
	"github.com/embeddedci-com/emi-analyzer/server/localapp"
)

// version adds a board with a finished ingest and its artifacts to a project, as an
// analysis would leave it.
func (a *fakeApp) version(projectID string, status emi.RunStatus, artifacts map[string]any) (boardID, runID string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.n++
	at := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC).Add(time.Duration(a.n) * time.Hour)
	b := emi.Board{ID: fmt.Sprintf("b%d", a.n), ProjectID: projectID, InputKey: "uploads/local/amp.zip", CreatedAt: at}
	run := &emi.Run{ID: "r" + b.ID, ProjectID: projectID, BoardID: b.ID, Kind: emi.RunKindIngest, Status: status, CreatedAt: at}
	a.boards = append(a.boards, b)
	a.runs[run.ID] = run
	a.artifacts[run.ID] = map[string][]byte{}
	for name, v := range artifacts {
		raw, _ := json.Marshal(v)
		a.artifacts[run.ID][name] = raw
	}
	return b.ID, run.ID
}

func (a *fakeApp) project(name string) string {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.n++
	p := emi.Project{ID: fmt.Sprintf("p%d", a.n), Name: name, SourceKind: emi.SourceKiCad,
		CreatedAt: time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)}
	a.projects = append(a.projects, p)
	return p.ID
}

func f(id, rule, sev, net string, x, y float64) map[string]any {
	return map[string]any{"id": id, "rule": rule, "severity": sev, "title": id + " title", "detail": id + " long explanation",
		"net": net, "layer": "F.Cu", "x": x, "y": y}
}

func rulesJSON(findings ...map[string]any) map[string]any {
	c := map[string]int{}
	for _, x := range findings {
		c[x["severity"].(string)]++
	}
	return map[string]any{
		"format_version": 1, "findings": findings, "notes": []string{"no rules file"},
		"summary":    map[string]any{"critical": c["critical"], "warning": c["warning"], "info": c["info"], "assumed_max_frequency_hz": 1e9},
		"suppressed": 1,
		"decoupling": map[string]any{
			"note": "Estimate.", "f_min_hz": 1e5, "f_max_hz": 1e8, "points_per_decade": 20,
			"rails": []any{
				map[string]any{"net": "+1V8", "v": 1.8, "status": "gaps",
					"parts": map[string]any{"C1": map[string]any{"value": "100n", "package": "0402"}},
					"ics": []any{map[string]any{
						"ref": "U5", "status": "gaps", "target_ohm": 0.18, "gaps": [][]float64{{1e5, 1e6}},
						"worst": map[string]any{"hz": 1e5, "ohm": 1.96, "excess_db": 20.8},
						"caps":  []any{map[string]any{"ref": "C1", "pin": "5", "distance_mm": 1.1, "ground_vias": 1, "useful_up_to_hz": 1.86e7}},
						"recommendations": []any{
							map[string]any{"kind": "remove", "text": "Remove C1", "improvement_db": -19.7, "at_hz": 1e5},
							map[string]any{"kind": "add", "text": "Add 10 uF within 3 mm of U5", "improvement_db": 22.5, "at_hz": 1e5},
						},
					}}},
				map[string]any{"net": "+3V3", "v": 3.3, "status": "ok", "parts": map[string]any{}, "ics": []any{}},
			},
		},
	}
}

func netsJSON(rows ...map[string]any) map[string]any {
	return map[string]any{"format_version": 1, "clock_net": "CLK", "rows": rows}
}

func TestFindingsAreShapedFilteredAndPaged(t *testing.T) {
	app := newFakeApp(t)
	p := app.project("amp")
	_, run := app.version(p, emi.StatusDone, map[string]any{"rules.json": rulesJSON(
		f("radiator-0", "radiator", "critical", "/I2C0_SCL", 46.784, 38.291),
		f("plane-gap-0", "plane-gap", "warning", "/USB_D+", 10, 10),
		f("plane-gap-1", "plane-gap", "warning", "/USB_D-", 11, 10),
		f("impedance-0", "impedance", "info", "/USB_D+", 80, 80),
	)})
	cs := connect(t, finder{e: localapp.Endpoint{URL: app.srv.URL}})

	var all findingsOut
	if e := call(t, cs, "findings", map[string]any{"project_id": p, "limit": 2}, &all); e != "" {
		t.Fatal(e)
	}
	if all.RunID != run || all.Matched != 4 || len(all.Findings) != 2 || all.NextOffset != 2 || all.Suppressed != 1 {
		t.Fatalf("all = %+v", all)
	}
	if all.Findings[0].ID != "radiator-0" || *all.Findings[0].X != 46.78 || all.Findings[0].Detail != "" {
		t.Errorf("first = %+v", all.Findings[0])
	}
	if all.ByRule[0].Rule != "radiator" || all.ByRule[1].Rule != "plane-gap" || all.ByRule[1].Warning != 2 {
		t.Errorf("by_rule = %+v", all.ByRule)
	}
	var page2 findingsOut
	call(t, cs, "findings", map[string]any{"run_id": run, "limit": 2, "offset": 2}, &page2)
	if len(page2.Findings) != 2 || page2.NextOffset != 0 || page2.Findings[1].ID != "impedance-0" {
		t.Errorf("page 2 = %+v", page2.Findings)
	}

	for _, c := range []struct {
		args map[string]any
		ids  string
	}{
		{map[string]any{"severity": "warning"}, "radiator-0 plane-gap-0 plane-gap-1"},
		{map[string]any{"net": "usb_d+"}, "plane-gap-0 impedance-0"},
		{map[string]any{"rule": "plane-gap", "detail": true}, "plane-gap-0 plane-gap-1"},
		{map[string]any{"near": map[string]any{"x": 10.5, "y": 10, "radius_mm": 1}}, "plane-gap-0 plane-gap-1"},
	} {
		c.args["run_id"] = run
		var out findingsOut
		if e := call(t, cs, "findings", c.args, &out); e != "" {
			t.Fatal(e)
		}
		var ids []string
		for _, x := range out.Findings {
			ids = append(ids, x.ID)
			if c.args["detail"] == true && x.Detail == "" {
				t.Errorf("%v: no detail", c.args)
			}
		}
		if strings.Join(ids, " ") != c.ids {
			t.Errorf("%v: %v, want %s", c.args, ids, c.ids)
		}
	}
	if e := call(t, cs, "findings", map[string]any{"run_id": run, "rule": "nope"}, &findingsOut{}); !strings.Contains(e, "no check is called") {
		t.Errorf("unknown rule: %q", e)
	}

	var one findingOut
	if e := call(t, cs, "finding", map[string]any{"run_id": run, "finding_id": "plane-gap-1"}, &one); e != "" {
		t.Fatal(e)
	}
	if one.Finding.Detail != "plane-gap-1 long explanation" || one.Check.Title != "Reference plane gaps" ||
		!strings.Contains(one.Suppress, "rule: plane-gap") || !strings.Contains(one.Suppress, `net: "/USB_D-"`) {
		t.Errorf("finding = %+v", one)
	}
}

func TestResolveTakesTheNewestFinishedAnalysis(t *testing.T) {
	app := newFakeApp(t)
	p := app.project("amp")
	_, done := app.version(p, emi.StatusDone, map[string]any{"rules.json": rulesJSON()})
	_, running := app.version(p, emi.StatusInProgress, nil)
	app.reads[running] = -1000 // keeps reporting in_progress
	cs := connect(t, finder{e: localapp.Endpoint{URL: app.srv.URL}})

	var out findingsOut
	if e := call(t, cs, "findings", map[string]any{"project_id": p}, &out); e != "" {
		t.Fatal(e)
	}
	if out.RunID != done || !strings.Contains(out.Note, running) {
		t.Errorf("out = %+v", out.analysis)
	}
	if e := call(t, cs, "findings", map[string]any{"run_id": running}, &findingsOut{}); !strings.Contains(e, "still in_progress") {
		t.Errorf("running: %q", e)
	}
	if e := call(t, cs, "findings", map[string]any{}, &findingsOut{}); !strings.Contains(e, "name the analysis") {
		t.Errorf("nothing named: %q", e)
	}
	empty := app.project("empty")
	if e := call(t, cs, "findings", map[string]any{"project_id": empty}, &findingsOut{}); !strings.Contains(e, "no analysis yet") {
		t.Errorf("empty project: %q", e)
	}
}

func TestDecoupling(t *testing.T) {
	app := newFakeApp(t)
	p := app.project("amp")
	app.version(p, emi.StatusDone, map[string]any{"rules.json": rulesJSON()})
	cs := connect(t, finder{e: localapp.Endpoint{URL: app.srv.URL}})

	var rails decouplingOut
	if e := call(t, cs, "decoupling", map[string]any{"project_id": p}, &rails); e != "" {
		t.Fatal(e)
	}
	if len(rails.Rails) != 2 || rails.Rails[0].Net != "+1V8" || rails.Rails[0].ICsWithGaps != 1 ||
		rails.Rails[0].Worst != "U5: 20.8 dB over at 100 kHz" {
		t.Fatalf("rails = %+v", rails.Rails)
	}
	var ic decouplingOut
	if e := call(t, cs, "decoupling", map[string]any{"project_id": p, "ic": "u5"}, &ic); e != "" {
		t.Fatal(e)
	}
	want := icOut{Rail: "+1V8", Ref: "U5", Status: "gaps", TargetOhm: 0.18, Worst: "1.96 ohm at 100 kHz, 20.8 dB over",
		Gaps:            []string{"100 kHz to 1 MHz"},
		Caps:            []string{"C1 100n 0402 at pin 5, 1.1 mm, 1 ground via, useful up to 18.6 MHz"},
		Recommendations: []string{"Add 10 uF within 3 mm of U5 (22.5 dB better at 100 kHz)"}}
	gj, _ := json.Marshal(ic.ICs)
	wj, _ := json.Marshal([]icOut{want})
	if !bytes.Equal(gj, wj) {
		t.Errorf("ic\n got %s\nwant %s", gj, wj)
	}
	if e := call(t, cs, "decoupling", map[string]any{"project_id": p, "rail": "+9V"}, &decouplingOut{}); !strings.Contains(e, "no decoupling data") {
		t.Errorf("unknown rail: %q", e)
	}
}

func TestNets(t *testing.T) {
	app := newFakeApp(t)
	p := app.project("amp")
	app.version(p, emi.StatusDone, map[string]any{"nets.json": netsJSON(
		map[string]any{"net": "DQ0", "copper_mm": 10, "path_mm": 10, "match_group": "byte0", "within_tolerance": "yes"},
		map[string]any{"net": "DQ1", "copper_mm": 12, "path_mm": 12, "match_group": "byte0", "within_tolerance": "no", "skew_ps": 12.5},
		map[string]any{"net": "GND", "copper_mm": 900},
	)})
	cs := connect(t, finder{e: localapp.Endpoint{URL: app.srv.URL}})
	var out netsOut
	if e := call(t, cs, "nets", map[string]any{"project_id": p, "out_of_tolerance": true}, &out); e != "" {
		t.Fatal(e)
	}
	if out.Matched != 1 || out.Nets[0].Net != "DQ1" || *out.Nets[0].SkewPS != 12.5 || strings.Join(out.Groups, ",") != "byte0" || out.ClockNet != "CLK" {
		t.Errorf("nets = %+v", out)
	}
}

func TestCompareTheTwoNewestAnalyzedVersions(t *testing.T) {
	app := newFakeApp(t)
	p := app.project("amp")
	v1, r1 := app.version(p, emi.StatusDone, map[string]any{
		"rules.json": rulesJSON(f("plane-gap-0", "plane-gap", "warning", "A", 10, 10), f("radiator-0", "radiator", "critical", "B", 50, 50)),
		"nets.json":  netsJSON(map[string]any{"net": "A", "copper_mm": 10, "path_mm": 10}),
	})
	v2, r2 := app.version(p, emi.StatusDone, map[string]any{
		// plane-gap moved 0.5 mm with a new id: the same finding. radiator is gone, return-via is new.
		"rules.json": rulesJSON(f("plane-gap-3", "plane-gap", "warning", "A", 10.5, 10), f("return-via-0", "return-via", "critical", "C", 5, 5)),
		"nets.json":  netsJSON(map[string]any{"net": "A", "copper_mm": 14, "path_mm": 14}),
	})
	app.version(p, emi.StatusFailed, nil) // a newer version that never analyzed is skipped
	cs := connect(t, finder{e: localapp.Endpoint{URL: app.srv.URL}})

	var out compareOut
	if e := call(t, cs, "compare", map[string]any{"project_id": p}, &out); e != "" {
		t.Fatal(e)
	}
	if out.Before != (versionRef{1, v1, r1}) || out.After != (versionRef{2, v2, r2}) || out.Headline != "1 fixed, 1 new" {
		t.Fatalf("out = %+v", out)
	}
	if len(out.Rules) != 3 || out.Rules[0].Rule != "return-via" || out.Rules[0].New[0].ID != "return-via-0" ||
		out.Rules[1].Rule != "radiator" || out.Rules[1].Fixed[0].ID != "radiator-0" ||
		out.Rules[2].Rule != "plane-gap" || out.Rules[2].Unchanged != 1 || out.Rules[2].Still != nil {
		t.Errorf("rules = %+v", out.Rules)
	}
	if len(out.Nets) != 1 || out.Nets[0].Net != "A" || *out.Nets[0].LengthAfter != 14 {
		t.Errorf("nets = %+v", out.Nets)
	}

	only := app.project("single")
	app.version(only, emi.StatusDone, map[string]any{"rules.json": rulesJSON()})
	if e := call(t, cs, "compare", map[string]any{"project_id": only}, &compareOut{}); !strings.Contains(e, "needs two analyzed versions") {
		t.Errorf("one version: %q", e)
	}
}

func TestChecksAndOpenInApp(t *testing.T) {
	app := newFakeApp(t)
	p := app.project("amp")
	cs := connect(t, finder{e: localapp.Endpoint{URL: app.srv.URL}})
	var all checksOut
	call(t, cs, "checks", nil, &all)
	if len(all.Checks) != len(catalogue) || !strings.Contains(all.RulesFile, "suppress:") {
		t.Errorf("checks = %d", len(all.Checks))
	}
	var one checksOut
	call(t, cs, "checks", map[string]any{"rule": "plane-gap"}, &one)
	if len(one.Checks) != 1 || one.Checks[0].Params[0]["key"] != "min_crossing_mm" {
		t.Errorf("plane-gap = %+v", one.Checks)
	}

	opened := ""
	orig := openBrowser
	openBrowser = func(u string) error { opened = u; return nil }
	t.Cleanup(func() { openBrowser = orig })
	var link openOut
	call(t, cs, "open_in_app", map[string]any{"project_id": p, "compare": true, "before_board_id": "b1", "board_id": "b2", "open": true}, &link)
	want := app.srv.URL + "/tools/emi/" + p + "/compare?after=b2&before=b1"
	if link.URL != want || !link.Opened || opened != want {
		t.Errorf("link = %+v, opened %q", link, opened)
	}
	if e := call(t, cs, "open_in_app", map[string]any{"project_id": "nope"}, &openOut{}); !strings.Contains(e, "project not found") {
		t.Errorf("unknown project: %q", e)
	}
}

// The MCP server's copy of the rule catalogue is the worker's export, unchanged.
func TestCatalogueMatchesTheWebapps(t *testing.T) {
	web, err := os.ReadFile("../../webapp/src/lib/ruleCatalogue.json")
	if err != nil {
		t.Skip("no webapp checkout beside the server:", err)
	}
	if !bytes.Equal(web, catalogueJSON) {
		t.Fatal("server/mcpserver/ruleCatalogue.json differs from the webapp's: run python worker/scripts/export_rule_catalogue.py")
	}
}

// The cable types offered are the worker's library, unchanged.
func TestCableLibraryMatchesTheWorkers(t *testing.T) {
	worker, err := os.ReadFile("../../worker/emi_worker/cables/library.json")
	if err != nil {
		t.Skip("no worker checkout beside the server:", err)
	}
	if !bytes.Equal(worker, cableLibraryJSON) {
		t.Fatal("server/mcpserver/cableLibrary.json differs from worker/emi_worker/cables/library.json: copy it")
	}
}

// run adds a finished run of a kind on a board, with artifacts.
func (a *fakeApp) run(projectID, boardID string, kind emi.RunKind, artifacts map[string]any) string {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.n++
	r := &emi.Run{ID: fmt.Sprintf("k%d", a.n), ProjectID: projectID, BoardID: boardID, Kind: kind, Status: emi.StatusDone,
		CreatedAt: time.Date(2026, 10, 2, 0, 0, 0, 0, time.UTC).Add(time.Duration(a.n) * time.Hour)}
	a.runs[r.ID] = r
	a.artifacts[r.ID] = map[string][]byte{}
	for name, v := range artifacts {
		raw, _ := json.Marshal(v)
		a.artifacts[r.ID][name] = raw
	}
	return r.ID
}

func esdLineJSON(net, conn string, pin *float64, connector float64, ic bool, clamp bool) map[string]any {
	l := map[string]any{"net": net, "connector": map[string]any{"ref": conn, "pad": "1"},
		"geometry": map[string]any{"routed": true, "clamp_ground_mm": 0.5, "clamp_ground_nh": 0.65},
		"variants": []any{
			map[string]any{"id": "as_laid_out", "v_pin_peak_v": pin, "v_connector_peak_v": connector, "unclamped": !clamp},
			map[string]any{"id": "clamp_at_connector", "v_pin_peak_v": pin, "v_connector_peak_v": connector / 2},
		}}
	if ic {
		l["ic"] = map[string]any{"ref": "U1", "pad": "3", "supply_v": 3.3}
	}
	if clamp {
		l["clamp"] = map[string]any{"ref": "D1", "part": "USBLC6", "model": "table"}
	}
	return l
}

func TestESDAndCables(t *testing.T) {
	app := newFakeApp(t)
	p := app.project("amp")
	board, _ := app.version(p, emi.StatusDone, map[string]any{"rules.json": rulesJSON()})
	v := func(x float64) *float64 { return &x }
	app.run(p, board, emi.RunKindTransient, map[string]any{"transient.json": map[string]any{
		"level": 4, "kv": 8, "lines": []any{
			esdLineJSON("SHIELDED", "J1", v(12), 30, true, true),
			esdLineJSON("NOIC", "J2", nil, 9000, false, false),
			esdLineJSON("SDA", "J3", v(120), 8000, true, false),
		}}})
	app.run(p, board, emi.RunKindCable, map[string]any{"cables.json": map[string]any{
		"cables": []any{
			map[string]any{"ref": "J7", "cable_id": "ethernet-utp", "cable_name": "Ethernet, unshielded", "length_m": 2,
				"declared_none": false, "radiation_peaks_hz": []float64{3.6e7},
				"tightest": map[string]any{"frequency_hz": 3.6e7, "max_current_dbua": 9.5, "limit_dbuv_per_m": 40}},
			map[string]any{"ref": "H1", "cable_id": nil, "declared_none": true},
		},
		"unassigned": []any{map[string]any{"ref": "USB1", "footprint": "USB-A", "suggested": "usb2-shielded", "reason": "contains 'usb'"}},
	}})
	cs := connect(t, finder{e: localapp.Endpoint{URL: app.srv.URL}})

	var e esdOut
	if msg := call(t, cs, "esd", map[string]any{"project_id": p}, &e); msg != "" {
		t.Fatal(msg)
	}
	var order []string
	for _, l := range e.Worst {
		order = append(order, l.Net)
	}
	// An IC pin far over its supply first, then the highest peak elsewhere.
	if strings.Join(order, " ") != "SDA SHIELDED NOIC" || e.Unclamped != 2 || e.Level != "level 4, 8 kV contact" {
		t.Fatalf("esd = %+v", e)
	}
	if w := e.Worst[0]; w.Peak != "120 V at the pin" || w.OverPinV != 116.7 || w.IC != "U1.3 (3.3 V supply)" || !w.Unclamped {
		t.Errorf("worst = %+v", w)
	}
	if w := e.Worst[1]; w.Clamp != "D1 USBLC6 (table model)" || w.Layout != "ground path 0.5 mm (0.65 nH)" {
		t.Errorf("clamped = %+v", w)
	}

	var c cablesOut
	if msg := call(t, cs, "cables", map[string]any{"project_id": p}, &c); msg != "" {
		t.Fatal(msg)
	}
	if len(c.Cables) != 2 || c.Cables[0].Connector != "J7" || c.Cables[0].Tightest != "9.5 dBuA (2.99 uA) at 36 MHz" ||
		c.Cables[1].Note != "never cabled" || len(c.Unassigned) != 1 || !strings.Contains(c.Unassigned[0], "suggested usb2-shielded") {
		t.Errorf("cables = %+v", c)
	}
	var types cablesOut
	call(t, cs, "cables", nil, &types)
	if len(types.Types) != 9 || types.Cables != nil {
		t.Errorf("types only = %+v", types)
	}
	empty := app.project("empty")
	app.version(empty, emi.StatusDone, map[string]any{"rules.json": rulesJSON()})
	if msg := call(t, cs, "esd", map[string]any{"project_id": empty}, &esdOut{}); !strings.Contains(msg, "start one with run_esd") {
		t.Errorf("no esd run: %q", msg)
	}
}
