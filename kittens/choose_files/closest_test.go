package choose_files

import "testing"

func TestSortedResultsClosestUsesNearestDirectionalMatches(t *testing.T) {
	results := NewSortedResults()
	items := [][]string{{"first", "backward-near"}, {"current", "forward-near"}, {"forward-far", "last"}}
	score := 0
	for _, names := range items {
		slice := make([]*ResultItem, len(names))
		for i, name := range names {
			slice[i] = &ResultItem{text: name, score: CombinedScore(score)}
			score++
		}
		results.AddSortedSlice(slice)
	}

	assert := func(idx CollectionIndex, selected map[string]bool, want *CollectionIndex) {
		t.Helper()
		got := results.Closest(idx, func(item *ResultItem) bool { return selected[item.text] })
		if want == nil {
			if got != nil {
				t.Fatalf("Closest(%v) = %v, want no match", idx, *got)
			}
			return
		}
		if got == nil || *got != *want {
			t.Fatalf("Closest(%v) = %v, want %v", idx, got, *want)
		}
	}

	current := CollectionIndex{Slice: 1, Pos: 0}
	wantForward := CollectionIndex{Slice: 1, Pos: 1}
	assert(current, map[string]bool{"first": true, "forward-near": true}, &wantForward)
	wantBackward := CollectionIndex{Slice: 0, Pos: 1}
	assert(current, map[string]bool{"backward-near": true, "last": true}, &wantBackward)
	assert(current, map[string]bool{"backward-near": true, "forward-near": true}, &wantForward)
	assert(current, map[string]bool{"current": true}, nil)
	assert(CollectionIndex{}, map[string]bool{"last": true}, &CollectionIndex{Slice: 2, Pos: 1})
	assert(CollectionIndex{Slice: 3}, map[string]bool{"first": true}, nil)
}
