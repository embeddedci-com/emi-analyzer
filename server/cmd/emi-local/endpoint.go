package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// The app tells other programs on this computer where to find it by leaving one small file
// behind: its URL, its pid and its version.
//
// The port is not fixed -- 7465 is only a preference, and the app falls back to any free
// port -- and the data folder is not fixed either, since the desktop app passes one of its
// own. So the file cannot live in the data folder: a program looking for a running app would
// have to know the folder to find the file that tells it the folder. It goes in one place
// that never moves, the user's config folder, and carries the data folder as information.
//
// The file is a hint, never an answer. It survives a crash, a kill and a machine that was
// switched off, so whoever reads it has to ask the URL whether anything is listening. That
// is cheap (GET /api/local/status) and it is the only check that cannot be wrong.
type endpoint struct {
	URL       string `json:"url"`
	PID       int    `json:"pid"`
	Version   string `json:"version"`
	DataDir   string `json:"data_dir"`
	StartedAt string `json:"started_at"`
}

// defaultEndpointFile is where a program looks for a running app. Keep it in step with
// kicad-plugin/emi_analyzer/desktop.py, which reads it.
func defaultEndpointFile() string {
	dir, err := os.UserConfigDir()
	if err != nil {
		dir = os.TempDir()
	}
	return filepath.Join(dir, "emi-analyzer", "endpoint.json")
}

// writeEndpoint publishes this server's URL and returns the function that takes it back.
//
// Two apps can be running -- a release and a development build -- and only one file can say
// where "the" app is. Last one to start wins, and the loser leaves the file alone when it
// exits rather than removing a pointer to a server that is still up.
func writeEndpoint(path, url, dataDir string) (func(), error) {
	if path == "" {
		return func() {}, nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, fmt.Errorf("endpoint folder: %w", err)
	}
	body, err := json.MarshalIndent(endpoint{
		URL:       url,
		PID:       os.Getpid(),
		Version:   version,
		DataDir:   dataDir,
		StartedAt: time.Now().UTC().Format(time.RFC3339),
	}, "", "  ")
	if err != nil {
		return nil, err
	}
	tmp := fmt.Sprintf("%s.%d.tmp", path, os.Getpid())
	if err := os.WriteFile(tmp, append(body, '\n'), 0o600); err != nil {
		return nil, fmt.Errorf("write %s: %w", path, err)
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return nil, fmt.Errorf("write %s: %w", path, err)
	}
	return func() { removeEndpoint(path) }, nil
}

// removeEndpoint deletes the file, but only while it still points at this process.
func removeEndpoint(path string) {
	b, err := os.ReadFile(path)
	if err != nil {
		return
	}
	var e endpoint
	if err := json.Unmarshal(b, &e); err != nil || e.PID != os.Getpid() {
		return
	}
	_ = os.Remove(path)
}
