// Command emi-local runs the whole EMI Analyzer on one computer.
//
// It is the same control plane embeddedci-server mounts -- the emi package, via emi.Mount --
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
	"crypto/rand"
	"encoding/hex"
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
		experiment  = flag.String("experimental", envOr("EMI_EXPERIMENTAL", ""), `comma-separated experimental features to enable: "full-wave" (openEMS solves, far field, cable emissions, compliance; unverified on real boards)`)
		issueKey    = flag.Bool("issue-key", false, "print a key for a worker you run yourself (with -worker=none), and exit")
		showVersion = flag.Bool("version", false, "print the version and exit")
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
	secret, err := loadSecret(filepath.Join(o.dataDir, "secret.key"))
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

	keys := local.NewKeys(store.DB(), secret)
	if err := keys.RevokeNamed(ctx, autoKeyName); err != nil {
		return err
	}

	blob, err := local.NewFileBlob(filepath.Join(o.dataDir, "blobs"), secret, "/blob", baseURL)
	if err != nil {
		return err
	}

	features, unknown := emi.ParseExperimental(o.experimental)
	if len(unknown) > 0 {
		return fmt.Errorf("unknown experimental feature %q (known: %s)", strings.Join(unknown, ","), emi.FeatureFullWave)
	}
	if features.FullWave {
		logger.Warn("experimental full-wave solving is enabled: it runs, but nothing it produces has been verified on a real board")
	}

	svc, err := emi.New(emi.Deps{
		Store:       store,
		Keys:        keys,
		Blob:        blob,
		TokenSecret: secret,
		Logger:      logger,
		Features:    features,
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
		})
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
	secret, err := loadSecret(filepath.Join(dataDir, "secret.key"))
	if err != nil {
		return err
	}
	store, err := local.OpenSQLite(ctx, filepath.Join(dataDir, "emi.db"))
	if err != nil {
		return err
	}
	defer store.Close()
	raw, err := local.NewKeys(store.DB(), secret).Issue(ctx, localUser.OrganizationID, "external worker")
	if err != nil {
		return err
	}
	fmt.Println(raw)
	return nil
}

// loadSecret returns the per-installation secret that signs run tokens, blob URLs and worker
// key hashes, creating it on first start. It never leaves the data folder.
func loadSecret(path string) ([]byte, error) {
	if b, err := os.ReadFile(path); err == nil {
		if s, err := hex.DecodeString(strings.TrimSpace(string(b))); err == nil && len(s) >= 32 {
			return s, nil
		}
	}
	s := make([]byte, 32)
	if _, err := rand.Read(s); err != nil {
		return nil, err
	}
	if err := os.WriteFile(path, []byte(hex.EncodeToString(s)+"\n"), 0o600); err != nil {
		return nil, fmt.Errorf("write %s: %w", path, err)
	}
	return s, nil
}

func defaultDataDir() string {
	if d, err := os.UserConfigDir(); err == nil {
		return filepath.Join(d, "emi-analyzer")
	}
	return filepath.Join(os.TempDir(), "emi-analyzer")
}

func defaultImage() string {
	tag := strings.TrimPrefix(version, "v")
	if tag == "" || tag == "dev" {
		tag = "latest"
	}
	return "ghcr.io/embeddedci-com/emi-worker:" + tag
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
