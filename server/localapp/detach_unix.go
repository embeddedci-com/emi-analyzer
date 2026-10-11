//go:build !windows

package localapp

import (
	"os/exec"
	"syscall"
)

// detach puts the child in a session of its own, so the signal that ends ours does not end it.
func detach(c *exec.Cmd) { c.SysProcAttr = &syscall.SysProcAttr{Setsid: true} }
