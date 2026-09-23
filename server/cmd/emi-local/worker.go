package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

// The worker supervisor.
//
// The worker is the published image (worker/Dockerfile), unchanged: it is told a server URL
// and a key, and dials in exactly as it would to a hosted server. This file only decides
// *how* to start it on this machine and keeps it running while the app is open:
//
//	find docker  ->  wait for the engine  ->  pull the image if absent  ->  run  ->  watch
//
// and removes the container again on the way out. Every failure along that path is a state
// with a sentence for the user rather than a log line, because "nothing happens when I upload
// a board" is otherwise all they would see.

type workerState string

const (
	stateDisabled         workerState = "disabled"
	stateDockerMissing    workerState = "docker_missing"
	stateDockerNotRunning workerState = "docker_not_running"
	statePulling          workerState = "pulling"
	stateStarting         workerState = "starting"
	stateRunning          workerState = "running"
	stateError            workerState = "error"
	stateStopped          workerState = "stopped"
)

type workerConfig struct {
	mode        string // "docker" or "none"
	image       string
	dialURL     string // override for the URL the container dials
	port        int
	dataDir     string
	concurrency int
	issueKey    func(context.Context) (string, error)
	revokeKeys  func(context.Context) error
}

type workerStatus struct {
	Mode    string      `json:"mode"`
	State   workerState `json:"state"`
	Message string      `json:"message,omitempty"`
	// Detail is what the command actually said. The UI keeps it out of the banner and shows
	// it with the worker log, because a registry error reads as gibberish to somebody who
	// only wants to analyse a board.
	Detail    string `json:"detail,omitempty"`
	Image     string `json:"image,omitempty"`
	Container string `json:"container,omitempty"`
}

type workerSupervisor struct {
	log  *slog.Logger
	cfg  workerConfig
	name string // container name, unique per data folder

	mu      sync.Mutex
	status  workerStatus
	docker  string
	restart chan struct{}
}

func newWorkerSupervisor(logger *slog.Logger, cfg workerConfig) *workerSupervisor {
	sum := sha256.Sum256([]byte(cfg.dataDir))
	s := &workerSupervisor{
		log: logger,
		cfg: cfg,
		// Two copies of the app with different data folders must not remove each other's
		// worker, so the name is derived from the folder.
		name:    "emi-local-worker-" + hex.EncodeToString(sum[:4]),
		restart: make(chan struct{}, 1),
	}
	s.status = workerStatus{Mode: cfg.mode, Image: cfg.image, Container: s.name}
	if cfg.mode == "none" {
		s.status = workerStatus{
			Mode:  "none",
			State: stateDisabled,
			Message: fmt.Sprintf("This app is not starting a worker. Connect one yourself: "+
				"EMBEDDEDCI_URL=http://127.0.0.1:%d, with a key from `emi-local -issue-key`.", cfg.port),
		}
	}
	return s
}

func (s *workerSupervisor) Status() workerStatus {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.status
}

func (s *workerSupervisor) set(state workerState, msg string) {
	s.setDetailed(state, msg, "")
}

// setDetailed records a state with a sentence for the user and, separately, whatever the
// underlying command printed.
func (s *workerSupervisor) setDetailed(state workerState, msg, detail string) {
	s.mu.Lock()
	changed := s.status.State != state || s.status.Message != msg
	s.status.State, s.status.Message, s.status.Detail = state, msg, detail
	s.mu.Unlock()
	if changed {
		s.log.Info("worker", "state", state, "message", msg, "detail", detail)
	}
}

// explainDocker turns what the docker CLI printed into something worth showing a user.
//
// The raw text is a registry error with a URL-encoded token request in it; the useful part is
// which of a handful of things went wrong. Anything unrecognised keeps the original text,
// because a wrong guess is worse than an ugly sentence.
func explainDocker(image, out string, err error) (msg, detail string) {
	detail = strings.TrimSpace(out)
	if detail == "" && err != nil {
		detail = err.Error()
	}
	low := strings.ToLower(detail)
	switch {
	case strings.Contains(low, "permission denied") && strings.Contains(low, "docker.sock"):
		return "Docker refused the connection: this user is not allowed to use it. On Linux, " +
			"add yourself to the `docker` group, then log out and back in.", detail
	case strings.Contains(low, "401") || strings.Contains(low, "unauthorized") ||
		strings.Contains(low, "denied") || strings.Contains(low, "authoriz"):
		return "The worker image " + image + " could not be downloaded: the registry refused " +
			"access. If this is a private image, sign in with `docker login ghcr.io` and click " +
			"Restart worker.", detail
	case strings.Contains(low, "manifest unknown") || strings.Contains(low, "not found"):
		return "The worker image " + image + " does not exist. Check the tag, or install the " +
			"app version that matches it.", detail
	case strings.Contains(low, "no space left"):
		return "There is not enough disk space to download the worker image (about 1 GB is " +
			"needed). Free some space and click Restart worker.", detail
	case strings.Contains(low, "timeout") || strings.Contains(low, "temporary failure") ||
		strings.Contains(low, "dial tcp") || strings.Contains(low, "no such host"):
		return "The worker image could not be downloaded: the registry could not be reached. " +
			"Check your internet connection or proxy settings, then click Restart worker.", detail
	}
	return "The worker could not be started: " + firstLine(detail, err), detail
}

// Restart asks the supervision loop to recreate the container now.
func (s *workerSupervisor) Restart() {
	select {
	case s.restart <- struct{}{}:
	default:
	}
}

// Run supervises the worker until ctx ends.
func (s *workerSupervisor) Run(ctx context.Context) {
	if s.cfg.mode != "docker" {
		return
	}
	backoff := 5 * time.Second
	for ctx.Err() == nil {
		ok := s.startOnce(ctx)
		if ok {
			backoff = 5 * time.Second
			s.watch(ctx)
		} else {
			// Waiting on the user (Docker not installed or not started) or on a transient
			// failure; retry either way, a little less eagerly each time.
			if backoff < 30*time.Second {
				backoff += 5 * time.Second
			}
		}
		select {
		case <-ctx.Done():
		case <-s.restart:
		case <-time.After(backoff):
		}
	}
}

// startOnce takes the worker from nothing to a running container. It reports false when it
// could not, with the reason recorded in the status.
func (s *workerSupervisor) startOnce(ctx context.Context) bool {
	docker, err := findDocker()
	if err != nil {
		s.set(stateDockerMissing, "Docker was not found on this computer. The worker that analyses boards runs in a Docker container.")
		return false
	}
	s.mu.Lock()
	s.docker = docker
	s.mu.Unlock()

	engine, err := s.dockerOut(ctx, 15*time.Second, "info", "--format", "{{.OperatingSystem}}")
	if err != nil {
		s.set(stateDockerNotRunning, "Docker is installed but its engine is not running. Start Docker Desktop (or the Docker service) and the worker starts on its own.")
		return false
	}

	if _, err := s.dockerOut(ctx, 15*time.Second, "image", "inspect", "--format", "{{.Id}}", s.cfg.image); err != nil {
		s.set(statePulling, "Downloading the worker image ("+s.cfg.image+"). This happens once per version and is about 1 GB, so it can take a few minutes.")
		if out, err := s.pull(ctx); err != nil {
			msg, detail := explainDocker(s.cfg.image, out, err)
			s.setDetailed(stateError, msg, detail)
			return false
		}
	}

	s.set(stateStarting, "Starting the worker container.")
	// The previous container's key, if this is a restart, goes with it.
	_ = s.cfg.revokeKeys(ctx)
	key, err := s.cfg.issueKey(ctx)
	if err != nil {
		s.set(stateError, "Could not issue a worker key: "+err.Error())
		return false
	}

	// A container left behind by a crashed app, or by the previous start of this loop.
	_, _ = s.dockerOut(ctx, 30*time.Second, "rm", "-f", s.name)

	netArgs, url := s.network(engine)
	args := runArgs(s.name, netArgs, url, s.cfg.concurrency, s.cfg.image)
	// The key reaches the container through the docker CLI's own environment. On its
	// command line it would be readable by every user of this computer through ps.
	if out, err := s.dockerOutEnv(ctx, 60*time.Second, []string{workerKeyEnv + "=" + key}, args...); err != nil {
		msg, detail := explainDocker(s.cfg.image, out, err)
		s.setDetailed(stateError, msg, detail)
		return false
	}
	s.set(stateRunning, fmt.Sprintf("The worker container is running and connects to this app at %s.", url))
	return true
}

// workerKeyEnv is the variable the worker reads its key from.
const workerKeyEnv = "EMBEDDEDCI_API_KEY"

// runArgs is the docker run command line. It names the key variable without a value, which
// makes docker copy it from its own environment: nothing secret is on the command line.
func runArgs(name string, netArgs []string, url string, concurrency int, image string) []string {
	args := []string{"run", "-d", "--name", name,
		"--label", "com.embeddedci.emi-local=1",
		"--stop-timeout", "5",
	}
	args = append(args, netArgs...)
	return append(args,
		"-e", "EMBEDDEDCI_URL="+url,
		"-e", workerKeyEnv,
		"-e", "EMI_WORKER_NAME=local",
		"-e", fmt.Sprintf("EMI_MAX_CONCURRENT=%d", max(1, concurrency)),
		image,
	)
}

// network chooses how the container reaches this server, which listens on loopback only.
//
// Docker Desktop (macOS, Windows, and Linux when installed) forwards host.docker.internal to
// the host's loopback, so a bridge network works. A native Linux engine does not: there the
// container shares the host's network namespace instead, and 127.0.0.1 is this server.
func (s *workerSupervisor) network(engine string) ([]string, string) {
	if s.cfg.dialURL != "" {
		return []string{"--add-host", "host.docker.internal:host-gateway"}, s.cfg.dialURL
	}
	desktop := strings.Contains(strings.ToLower(engine), "docker desktop")
	if runtime.GOOS == "linux" && !desktop {
		return []string{"--network", "host"}, fmt.Sprintf("http://127.0.0.1:%d", s.cfg.port)
	}
	return []string{"--add-host", "host.docker.internal:host-gateway"},
		fmt.Sprintf("http://host.docker.internal:%d", s.cfg.port)
}

// watch returns when the container stops or a restart is requested.
func (s *workerSupervisor) watch(ctx context.Context) {
	t := time.NewTicker(5 * time.Second)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-s.restart:
			// Put it back so Run's select sees it and starts immediately.
			s.Restart()
			return
		case <-t.C:
			out, err := s.dockerOut(ctx, 15*time.Second, "inspect", "--format", "{{.State.Running}} {{.State.ExitCode}}", s.name)
			if ctx.Err() != nil {
				return
			}
			if err != nil {
				s.setDetailed(stateError, "The worker container is gone. Click Restart worker to start it again.",
					firstLine(out, err))
				return
			}
			if !strings.HasPrefix(strings.TrimSpace(out), "true") {
				tail, _ := s.dockerOut(ctx, 15*time.Second, "logs", "--tail", "20", s.name)
				s.setDetailed(stateError,
					"The worker stopped unexpectedly. Click Restart worker to start it again; "+
						"the log below says why it exited.", strings.TrimSpace(tail))
				return
			}
		}
	}
}

func (s *workerSupervisor) pull(ctx context.Context) (string, error) {
	cmd := s.command(ctx, "pull", s.cfg.image)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return "", err
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return "", err
	}
	sc := bufio.NewScanner(stdout)
	for sc.Scan() {
		if line := strings.TrimSpace(sc.Text()); line != "" {
			s.set(statePulling, "Downloading the worker image (about 1 GB, once): "+line)
		}
	}
	if err := cmd.Wait(); err != nil {
		return stderr.String(), err
	}
	return "", nil
}

// Logs returns the container's recent output.
func (s *workerSupervisor) Logs(ctx context.Context) string {
	if s.cfg.mode != "docker" {
		return ""
	}
	out, err := s.dockerOut(ctx, 15*time.Second, "logs", "--tail", "300", s.name)
	if err != nil {
		return "No worker container: " + firstLine(out, err)
	}
	return out
}

// Stop removes the container and revokes its key. The worker keeps nothing, so there is
// nothing to preserve by stopping it gently.
func (s *workerSupervisor) Stop(ctx context.Context) {
	if s.cfg.mode != "docker" {
		return
	}
	s.mu.Lock()
	docker := s.docker
	s.mu.Unlock()
	if docker != "" {
		if _, err := s.dockerOut(ctx, 15*time.Second, "rm", "-f", s.name); err == nil {
			s.log.Info("worker container removed", "container", s.name)
		}
	}
	_ = s.cfg.revokeKeys(ctx)
	s.set(stateStopped, "The app is shutting down.")
}

func (s *workerSupervisor) command(ctx context.Context, args ...string) *exec.Cmd {
	s.mu.Lock()
	docker := s.docker
	s.mu.Unlock()
	cmd := exec.CommandContext(ctx, docker, args...)
	// An app started from the Finder or a desktop launcher inherits a bare PATH, and the
	// docker CLI shells out to credential helpers that live next to it.
	cmd.Env = append(os.Environ(), "PATH="+filepath.Dir(docker)+string(os.PathListSeparator)+os.Getenv("PATH"))
	hideWindow(cmd)
	return cmd
}

func (s *workerSupervisor) dockerOut(ctx context.Context, timeout time.Duration, args ...string) (string, error) {
	return s.dockerOutEnv(ctx, timeout, nil, args...)
}

// dockerOutEnv runs docker with extra environment variables, for values that must not appear
// on a command line.
func (s *workerSupervisor) dockerOutEnv(ctx context.Context, timeout time.Duration, env []string, args ...string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := s.command(ctx, args...)
	cmd.Env = append(cmd.Env, env...)
	out, err := cmd.CombinedOutput()
	return string(out), err
}

// findDocker looks on PATH and then where the common installers put the CLI, because a GUI
// app does not see the PATH a terminal does.
func findDocker() (string, error) {
	if p, err := exec.LookPath("docker"); err == nil {
		return p, nil
	}
	home, _ := os.UserHomeDir()
	var candidates []string
	switch runtime.GOOS {
	case "darwin":
		candidates = []string{
			"/usr/local/bin/docker",
			"/opt/homebrew/bin/docker",
			filepath.Join(home, ".docker", "bin", "docker"),
			"/Applications/Docker.app/Contents/Resources/bin/docker",
			filepath.Join(home, ".orbstack", "bin", "docker"),
			"/Applications/OrbStack.app/Contents/MacOS/xbin/docker",
		}
	case "windows":
		candidates = []string{
			filepath.Join(os.Getenv("ProgramFiles"), "Docker", "Docker", "resources", "bin", "docker.exe"),
			filepath.Join(os.Getenv("ProgramData"), "DockerDesktop", "version-bin", "docker.exe"),
		}
	default:
		candidates = []string{"/usr/bin/docker", "/usr/local/bin/docker", "/snap/bin/docker",
			filepath.Join(home, ".docker", "bin", "docker")}
	}
	for _, c := range candidates {
		if st, err := os.Stat(c); err == nil && !st.IsDir() {
			return c, nil
		}
	}
	return "", exec.ErrNotFound
}

func firstLine(out string, err error) string {
	for _, l := range strings.Split(out, "\n") {
		if l = strings.TrimSpace(l); l != "" {
			return l
		}
	}
	if err != nil {
		return err.Error()
	}
	return ""
}
