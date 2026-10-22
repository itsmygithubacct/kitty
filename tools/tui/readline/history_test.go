// License: GPLv3

package readline

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestHistoryWriteMergesUpdatesSavedByAnotherWriter(t *testing.T) {
	path := filepath.Join(t.TempDir(), "history.json")
	first := NewHistory(path, 20)
	second := NewHistory(path, 20)
	if first.file == nil || second.file == nil {
		t.Fatal("Failed to open temporary history files")
	}
	defer first.file.Close()
	defer second.file.Close()

	first.AddItem("first writer", 0)
	second.AddItem("second writer", 0)
	if err := second.Write(); err != nil {
		t.Fatalf("Failed to write second history: %s", err)
	}
	if err := first.Write(); err != nil {
		t.Fatalf("Failed to merge and write first history: %s", err)
	}

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("Failed to read saved history: %s", err)
	}
	var items []HistoryItem
	if err = json.Unmarshal(data, &items); err != nil {
		t.Fatalf("Failed to parse saved history: %s", err)
	}
	seen := make(map[string]bool, len(items))
	for _, item := range items {
		seen[item.Cmd] = true
	}
	if len(seen) != 2 || !seen["first writer"] || !seen["second writer"] {
		t.Fatalf("Saved history did not retain both writers: %#v", items)
	}
}

func TestHistoryWritePreservesMalformedFileAndReturnsIOErrors(t *testing.T) {
	path := filepath.Join(t.TempDir(), "history.json")
	history := NewHistory(path, 20)
	if history.file == nil {
		t.Fatal("Failed to open temporary history file")
	}
	history.AddItem("local command", 0)

	corrupt := []byte("{")
	if err := os.WriteFile(path, corrupt, 0o600); err != nil {
		t.Fatalf("Failed to write malformed history fixture: %s", err)
	}
	if err := history.Write(); err == nil {
		t.Fatal("Expected malformed history JSON to be reported")
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("Failed to read malformed history fixture: %s", err)
	}
	if string(got) != string(corrupt) {
		t.Fatalf("Malformed history was overwritten: got %q, expected %q", got, corrupt)
	}

	if err = history.file.Close(); err != nil {
		t.Fatalf("Failed to close history file: %s", err)
	}
	if err = history.Write(); err == nil {
		t.Fatal("Expected a closed history file to return an I/O error")
	}
}

func TestHistoryWriteMergesConcurrentDiskEntries(t *testing.T) {
	path := filepath.Join(t.TempDir(), "history.json")
	first := NewHistory(path, 20)
	second := NewHistory(path, 20)
	t.Cleanup(func() {
		if first.file != nil {
			first.file.Close()
		}
		if second.file != nil {
			second.file.Close()
		}
	})
	first.merge_items(HistoryItem{Cmd: "first process", Timestamp: time.Unix(1, 0)})
	second.merge_items(HistoryItem{Cmd: "concurrent process", Timestamp: time.Unix(2, 0)})
	second.Write()
	first.Write()

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var items []HistoryItem
	if err = json.Unmarshal(data, &items); err != nil {
		t.Fatal(err)
	}
	got := make(map[string]bool, len(items))
	for _, item := range items {
		got[item.Cmd] = true
	}
	if !got["first process"] || !got["concurrent process"] {
		t.Fatalf("saved history lost an entry: %+v", items)
	}
}
