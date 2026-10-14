package choose_files

import (
	"testing"

	"github.com/kovidgoyal/kitty/tools/utils"
)

func TestXDND58ShellOutputAlwaysQuotesAPathAsAnArgument(t *testing.T) {
	path := "folder/file;name"
	got, want := quote_if_needed(path), utils.QuoteStringForSH(path)
	if got != want {
		t.Fatalf("shell path was not serialized as a quoted argument: got %q, want %q", got, want)
	}
}
