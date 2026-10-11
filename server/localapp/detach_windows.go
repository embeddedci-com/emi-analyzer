//go:build windows

package localapp

import (
	"os/exec"
	"syscall"
)

// detach starts the child with no console window, and not killed with this one.
func detach(c *exec.Cmd) {
	const detachedProcess, newProcessGroup = 0x00000008, 0x00000200
	c.SysProcAttr = &syscall.SysProcAttr{CreationFlags: detachedProcess | newProcessGroup}
}
