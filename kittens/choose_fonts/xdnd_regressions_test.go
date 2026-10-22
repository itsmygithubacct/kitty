package choose_fonts

import "testing"

func TestXDND17FamilyClickIsIgnoredOutsideListingPane(t *testing.T) {
	h := &handler{state: LISTING_FAMILIES}
	h.listing.handler = h
	h.listing.family_list.UpdateFamilies([]string{"First", "Second"})
	h.current_pane = &h.faces

	if err := h.listing.on_click("family-chosen:Second"); err != nil {
		t.Fatalf("family click returned an error: %v", err)
	}
	if got := h.listing.family_list.current_idx; got != 0 {
		t.Fatalf("family click changed selection outside the listing pane: current_idx=%d", got)
	}
}

func TestXDND18NoWrapPageMoveAtEndDoesNotWrap(t *testing.T) {
	var list FamilyList
	list.UpdateFamilies([]string{"First", "Second"})
	list.current_idx = 1
	if list.Next(1, false) {
		t.Fatal("no-wrap move past the last family unexpectedly succeeded")
	}
	if list.current_idx != 1 {
		t.Fatalf("no-wrap move changed selection: current_idx=%d", list.current_idx)
	}
}
