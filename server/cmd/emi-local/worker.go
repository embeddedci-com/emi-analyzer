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
	Mode      string      `json:"mode"`
	State     workerState `json:"state"`
	Message   string      `json:"message,omitempty"`
	Image     string      `json:"image,omitempty"`
	Container string      `json:"container,omitempty"`
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
	s.mu.Lock()
	changed := s.status.State != state
	s.status.State, s.status.Message = state, msg
	s.mu.Unlock()
	if changed {
		s.log.Info("worker", "state", state, "message", msg)
	}
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
		s.set(statePulling, "Downloading "+s.cfg.image+". This happens once and is about 1 GB, so it can take a few minutes.")
		if err := s.pull(ctx); err != nil {
			s.set(stateError, "Could not download the worker image: "+err.Error())
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

	args := []string{"run", "-d", "--name", s.name,
		"--label", "com.embeddedci.emi-local=1",
		"--stop-timeout", "5",
	}
	netArgs, url := s.network(engine)
	args = append(args, netArgs...)
	args = append(args,
		"-e", "EMBEDDEDCI_URL="+url,
		"-e", "EMBEDDEDCI_API_KEY="+key,
		"-e", "EMI_WORKER_NAME=local",
		"-e", fmt.Sprintf("EMI_MAX_CONCURRENT=%d", max(1, s.cfg.concurrency)),
		s.cfg.image,
	)
	if out, err := s.dockerOut(ctx, 60*time.Second, args...); err != nil {
		s.set(stateError, "Could not start the worker container: "+firstLine(out, err))
		return false
	}
	s.set(stateRunning, fmt.Sprintf("The worker container is running and connects to this app at %s.", url))
	return true
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
				s.set(stateError, "The worker container disappeared: "+firstLine(out, err))
				return
			}
			if !strings.HasPrefix(strings.TrimSpace(out), "true") {
				tail, _ := s.dockerOut(ctx, 15*time.Second, "logs", "--tail", "5", s.name)
				s.set(stateError, "The worker container exited ("+strings.TrimSpace(out)+"). Last output: "+strings.TrimSpace(tail))
				return
			}
		}
	}
}

func (s *workerSupervisor) pull(ctx context.Context) error {
	cmd := s.command(ctx, "pull", s.cfg.image)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return err
	}
	sc := bufio.NewScanner(stdout)
	for sc.Scan() {
		if line := strings.TrimSpace(sc.Text()); line != "" {
			s.set(statePulling, "Downloading "+s.cfg.image+" (about 1 GB, once): "+line)
		}
	}
	if err := cmd.Wait(); err != nil {
		return fmt.Errorf("%s", firstLine(stderr.String(), err))
	}
	return nil
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
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	out, err := s.command(ctx, args...).CombinedOutput()
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
