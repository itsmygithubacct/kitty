package choose_fonts

import "testing"

func TestFamilyListNextPreservesNoWrapAndWrapBoundaries(t *testing.T) {
	tests := []struct {
		name       string
		families   []string
		current    int
		delta      int
		allowWrap  bool
		wantMoved  bool
		wantFamily string
	}{
		{name: "last valid index", families: []string{"A", "B", "C"}, current: 1, delta: 1, wantMoved: true, wantFamily: "C"},
		{name: "exactly past final index", families: []string{"A", "B", "C"}, current: 2, delta: 1, wantFamily: "C"},
		{name: "farther past final index", families: []string{"A", "B", "C"}, current: 2, delta: 2, wantFamily: "C"},
		{name: "negative index without wrapping", families: []string{"A", "B", "C"}, current: 0, delta: -1, wantFamily: "A"},
		{name: "forward wrapping", families: []string{"A", "B", "C"}, current: 2, delta: 1, allowWrap: true, wantMoved: true, wantFamily: "A"},
		{name: "backward wrapping", families: []string{"A", "B", "C"}, current: 0, delta: -1, allowWrap: true, wantMoved: true, wantFamily: "C"},
		{name: "empty list", delta: 1},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			families := FamilyList{families: test.families, current_idx: test.current}
			if moved := families.Next(test.delta, test.allowWrap); moved != test.wantMoved {
				t.Fatalf("Next(%d, %v) returned %v, want %v", test.delta, test.allowWrap, moved, test.wantMoved)
			}
			if got := families.CurrentFamily(); got != test.wantFamily {
				t.Fatalf("current family is %q, want %q", got, test.wantFamily)
			}
		})
	}
}
