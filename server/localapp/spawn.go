package localapp

import (
	"os"
	"os/exec"
	"path/filepath"
)

// spawn starts the app and lets go of it: it must outlive this process, which an MCP client
// starts and stops with each session.
//
// `open` (macOS) is the exception. It hands the app to launchd and exits at once, non-zero when
// there is no such app, so it is waited for and its exit status is the answer.
func spawn(cmd []string) error {
	c := exec.Command(cmd[0], cmd[1:]...)
	if cmd[0] == "open" {
		return c.Run()
	}
	// Whatever the app prints goes to a log in the user cache folder, so a server
	// that fails to come up has left a reason somewhere a person can find it.
	if f, err := launchLog(); err == nil {
		defer f.Close()
		c.Stdout, c.Stderr = f, f
	}
	detach(c)
	if err := c.Start(); err != nil {
		return err
	}
	go func() { _ = c.Wait() }() // reap it if it exits while we are still here
	return nil
}

// LaunchLogFile is where an app this package started writes its output.
func LaunchLogFile() string {
	dir, err := os.UserCacheDir()
	if err != nil {
		dir = os.TempDir()
	}
	return filepath.Join(dir, "emi-analyzer", "launch.log")
}

func launchLog() (*os.File, error) {
	p := LaunchLogFile()
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		return nil, err
	}
	return os.OpenFile(p, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
}
