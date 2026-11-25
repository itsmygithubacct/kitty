// License: GPLv3

package at

import (
	"fmt"
	"io"
	"os"
	"strings"

	"github.com/kovidgoyal/kitty/tools/tty"
)

// The engine refuses anything longer; read one byte more to detect it.
const shell_submit_max_bytes = 4096

// read_shell_submit_command takes the command from the arguments or, with none,
// from STDIN. STDIN keeps the command out of process listings. One trailing
// newline, as a here-string or echo adds, is dropped; any other control
// character is left for the engine to refuse.
func read_shell_submit_command(args []string) (escaped_string, error) {
	if len(args) > 0 {
		return escaped_string(strings.Join(args, " ")), nil
	}
	if tty.IsTerminal(os.Stdin.Fd()) {
		return "", fmt.Errorf("give the command as arguments or on STDIN")
	}
	data, err := io.ReadAll(io.LimitReader(os.Stdin, shell_submit_max_bytes+2))
	if err != nil {
		return "", err
	}
	text := strings.TrimSuffix(string(data), "\n")
	if len(text) > shell_submit_max_bytes {
		return "", fmt.Errorf("invalid_command: the command is longer than %d bytes", shell_submit_max_bytes)
	}
	return escaped_string(text), nil
}
