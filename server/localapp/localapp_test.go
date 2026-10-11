package localapp

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"
)

// app is a fake emi-local: it answers /api/local/status.
func app(t *testing.T) *httptest.Server {
	t.Helper()
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/local/status" {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"version": "1.2.3", "data_dir": "/data", "worker": map[string]any{"mode": "docker", "state": "running"},
		})
	}))
	t.Cleanup(s.Close)
	return s
}

func writeEndpoint(t *testing.T, path string, url any) {
	t.Helper()
	b, _ := json.Marshal(map[string]any{"url": url, "pid": 1})
	if err := os.WriteFile(path, b, 0o600); err != nil {
		t.Fatal(err)
	}
}

type env map[string]string

func (e env) get(k string) string { return e[k] }

func finder(t *testing.T, e env, o Options) (*Finder, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "endpoint.json")
	o.EndpointFile = path
	o.Getenv = e.get
	if o.Poll == 0 {
		o.Poll = 5 * time.Millisecond
	}
	if o.Candidates == nil {
		o.Candidates = func() [][]string { return nil }
	}
	if o.Spawn == nil {
		o.Spawn = func([]string) error { t.Fatal("nothing should be started"); return nil }
	}
	return New(o), path
}

func TestNoFileMeansNoApp(t *testing.T) {
	f, _ := finder(t, env{}, Options{})
	if _, ok := f.Running(context.Background()); ok {
		t.Fatal("found an app with no endpoint file")
	}
}

func TestAFileLeftByAnAppThatIsGoneIsNotBelieved(t *testing.T) {
	s := app(t)
	url := s.URL
	s.Close()
	f, path := finder(t, env{}, Options{})
	writeEndpoint(t, path, url)
	if _, ok := f.Running(context.Background()); ok {
		t.Fatal("believed a file whose server is gone")
	}
}

func TestAnAppThatAnswersIsTheApp(t *testing.T) {
	s := app(t)
	f, path := finder(t, env{}, Options{})
	writeEndpoint(t, path, s.URL)
	e, ok := f.Running(context.Background())
	if !ok {
		t.Fatal("did not find the app")
	}
	if e.URL != s.URL || e.Version != "1.2.3" || e.DataDir != "/data" || e.Worker.State != "running" {
		t.Errorf("endpoint = %+v", e)
	}
	if e.API() != s.URL+"/api" {
		t.Errorf("API() = %s", e.API())
	}
}

func TestTheFileCanOnlyEverPointAtThisComputer(t *testing.T) {
	for _, url := range []any{
		"http://192.168.1.10:7465",
		"https://example.com",
		"file:///etc/passwd",
		"",
		7465,
		// All of these start with "http://127.0.0.1" and none of them is this computer.
		"http://127.0.0.1.example.com:7465",
		"http://127.0.0.1@example.com",
		"http://127.0.0.1:7465@example.com",
		"http://127.0.0.10:7465",
		"http://127.0.0.1:notaport",
	} {
		path := filepath.Join(t.TempDir(), "endpoint.json")
		writeEndpoint(t, path, url)
		if got, ok := ReadEndpoint(path); ok {
			t.Errorf("%v: accepted as %q", url, got)
		}
	}
	for _, url := range []string{"http://127.0.0.1", "http://127.0.0.1:7465", "http://127.0.0.1:7465/"} {
		if !IsLoopback(url) {
			t.Errorf("%s: refused", url)
		}
	}
}

func TestAURLGivenByHandSkipsDiscovery(t *testing.T) {
	s := app(t)
	f, path := finder(t, env{URLEnv: s.URL}, Options{})
	writeEndpoint(t, path, "http://127.0.0.1:1") // would fail if it were read
	if e, ok := f.Running(context.Background()); !ok || e.URL != s.URL {
		t.Fatalf("Running = %+v, %v", e, ok)
	}
}

func TestAURLGivenByHandIsNeverSecondGuessedByStartingTheApp(t *testing.T) {
	f, _ := finder(t, env{URLEnv: "http://127.0.0.1:1"}, Options{
		Candidates: func() [][]string { return [][]string{{"app"}} },
	})
	_, err := f.AttachOrStart(context.Background(), time.Second)
	if !errors.Is(err, ErrUnavailable) || !strings.Contains(err.Error(), URLEnv) {
		t.Fatalf("err = %v", err)
	}
}

func TestARunningAppIsUsedAsItIs(t *testing.T) {
	s := app(t)
	f, path := finder(t, env{}, Options{Candidates: func() [][]string { return [][]string{{"app"}} }})
	writeEndpoint(t, path, s.URL)
	if e, err := f.AttachOrStart(context.Background(), time.Second); err != nil || e.URL != s.URL {
		t.Fatalf("AttachOrStart = %+v, %v", e, err)
	}
}

func TestNoInstalledAppSaysWhatToDoAboutIt(t *testing.T) {
	f, _ := finder(t, env{}, Options{Spawn: func([]string) error { return errors.New("no such app") },
		Candidates: func() [][]string { return [][]string{{"open", "-b", BundleID}} }})
	_, err := f.AttachOrStart(context.Background(), time.Second)
	if !errors.Is(err, ErrUnavailable) {
		t.Fatalf("err = %v", err)
	}
	for _, want := range []string{"not installed", InstallURL, AppEnv, URLEnv} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error does not mention %q: %v", want, err)
		}
	}
}

// launcher records what was started, and makes the app come up after the given command.
type launcher struct {
	mu      sync.Mutex
	started [][]string
	upAfter string // the command that brings the app up
	path    string
	url     string
}

func (l *launcher) spawn(t *testing.T) func([]string) error {
	return func(cmd []string) error {
		l.mu.Lock()
		defer l.mu.Unlock()
		l.started = append(l.started, cmd)
		if cmd[0] == l.upAfter {
			writeEndpoint(t, l.path, l.url)
		}
		return nil
	}
}

func TestOnlyOneCopyOfTheAppIsEverStarted(t *testing.T) {
	s := app(t)
	l := &launcher{upAfter: "first", url: s.URL}
	f, path := finder(t, env{}, Options{
		Candidates: func() [][]string { return [][]string{{"first"}, {"second"}} },
		Self:       "emi-local",
	})
	l.path = path
	f.o.Spawn = l.spawn(t)
	if _, err := f.AttachOrStart(context.Background(), 5*time.Second); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(l.started, [][]string{{"first"}}) {
		t.Errorf("started %v", l.started)
	}
}

func TestTheNextWayOfStartingItIsTriedWhenTheFirstFails(t *testing.T) {
	s := app(t)
	l := &launcher{upAfter: "second", url: s.URL}
	f, path := finder(t, env{}, Options{
		Candidates: func() [][]string { return [][]string{{"first"}, {"second"}} },
	})
	l.path = path
	spawn := l.spawn(t)
	f.o.Spawn = func(cmd []string) error {
		if cmd[0] == "first" {
			return errors.New("not there")
		}
		return spawn(cmd)
	}
	if _, err := f.AttachOrStart(context.Background(), 5*time.Second); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(l.started, [][]string{{"second"}}) {
		t.Errorf("started %v", l.started)
	}
}

func TestHeadlessOnlyWhenNoAppCouldBeStarted(t *testing.T) {
	s := app(t)
	l := &launcher{upAfter: "/bin/emi-local", url: s.URL}
	f, path := finder(t, env{}, Options{
		Candidates: func() [][]string { return [][]string{{"open", "-b", BundleID}} },
		Self:       "/bin/emi-local",
	})
	l.path = path
	spawn := l.spawn(t)
	f.o.Spawn = func(cmd []string) error {
		if cmd[0] == "open" {
			return errors.New("no such app")
		}
		return spawn(cmd)
	}
	if _, err := f.AttachOrStart(context.Background(), 5*time.Second); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(l.started, [][]string{{"/bin/emi-local", "-open=false"}}) {
		t.Errorf("started %v", l.started)
	}
}

func TestAnAppThatDoesNotComeUpIsReportedRatherThanWaitedOn(t *testing.T) {
	var started [][]string
	f, _ := finder(t, env{}, Options{
		Candidates: func() [][]string { return [][]string{{"slow"}} },
		Self:       "/bin/emi-local", // never started: the app did start, it is just slow
		Spawn:      func(cmd []string) error { started = append(started, cmd); return nil },
	})
	_, err := f.AttachOrStart(context.Background(), 50*time.Millisecond)
	if !errors.Is(err, ErrUnavailable) || !strings.Contains(err.Error(), "did not answer") {
		t.Fatalf("err = %v", err)
	}
	if !reflect.DeepEqual(started, [][]string{{"slow"}}) {
		t.Errorf("started %v", started)
	}
}

func TestCandidates(t *testing.T) {
	home, _ := os.UserHomeDir()
	none := func(string) bool { return false }
	noPath := func(string) (string, error) { return "", errors.New("not found") }

	mac := appCandidates("darwin", env{}.get, func(p string) bool { return p == "/Applications/EMI Analyzer.app" }, none, noPath)
	want := [][]string{{"open", "-b", BundleID}, {"open", "-a", "/Applications/EMI Analyzer.app"}}
	if !reflect.DeepEqual(mac, want) {
		t.Errorf("darwin = %v", mac)
	}
	// Not in /Applications: the bundle id still finds it wherever it was filed.
	if got := appCandidates("darwin", env{}.get, none, none, noPath); !reflect.DeepEqual(got, [][]string{{"open", "-b", BundleID}}) {
		t.Errorf("darwin, elsewhere = %v", got)
	}
	got := appCandidates("darwin", env{AppEnv: "/x/EMI Analyzer.app"}.get, none, none, noPath)
	if !reflect.DeepEqual(got[0], []string{"open", "-a", "/x/EMI Analyzer.app"}) {
		t.Errorf("darwin override = %v", got)
	}

	exe := filepath.Join(`C:\Users\u\AppData\Local`, "Programs", "EMI Analyzer", "EMI Analyzer.exe")
	win := appCandidates("windows", env{"LOCALAPPDATA": `C:\Users\u\AppData\Local`}.get, none,
		func(p string) bool { return p == exe }, noPath)
	if !reflect.DeepEqual(win, [][]string{{exe}}) {
		t.Errorf("windows = %v", win)
	}

	appimage := filepath.Join(home, "Applications", "EMI Analyzer.AppImage")
	linux := appCandidates("linux", env{}.get, none, func(p string) bool { return p == appimage },
		func(n string) (string, error) {
			if n == "emi-analyzer" {
				return "/usr/bin/emi-analyzer", nil
			}
			return "", errors.New("not found")
		})
	if !reflect.DeepEqual(linux, [][]string{{"/usr/bin/emi-analyzer"}, {appimage}}) {
		t.Errorf("linux = %v", linux)
	}
}
