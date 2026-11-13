//go:build !windows

package main

import (
	"fmt"
	"os"
	"os/exec"
	"runtime"
)

func openBrowser(address string) error {
	command := "xdg-open"
	if runtime.GOOS == "darwin" {
		command = "open"
	}
	process := exec.Command(command, address)
	if err := process.Start(); err != nil {
		return err
	}
	go process.Wait()
	return nil
}

func showError(message string) {
	fmt.Fprintln(os.Stderr, message)
}
