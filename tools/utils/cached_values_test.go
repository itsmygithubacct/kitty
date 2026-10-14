package utils

import (
	"os"
	"path/filepath"
	"testing"
)

type themeCacheFixture struct {
	Recent   []string `json:"recent"`
	Category string   `json:"category"`
}

type unicodeCacheFixture struct {
	Recent []rune `json:"recent,omitempty"`
	Mode   string `json:"mode,omitempty"`
}

func useCachedValuesTestDir(t *testing.T) string {
	t.Helper()
	cacheDir := t.TempDir()
	previous := CacheDir
	CacheDir = func() string { return cacheDir }
	t.Cleanup(func() { CacheDir = previous })
	return cacheDir
}

func TestCachedValuesSeparateSchemasAndPreserveLegacyFile(t *testing.T) {
	cacheDir := useCachedValuesTestDir(t)
	legacy := []byte(`{"recent":["old theme"],"category":"Dark","mode":"NAME"}`)
	legacyPath := filepath.Join(cacheDir, "unicode-input.json")
	if err := os.WriteFile(legacyPath, legacy, 0o600); err != nil {
		t.Fatal(err)
	}

	themes := NewCachedValues("themes-v1", &themeCacheFixture{Category: "All"})
	themes.Opts.Recent = []string{"new theme"}
	themes.Save()
	unicode := NewCachedValues("unicode-input-v1", &unicodeCacheFixture{Recent: []rune{'A'}, Mode: "HEX"})
	unicode.Opts.Recent = []rune{'B'}
	unicode.Save()

	loadedThemes := NewCachedValues("themes-v1", &themeCacheFixture{Category: "All"})
	if got := loadedThemes.Load(); loadedThemes.LoadError() != nil || got.Category != "All" || len(got.Recent) != 1 || got.Recent[0] != "new theme" {
		t.Fatalf("theme cache was not kept separate: got %#v, error %v", got, loadedThemes.LoadError())
	}
	loadedUnicode := NewCachedValues("unicode-input-v1", &unicodeCacheFixture{Recent: []rune{'A'}, Mode: "HEX"})
	if got := loadedUnicode.Load(); loadedUnicode.LoadError() != nil || got.Mode != "HEX" || len(got.Recent) != 1 || got.Recent[0] != 'B' {
		t.Fatalf("Unicode cache was not kept separate: got %#v, error %v", got, loadedUnicode.LoadError())
	}
	if got, err := os.ReadFile(legacyPath); err != nil || string(got) != string(legacy) {
		t.Fatalf("legacy shared cache changed: got %q, error %v", got, err)
	}
}

func TestCachedValuesDecodeFailureKeepsDefaults(t *testing.T) {
	cacheDir := useCachedValuesTestDir(t)
	// The legacy theme schema stores recent as strings, which cannot decode as []rune.
	legacySchema := []byte(`{"recent":["theme name"],"category":"Dark"}`)
	if err := os.WriteFile(filepath.Join(cacheDir, "unicode-input-v1.json"), legacySchema, 0o600); err != nil {
		t.Fatal(err)
	}
	values := NewCachedValues("unicode-input-v1", &unicodeCacheFixture{Recent: []rune{'A'}, Mode: "HEX"})
	got := values.Load()
	if values.LoadError() == nil {
		t.Fatal("incompatible cached schema did not report a decode error")
	}
	if got.Mode != "HEX" || len(got.Recent) != 1 || got.Recent[0] != 'A' {
		t.Fatalf("decode failure changed defaults: got %#v", got)
	}
}

func TestCachedValuesNullKeepsDefaults(t *testing.T) {
	cacheDir := useCachedValuesTestDir(t)
	if err := os.WriteFile(filepath.Join(cacheDir, "unicode-input-v1.json"), []byte("null\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	values := NewCachedValues("unicode-input-v1", &unicodeCacheFixture{Recent: []rune{'A'}, Mode: "HEX"})
	got := values.Load()
	if values.LoadError() == nil {
		t.Fatal("null cache did not report a decode error")
	}
	if got.Mode != "HEX" || len(got.Recent) != 1 || got.Recent[0] != 'A' {
		t.Fatalf("null cache changed defaults: got %#v", got)
	}
}

func TestCachedValuesPartialJSONPreservesDefaults(t *testing.T) {
	cacheDir := useCachedValuesTestDir(t)
	if err := os.WriteFile(filepath.Join(cacheDir, "unicode-input-v1.json"), []byte(`{"mode":"NAME"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	values := NewCachedValues("unicode-input-v1", &unicodeCacheFixture{Recent: []rune{'A'}, Mode: "HEX"})
	got := values.Load()
	if values.LoadError() != nil {
		t.Fatalf("valid partial cache reported an error: %v", values.LoadError())
	}
	if got.Mode != "NAME" || len(got.Recent) != 1 || got.Recent[0] != 'A' {
		t.Fatalf("partial cache did not preserve omitted defaults: got %#v", got)
	}
}
