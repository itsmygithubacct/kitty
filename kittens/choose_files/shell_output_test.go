package choose_files

import (
	"testing"

	"github.com/kovidgoyal/kitty/tools/utils/shlex"
)

func TestQuoteIfNeededPreservesShellArgumentBytes(t *testing.T) {
	for _, path := range []string{
		"ordinary-name",
		"name;touch marker",
		"name&other",
		"name|other",
		"single'quote",
		`double"quote`,
		`back\slash`,
		"$HOME",
		"`command`",
		"name with spaces",
		"line\nbreak",
		"#comment",
	} {
		quoted := quote_if_needed(path)
		words, err := shlex.Split(quoted)
		if err != nil {
			t.Fatalf("quoted path %q did not parse: %v", path, err)
		}
		if len(words) != 1 || words[0] != path {
			t.Fatalf("quoted path %q became %#v", path, words)
		}
	}
}
