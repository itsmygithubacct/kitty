package dnd

import "testing"

// Reviewer-written (non-author reviewer429), added to the live review tree after the bound 2356-byte file (sha256 85c0bac5...). Not part of INPUTS-435.json.
// Source file sha256 of the later 3872-byte version: 37bec4f5da9f31b49bd8b74071b2a22ee52502a8725eb8ffc3cbcd7040d0e86a

// Same scenario as TestReviewerEarlierCopyGestureMarksDoNotLeakIntoALaterMove, but both gestures begin through the production start callback
// (on_potential_drag_start) and use the production event handler for x=2/x=3/x=4; only the per-item transfer marks that a remote client's data
// requests would leave are set directly (the Go test cannot drive the terminal write-completion events).
func TestReviewerProductionStartCallbackDoesNotClearTransferMarks(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	gesture := func(op int, transferred ...int) {
		t.Helper()
		if err := f.d.on_potential_drag_start(0, 0); err != nil {
			t.Fatal(err)
		}
		if !f.d.drag_status.active {
			t.Fatal("the production start callback did not start a gesture")
		}
		ds := f.d.drag_sources["text/uri-list"]
		for _, i := range transferred {
			ds.uri_list[i].was_requested = true
			mark_transfer_complete(&ds.uri_list[i], true)
		}
		for _, ev := range [][3]int{{2, 0, op}, {3, 0, 0}, {4, 0, op}} {
			if err := f.d.on_drag_event(ev[0], ev[1], ev[2]); err != nil {
				t.Fatal(err)
			}
		}
	}
	gesture(1, 0, 1) // Copy of file.txt and tree
	if !f.exists("file.txt") || !f.exists("tree") {
		t.Fatal("a Copy must not delete anything")
	}
	gesture(2, 3) // Move of the symlink only
	if f.exists("link") {
		t.Error("the symlink that the Move transferred was not removed")
	}
	if !f.exists("file.txt") || !f.exists("tree") {
		t.Error("items transferred only by an earlier Copy gesture were deleted by a later Move gesture")
	}
}
