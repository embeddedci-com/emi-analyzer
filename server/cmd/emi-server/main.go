// Command emi-server runs the EMI control plane, and optionally the webapp, on its own.
//
// It is the development harness: one binary that speaks the whole worker protocol against
// Postgres and S3-compatible storage, so that storage path can be exercised end to end
// (`make up`, `make smoke`) without a host application around it.
//
// It has no accounts. Every request is a signed-out visitor with a private space of their own
// (see userauth.go); sign-in, sessions and API keys are the mounting host's business.
//
// It listens on 127.0.0.1 unless told otherwise, and will not start with missing or published
// secrets unless -dev (or EMI_DEV=1) says this is the development stack; see config.go.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	var (
		addr     = flag.String("addr", env("ADDR", "127.0.0.1:8090"), "listen address")
		dev      = flag.Bool("dev", envBool(os.Getenv, "EMI_DEV"), "development stack: allow the built-in development secrets and MinIO credentials")
		dsn      = flag.String("dsn", env("DATABASE_URL", "postgres://emi:emi@localhost:5432/emi?sslmode=disable"), "postgres DSN")
		issueKey = flag.Bool("issue-key", false, "issue an EMI worker key and exit")
		keyOrg   = flag.String("org", "", "restrict an issued key to one organisation (default: serves all of them)")
		keyName  = flag.String("name", "worker key", "label stored with an issued key")
		webapp   = flag.String("webapp", env("EMI_WEBAPP_DIR", ""), "directory holding the built webapp; empty serves the API only")
		webBase  = flag.String("webapp-base", env("EMI_WEBAPP_BASE", "/tools/emi"), "path the webapp is served under")
		health   = flag.Bool("health-check", false, "probe the local server and exit 0 if healthy")
		maxMB    = flag.Int64("max-upload-mb", envInt64(os.Getenv, "EMI_MAX_UPLOAD_MB", emi.DefaultMaxUploadBytes>>20), "largest board file or result accepted, in MB")
	)
	flag.Parse()

	// The deployed image is distroless: no shell, no wget, no curl. So the binary is its
	// own health probe, which is also the honest one -- it exercises the same handler a
	// caller would rather than a substitute for it.
	if *health {
		os.Exit(probeHealth(*addr))
	}

	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	// Before anything else, so a misconfigured server fails at once and says why.
	sec, err := loadSecrets(*dev, os.Getenv)
	if err != nil {
		logger.Error("refusing to start", "err", err)
		os.Exit(1)
	}
	if *dev {
		logger.Warn("development mode: unset secrets fall back to public development values")
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	store, err := connectWithRetry(ctx, *dsn, 30*time.Second)
	if err != nil {
		logger.Error("database unavailable", "err", err)
		os.Exit(1)
	}
	defer store.Close()

	pepper := []byte(sec.pepper)
	keys := emi.NewDevKeyVerifier(store.Pool(), pepper)

	// One-shot mode for `make key`.
	//
	// No -org means a shared worker: one that takes runs from every organisation, which is
	// what a hosted service's own workers are. -org is for the other case -- a customer
	// running a worker on their own hardware, which must only ever see their own boards.
	if *issueKey {
		raw, err := keys.IssueKey(ctx, *keyOrg, *keyName)
		if err != nil {
			logger.Error("failed to issue key", "err", err)
			os.Exit(1)
		}
		if *keyOrg == "" {
			fmt.Fprintln(os.Stderr, "issued a shared worker key: it serves every organisation")
		} else {
			fmt.Fprintf(os.Stderr, "issued a worker key scoped to organisation %q\n", *keyOrg)
		}
		fmt.Println(raw)
		return
	}

	blob, err := emi.NewS3Blob(ctx, emi.S3Config{
		Endpoint:     env("S3_ENDPOINT", "http://localhost:9000"),
		Region:       env("S3_REGION", "us-east-1"),
		Bucket:       env("S3_BUCKET", "emi"),
		AccessKey:    sec.s3Key,
		SecretKey:    sec.s3Secret,
		UsePathStyle: true,
		// The server talks to MinIO over the compose network; workers and browsers reach
		// it from the host. Presigned URLs sign path and query, not host, so rewriting the
		// origin here is safe and is what makes the local stack usable from both sides.
		PublicURL: env("S3_PUBLIC_URL", ""),
		// Empty means emi/. Set it only to move the namespace, never to remove it: the
		// bucket is shared with the rest of the app.
		KeyPrefix: env("S3_KEY_PREFIX", ""),
	})
	if err != nil {
		logger.Error("object storage unavailable", "err", err)
		os.Exit(1)
	}

	features, unknown := emi.ParseExperimental(env("EMI_EXPERIMENTAL", ""))
	if len(unknown) > 0 {
		logger.Error("unknown experimental feature in EMI_EXPERIMENTAL", "names", unknown)
		os.Exit(1)
	}

	svc, err := emi.New(emi.Deps{
		Features:    features,
		Store:       store,
		Keys:        keys,
		Blob:        blob,
		TokenSecret: []byte(sec.tokenSecret),
		Logger:      logger,
		RunTimeout:  24 * time.Hour,

		MaxUploadBytes: *maxMB << 20,
	})
	if err != nil {
		logger.Error("failed to build service", "err", err)
		os.Exit(1)
	}
	svc.StartSweeper(ctx, time.Minute)

	mux := http.NewServeMux()

	svc.Mount(mux, "/api", visitorAuth(emi.Visitors{
		Secret: []byte(sec.tokenSecret),
	}))

	if *webapp != "" {
		spa, err := spaHandler(*webapp, *webBase)
		if err != nil {
			logger.Error("webapp directory is not usable", "dir", *webapp, "err", err)
			os.Exit(1)
		}
		base := "/" + strings.Trim(*webBase, "/")
		// Both spellings, so the canonical URL people are given -- /tools/emi -- serves the
		// app directly instead of redirecting to /tools/emi/ first.
		mux.Handle(base, spa)
		mux.Handle(base+"/", spa)
		logger.Info("serving webapp", "dir", *webapp, "base", base)
	}

	mux.HandleFunc("GET /api/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"status":         "ok",
			"workers_online": svc.Hub().OnlineCount(),
		})
	})

	srv := &http.Server{
		Addr:              *addr,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
		// No WriteTimeout: the worker WebSocket is a long-lived connection and a write
		// deadline would kill it mid-solve.
		IdleTimeout: 120 * time.Second,
	}

	go func() {
		logger.Info("emi-server listening", "addr", *addr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Error("listen failed", "err", err)
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	logger.Info("shutting down")
	shutCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = srv.Shutdown(shutCtx)
}

func connectWithRetry(ctx context.Context, dsn string, limit time.Duration) (*emi.PGStore, error) {
	deadline := time.Now().Add(limit)
	var lastErr error
	for time.Now().Before(deadline) {
		store, err := emi.NewPGStore(ctx, dsn)
		if err == nil {
			return store, nil
		}
		lastErr = err
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(time.Second):
		}
	}
	return nil, lastErr
}
