//go:build e2e

// The MCP server end to end: a real emi-local with its Docker worker, and `emi-local mcp`
// driven over stdio the way Claude Code drives it. Opt-in, because it needs Docker and the
// worker image:
//
//	make e2e-mcp                       # builds nothing but the Go binary; uses WORKER_IMAGE
//	EMI_WORKER_IMAGE=... go test -tags e2e -run TestMCPEndToEnd -v ./mcpserver
//
// Everything lives in the test's temp folder -- data, endpoint file, the MCP server's own
// state -- so a desktop app on the same computer is never touched.
package mcpserver

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/embeddedci-com/emi-analyzer/server/localapp"
)

func TestMCPEndToEnd(t *testing.T) {
	if _, err := exec.LookPath("docker"); err != nil {
		t.Skip("needs Docker")
	}
	tmp := t.TempDir()
	bin := filepath.Join(tmp, "emi-local")
	build := exec.Command("go", "build", "-o", bin, "./cmd/emi-local")
	build.Dir = ".."
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build emi-local: %v\n%s", err, out)
	}

	// The app, headless, on a free port, with a worker container of its own.
	endpoint := filepath.Join(tmp, "endpoint.json")
	app := exec.Command(bin, "-open=false", "-addr", "127.0.0.1:0", "-data-dir", filepath.Join(tmp, "data"),
		"-endpoint-file", endpoint)
	if img := os.Getenv("EMI_WORKER_IMAGE"); img != "" {
		app.Args = append(app.Args, "-worker-image", img)
	}
	logf, _ := os.Create(filepath.Join(tmp, "app.log"))
	app.Stdout, app.Stderr = logf, logf
	if err := app.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		// SIGTERM, so the app removes its worker container on the way out.
		_ = app.Process.Signal(syscall.SIGTERM)
		done := make(chan struct{})
		go func() { _ = app.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(30 * time.Second):
			_ = app.Process.Kill()
		}
		if t.Failed() {
			b, _ := os.ReadFile(filepath.Join(tmp, "app.log"))
			t.Logf("app log:\n%s", b)
		}
	})

	ctx := context.Background()
	finder := localapp.New(localapp.Options{EndpointFile: endpoint, Candidates: func() [][]string { return nil }})
	var e localapp.Endpoint
	deadline := time.Now().Add(4 * time.Minute) // a first run may pull the worker image
	for {
		if got, ok := finder.Running(ctx); ok && got.Worker.State == "running" {
			e = got
			break
		} else if ok && got.Worker.State == "error" {
			t.Fatalf("worker: %s %s", got.Worker.Message, got.Worker.Detail)
		}
		if time.Now().After(deadline) {
			t.Fatal("the app or its worker did not come up")
		}
		time.Sleep(time.Second)
	}

	// The MCP server, over stdio, as a plugin starts it.
	server := exec.Command(bin, "mcp")
	server.Env = append(os.Environ(), "EMI_ANALYZER_URL="+e.URL, "EMI_ANALYZER_MCP_STATE="+filepath.Join(tmp, "mcp"))
	server.Stderr = os.Stderr
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "e2e"}, nil).Connect(ctx, &mcp.CommandTransport{Command: server}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer cs.Close()

	call := func(name string, args map[string]any, out any) {
		t.Helper()
		res, err := cs.CallTool(ctx, &mcp.CallToolParams{Name: name, Arguments: args})
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if res.IsError {
			t.Fatalf("%s: %v", name, res.Content[0].(*mcp.TextContent).Text)
		}
		raw, _ := json.Marshal(res.StructuredContent)
		if err := json.Unmarshal(raw, out); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
	}

	// A board in its own project folder, as a designer keeps it.
	dir := filepath.Join(tmp, "demo")
	_ = os.Mkdir(dir, 0o755)
	src, err := os.ReadFile("../../worker/tests/fixtures/decoupling_demo.kicad_pcb")
	if err != nil {
		t.Fatal(err)
	}
	board := filepath.Join(dir, "demo.kicad_pcb")
	if err := os.WriteFile(board, src, 0o644); err != nil {
		t.Fatal(err)
	}

	var st statusOut
	call("status", nil, &st)
	if st.Worker.State != "running" {
		t.Fatalf("status = %+v", st)
	}

	// 1. Analyze, and wait.
	var first analyzeOut
	call("analyze_board", map[string]any{"path": board, "wait_s": 300}, &first)
	if first.Run == nil || first.Run.Status != "done" || !first.NewProject {
		t.Fatalf("first analysis = %+v run %+v", first, first.Run)
	}
	t.Logf("analyzed in %s: %v", first.Run.Waited, first.Run.Summary["findings"])

	// 2. What it found, read three ways.
	var fs findingsOut
	call("findings", map[string]any{"project_id": first.ProjectID}, &fs)
	total := fs.Totals["critical"] + fs.Totals["warning"] + fs.Totals["info"]
	if total == 0 || len(fs.Findings) == 0 || fs.RunID != first.Run.ID {
		t.Fatalf("findings = %+v", fs)
	}
	var one findingOut
	call("finding", map[string]any{"run_id": fs.RunID, "finding_id": fs.Findings[0].ID}, &one)
	if one.Finding.Detail == "" || one.Check.Title == "" {
		t.Errorf("finding = %+v", one)
	}
	var dec decouplingOut
	call("decoupling", map[string]any{"project_id": first.ProjectID}, &dec)
	if len(dec.Rails) == 0 && dec.Missing == "" {
		t.Errorf("decoupling = %+v", dec)
	}
	if len(dec.Rails) > 0 {
		var z decouplingOut
		call("decoupling", map[string]any{"project_id": first.ProjectID, "rail": dec.Rails[0].Net, "impedance_hz": []float64{4e5, 1e6}}, &z)
		if len(z.ICs) == 0 || z.ICs[0].Impedance == nil || len(z.ICs[0].Impedance.AsLaidOut) != 2 || z.ICs[0].Impedance.AsLaidOut[0] <= 0 {
			t.Errorf("impedance = %+v", z.ICs)
		}
	}

	// 3. Unchanged: nothing is sent and nothing re-runs.
	var again analyzeOut
	call("analyze_board", map[string]any{"path": board}, &again)
	if again.Uploaded || again.ProjectID != first.ProjectID {
		t.Errorf("unchanged board was sent again: %+v", again)
	}

	// 4. A rules file beside the board turns the check with the most findings off: a new
	//    version, and compare says those findings are fixed and nothing is new.
	worst := fs.ByRule[0].Rule
	if err := os.WriteFile(filepath.Join(dir, "emi.rules.yaml"), []byte("version: 1\nrules:\n  "+worst+": false\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var second analyzeOut
	call("analyze_board", map[string]any{"path": board, "wait_s": 300}, &second)
	if second.Run == nil || second.Run.Status != "done" || second.ProjectID != first.ProjectID ||
		!strings.Contains(strings.Join(second.Files, ","), "emi.rules.yaml") {
		t.Fatalf("second analysis = %+v", second)
	}
	var cmp compareOut
	call("compare", map[string]any{"project_id": first.ProjectID}, &cmp)
	if cmp.Before.Version != 1 || cmp.After.Version != 2 || cmp.Counts["fixed"].Total == 0 || cmp.Counts["new"].Total != 0 {
		t.Fatalf("compare = %s %+v", cmp.Headline, cmp.Counts)
	}
	for _, r := range cmp.Rules {
		if r.Rule != worst && len(r.Fixed) > 0 {
			t.Errorf("turning %s off fixed %s findings too", worst, r.Rule)
		}
	}
	t.Logf("compare: %s", cmp.Headline)

	var vs listVersionsOut
	call("list_versions", map[string]any{"project_id": first.ProjectID}, &vs)
	if len(vs.Versions) != 2 {
		t.Errorf("versions = %+v", vs.Versions)
	}
}
