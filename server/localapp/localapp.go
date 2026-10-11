// Package localapp finds the EMI Analyzer app running on this computer, and starts it when it
// is not up. It is the Go twin of kicad-plugin/emi_analyzer/desktop.py, for the MCP server;
// keep the two in step (the file they read, the environment variables, the order they try).
//
// The app publishes where it is listening in one file that never moves -- the user's config
// folder, emi-analyzer/endpoint.json -- because its port is not fixed and its data folder is
// not either (see cmd/emi-local/endpoint.go). That file is a hint and nothing more: it
// outlives a crash, a kill and a reboot, so it is never believed without asking the URL in it
// whether anything is there.
//
// Starting the app means starting the *app*, not a server of our own. It owns the data folder
// and the worker container, it knows how to stop them, and a user who opens it from the Dock
// afterwards must find the same boards. Only when no app is installed at all does a client
// start emi-local itself, headless (see Options.Self).
package localapp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

const (
	// URLEnv points a client at a server you started yourself: `make run-local`, or an app on
	// another port. Discovery and starting are both skipped.
	URLEnv = "EMI_ANALYZER_URL"
	// AppEnv names the application to start, when it is somewhere this package would not
	// look.
	AppEnv = "EMI_ANALYZER_APP"

	// BundleID is the macOS app's bundle identifier.
	BundleID = "com.embeddedci.emi-analyzer"

	// StartTimeout is how long to wait for an app we started to answer. It has a database to
	// open and a webapp to serve, both local; the Docker worker starts later and is not waited
	// for.
	StartTimeout = 90 * time.Second
	probeTimeout = 2 * time.Second
	// perCandidate is how long one way of starting the app gets before another is tried.
	// Generous, because the cost of being impatient is a second copy of the app with a second
	// worker container.
	perCandidate = 20 * time.Second

	// InstallURL is where to get the app.
	InstallURL = "https://github.com/embeddedci-com/emi-analyzer#readme"
)

// ErrUnavailable is wrapped by every error that means "no app to talk to".
var ErrUnavailable = errors.New("EMI Analyzer is not available")

// Worker is the app's report on its Docker worker.
type Worker struct {
	Mode    string `json:"mode"`
	State   string `json:"state"` // disabled, docker_missing, docker_not_running, pulling, starting, running, error, stopped
	Message string `json:"message,omitempty"`
	Detail  string `json:"detail,omitempty"`
	Image   string `json:"image,omitempty"`
}

// Endpoint is a running app: where it is, and what it says about itself.
type Endpoint struct {
	URL     string `json:"url"`
	Version string `json:"version"`
	DataDir string `json:"data_dir"`
	Worker  Worker `json:"worker"`
}

// API is the base of the app's JSON API.
func (e Endpoint) API() string { return strings.TrimRight(e.URL, "/") + "/api" }

// EndpointFile is where the app leaves its address: the folder Go's os.UserConfigDir returns,
// as cmd/emi-local writes it.
func EndpointFile() string {
	dir, err := os.UserConfigDir()
	if err != nil {
		dir = os.TempDir()
	}
	return filepath.Join(dir, "emi-analyzer", "endpoint.json")
}

// IsLoopback accepts http://127.0.0.1[:port] and nothing else.
//
// Parsed, not prefix-matched: "http://127.0.0.1.example.com" and "http://127.0.0.1@example.com"
// both start with the loopback address and point somewhere else entirely.
func IsLoopback(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "http" || u.User != nil || u.Hostname() != "127.0.0.1" {
		return false
	}
	if p := u.Port(); p != "" {
		for _, c := range p {
			if c < '0' || c > '9' {
				return false
			}
		}
	}
	return !strings.Contains(u.Host, "@")
}

// ReadEndpoint reads the app's file. ok is false when there is none, it does not parse, or
// its URL is not loopback: this file is the one thing that decides where a board is sent, and
// a file is easier to write than a process is to run.
func ReadEndpoint(path string) (url string, ok bool) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", false
	}
	var e struct {
		URL string `json:"url"`
	}
	if json.Unmarshal(b, &e) != nil || !IsLoopback(e.URL) {
		return "", false
	}
	return e.URL, true
}

// Options are what a Finder needs from its surroundings. The zero value is the real computer.
type Options struct {
	// EndpointFile overrides EndpointFile().
	EndpointFile string
	// Getenv overrides os.Getenv.
	Getenv func(string) string
	// HTTP overrides the client used to probe.
	HTTP *http.Client
	// Self is this program, when it is emi-local itself: started headless as the last resort,
	// and only when no app is installed.
	Self string
	// Spawn starts a command and lets go of it; an error means it did not start. Overridden
	// in tests.
	Spawn func(cmd []string) error
	// Candidates overrides AppCandidates. Overridden in tests.
	Candidates func() [][]string
	// Poll is how often to ask a starting app whether it is up.
	Poll time.Duration
}

// Finder finds and starts the app.
type Finder struct{ o Options }

// New returns a Finder over the given options.
func New(o Options) *Finder {
	if o.EndpointFile == "" {
		o.EndpointFile = EndpointFile()
	}
	if o.Getenv == nil {
		o.Getenv = os.Getenv
	}
	if o.HTTP == nil {
		o.HTTP = &http.Client{Timeout: probeTimeout}
	}
	if o.Spawn == nil {
		o.Spawn = spawn
	}
	if o.Candidates == nil {
		o.Candidates = func() [][]string { return AppCandidates(o.Getenv) }
	}
	if o.Poll == 0 {
		o.Poll = 500 * time.Millisecond
	}
	return &Finder{o: o}
}

// Probe asks a URL whether the app is there. ok is false when nothing answers.
func (f *Finder) Probe(ctx context.Context, base string) (Endpoint, bool) {
	ctx, cancel := context.WithTimeout(ctx, probeTimeout)
	defer cancel()
	base = strings.TrimRight(base, "/")
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/api/local/status", nil)
	if err != nil {
		return Endpoint{}, false
	}
	resp, err := f.o.HTTP.Do(req)
	if err != nil {
		return Endpoint{}, false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return Endpoint{}, false
	}
	var e Endpoint
	if json.NewDecoder(resp.Body).Decode(&e) != nil {
		return Endpoint{}, false
	}
	e.URL = base
	return e, true
}

// Running is the app, if one is up. EMI_ANALYZER_URL is honoured before the file.
func (f *Finder) Running(ctx context.Context) (Endpoint, bool) {
	if u := f.o.Getenv(URLEnv); u != "" {
		return f.Probe(ctx, u)
	}
	u, ok := ReadEndpoint(f.o.EndpointFile)
	if !ok {
		return Endpoint{}, false
	}
	return f.Probe(ctx, u)
}

// AttachOrStart is the running app, starting it first if it is not up.
func (f *Finder) AttachOrStart(ctx context.Context, timeout time.Duration) (Endpoint, error) {
	if e, ok := f.Running(ctx); ok {
		return e, nil
	}
	if u := f.o.Getenv(URLEnv); u != "" {
		return Endpoint{}, fmt.Errorf("%w: nothing answered at %s (%s). Start it, or unset the variable to use the installed app", ErrUnavailable, u, URLEnv)
	}
	return f.Start(ctx, timeout)
}

// Start starts the installed app and waits for it to answer.
//
// One way of starting it at a time, each given its own chance. Starting them all at once
// would be faster and wrong: the ways of naming the app can resolve to different copies of
// it, and then the user has two apps, two data folders and two worker containers.
func (f *Finder) Start(ctx context.Context, timeout time.Duration) (Endpoint, error) {
	deadline := time.Now().Add(timeout)
	candidates := f.o.Candidates()
	started := false
	for i, cmd := range candidates {
		if f.o.Spawn(cmd) != nil {
			continue
		}
		started = true
		until := deadline
		if i < len(candidates)-1 {
			if t := time.Now().Add(perCandidate); t.Before(until) {
				until = t
			}
		}
		if e, err := f.wait(ctx, until); err == nil {
			return e, nil
		} else if ctx.Err() != nil {
			return Endpoint{}, err
		}
		if !time.Now().Before(deadline) {
			break
		}
	}
	if !started && f.o.Self != "" {
		// No app could be started, but this program is emi-local: run it without a window.
		// Only now, never after an app did start: that app may still be coming up, and a
		// second server would make a second data folder and a second worker.
		if err := f.o.Spawn([]string{f.o.Self, "-open=false"}); err == nil {
			started = true
			if e, err := f.wait(ctx, deadline); err == nil {
				return e, nil
			} else if ctx.Err() != nil {
				return Endpoint{}, err
			}
		}
	}
	if !started {
		return Endpoint{}, fmt.Errorf("%w: it is not installed on this computer, or it is somewhere this program does not look. "+
			"Install it from %s and start it once; it needs Docker. Installed somewhere unusual? Set %s to the application. "+
			"Running the server from source? Set %s to its address, for example http://127.0.0.1:7465",
			ErrUnavailable, InstallURL, AppEnv, URLEnv)
	}
	return Endpoint{}, fmt.Errorf("%w: it was started but did not answer within %s. Open it yourself, wait until it shows your boards, and try again",
		ErrUnavailable, timeout.Round(time.Second))
}

// wait polls until the app answers or until passes.
func (f *Finder) wait(ctx context.Context, until time.Time) (Endpoint, error) {
	for {
		if e, ok := f.Running(ctx); ok {
			return e, nil
		}
		if !time.Now().Before(until) {
			return Endpoint{}, errors.New("timed out")
		}
		select {
		case <-ctx.Done():
			return Endpoint{}, ctx.Err()
		case <-time.After(f.o.Poll):
		}
	}
}

// AppCandidates are the commands that start the installed app, best first.
func AppCandidates(getenv func(string) string) [][]string {
	return appCandidates(runtime.GOOS, getenv, isDir, isFile, exec.LookPath)
}

func appCandidates(goos string, getenv func(string) string, dirExists, fileExists func(string) bool,
	lookPath func(string) (string, error)) [][]string {
	var out [][]string
	home, _ := os.UserHomeDir()
	if p := getenv(AppEnv); p != "" {
		if goos == "darwin" && strings.HasSuffix(p, ".app") {
			out = append(out, []string{"open", "-a", p})
		} else {
			out = append(out, []string{p})
		}
	}
	switch goos {
	case "darwin":
		// By bundle id first: it finds the app wherever the user filed it. `open` exits
		// non-zero at once when no such app is registered, so it costs nothing when absent.
		out = append(out, []string{"open", "-b", BundleID})
		for _, base := range []string{"/Applications", filepath.Join(home, "Applications")} {
			if b := filepath.Join(base, "EMI Analyzer.app"); dirExists(b) {
				out = append(out, []string{"open", "-a", b})
			}
		}
	case "windows":
		for _, base := range []string{
			filepath.Join(getenv("LOCALAPPDATA"), "Programs"), getenv("PROGRAMFILES"), getenv("PROGRAMFILES(X86)"),
		} {
			if base == "" || base == "Programs" {
				continue
			}
			if exe := filepath.Join(base, "EMI Analyzer", "EMI Analyzer.exe"); fileExists(exe) {
				out = append(out, []string{exe})
			}
		}
	default:
		for _, name := range []string{"emi-analyzer", "EMI Analyzer"} {
			if p, err := lookPath(name); err == nil {
				out = append(out, []string{p})
			}
		}
		for _, p := range []string{
			"/opt/EMI Analyzer/emi-analyzer",
			filepath.Join(home, "Applications", "EMI Analyzer.AppImage"),
			filepath.Join(home, ".local", "bin", "emi-analyzer"),
		} {
			if fileExists(p) {
				out = append(out, []string{p})
			}
		}
	}
	return out
}

func isDir(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.IsDir()
}

func isFile(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.Mode().IsRegular()
}
