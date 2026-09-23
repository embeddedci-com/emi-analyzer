// Command emi-local runs the whole EMI Analyzer on one computer.
//
// It is the same control plane a hosted server mounts -- the emi package, via emi.Mount --
// with the three host dependencies swapped for local ones:
//
//	Store  SQLite, in the data folder              (local.SQLiteStore)
//	Blob   files in the data folder, served here   (local.FileBlob)
//	Keys   a worker key issued at startup          (local.Keys)
//
// plus the webapp, embedded in the binary, and a supervisor for the worker, which is the
// published Docker image dialling back into this process exactly as it would dial into a
// hosted server.
//
// There is no sign-in. Every request is one fixed local identity, which is only safe because
// the server listens on the loopback interface and refuses requests that did not come from a
// page it served (see guard.go).
//
// Run it directly and it opens a browser tab. The desktop app runs it as a sidecar with
// -open=false -lifeline and shows the same page in its own window.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
	"github.com/embeddedci-com/emi-analyzer/server/local"
)

// version is set at build time: -ldflags "-X main.version=0.3.0". It also picks the worker
// image tag, so an app and the worker it starts are always from the same release.
var version = "dev"

// The identity every request is served as. Not "anon-": the component library refuses
// visitors, and locally there is nobody else to be.
var localUser = emi.UserIdentity{UserID: "local", OrganizationID: "local", Login: "local"}

const defaultAddr = "127.0.0.1:7465"

// autoKeyName labels the keys issued to the container this app starts, which are revoked
// whenever it stops. Keys from -issue-key carry another name and survive restarts.
const autoKeyName = "local worker (automatic)"

// ReadyPrefix is the line printed once the server is accepting connections. The desktop app
// waits for it and reads the URL from it.
const ReadyPrefix = "EMI_LOCAL_READY "

// HideWindowLine asks the desktop shell to put its window away and carry on serving from the
// tray. Stdout, because that is the channel the shell already watches; this process cannot
// touch the window itself, and the page that asks is deliberately given no Tauri APIs.
const HideWindowLine = "EMI_LOCAL_HIDE_WINDOW"

// shellDesktop is what the desktop app passes for -shell. Anything else means nobody is
// holding a window for us, and the calls that move one are not offered.
const shellDesktop = "desktop"

func main() {
	var (
		addr        = flag.String("addr", envOr("EMI_LOCAL_ADDR", defaultAddr), "listen address; must be a loopback address")
		dataDir     = flag.String("data-dir", envOr("EMI_LOCAL_DATA_DIR", defaultDataDir()), "where the database, board files and results are kept")
		open        = flag.Bool("open", true, "open the app in the default browser once it is ready")
		lifeline    = flag.Bool("lifeline", false, "exit when stdin closes (used by the desktop app, so the server never outlives it)")
		workerMode  = flag.String("worker", envOr("EMI_LOCAL_WORKER", "docker"), `"docker" to run the worker container, "none" to connect a worker yourself`)
		workerImage = flag.String("worker-image", envOr("EMI_WORKER_IMAGE", defaultImage()), "worker image to run")
		workerURL   = flag.String("worker-url", envOr("EMI_LOCAL_WORKER_URL", ""), "URL the worker container dials to reach this server (default: detected from the Docker engine)")
		concurrent  = flag.Int("worker-concurrency", 1, "runs the worker takes at once; a solve is memory-bound, so more is rarely faster")
		webDir      = flag.String("webapp", envOr("EMI_LOCAL_WEBAPP", ""), "serve the webapp from this directory instead of the embedded copy (development)")
		experiment  = flag.String("experimental", envOr("EMI_EXPERIMENTAL", ""), `comma-separated experimental features to enable: "small-part-solve" (openEMS solves of one net or a small region, with near-field maps and port impedance, under a size budget) and "full-wave" (all openEMS solves, with their far field and cable emissions, and compliance estimates; unverified on real boards; includes small-part-solve; the standalone cable budget is always on)`)
		shell       = flag.String("shell", "", `the program holding a window on this server: "desktop" for the app, empty when there is none`)
		endpoint    = flag.String("endpoint-file", envOr("EMI_LOCAL_ENDPOINT_FILE", defaultEndpointFile()), "file left behind so other programs on this computer (the KiCad plugin) can find this app; empty to write none")
		issueKey    = flag.Bool("issue-key", false, "print a key for a worker you run yourself (with -worker=none), and exit")
		showVersion = flag.Bool("version", false, "print the version and exit")
		maxUploadMB = flag.Int64("max-upload-mb", envInt64("EMI_MAX_UPLOAD_MB", emi.DefaultMaxUploadBytes>>20), "largest board file or result the app stores, in MB")
	)
	flag.Parse()
	if *showVersion {
		fmt.Println(version)
		return
	}

	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	if *issueKey {
		if err := printKey(*dataDir); err != nil {
			logger.Error("could not issue a key", "err", err)
			os.Exit(1)
		}
		return
	}

	if err := run(logger, options{
		addr: *addr, dataDir: *dataDir, open: *open, lifeline: *lifeline,
		workerMode: *workerMode, workerImage: *workerImage, workerURL: *workerURL,
		concurrency: *concurrent, webDir: *webDir, experimental: *experiment,
		endpointFile: *endpoint, shell: *shell, maxUploadBytes: *maxUploadMB << 20,
	}); err != nil {
		logger.Error("emi-local failed", "err", err)
		os.Exit(1)
	}
}

type options struct {
	addr, dataDir  string
	open, lifeline bool
	workerMode     string
	workerImage    string
	workerURL      string
	concurrency    int
	webDir         string
	experimental   string
	endpointFile   string
	shell          string
	maxUploadBytes int64
}

func run(logger *slog.Logger, o options) error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if o.lifeline {
		// When the parent dies, our stdout and stderr are pipes with no reader. Go kills a
		// process that writes to such a pipe on fd 1 or 2 -- so the first log line about the
		// parent going away would end this process before it removed the worker container.
		// Ignored, the write fails quietly instead and shutdown carries on.
		signal.Ignore(syscall.SIGPIPE)
		ctx = withLifeline(ctx, os.Stdin, logger)
	}

	if err := os.MkdirAll(o.dataDir, 0o700); err != nil {
		return fmt.Errorf("data folder %s: %w", o.dataDir, err)
	}
	sec, err := loadSecrets(filepath.Join(o.dataDir, "secret.key"))
	if err != nil {
		return err
	}

	ln, err := listen(o.addr, logger)
	if err != nil {
		return err
	}
	port := ln.Addr().(*net.TCPAddr).Port
	baseURL := fmt.Sprintf("http://127.0.0.1:%d", port)

	store, err := local.OpenSQLite(ctx, filepath.Join(o.dataDir, "emi.db"))
	if err != nil {
		return err
	}
	defer store.Close()
	now := time.Now().UTC()
	if n, err := store.FailInterrupted(ctx, now); err != nil {
		return err
	} else if n > 0 {
		logger.Warn("marked runs interrupted by the last shutdown as failed", "count", n)
	}
	if err := store.DeregisterAll(ctx, now); err != nil {
		return err
	}

	keys := local.NewKeys(store.DB(), sec.pepper)
	if err := keys.RevokeNamed(ctx, autoKeyName); err != nil {
		return err
	}

	blob, err := local.NewFileBlob(filepath.Join(o.dataDir, "blobs"), sec.blob, "/blob", baseURL)
	if err != nil {
		return err
	}

	features, unknown := emi.ParseExperimental(o.experimental)
	if len(unknown) > 0 {
		return fmt.Errorf("unknown experimental feature %q (known: %s)", strings.Join(unknown, ","), strings.Join(emi.KnownFeatures, ", "))
	}
	if features.FullWave {
		logger.Warn("experimental full-wave solving is enabled: it runs, but nothing it produces has been verified on a real board")
	} else if features.SmallPartSolve {
		logger.Warn("experimental small-part solving is enabled: see docs/verification/small-part-solve.md for what it was checked against")
	}

	blob.MaxBytes = o.maxUploadBytes
	svc, err := emi.New(emi.Deps{
		Store:          store,
		Keys:           keys,
		Blob:           blob,
		TokenSecret:    sec.token,
		Logger:         logger,
		Features:       features,
		MaxUploadBytes: o.maxUploadBytes,
	})
	if err != nil {
		return err
	}
	svc.StartSweeper(ctx, time.Minute)

	worker := newWorkerSupervisor(logger, workerConfig{
		mode:        o.workerMode,
		image:       o.workerImage,
		dialURL:     o.workerURL,
		port:        port,
		dataDir:     o.dataDir,
		concurrency: o.concurrency,
		issueKey: func(ctx context.Context) (string, error) {
			return keys.Issue(ctx, localUser.OrganizationID, autoKeyName)
		},
		revokeKeys: func(ctx context.Context) error { return keys.RevokeNamed(ctx, autoKeyName) },
	})

	mux := http.NewServeMux()
	svc.Mount(mux, "/api", func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			next(w, r.WithContext(emi.WithUser(r.Context(), localUser)))
		}
	})
	mux.Handle("/blob/", blob.Handler())
	mux.HandleFunc("GET /api/health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{"status": "ok", "workers_online": svc.Hub().OnlineCount()})
	})
	mux.HandleFunc("GET /api/local/status", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{
			"version":  version,
			"data_dir": o.dataDir,
			"worker":   worker.Status(),
			// Which shell is showing this page, so it can offer to put that shell away. Empty
			// in a browser tab, where there is no window of ours to move.
			"shell": o.shell,
		})
	})
	// Put the desktop app's window away and leave it serving from the tray, which is what the
	// KiCad plugin needs from it. Refused when no window is ours to move.
	mux.HandleFunc("POST /api/local/window/background", func(w http.ResponseWriter, r *http.Request) {
		if o.shell != shellDesktop {
			http.Error(w, "this server has no window to put away", http.StatusNotFound)
			return
		}
		fmt.Println(HideWindowLine)
		writeJSON(w, map[string]any{"ok": true})
	})
	mux.HandleFunc("GET /api/local/worker/logs", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		_, _ = io.WriteString(w, worker.Logs(r.Context()))
	})
	mux.HandleFunc("POST /api/local/worker/restart", func(w http.ResponseWriter, r *http.Request) {
		worker.Restart()
		writeJSON(w, map[string]any{"ok": true})
	})

	web, err := webHandler(o.webDir)
	if err != nil {
		return err
	}
	mux.Handle("/", web)

	srv := &http.Server{
		Handler:           guard(mux),
		ReadHeaderTimeout: 10 * time.Second,
		// No WriteTimeout: the worker WebSocket is long-lived, and board uploads can be large.
		IdleTimeout: 120 * time.Second,
	}
	serveErr := make(chan error, 1)
	go func() { serveErr <- srv.Serve(ln) }()

	// Published once the server answers, so a program that finds the file and asks it a
	// question gets an answer rather than a refused connection.
	forgetEndpoint, err := writeEndpoint(o.endpointFile, baseURL, o.dataDir)
	if err != nil {
		logger.Warn("could not publish where this app is listening; the KiCad plugin will not find it",
			"file", o.endpointFile, "err", err)
		forgetEndpoint = func() {}
	}
	defer forgetEndpoint()

	logger.Info("emi-local ready", "url", baseURL, "data_dir", o.dataDir, "version", version)
	// On stdout, alone on its line, for the desktop app to read.
	fmt.Println(ReadyPrefix + baseURL)

	go worker.Run(ctx)

	if o.open {
		openBrowser(baseURL)
	}

	select {
	case <-ctx.Done():
	case err := <-serveErr:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			logger.Error("server stopped", "err", err)
		}
	}

	logger.Info("shutting down")
	// The worker first: it is a container that would otherwise keep running after we exit.
	stopCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	worker.Stop(stopCtx)
	_ = srv.Shutdown(stopCtx)
	return nil
}

// listen binds the address, refusing anything but loopback. The API serves one fixed identity
// with no credentials, so binding it to a LAN interface would hand every board to the network.
//
// When the default port is taken it falls back to any free port rather than failing: the
// user asked for the app, not for port 7465.
func listen(addr string, logger *slog.Logger) (net.Listener, error) {
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, fmt.Errorf("listen address %q: %w", addr, err)
	}
	if ip := net.ParseIP(host); host != "localhost" && (ip == nil || !ip.IsLoopback()) {
		return nil, fmt.Errorf("listen address %q is not a loopback address; the local app has no sign-in, so it only listens on this computer", addr)
	}
	ln, err := net.Listen("tcp", addr)
	if err == nil {
		return ln, nil
	}
	if addr != defaultAddr {
		return nil, err
	}
	logger.Warn("default port is in use, choosing another", "addr", addr, "err", err)
	return net.Listen("tcp", "127.0.0.1:0")
}

// withLifeline cancels ctx when stdin reaches EOF, which is what happens when the parent
// process exits for any reason -- including being killed, when it gets no chance to tell us.
func withLifeline(parent context.Context, stdin io.Reader, logger *slog.Logger) context.Context {
	ctx, cancel := context.WithCancel(parent)
	go func() {
		buf := make([]byte, 256)
		for {
			n, err := stdin.Read(buf)
			if n > 0 && strings.Contains(string(buf[:n]), "quit") {
				logger.Info("asked to quit by the parent process")
				cancel()
				return
			}
			if err != nil {
				logger.Info("parent process went away (stdin closed)")
				cancel()
				return
			}
		}
	}()
	return ctx
}

// printKey issues a long-lived key for a worker the user runs themselves.
func printKey(dataDir string) error {
	ctx := context.Background()
	if err := os.MkdirAll(dataDir, 0o700); err != nil {
		return err
	}
	sec, err := loadSecrets(filepath.Join(dataDir, "secret.key"))
	if err != nil {
		return err
	}
	store, err := local.OpenSQLite(ctx, filepath.Join(dataDir, "emi.db"))
	if err != nil {
		return err
	}
	defer store.Close()
	raw, err := local.NewKeys(store.DB(), sec.pepper).Issue(ctx, localUser.OrganizationID, "external worker")
	if err != nil {
		return err
	}
	fmt.Println(raw)
	return nil
}

func defaultDataDir() string {
	if d, err := os.UserConfigDir(); err == nil {
		return filepath.Join(d, "emi-analyzer")
	}
	return filepath.Join(os.TempDir(), "emi-analyzer")
}

// defaultImage names the worker this build belongs with.
//
// A release runs the image tagged with its own version, so an app and its worker are always
// from the same commit. Anything else -- a build from source, which is stamped "dev" -- runs
// the "dev" tag, which the worker-image workflow pushes from main. Not "latest": that is the
// newest *release*, and pairing today's source with the last release's worker is the kind of
// mismatch this scheme exists to prevent.
func defaultImage() string {
	tag := strings.TrimPrefix(version, "v")
	if tag == "" || tag == "dev" {
		tag = "dev"
	}
	return "ghcr.io/embeddedci-com/emi-worker:" + tag
}

// envInt64 reads a whole number from the environment, falling back to def when it is unset or
// not a number.
func envInt64(key string, def int64) int64 {
	if n, err := strconv.ParseInt(strings.TrimSpace(os.Getenv(key)), 10, 64); err == nil && n > 0 {
		return n
	}
	return def
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	_ = json.NewEncoder(w).Encode(v)
}

func openBrowser(url string) {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", url)
	case "windows":
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	_ = cmd.Start()
}
