//go:build !windows

// License: GPLv3

package utils

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type revokeDirectoryWrite struct {
	dir    string
	reader *strings.Reader
}

func (r revokeDirectoryWrite) Read(p []byte) (int, error) {
	if err := os.Chmod(r.dir, 0o500); err != nil {
		return 0, err
	}
	return r.reader.Read(p)
}

func TestAtomicWriteFilePreservesDestinationOnRenameError(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root can rename files in a directory without write permission")
	}
	dir := t.TempDir()
	defer os.Chmod(dir, 0o700)
	path := filepath.Join(dir, "executable")
	if err := os.WriteFile(path, []byte("original"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, 0o755); err != nil {
		t.Fatal(err)
	}
	// Revoke directory write permission after the temporary file is created,
	// so writing succeeds but renaming it over the destination fails.
	err := AtomicWriteFile(path, revokeDirectoryWrite{dir, strings.NewReader("replacement")}, 0o755)
	if !errors.Is(err, fs.ErrPermission) {
		t.Fatalf("expected rename permission error, got: %v", err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "original" {
		t.Fatalf("failed write changed destination contents to %q", data)
	}
	st, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if st.Mode().Perm() != 0o755 {
		t.Fatalf("failed write changed destination permissions to %o", st.Mode().Perm())
	}
}
