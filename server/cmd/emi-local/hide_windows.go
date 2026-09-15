//go:build windows

package main

import (
	"os/exec"
	"syscall"
)

// hideWindow stops each docker call flashing a console window when the app is a GUI process.
func hideWindow(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000} // CREATE_NO_WINDOW
}
