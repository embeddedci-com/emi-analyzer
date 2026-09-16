package main

import (
	"errors"
	"strings"
	"testing"
)

// What a user is told when Docker fails. The raw text is a registry error with a URL-encoded
// token request in it, and it used to go on screen unedited.
func TestExplainDocker(t *testing.T) {
	const image = "ghcr.io/embeddedci-com/emi-worker:0.1.0"
	cases := []struct {
		name, out, want string
	}{
		{
			"private image",
			`Error response from daemon: failed to resolve reference "ghcr.io/embeddedci-com/emi-worker:0.1.0": failed to authorize: failed to fetch anonymous token: unexpected status from GET request to https://ghcr.io/token?scope=repository%3A...: 401 Unauthorized`,
			"registry refused access",
		},
		{"wrong tag", "Error response from daemon: manifest unknown", "does not exist"},
		{"disk full", "write /var/lib/docker: no space left on device", "disk space"},
		{"offline", `dial tcp: lookup ghcr.io: no such host`, "could not be reached"},
		{"socket", "permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock", "docker` group"},
	}
	for _, c := range cases {
		msg, detail := explainDocker(image, c.out, errors.New("exit status 1"))
		if !strings.Contains(msg, c.want) {
			t.Errorf("%s: message %q does not mention %q", c.name, msg, c.want)
		}
		if detail != strings.TrimSpace(c.out) {
			t.Errorf("%s: detail lost the original text", c.name)
		}
		if strings.Contains(msg, "https://ghcr.io/token") {
			t.Errorf("%s: the raw registry URL reached the message", c.name)
		}
	}

	// Anything unrecognised keeps the original text rather than being guessed at.
	msg, _ := explainDocker(image, "something new went wrong", nil)
	if !strings.Contains(msg, "something new went wrong") {
		t.Errorf("unrecognised error was swallowed: %q", msg)
	}
}
