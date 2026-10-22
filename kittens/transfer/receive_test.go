// License: GPLv3 Copyright: 2023, Kovid Goyal, <kovid at kovidgoyal.net>

package transfer

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestDirectoryOnlyReceiveFinalizesAfterCollection(t *testing.T) {
	base := t.TempDir()
	dest := filepath.Join(base, "received") + string(os.PathSeparator)
	rootMtime := time.Unix(1_600_000_000, 0)
	childMtime := time.Unix(1_610_000_000, 0)
	m := manager{
		cli_opts: &Options{Mode: "normal"}, dest: dest, spec: []string{"/remote/tree"},
		files: []*remote_file{
			{spec_id: 0, file_id: "1", remote_path: "/remote/tree", remote_id: "root", ftype: FileType_directory, mtime: time.Duration(rootMtime.UnixNano()), permissions: 0o755},
			{spec_id: 0, file_id: "2", remote_path: "/remote/tree/child", remote_id: "child", parent: "root", ftype: FileType_directory, mtime: time.Duration(childMtime.UnixNano()), permissions: 0o755},
		},
		files_to_be_transferred: make(map[string]*remote_file),
	}
	if err := m.collect_files(); err != nil {
		t.Fatal(err)
	}
	if len(m.files_to_be_transferred) != 0 {
		t.Fatalf("expected no payload-bearing files, got %d", len(m.files_to_be_transferred))
	}
	done, err := m.finalize_if_no_files_to_transfer()
	if err != nil {
		t.Fatal(err)
	}
	if !done || !m.transfer_done {
		t.Fatal("directory-only receive was not marked complete")
	}
	rootPath := filepath.Join(base, "received", "tree")
	rootStat, err := os.Stat(rootPath)
	if err != nil || !rootStat.IsDir() {
		t.Fatalf("received root directory was not created: stat=%v err=%v", rootStat, err)
	}
	if got := rootStat.ModTime().Unix(); got != rootMtime.Unix() {
		t.Fatalf("root mtime = %d, want %d", got, rootMtime.Unix())
	}
	childStat, err := os.Stat(filepath.Join(rootPath, "child"))
	if err != nil || !childStat.IsDir() {
		t.Fatalf("received child directory was not created: stat=%v err=%v", childStat, err)
	}
	if got := childStat.ModTime().Unix(); got != childMtime.Unix() {
		t.Fatalf("child mtime = %d, want %d", got, childMtime.Unix())
	}
}

func TestMetadataOnlyFinalizationReturnsCreationErrors(t *testing.T) {
	base := t.TempDir()
	blocker := filepath.Join(base, "not-a-directory")
	if err := os.WriteFile(blocker, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	m := manager{
		files:                   []*remote_file{{ftype: FileType_directory, expanded_local_path: filepath.Join(blocker, "child")}},
		files_to_be_transferred: make(map[string]*remote_file),
	}
	done, err := m.finalize_if_no_files_to_transfer()
	if !done || err == nil {
		t.Fatalf("expected metadata-only finalization error, done=%v err=%v", done, err)
	}
	if m.transfer_done {
		t.Fatal("failed finalization was marked complete")
	}
}

func TestReceiveMetadataApplicationReturnsErrors(t *testing.T) {
	f := remote_file{
		ftype: FileType_directory, expanded_local_path: filepath.Join(t.TempDir(), "missing"), permissions: 0o755,
	}
	if err := f.apply_metadata(); err == nil {
		t.Fatal("metadata application accepted a missing destination")
	}
}
