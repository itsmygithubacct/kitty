//go:build linux

// License: GPLv3

package tty

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"reflect"
	"strings"
	"syscall"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

type readRestorePTY struct {
	master    *os.File
	slavePath string
	inspectFD int
	original  unix.Termios
}

func openReadRestorePTY(t *testing.T) *readRestorePTY {
	t.Helper()

	masterFD, err := unix.Open("/dev/ptmx", unix.O_RDWR|unix.O_NOCTTY|unix.O_CLOEXEC, 0)
	if err != nil {
		t.Skipf("cannot open PTY master: %v", err)
	}
	pty := &readRestorePTY{master: os.NewFile(uintptr(masterFD), "pty-master")}
	t.Cleanup(func() {
		if pty.master != nil {
			_ = pty.master.Close()
		}
	})

	if err := unix.IoctlSetPointerInt(masterFD, unix.TIOCSPTLCK, 0); err != nil {
		t.Skipf("cannot unlock PTY slave: %v", err)
	}
	ptNumber, err := unix.IoctlGetInt(masterFD, unix.TIOCGPTN)
	if err != nil {
		t.Skipf("cannot get PTY number: %v", err)
	}
	pty.slavePath = fmt.Sprintf("/dev/pts/%d", ptNumber)
	pty.inspectFD, err = unix.Open(pty.slavePath, unix.O_RDWR|unix.O_NOCTTY|unix.O_CLOEXEC, 0)
	if err != nil {
		t.Skipf("cannot open PTY slave: %v", err)
	}
	t.Cleanup(func() { _ = unix.Close(pty.inspectFD) })

	if err := Tcgetattr(pty.inspectFD, &pty.original); err != nil {
		t.Skipf("cannot read PTY state: %v", err)
	}
	pty.original.Lflag |= unix.ICANON
	pty.original.Cc[unix.VMIN] = 7
	pty.original.Cc[unix.VTIME] = 3
	if err := Tcsetattr(pty.inspectFD, TCSANOW, &pty.original); err != nil {
		t.Skipf("cannot set initial PTY state: %v", err)
	}
	if err := Tcgetattr(pty.inspectFD, &pty.original); err != nil {
		t.Skipf("cannot confirm initial PTY state: %v", err)
	}
	return pty
}

func (pty *readRestorePTY) closeMaster() {
	if pty.master != nil {
		_ = pty.master.Close()
		pty.master = nil
	}
}

func (pty *readRestorePTY) assertRestored(t *testing.T) {
	t.Helper()
	var got unix.Termios
	if err := Tcgetattr(pty.inspectFD, &got); err != nil {
		t.Fatalf("cannot read restored PTY state: %v", err)
	}
	if !reflect.DeepEqual(got, pty.original) {
		t.Fatalf("PTY state was not restored:\ngot:  %#v\nwant: %#v", got, pty.original)
	}
}

const (
	readRestoreHelperModeEnv = "TTY_READ_RESTORE_HELPER_MODE"
	readRestoreSlavePathEnv  = "TTY_READ_RESTORE_SLAVE_PATH"
)

func readRestoreHelperEnv(mode, slavePath string) []string {
	ans := make([]string, 0, len(os.Environ())+2)
	for _, item := range os.Environ() {
		if strings.HasPrefix(item, readRestoreHelperModeEnv+"=") || strings.HasPrefix(item, readRestoreSlavePathEnv+"=") {
			continue
		}
		ans = append(ans, item)
	}
	return append(ans, readRestoreHelperModeEnv+"="+mode, readRestoreSlavePathEnv+"="+slavePath)
}

func runReadRestoreHelper(t *testing.T, mode, slavePath string, background bool) {
	t.Helper()
	timeout := 8 * time.Second
	if mode == "reader" {
		timeout = 4 * time.Second
	}
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	t.Cleanup(cancel)
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestReadSingleByteFromTerminalRestoresPTYStateAfterReadError$")
	cmd.Env = readRestoreHelperEnv(mode, slavePath)
	if background {
		cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	}
	output, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("%s PTY helper failed: %v\n%s", mode, err, output)
	}
}

type readRestoreResult struct {
	byte byte
	err  error
}

func TestReadSingleByteFromTerminalRestoresPTYStateAfterByte(t *testing.T) {
	for _, tc := range []struct {
		name  string
		input byte
	}{
		{name: "lowercase acceptance", input: 'y'},
		{name: "uppercase acceptance", input: 'Y'},
		{name: "refusal", input: 'n'},
		{name: "other refusal", input: 'x'},
	} {
		t.Run(tc.name, func(t *testing.T) {
			pty := openReadRestorePTY(t)
			opened := make(chan *Term, 1)
			done := make(chan readRestoreResult, 1)
			workerFinished := make(chan struct{})
			workerJoined := false
			var openedTerm *Term
			go func() {
				defer close(workerFinished)
				b, err := read_single_byte_from_terminal(func(operations ...TermiosOperation) (*Term, error) {
					term, err := OpenTerm(pty.slavePath, operations...)
					if err == nil {
						opened <- term
					}
					return term, err
				})
				done <- readRestoreResult{byte: b, err: err}
			}()
			t.Cleanup(func() {
				if workerJoined {
					return
				}
				if pty.master != nil {
					_, _ = pty.master.Write([]byte{tc.input})
				}
				select {
				case <-workerFinished:
					return
				case <-time.After(2 * time.Second):
				}
				pty.closeMaster()
				select {
				case <-workerFinished:
					t.Errorf("read worker did not finish after cleanup input; closed PTY master to unblock it")
				case <-time.After(2 * time.Second):
					t.Errorf("read worker did not finish within 2s after closing PTY master")
				}
			})

			select {
			case openedTerm = <-opened:
			case result := <-done:
				<-workerFinished
				workerJoined = true
				t.Fatalf("read returned before terminal opened: byte=%q error=%v", result.byte, result.err)
			case <-time.After(2 * time.Second):
				t.Fatal("timed out opening PTY slave")
			}
			select {
			case result := <-done:
				<-workerFinished
				workerJoined = true
				t.Fatalf("read returned before a byte was sent: byte=%q error=%v", result.byte, result.err)
			case <-time.After(10 * time.Millisecond):
			}
			if _, err := pty.master.Write([]byte{tc.input}); err != nil {
				t.Fatalf("cannot write test byte to PTY: %v", err)
			}

			var result readRestoreResult
			select {
			case result = <-done:
				<-workerFinished
				workerJoined = true
			case <-time.After(2 * time.Second):
				t.Fatal("timed out reading test byte from PTY")
			}
			if result.err != nil || result.byte != tc.input {
				t.Fatalf("read returned byte=%q error=%v, want byte=%q and nil error", result.byte, result.err, tc.input)
			}
			pty.assertRestored(t)
			if openedTerm.Fd() != -1 {
				t.Fatalf("terminal fd is still open after read: %d", openedTerm.Fd())
			}
		})
	}
}

func TestReadSingleByteFromTerminalRestoresPTYStateAfterReadError(t *testing.T) {
	if mode := os.Getenv(readRestoreHelperModeEnv); mode != "" {
		slavePath := os.Getenv(readRestoreSlavePathEnv)
		switch mode {
		case "session":
			if _, err := syscall.Setsid(); err != nil {
				t.Fatalf("cannot create PTY helper session: %v", err)
			}
			fd, err := unix.Open(slavePath, unix.O_RDWR|unix.O_CLOEXEC, 0)
			if err != nil {
				t.Fatalf("cannot acquire PTY as controlling terminal: %v", err)
			}
			defer unix.Close(fd)
			foreground, err := unix.IoctlGetInt(fd, unix.TIOCGPGRP)
			if err != nil {
				t.Fatalf("cannot inspect PTY foreground process group: %v", err)
			}
			if foreground != syscall.Getpgrp() {
				t.Fatalf("PTY foreground process group = %d, want helper group %d", foreground, syscall.Getpgrp())
			}
			runReadRestoreHelper(t, "reader", slavePath, true)
		case "reader":
			// Term.Read retries EAGAIN and EWOULDBLOCK. A background-group read with SIGTTIN ignored returns EIO while the PTY stays live.
			signal.Ignore(syscall.SIGTTIN, syscall.SIGTTOU)
			probeFD, err := unix.Open(slavePath, unix.O_RDWR|unix.O_NOCTTY|unix.O_CLOEXEC, 0)
			if err != nil {
				t.Fatalf("cannot inspect PTY in background reader: %v", err)
			}
			foreground, err := unix.IoctlGetInt(probeFD, unix.TIOCGPGRP)
			_ = unix.Close(probeFD)
			if err != nil {
				t.Fatalf("cannot inspect PTY foreground process group: %v", err)
			}
			if foreground == syscall.Getpgrp() {
				t.Fatalf("reader process group %d unexpectedly owns PTY foreground", foreground)
			}
			var opened *Term
			b, err := read_single_byte_from_terminal(func(operations ...TermiosOperation) (*Term, error) {
				term, openErr := OpenTerm(slavePath, operations...)
				opened = term
				return term, openErr
			})
			if b != 0 || !errors.Is(err, unix.EIO) {
				t.Fatalf("background PTY read returned byte=%q error=%v, want EIO", b, err)
			}
			if opened == nil || opened.Fd() != -1 {
				t.Fatalf("terminal was not closed after read error: %#v", opened)
			}
		default:
			t.Fatalf("unknown PTY helper mode %q", mode)
		}
		return
	}

	// The helper subprocess receives EIO from a real background PTY read; this process keeps the master and observer open.
	pty := openReadRestorePTY(t)
	runReadRestoreHelper(t, "session", pty.slavePath, false)
	if pty.master == nil {
		t.Fatal("PTY master was closed before restoration could be observed")
	}
	if _, err := pty.master.Stat(); err != nil {
		t.Fatalf("PTY master is not live after the read error: %v", err)
	}
	pty.assertRestored(t)
}

func TestReadSingleByteFromTerminalPreservesOpenFailure(t *testing.T) {
	openErr := errors.New("open failed")
	openCalls := 0
	b, err := read_single_byte_from_terminal(func(...TermiosOperation) (*Term, error) {
		openCalls++
		return nil, openErr
	})
	if b != 0 || err != openErr {
		t.Fatalf("read returned byte=%q error=%v, want zero byte and original open error", b, err)
	}
	if openCalls != 1 {
		t.Fatalf("terminal opener called %d times, want 1", openCalls)
	}
}

func TestReadSingleByteFromTerminalRejectsNilTerminal(t *testing.T) {
	openCalls := 0
	b, err := read_single_byte_from_terminal(func(...TermiosOperation) (*Term, error) {
		openCalls++
		return nil, nil
	})
	if b != 0 || !errors.Is(err, os.ErrInvalid) {
		t.Fatalf("read returned byte=%q error=%v, want zero byte and invalid-terminal error", b, err)
	}
	if openCalls != 1 {
		t.Fatalf("terminal opener called %d times, want 1", openCalls)
	}
}
