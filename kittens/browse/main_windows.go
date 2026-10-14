//go:build windows

package browse

import (
	"fmt"

	"github.com/kovidgoyal/kitty/tools/cli"
)

// The browser's POSIX terminal and private-file transport require a separate
// Windows port. Keep the command discoverable without claiming it works here.
func EntryPoint(root *cli.Command) {
	root.AddSubCommand(&cli.Command{
		Name: "browse",
		ShortDescription: "In-pane browser (unavailable on Windows)",
		Run: func(cmd *cli.Command, args []string) (int, error) {
			return 1, fmt.Errorf("kilix browse is not available in the native Windows build")
		},
	})
}
