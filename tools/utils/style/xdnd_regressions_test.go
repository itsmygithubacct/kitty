package style

import "testing"

func TestXDND66RGBComponentWidthRules(t *testing.T) {
	for _, input := range []string{"rgb:e/e/e", "rgb:23/45/67", "rgb:abc/abc/def"} {
		if _, err := ParseColor(input); err != nil {
			t.Errorf("equal-width RGB components %q were rejected: %v", input, err)
		}
	}
	if _, err := ParseColor("rgb:ff/123/3"); err == nil {
		t.Fatal("unequal RGB component widths were silently truncated")
	}
}
