// License: GPLv3

package loop

import (
	"bytes"
	"errors"
	"fmt"
	"strings"
	"testing"
)

type panicRecoveryTestTerminal struct {
	actions    []string
	debugLines []string
	readSize   int
}

func (t *panicRecoveryTestTerminal) RestoreAndClose() error {
	t.actions = append(t.actions, "restore")
	return nil
}

func (t *panicRecoveryTestTerminal) DebugPrintln(args ...any) {
	t.actions = append(t.actions, "debug")
	t.debugLines = append(t.debugLines, fmt.Sprint(args...))
}

func (t *panicRecoveryTestTerminal) Read(buf []byte) (int, error) {
	t.actions = append(t.actions, "read")
	t.readSize = len(buf)
	return 0, nil
}

func TestRunPanicRecoveryOpenFailurePreservesRecoveredError(t *testing.T) {
	var stderr, stdout bytes.Buffer
	openErr := errors.New("terminal open failed")
	openCalls := 0

	err := run_with_panic_recovery(func() error {
		panic("original panic")
	}, func() bool {
		return true
	}, &stderr, &stdout, func() (panic_recovery_terminal, error) {
		openCalls++
		return nil, openErr
	})

	if err == nil || !strings.Contains(err.Error(), "original panic") {
		t.Fatalf("recovered error lost the panic value: %v", err)
	}
	if strings.Contains(err.Error(), openErr.Error()) {
		t.Fatalf("terminal open error replaced the recovered error: %v", err)
	}
	if !strings.Contains(stderr.String(), "original panic") {
		t.Fatalf("recovered panic was not written to stderr: %q", stderr.String())
	}
	if openCalls != 1 {
		t.Fatalf("terminal opener called %d times, want 1", openCalls)
	}
	if stdout.Len() != 0 {
		t.Fatalf("failed open unexpectedly prompted on stdout: %q", stdout.String())
	}
}

func TestRunPanicRecoveryRestoresSuccessfullyOpenedTerminal(t *testing.T) {
	var stderr, stdout bytes.Buffer
	term := &panicRecoveryTestTerminal{}
	openCalls := 0

	err := run_with_panic_recovery(func() error {
		panic("original panic")
	}, func() bool {
		return true
	}, &stderr, &stdout, func() (panic_recovery_terminal, error) {
		openCalls++
		return term, nil
	})

	if err == nil || !strings.Contains(err.Error(), "original panic") {
		t.Fatalf("recovered error lost the panic value: %v", err)
	}
	if !strings.Contains(stderr.String(), "original panic") {
		t.Fatalf("recovered panic was not written to stderr: %q", stderr.String())
	}
	if openCalls != 1 {
		t.Fatalf("terminal opener called %d times, want 1", openCalls)
	}
	if len(term.debugLines) != 1 || term.debugLines[0] != err.Error() {
		t.Fatalf("terminal debug output = %#v, want recovered error", term.debugLines)
	}
	if term.readSize != 16 {
		t.Fatalf("terminal read buffer size = %d, want 16", term.readSize)
	}
	if got, want := strings.Join(term.actions, ","), "debug,read,restore"; got != want {
		t.Fatalf("terminal actions = %q, want %q", got, want)
	}
	if got, want := stdout.String(), "Press any key to exit.\r\n"; got != want {
		t.Fatalf("stdout prompt = %q, want %q", got, want)
	}
}

func TestRunPanicRecoveryPreservesOrdinaryReturns(t *testing.T) {
	ordinaryErr := errors.New("ordinary run error")
	for _, tc := range []struct {
		name string
		err  error
	}{
		{name: "success"},
		{name: "error", err: ordinaryErr},
	} {
		t.Run(tc.name, func(t *testing.T) {
			runCalls := 0
			got := run_with_panic_recovery(func() error {
				runCalls++
				return tc.err
			}, func() bool {
				t.Fatal("terminal detection called on ordinary return")
				return false
			}, &bytes.Buffer{}, &bytes.Buffer{}, func() (panic_recovery_terminal, error) {
				t.Fatal("terminal opened on ordinary return")
				return nil, nil
			})
			if got != tc.err {
				t.Fatalf("Run() error = %v, want original error %v", got, tc.err)
			}
			if runCalls != 1 {
				t.Fatalf("run callback called %d times, want 1", runCalls)
			}
		})
	}
}
