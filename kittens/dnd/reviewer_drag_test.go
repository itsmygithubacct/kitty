package dnd

// Reviewer-written tests, not part of the r3 packet. They reuse the fixture helpers of drag_finish_test.go (r3).
import "testing"

// A Copy drop in one gesture and a Move drop in the next, in one kitten session. The sources are offered again after a Copy (drag_sources is kept),
// but the per-item was_requested / transfer_completed marks are never cleared at the start of a gesture, so the Move that only read "link" must not
// delete the two items that an earlier gesture merely copied.
func TestReviewerEarlierCopyGestureMarksDoNotLeakIntoALaterMove(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	ds := f.d.drag_sources["text/uri-list"]
	for _, i := range []int{0, 1} { // gesture 1: file.txt and tree fully read by the target
		ds.uri_list[i].was_requested = true
		mark_transfer_complete(&ds.uri_list[i], true)
	}
	f.d.drag_status = drag_status{active: true, dropped: true, accepted_operation: 1}
	if err := f.d.on_drag_event(4, 0, 1); err != nil { // finished as Copy
		t.Fatal(err)
	}
	if !f.exists("file.txt") || !f.exists("tree") {
		t.Fatal("a Copy must not delete anything")
	}
	// gesture 2: Move; the target reads only the symlink
	ds = f.d.drag_sources["text/uri-list"]
	ds.uri_list[3].was_requested = true
	mark_transfer_complete(&ds.uri_list[3], true)
	f.d.drag_status = drag_status{active: true, dropped: true, accepted_operation: 2}
	if err := f.d.on_drag_event(4, 0, 2); err != nil {
		t.Fatal(err)
	}
	if f.exists("link") {
		t.Error("the symlink that the Move transferred was not removed")
	}
	if !f.exists("file.txt") {
		t.Error("file.txt was only copied in an earlier gesture but a later Move deleted it")
	}
	if !f.exists("tree") {
		t.Error("tree was only copied in an earlier gesture but a later Move deleted it")
	}
}

// docs/dnd-protocol.rst (in this tree): "Source completion t=e:x=4 includes o with the actual successful action, or zero on refusal/cancellation."
// An o=0 report after a drop that the target accepted as Move is therefore a refusal, not a completed Move, and must not delete the sources.
func TestReviewerDocumentedZeroOperationAfterDropIsARefusal(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.sent()
	f.d.drag_status = drag_status{active: true, dropped: true, accepted_operation: 2}
	if err := f.d.on_drag_event(4, 0, 0); err != nil {
		t.Fatal(err)
	}
	f.assert_nothing_removed(t)
}
