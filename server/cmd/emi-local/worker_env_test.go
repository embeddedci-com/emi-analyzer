package main

import (
	"context"
	"log/slog"
	"runtime"
	"strings"
	"testing"
	"time"
)

// The worker key used to be on the docker command line, where any user of the computer can
// read it with ps. The command line now names the variable only.
func TestRunArgsKeepTheKeyOffTheCommandLine(t *testing.T) {
	args := runArgs("emi-worker", []string{"--network", "host"}, "http://127.0.0.1:1", 2, "img")
	joined := strings.Join(args, " ")
	if strings.Contains(joined, workerKeyEnv+"=") {
		t.Fatalf("key value on the command line: %s", joined)
	}
	found := false
	for i := range args[:len(args)-1] {
		if args[i] == "-e" && args[i+1] == workerKeyEnv {
			found = true
		}
	}
	if !found {
		t.Fatalf("run args do not pass %s through: %s", workerKeyEnv, joined)
	}
	if args[len(args)-1] != "img" {
		t.Fatalf("image is not last: %s", joined)
	}
}

// And the value reaches docker through its environment instead.
func TestDockerOutEnvPassesTheEnvironment(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("uses /bin/sh as a stand-in for docker")
	}
	s := newWorkerSupervisor(slog.Default(), workerConfig{})
	s.docker = "/bin/sh"
	out, err := s.dockerOutEnv(context.Background(), 5*time.Second,
		[]string{workerKeyEnv + "=secret-value"}, "-c", "printf %s \"$"+workerKeyEnv+"\"")
	if err != nil {
		t.Fatal(err)
	}
	if out != "secret-value" {
		t.Fatalf("docker saw %q, want the key", out)
	}
}
