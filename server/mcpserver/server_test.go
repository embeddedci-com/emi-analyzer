package mcpserver

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
	"github.com/embeddedci-com/emi-analyzer/server/emiclient"
	"github.com/embeddedci-com/emi-analyzer/server/localapp"
)

// fakeApp is the bit of emi-local the tools use. A run reports in_progress for the first
// `polls` reads and then done, the way an ingest finishes.
type fakeApp struct {
	mu       sync.Mutex
	srv      *httptest.Server
	projects []emi.Project
	boards   []emi.Board
	runs     map[string]*emi.Run
	reads    map[string]int
	polls    int
	n        int
	// artifacts by run id, then name.
	artifacts map[string]map[string][]byte
}

func newFakeApp(t *testing.T) *fakeApp {
	t.Helper()
	a := &fakeApp{runs: map[string]*emi.Run{}, reads: map[string]int{}, polls: 2, artifacts: map[string]map[string][]byte{}}
	mux := http.NewServeMux()
	j := func(w http.ResponseWriter, code int, v any) {
		w.WriteHeader(code)
		_ = json.NewEncoder(w).Encode(v)
	}
	at := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	mux.HandleFunc("POST /api/emi/projects", func(w http.ResponseWriter, r *http.Request) {
		var in emi.Project
		_ = json.NewDecoder(r.Body).Decode(&in)
		a.mu.Lock()
		defer a.mu.Unlock()
		a.n++
		p := emi.Project{ID: fmt.Sprintf("p%d", a.n), Name: in.Name, SourceKind: in.SourceKind, CreatedAt: at.Add(time.Duration(a.n) * time.Hour)}
		a.projects = append(a.projects, p)
		j(w, 201, p)
	})
	mux.HandleFunc("GET /api/emi/projects", func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		defer a.mu.Unlock()
		j(w, 200, map[string]any{"projects": a.projects})
	})
	mux.HandleFunc("GET /api/emi/projects/{id}", func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		defer a.mu.Unlock()
		for _, p := range a.projects {
			if p.ID == r.PathValue("id") {
				j(w, 200, p)
				return
			}
		}
		j(w, 404, map[string]string{"error": "project not found"})
	})
	mux.HandleFunc("GET /api/emi/boards/lookup", func(w http.ResponseWriter, r *http.Request) {
		j(w, 200, map[string]any{"found": false})
	})
	mux.HandleFunc("POST /api/emi/projects/{id}/uploads", func(w http.ResponseWriter, r *http.Request) {
		j(w, 200, map[string]any{"key": "uploads/local/x.zip", "upload_url": a.srv.URL + "/blob/x"})
	})
	mux.HandleFunc("PUT /blob/x", func(w http.ResponseWriter, r *http.Request) { _, _ = io.Copy(io.Discard, r.Body) })
	mux.HandleFunc("POST /api/emi/projects/{id}/boards", func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		defer a.mu.Unlock()
		a.n++
		b := emi.Board{ID: fmt.Sprintf("b%d", a.n), ProjectID: r.PathValue("id"), InputKey: "uploads/local/amp.zip",
			CreatedAt: at.Add(time.Duration(a.n) * time.Hour)}
		run := &emi.Run{ID: "r" + b.ID, ProjectID: b.ProjectID, BoardID: b.ID, Kind: emi.RunKindIngest,
			Status: emi.StatusNew, CreatedAt: b.CreatedAt}
		b.IngestRunID = run.ID
		a.boards = append(a.boards, b)
		a.runs[run.ID] = run
		j(w, 201, map[string]any{"board": b, "run": run})
	})
	mux.HandleFunc("GET /api/emi/projects/{id}/boards", func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		defer a.mu.Unlock()
		var out []emi.Board
		for _, b := range a.boards {
			if b.ProjectID == r.PathValue("id") {
				out = append(out, b)
			}
		}
		j(w, 200, map[string]any{"boards": out})
	})
	mux.HandleFunc("GET /api/emi/projects/{id}/runs", func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		defer a.mu.Unlock()
		var out []emi.Run
		for _, run := range a.runs {
			if run.ProjectID == r.PathValue("id") {
				out = append(out, *run)
			}
		}
		j(w, 200, map[string]any{"runs": out})
	})
	mux.HandleFunc("GET /api/emi/runs/{id}", func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		defer a.mu.Unlock()
		run, ok := a.runs[r.PathValue("id")]
		if !ok {
			j(w, 404, map[string]string{"error": "run not found"})
			return
		}
		a.reads[run.ID]++
		if run.Status == emi.StatusDone || run.Status == emi.StatusFailed {
			j(w, 200, run)
			return
		}
		if a.reads[run.ID] > a.polls {
			run.Status = emi.StatusDone
			run.Progress = &emi.Progress{Stage: "done", Pct: 100}
			run.Summary = json.RawMessage(`{"stage":"ingest","layers":4}`)
		} else {
			run.Status = emi.StatusInProgress
			run.Progress = &emi.Progress{Stage: "parse", Pct: 40, Message: "reading the board"}
		}
		j(w, 200, run)
	})
	mux.HandleFunc("GET /api/emi/runs/{id}/artifacts/{name...}", func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		_, ok := a.artifacts[r.PathValue("id")][r.PathValue("name")]
		a.mu.Unlock()
		if !ok {
			j(w, 404, map[string]string{"error": "artifact not found"})
			return
		}
		j(w, 200, map[string]any{"url": "/blob/art/" + r.PathValue("id") + "/" + r.PathValue("name")})
	})
	mux.HandleFunc("GET /blob/art/{id}/{name...}", func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		defer a.mu.Unlock()
		_, _ = w.Write(a.artifacts[r.PathValue("id")][r.PathValue("name")])
	})
	a.srv = httptest.NewServer(mux)
	t.Cleanup(a.srv.Close)
	return a
}

// finder hands out the fake app, or an error.
type finder struct {
	e   localapp.Endpoint
	err error
}

func (f finder) Running(context.Context) (localapp.Endpoint, bool) { return f.e, f.err == nil }
func (f finder) AttachOrStart(context.Context, time.Duration) (localapp.Endpoint, error) {
	return f.e, f.err
}

func connect(t *testing.T, f Finder) *mcp.ClientSession {
	t.Helper()
	srv := New("test", Options{Finder: f, Store: emiclient.NewProjectStore(t.TempDir()), Poll: time.Millisecond})
	st, ct := mcp.NewInMemoryTransports()
	ctx := context.Background()
	ss, err := srv.Connect(ctx, st, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ss.Close() })
	cs, err := mcp.NewClient(&mcp.Implementation{Name: "test"}, nil).Connect(ctx, ct, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cs.Close() })
	return cs
}

// call runs a tool and returns its structured output, or its error text.
func call(t *testing.T, cs *mcp.ClientSession, name string, args map[string]any, out any) (errText string) {
	t.Helper()
	res, err := cs.CallTool(context.Background(), &mcp.CallToolParams{Name: name, Arguments: args})
	if err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	if res.IsError {
		var parts []string
		for _, c := range res.Content {
			if tc, ok := c.(*mcp.TextContent); ok {
				parts = append(parts, tc.Text)
			}
		}
		return strings.Join(parts, " ")
	}
	raw, _ := json.Marshal(res.StructuredContent)
	if err := json.Unmarshal(raw, out); err != nil {
		t.Fatalf("%s: %v in %s", name, err, raw)
	}
	return ""
}

func boardFile(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	for name, body := range map[string]string{"amp.kicad_pcb": "(kicad_pcb)", "amp.kicad_pro": "{}"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return filepath.Join(dir, "amp.kicad_pcb")
}

func TestToolsAndInstructions(t *testing.T) {
	cs := connect(t, finder{})
	res, err := cs.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, tool := range res.Tools {
		names = append(names, tool.Name)
		if tool.Description == "" || tool.InputSchema == nil {
			t.Errorf("%s: no description or schema", tool.Name)
		}
	}
	sort.Strings(names)
	want := "analyze_board cables checks compare decoupling esd finding findings list_projects list_versions nets open_in_app run_cables run_esd run_status status"
	if strings.Join(names, " ") != want {
		t.Errorf("tools = %v, want %s", names, want)
	}
	if !strings.Contains(cs.InitializeResult().Instructions, "not a pre-compliance test") {
		t.Error("instructions do not say what the results are not")
	}
}

func TestAnalyzeAndWait(t *testing.T) {
	app := newFakeApp(t)
	cs := connect(t, finder{e: localapp.Endpoint{URL: app.srv.URL}})
	var out analyzeOut
	if e := call(t, cs, "analyze_board", map[string]any{"path": boardFile(t), "wait_s": 30}, &out); e != "" {
		t.Fatal(e)
	}
	if !out.NewProject || !out.Uploaded || out.Kind != "kicad" || strings.Join(out.Files, ",") != "amp.kicad_pcb,amp.kicad_pro" {
		t.Errorf("out = %+v", out)
	}
	if out.Run == nil || !out.Run.Finished || out.Run.Status != "done" || out.Run.Summary["layers"] != 4.0 {
		t.Errorf("run = %+v", out.Run)
	}

	// Without a wait, run_status answers at once.
	app.mu.Lock()
	app.reads[out.Run.ID], app.runs[out.Run.ID].Status = 0, emi.StatusInProgress
	app.mu.Unlock()
	var r runOut
	if e := call(t, cs, "run_status", map[string]any{"run_id": out.Run.ID}, &r); e != "" {
		t.Fatal(e)
	}
	if r.Finished || r.Stage != "parse" || r.Pct != 40 || r.Summary != nil {
		t.Errorf("run_status = %+v", r)
	}
}

func TestVersionsAreNumberedOldestFirst(t *testing.T) {
	app := newFakeApp(t)
	app.polls = 0
	cs := connect(t, finder{e: localapp.Endpoint{URL: app.srv.URL}})
	path := boardFile(t)
	var first, second analyzeOut
	call(t, cs, "analyze_board", map[string]any{"path": path, "wait_s": 5}, &first)
	_ = os.WriteFile(path, []byte("(kicad_pcb (edited))"), 0o644)
	call(t, cs, "analyze_board", map[string]any{"path": path}, &second)
	if second.ProjectID != first.ProjectID || second.NewProject {
		t.Fatalf("second = %+v", second)
	}
	var v listVersionsOut
	if e := call(t, cs, "list_versions", map[string]any{"project_id": first.ProjectID}, &v); e != "" {
		t.Fatal(e)
	}
	if len(v.Versions) != 2 || v.Versions[0].BoardID != first.BoardID || v.Versions[1].Number != 2 ||
		v.Versions[0].Filename != "amp.zip" {
		t.Fatalf("versions = %+v", v.Versions)
	}
	if v.Versions[0].Analysis == nil || v.Versions[0].Analysis.ID != first.Run.ID {
		t.Errorf("version 1 analysis = %+v", v.Versions[0].Analysis)
	}
	var ps listProjectsOut
	call(t, cs, "list_projects", nil, &ps)
	if len(ps.Projects) != 1 || ps.Projects[0].Name != "amp" {
		t.Errorf("projects = %+v", ps.Projects)
	}
}

func TestErrorsReachTheAgentAsText(t *testing.T) {
	app := newFakeApp(t)
	cs := connect(t, finder{e: localapp.Endpoint{URL: app.srv.URL}})
	var out analyzeOut
	if e := call(t, cs, "analyze_board", map[string]any{"path": "/nope/board.kicad_pcb"}, &out); !strings.Contains(e, "no board at") {
		t.Errorf("missing file: %q", e)
	}
	if e := call(t, cs, "run_status", map[string]any{"run_id": "zzz"}, &runOut{}); !strings.Contains(e, "run not found") {
		t.Errorf("unknown run: %q", e)
	}
	if e := call(t, cs, "list_versions", map[string]any{"project_id": "zzz"}, &listVersionsOut{}); !strings.Contains(e, "project not found") {
		t.Errorf("unknown project: %q", e)
	}

	gone := connect(t, finder{err: fmt.Errorf("%w: it is not installed", localapp.ErrUnavailable)})
	if e := call(t, gone, "status", nil, &statusOut{}); !strings.Contains(e, "not installed") {
		t.Errorf("no app: %q", e)
	}
}

func TestStatusSaysWhatToDoAboutDocker(t *testing.T) {
	cs := connect(t, finder{e: localapp.Endpoint{URL: "http://127.0.0.1:1", Version: "1.0",
		Worker: localapp.Worker{Mode: "docker", State: "docker_not_running"}}})
	var out statusOut
	if e := call(t, cs, "status", nil, &out); e != "" {
		t.Fatal(e)
	}
	if out.Version != "1.0" || !strings.Contains(out.Advice, "Start Docker") {
		t.Errorf("status = %+v", out)
	}
}
