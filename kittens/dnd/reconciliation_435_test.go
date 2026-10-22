package dnd

import (
	"github.com/kovidgoyal/kitty/tools/tui/loop"
	"testing"
)

func TestReconciliationLegacyMoveRetainsMarksUntilTheConfirmedFinalWrite(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.start_gesture(t)
	f.target_drops(t, 2)
	f.serve_started(t, idx_link)
	id := f.d.drag_status.remote_item_write_id
	op := drag_event_operation(loop.DndCommand{Type: 'e', X: 4})
	f.event(t, 4, 0, op)
	requested, completed := f.marks(idx_link)
	if f.d.drag_status.deferred_finish == nil || !requested || completed || !f.exists("link") {
		t.Fatal("an unreported accepted Move must retain the held finish and current-gesture marks")
	}
	if err := f.d.on_send_done(id); err != nil {
		t.Fatal(err)
	}
	if f.exists("link") {
		t.Fatal("a confirmed legacy Move did not remove its transferred source")
	}
	if !f.exists("unsent.txt") {
		t.Fatal("a legacy Move removed an unrequested source")
	}
}

func TestReconciliationReportedMoveDoesNotDeleteAfterAnInconsistentStatus(t *testing.T) {
	for _, accepted := range []int{0, 1} {
		f := new_finish_fixture(t, "esc-key")
		f.start_gesture(t)
		f.target_drops(t, accepted)
		f.serve_complete(t, idx_file)
		f.event(t, 4, 0, 2)
		f.assert_nothing_removed(t)
		f.assert_no_marks(t, "after an inconsistent Move completion")
	}
}
