//go:build windows

package transfer

import (
	"fmt"
	"os"
	"time"
)

func (self *remote_file) apply_metadata() error {
	mt := time.Unix(0, int64(self.mtime))
	if err := os.Chtimes(self.expanded_local_path, mt, mt); err != nil {
		return fmt.Errorf("Failed to set modification time for %s: %w", self.expanded_local_path, err)
	}
	// Symlink permissions are not meaningful on Windows.
	if self.ftype != FileType_symlink {
		if err := os.Chmod(self.expanded_local_path, self.permissions); err != nil {
			return fmt.Errorf("Failed to set permissions for %s: %w", self.expanded_local_path, err)
		}
	}
	return nil
}
