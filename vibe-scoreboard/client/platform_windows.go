package main

import (
	"os/exec"
	"syscall"
	"unsafe"
)

func openBrowser(address string) error {
	command := exec.Command("rundll32.exe", "url.dll,FileProtocolHandler", address)
	if err := command.Start(); err != nil {
		return err
	}
	go command.Wait()
	return nil
}

func showError(message string) {
	text, _ := syscall.UTF16PtrFromString(message)
	title, _ := syscall.UTF16PtrFromString("Blue3 录分客户端")
	_, _, _ = syscall.NewLazyDLL("user32.dll").NewProc("MessageBoxW").Call(
		0, uintptr(unsafe.Pointer(text)), uintptr(unsafe.Pointer(title)), 0x10)
}
