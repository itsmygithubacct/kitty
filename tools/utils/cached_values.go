// License: GPLv3 Copyright: 2023, Kovid Goyal, <kovid at kovidgoyal.net>

package utils

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

type CachedValues[T any] struct {
	Name      string
	Opts      T
	loadError error
}

func (self *CachedValues[T]) Path() string {
	return filepath.Join(CacheDir(), self.Name+".json")
}

func (self *CachedValues[T]) Load() T {
	self.loadError = nil
	path := self.Path()
	raw, err := os.ReadFile(path)
	if err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			self.loadError = fmt.Errorf("read cached values %q: %w", path, err)
		}
		return self.Opts
	}
	if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		self.loadError = fmt.Errorf("decode cached values %q: expected a non-null JSON value", path)
		return self.Opts
	}
	defaults, err := json.Marshal(self.Opts)
	if err != nil {
		self.loadError = fmt.Errorf("copy defaults for cached values %q: %w", path, err)
		return self.Opts
	}
	var loaded T
	if err = json.Unmarshal(defaults, &loaded); err == nil {
		err = json.Unmarshal(raw, &loaded)
	}
	if err != nil {
		self.loadError = fmt.Errorf("decode cached values %q: %w", path, err)
		return self.Opts
	}
	self.Opts = loaded
	return self.Opts
}

// LoadError returns the read or decode error from the most recent Load call.
// A failed load leaves Opts unchanged.
func (self *CachedValues[T]) LoadError() error { return self.loadError }

func (self *CachedValues[T]) Save() {
	raw, err := json.Marshal(self.Opts)
	if err == nil {
		AtomicUpdateFile(self.Path(), bytes.NewReader(raw), 0o600)
	}
}

func NewCachedValues[T any](name string, initial_val T) *CachedValues[T] {
	return &CachedValues[T]{Name: name, Opts: initial_val}
}
