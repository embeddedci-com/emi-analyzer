package emiclient

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"sync"
	"testing"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

// ---- packing ----

func write(t *testing.T, dir string, files map[string]string) {
	t.Helper()
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func members(t *testing.T, data []byte) []string {
	t.Helper()
	zr, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, f := range zr.File {
		out = append(out, f.Name)
	}
	return out
}

func TestPackKiCadTakesItsProjectAndTheFirstRulesFile(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "amp")
	_ = os.Mkdir(dir, 0o755)
	write(t, dir, map[string]string{
		"amp.kicad_pcb": "(kicad_pcb)", "amp.kicad_pro": "{}", "other.kicad_pro": "{}",
		"emi.rules.yml": "a: 1", "emi.rules.json": "{}", "notes.txt": "x",
	})
	for _, path := range []string{"amp.kicad_pcb", "amp.kicad_pro", "."} {
		p, err := Pack(filepath.Join(dir, path))
		if err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		want := []string{"amp.kicad_pcb", "amp.kicad_pro", "emi.rules.yml"}
		if got := members(t, p.Data); !reflect.DeepEqual(got, want) {
			t.Errorf("%s: archive = %v, want %v", path, got, want)
		}
		if p.Name != "amp" || p.Filename != "amp.zip" || p.Kind != emi.SourceKiCad || p.Key != filepath.Join(dir, "amp.kicad_pcb") {
			t.Errorf("%s: packed = %+v", path, p)
		}
	}
}

func TestProjectNameSaysWhichBoard(t *testing.T) {
	for _, c := range []struct{ dir, stem, want string }{
		{"/pcb/amp", "amp", "amp"},
		{"/pcb/AMP", "amp", "amp"},
		{"/pcb/benchpod-digital", "vbench-pod", "benchpod-digital/vbench-pod"},
	} {
		if got := projectName(c.dir, c.stem); got != c.want {
			t.Errorf("projectName(%q, %q) = %q, want %q", c.dir, c.stem, got, c.want)
		}
	}
}

func TestPackIsTheSameBytesEveryTime(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, map[string]string{"b.kicad_pcb": "(kicad_pcb)", "b.kicad_pro": "{}"})
	a, _ := Pack(filepath.Join(dir, "b.kicad_pcb"))
	// A later mtime must not change the archive: uploads are content-addressed.
	write(t, dir, map[string]string{"b.kicad_pro": "{}"})
	b, _ := Pack(filepath.Join(dir, "b.kicad_pcb"))
	if a.SHA256 != b.SHA256 {
		t.Fatal("the same files packed to different bytes")
	}
	write(t, dir, map[string]string{"b.kicad_pcb": "(kicad_pcb (edited))"})
	c, _ := Pack(filepath.Join(dir, "b.kicad_pcb"))
	if c.SHA256 == a.SHA256 {
		t.Fatal("an edited board packed to the same bytes")
	}
}

func TestPackFolderWithSeveralBoards(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, map[string]string{"a.kicad_pcb": "(kicad_pcb)", "b.kicad_pcb": "(kicad_pcb)"})
	if _, err := Pack(dir); err == nil || !strings.Contains(err.Error(), "a.kicad_pcb, b.kicad_pcb") {
		t.Fatalf("err = %v", err)
	}
	// The one named after the project file is the project's board.
	write(t, dir, map[string]string{"b.kicad_pro": "{}"})
	p, err := Pack(dir)
	if err != nil || filepath.Base(p.Key) != "b.kicad_pcb" {
		t.Fatalf("Pack = %+v, %v", p, err)
	}
}

func TestPackGerberFolderTakesWhatTheWorkerReads(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, map[string]string{
		"top.gtl": "G04*", "bot.gbl": "G04*", "edge.gm1": "G04*", "drill.drl": "M48", "net.d356": "P",
		"silk.gto": "G04*", "readme.md": "x", "emi.rules.yaml": "a: 1",
	})
	p, err := Pack(dir)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"bot.gbl", "drill.drl", "edge.gm1", "emi.rules.yaml", "net.d356", "top.gtl"}
	if got := members(t, p.Data); !reflect.DeepEqual(got, want) {
		t.Errorf("archive = %v, want %v", got, want)
	}
	if p.Kind != emi.SourceGerber || p.Name != filepath.Base(dir) {
		t.Errorf("packed = %+v", p)
	}
}

func TestPackZipIsSentAsItIs(t *testing.T) {
	dir := t.TempDir()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	w, _ := zw.Create("proj/board.kicad_pcb")
	_, _ = w.Write([]byte("(kicad_pcb)"))
	_ = zw.Close()
	path := filepath.Join(dir, "release-2.zip")
	_ = os.WriteFile(path, buf.Bytes(), 0o644)
	p, err := Pack(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(p.Data, buf.Bytes()) || p.Kind != emi.SourceKiCad || p.Name != "release-2" {
		t.Errorf("packed = %+v", p)
	}
}

func TestPackRefusesWhatIsNotABoard(t *testing.T) {
	dir := t.TempDir()
	write(t, dir, map[string]string{"x.sch": "", "lonely.kicad_pro": "{}", "fake.zip": "no"})
	for _, name := range []string{"x.sch", "lonely.kicad_pro", "fake.zip", "missing.kicad_pcb"} {
		if _, err := Pack(filepath.Join(dir, name)); err == nil {
			t.Errorf("%s: packed", name)
		}
	}
	empty := t.TempDir()
	if _, err := Pack(empty); err == nil {
		t.Error("an empty folder packed")
	}
}

// ---- a fake app ----

type fakeApp struct {
	mu       sync.Mutex
	srv      *httptest.Server
	projects map[string]emi.Project
	boards   []emi.Board
	blobs    map[string][]byte
	puts     int
	n        int
	// unparsed leaves new boards waiting for an ingest that runStatus reports.
	unparsed  bool
	runStatus emi.RunStatus
}

func newFakeApp(t *testing.T) *fakeApp {
	a := &fakeApp{projects: map[string]emi.Project{}, blobs: map[string][]byte{}}
	mux := http.NewServeMux()
	j := func(w http.ResponseWriter, code int, v any) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(code)
		_ = json.NewEncoder(w).Encode(v)
	}
	mux.HandleFunc("POST /api/emi/projects", func(w http.ResponseWriter, r *http.Request) {
		var in emi.Project
		_ = json.NewDecoder(r.Body).Decode(&in)
		a.mu.Lock()
		a.n++
		p := emi.Project{ID: fmt.Sprintf("p%d", a.n), Name: in.Name, SourceKind: in.SourceKind}
		a.projects[p.ID] = p
		a.mu.Unlock()
		j(w, 201, p)
	})
	mux.HandleFunc("GET /api/emi/projects/{id}", func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		p, ok := a.projects[r.PathValue("id")]
		a.mu.Unlock()
		if !ok {
			j(w, 404, map[string]string{"error": "project not found"})
			return
		}
		j(w, 200, p)
	})
	mux.HandleFunc("GET /api/emi/boards/lookup", func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		defer a.mu.Unlock()
		for i := len(a.boards) - 1; i >= 0; i-- {
			if b := a.boards[i]; b.ContentSHA256 == r.URL.Query().Get("sha256") {
				p := a.projects[b.ProjectID]
				j(w, 200, map[string]any{"found": true, "parsed": b.BoardKey != "", "board": b, "project": p})
				return
			}
		}
		j(w, 200, map[string]any{"found": false})
	})
	mux.HandleFunc("POST /api/emi/projects/{id}/uploads", func(w http.ResponseWriter, r *http.Request) {
		var in struct{ SHA256 string }
		_ = json.NewDecoder(r.Body).Decode(&in)
		key := "uploads/" + in.SHA256
		a.mu.Lock()
		_, have := a.blobs[key]
		a.mu.Unlock()
		if have {
			j(w, 200, map[string]any{"key": key, "already_uploaded": true})
			return
		}
		j(w, 200, map[string]any{"key": key, "upload_url": a.srv.URL + "/blob/" + key, "content_type": "application/zip"})
	})
	mux.HandleFunc("PUT /blob/{key...}", func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		a.mu.Lock()
		a.blobs[r.PathValue("key")] = b
		a.puts++
		a.mu.Unlock()
	})
	mux.HandleFunc("POST /api/emi/projects/{id}/boards", func(w http.ResponseWriter, r *http.Request) {
		var in struct {
			InputKey string `json:"input_key"`
			SHA256   string `json:"sha256"`
		}
		_ = json.NewDecoder(r.Body).Decode(&in)
		a.mu.Lock()
		defer a.mu.Unlock()
		if _, ok := a.blobs[in.InputKey]; !ok {
			j(w, 400, map[string]string{"error": "no uploaded object at that key"})
			return
		}
		a.n++
		b := emi.Board{ID: fmt.Sprintf("b%d", a.n), ProjectID: r.PathValue("id"), ContentSHA256: in.SHA256, BoardKey: "x"}
		b.IngestRunID = "r" + b.ID
		if a.unparsed {
			// As the real app: the hash is recorded only once the ingest has read the bytes.
			b.BoardKey, b.ContentSHA256 = "", ""
		}
		a.boards = append(a.boards, b)
		j(w, 201, map[string]any{"board": b, "run": emi.Run{ID: "r" + b.ID, Kind: "ingest", Status: "queued"}})
	})
	mux.HandleFunc("GET /api/emi/runs/{id}", func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		defer a.mu.Unlock()
		j(w, 200, emi.Run{ID: r.PathValue("id"), Kind: "ingest", Status: a.runStatus})
	})
	mux.HandleFunc("GET /api/emi/runs/{id}/artifacts/{name...}", func(w http.ResponseWriter, r *http.Request) {
		j(w, 200, map[string]any{"url": "/blob/art/" + r.PathValue("name")})
	})
	mux.HandleFunc("GET /blob/art/{name...}", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"name":"` + r.PathValue("name") + `"}`))
	})
	a.srv = httptest.NewServer(mux)
	t.Cleanup(a.srv.Close)
	return a
}

func board(t *testing.T, dir, body string) *Packed {
	t.Helper()
	dir = filepath.Join(dir, "amp") // a project folder named after its board
	_ = os.MkdirAll(dir, 0o755)
	write(t, dir, map[string]string{"amp.kicad_pcb": body})
	p, err := Pack(filepath.Join(dir, "amp.kicad_pcb"))
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func TestOpenFilesEveryVersionOfABoardInOneProject(t *testing.T) {
	app := newFakeApp(t)
	c := New(app.srv.URL)
	store := NewProjectStore(t.TempDir())
	dir := t.TempDir()
	ctx := context.Background()

	first, err := Open(ctx, c, store, board(t, dir, "(kicad_pcb 1)"), "", false)
	if err != nil {
		t.Fatal(err)
	}
	if !first.Uploaded || !first.NewProject || first.RunID == "" || app.projects[first.ProjectID].Name != "amp" {
		t.Fatalf("first = %+v", first)
	}

	// Unchanged: nothing is sent.
	again, err := Open(ctx, c, store, board(t, dir, "(kicad_pcb 1)"), "", false)
	if err != nil || again.Uploaded || again.ProjectID != first.ProjectID || again.BoardID != first.BoardID || app.puts != 1 {
		t.Fatalf("again = %+v, %v (puts %d)", again, err, app.puts)
	}

	// Edited: a new version in the same project.
	edited, err := Open(ctx, c, store, board(t, dir, "(kicad_pcb 2)"), "", false)
	if err != nil || !edited.Uploaded || edited.NewProject || edited.ProjectID != first.ProjectID || edited.BoardID == first.BoardID {
		t.Fatalf("edited = %+v, %v", edited, err)
	}

	// Forced: analyzed again although the app has it. The bytes are not sent twice.
	forced, err := Open(ctx, c, store, board(t, dir, "(kicad_pcb 2)"), "", true)
	if err != nil || !forced.Uploaded || forced.RunID == "" || forced.ProjectID != first.ProjectID || app.puts != 2 {
		t.Fatalf("forced = %+v, %v (puts %d)", forced, err, app.puts)
	}
}

func TestOpenTwiceWhileTheIngestIsPendingQueuesItOnce(t *testing.T) {
	app := newFakeApp(t)
	app.unparsed, app.runStatus = true, emi.StatusNew
	c := New(app.srv.URL)
	dir := t.TempDir()
	store := NewProjectStore(t.TempDir())
	first, err := Open(context.Background(), c, store, board(t, dir, "(kicad_pcb)"), "", false)
	if err != nil {
		t.Fatal(err)
	}
	again, err := Open(context.Background(), c, store, board(t, dir, "(kicad_pcb)"), "", false)
	if err != nil || again.Uploaded || again.RunID != first.RunID || len(app.boards) != 1 {
		t.Fatalf("again = %+v, %v (boards %d)", again, err, len(app.boards))
	}
	// An ingest that failed is not waited on: the board is sent for a fresh one.
	app.runStatus = emi.StatusFailed
	retry, err := Open(context.Background(), c, store, board(t, dir, "(kicad_pcb)"), "", false)
	if err != nil || !retry.Uploaded || retry.RunID == first.RunID || len(app.boards) != 2 {
		t.Fatalf("retry = %+v, %v (boards %d)", retry, err, len(app.boards))
	}
}

func TestOpenAfterTheProjectWasDeletedMakesANewOne(t *testing.T) {
	app := newFakeApp(t)
	c := New(app.srv.URL)
	store := NewProjectStore(t.TempDir())
	dir := t.TempDir()
	first, _ := Open(context.Background(), c, store, board(t, dir, "(kicad_pcb 1)"), "", false)
	delete(app.projects, first.ProjectID)
	app.boards = nil
	next, err := Open(context.Background(), c, store, board(t, dir, "(kicad_pcb 1)"), "", false)
	if err != nil || !next.NewProject || next.ProjectID == first.ProjectID {
		t.Fatalf("next = %+v, %v", next, err)
	}
}

func TestOpenIntoANamedProject(t *testing.T) {
	app := newFakeApp(t)
	c := New(app.srv.URL)
	dir := t.TempDir()
	if _, err := Open(context.Background(), c, nil, board(t, dir, "(kicad_pcb)"), "nope", false); err == nil {
		t.Fatal("filed into a project that does not exist")
	} else {
		var ae *APIError
		if !errors.As(err, &ae) || ae.Status != 404 || ae.Message != "project not found" {
			t.Fatalf("err = %#v", err)
		}
	}
	p, _ := c.CreateProject(context.Background(), "mine", emi.SourceKiCad)
	o, err := Open(context.Background(), c, nil, board(t, dir, "(kicad_pcb)"), p.ID, false)
	if err != nil || o.ProjectID != p.ID || o.NewProject {
		t.Fatalf("o = %+v, %v", o, err)
	}
}

func TestArtifactFollowsTheURLTheAppMints(t *testing.T) {
	app := newFakeApp(t)
	var got map[string]string
	if err := New(app.srv.URL).ArtifactJSON(context.Background(), "r1", "nearfield/1.json", &got); err != nil {
		t.Fatal(err)
	}
	if got["name"] != "nearfield/1.json" {
		t.Errorf("got %v", got)
	}
}

func TestStoreIgnoresAnotherBoardsFile(t *testing.T) {
	s := NewProjectStore(t.TempDir())
	if err := s.Save("/a.kicad_pcb", "p1"); err != nil {
		t.Fatal(err)
	}
	if s.Load("/a.kicad_pcb") != "p1" || s.Load("/b.kicad_pcb") != "" {
		t.Fatal("store mixed boards up")
	}
	s.Forget("/a.kicad_pcb")
	if s.Load("/a.kicad_pcb") != "" {
		t.Fatal("forgotten board still remembered")
	}
	entries, _ := os.ReadDir(filepath.Join(s.dir))
	names := []string{}
	for _, e := range entries {
		names = append(names, e.Name())
	}
	sort.Strings(names)
	if len(names) != 0 {
		t.Errorf("left %v", names)
	}
}
