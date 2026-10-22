//go:build !windows

package transfer

import (
	"errors"
	"fmt"
	"os"

	"golang.org/x/sys/unix"
)

func (self *remote_file) apply_metadata() error {
	t := unix.NsecToTimespec(int64(self.mtime))
	var err error
	for {
		err = unix.UtimesNanoAt(unix.AT_FDCWD, self.expanded_local_path, []unix.Timespec{t, t}, unix.AT_SYMLINK_NOFOLLOW)
		if err == nil || !(errors.Is(err, unix.EINTR) || errors.Is(err, unix.EAGAIN)) {
			break
		}
	}
	if err != nil {
		return fmt.Errorf("Failed to set modification time for %s: %w", self.expanded_local_path, err)
	}
	if self.ftype == FileType_symlink {
		for {
			if err = unix.Fchmodat(unix.AT_FDCWD, self.expanded_local_path, syscall_mode(self.permissions), unix.AT_SYMLINK_NOFOLLOW); err == nil || !(errors.Is(err, unix.EINTR) || errors.Is(err, unix.EAGAIN)) {
				break
			}
		}
	} else {
		if err = os.Chmod(self.expanded_local_path, self.permissions); err != nil {
			return fmt.Errorf("Failed to set permissions for %s: %w", self.expanded_local_path, err)
		}
	}
	return nil
}
