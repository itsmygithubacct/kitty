package dnd

import (
	"testing"
)

// These tests drive the production path of a gesture: the terminal's o message
// starts it, x=2/x=3 describe the target, k messages are served by
// on_drag_remote_data_request with the write-completion events delivered
// afterwards, and x=4 finishes it. Nothing sets the per-item marks directly.

const (
	idx_file   = 0
	idx_tree   = 1
	idx_empty  = 2
	idx_link   = 3
	idx_unsent = 4
)

func (f *finish_fixture) start_gesture(t *testing.T) {
	t.Helper()
	f.d.allow_drags = true
	if err := f.d.on_potential_drag_start(0, 0); err != nil {
		t.Fatal(err)
	}
	if !f.d.drag_status.active {
		t.Fatal("the production start callback did not start a gesture")
	}
	if err := f.d.on_drag_error(DC{Type: 'E', Payload: []byte("OK")}); err != nil {
		t.Fatal(err)
	}
}

func (f *finish_fixture) event(t *testing.T, x, y, o int) {
	t.Helper()
	if err := f.d.on_drag_event(x, y, o); err != nil {
		t.Fatal(err)
	}
}

// target_drops: the target accepts the given operation and the drop happens.
func (f *finish_fixture) target_drops(t *testing.T, operation int) {
	t.Helper()
	f.event(t, 1, 0, 0)
	f.event(t, 2, 0, operation)
	f.event(t, 3, 0, 0)
}

// serve_complete: the target requests the item and every write completes.
func (f *finish_fixture) serve_complete(t *testing.T, idx int) {
	t.Helper()
	if err := f.d.on_drag_remote_data_request(idx); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 1000; i++ {
		id := f.d.drag_status.remote_item_write_id
		if id == 0 {
			return
		}
		if err := f.d.on_send_done(id); err != nil {
			t.Fatal(err)
		}
	}
	t.Fatal("transfer did not finish")
}

// serve_started: the target requests the item but the write completions never arrive.
func (f *finish_fixture) serve_started(t *testing.T, idx int) {
	t.Helper()
	if err := f.d.on_drag_remote_data_request(idx); err != nil {
		t.Fatal(err)
	}
}

func (f *finish_fixture) marks(i int) (bool, bool) {
	it := f.d.drag_sources["text/uri-list"].uri_list[i]
	return it.was_requested, it.transfer_completed
}

func (f *finish_fixture) assert_no_marks(t *testing.T, when string) {
	t.Helper()
	for i := range f.items {
		if s, c := f.marks(i); s || c {
			t.Errorf("%s: item %d still has was_requested=%v transfer_completed=%v from an earlier gesture", when, i, s, c)
		}
	}
}

func TestXDND435ServedTransferMarksItems(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.start_gesture(t)
	f.target_drops(t, 2)
	for _, i := range []int{idx_file, idx_tree, idx_empty, idx_link} {
		f.serve_complete(t, i)
		if s, c := f.marks(i); !s || !c {
			t.Errorf("item %d: was_requested=%v transfer_completed=%v after a served transfer", i, s, c)
		}
	}
	if s, c := f.marks(idx_unsent); s || c {
		t.Error("an item the target never requested has marks")
	}
}

// The reviewer's finding, through the production path: a Copy gesture that
// transferred file and tree, then a Move gesture that transferred only the link.
func TestXDND435CopyThenMoveOfAnotherItemKeepsEarlierCopiedSources(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.start_gesture(t)
	f.target_drops(t, 1)
	f.serve_complete(t, idx_file)
	f.serve_complete(t, idx_tree)
	f.event(t, 4, 0, 1)
	if !f.exists("file.txt") || !f.exists("tree") {
		t.Fatal("a Copy must not delete anything")
	}
	f.assert_no_marks(t, "after the Copy gesture finished")

	f.start_gesture(t)
	f.assert_no_marks(t, "after the next gesture started")
	f.target_drops(t, 2)
	f.serve_complete(t, idx_link)
	f.event(t, 4, 0, 2)
	if f.exists("link") {
		t.Error("the symlink that the Move transferred was not removed")
	}
	for _, name := range []string{"file.txt", "tree", "empty", "unsent.txt"} {
		if !f.exists(name) {
			t.Errorf("%s was not transferred by the Move gesture but was deleted", name)
		}
	}
}

// Every way a gesture can end without deleting must also end its marks.
func TestXDND435NonDeletingEndsOfAGestureDoNotLeakIntoALaterMove(t *testing.T) {
	cases := []struct {
		name string
		run  func(t *testing.T, f *finish_fixture)
	}{
		{"copy completed", func(t *testing.T, f *finish_fixture) {
			f.target_drops(t, 1)
			f.serve_complete(t, idx_file)
			f.event(t, 4, 0, 1)
		}},
		{"copy completed, operation unreported", func(t *testing.T, f *finish_fixture) {
			f.target_drops(t, 1)
			f.serve_complete(t, idx_file)
			f.event(t, 4, 0, operation_not_reported)
		}},
		{"canceled after the transfer", func(t *testing.T, f *finish_fixture) {
			f.target_drops(t, 2)
			f.serve_complete(t, idx_file)
			f.event(t, 4, 1, 0)
		}},
		{"explicit refusal after the transfer", func(t *testing.T, f *finish_fixture) {
			f.target_drops(t, 2)
			f.serve_complete(t, idx_file)
			f.serve_complete(t, idx_tree)
			f.event(t, 4, 0, 0)
		}},
		{"never dropped", func(t *testing.T, f *finish_fixture) {
			f.event(t, 2, 0, 2)
			f.event(t, 4, 0, 2)
		}},
		{"transfer half way, then refusal", func(t *testing.T, f *finish_fixture) {
			f.target_drops(t, 2)
			f.serve_started(t, idx_tree)
			f.event(t, 4, 0, 0)
		}},
		{"kitten abandoned the gesture", func(t *testing.T, f *finish_fixture) {
			f.target_drops(t, 2)
			f.serve_complete(t, idx_file)
			f.d.finish_drag("EIO")
		}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := new_finish_fixture(t, "esc-key")
			f.start_gesture(t)
			c.run(t, f)
			f.assert_no_marks(t, "after the gesture ended")
			if !f.exists("file.txt") || !f.exists("tree") {
				t.Fatal("the gesture deleted a source although it did not complete as a Move")
			}
			f.start_gesture(t)
			f.target_drops(t, 2)
			f.serve_complete(t, idx_link)
			f.event(t, 4, 0, 2)
			if f.exists("link") {
				t.Error("the symlink that the Move transferred was not removed")
			}
			for _, name := range []string{"file.txt", "tree", "empty", "unsent.txt"} {
				if !f.exists(name) {
					t.Errorf("%s was deleted by a Move that did not transfer it", name)
				}
			}
		})
	}
}

// A mark can outlive its gesture only if no x=4 or abandonment reached the kitten
// (a lost terminal message). The next gesture's start must still clear it.
func TestXDND435StartOfAGestureClearsMarksOfALostGesture(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.start_gesture(t)
	f.target_drops(t, 2)
	f.serve_complete(t, idx_file)
	f.d.reset_drag() // the gesture just disappears, as after a lost x=4
	if s, c := f.marks(idx_file); !s || !c {
		t.Fatal("test setup: the lost gesture should have left marks")
	}
	f.start_gesture(t)
	f.assert_no_marks(t, "after the next gesture started")
}

// The current gesture's marks must survive until its own Move consumes them.
func TestXDND435MoveDeletesEverythingItsOwnGestureTransferred(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.start_gesture(t)
	f.target_drops(t, 2)
	for _, i := range []int{idx_file, idx_tree, idx_empty, idx_link} {
		f.serve_complete(t, i)
	}
	f.event(t, 4, 0, 2)
	f.assert_all_sent_removed(t)
}

// Control: a refusal keeps the sources offered, and the next Move gesture deletes
// exactly what it transfers itself.
func TestXDND435RefusalThenMoveDeletesOnlyTheSecondGesturesItems(t *testing.T) {
	f := new_finish_fixture(t, "drag-finish")
	f.d.allow_drops = true
	f.start_gesture(t)
	f.target_drops(t, 2)
	for _, i := range []int{idx_file, idx_tree, idx_empty, idx_link} {
		f.serve_complete(t, i)
	}
	f.event(t, 4, 0, 0) // explicit refusal
	if !loop_keep_going(f.d.lp) {
		t.Fatal("a refusal ended the kitten as a successful drag-finish")
	}
	f.assert_nothing_removed(t)

	f.start_gesture(t)
	f.target_drops(t, 2)
	f.serve_complete(t, idx_empty)
	f.event(t, 4, 0, 2)
	if f.exists("empty") {
		t.Error("the directory that the Move transferred was not removed")
	}
	for _, name := range []string{"file.txt", "tree", "link", "unsent.txt"} {
		if !f.exists(name) {
			t.Errorf("%s was not transferred by the Move gesture but was deleted", name)
		}
	}
	if loop_keep_going(f.d.lp) {
		t.Error("a completed Move did not end the kitten with --exit-on drag-finish")
	}
}

// Partial and fully queued transfers within one gesture.
func TestXDND435MoveKeepsATransferThatHadNotFinished(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.start_gesture(t)
	f.target_drops(t, 2)
	f.serve_complete(t, idx_file)
	f.serve_started(t, idx_tree) // the tree's items are still waiting for write completions
	if _, c := f.marks(idx_tree); c {
		t.Fatal("test setup: the tree transfer must still be incomplete")
	}
	f.event(t, 4, 0, 2)
	if !f.exists("file.txt") {
		t.Error("an incomplete gesture deleted a fully transferred source")
	}
	if !f.exists("tree/sub/f") || !f.exists("tree") {
		t.Error("a source whose transfer had not finished was deleted")
	}
}

func TestXDND435MoveCountsARequestWhoseLastWriteIsStillPending(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.start_gesture(t)
	f.target_drops(t, 2)
	f.serve_started(t, idx_link) // a symlink queues its only message at once
	if _, c := f.marks(idx_link); c {
		t.Fatal("test setup: the write completion must not have been delivered")
	}
	f.event(t, 4, 0, 2)
	if !f.exists("link") {
		t.Fatal("the final write was not confirmed before deletion")
	}
	id := f.d.drag_status.remote_item_write_id
	if err := f.d.on_send_done(id); err != nil {
		t.Fatal(err)
	}
	if f.exists("link") {
		t.Error("the confirmed write did not complete the Move")
	}
}

// Legacy: a terminal with no "o" in its completion report.
func TestXDND435UnreportedOperationAfterAcceptedMoveStillCompletesTheMove(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.start_gesture(t)
	f.target_drops(t, 2)
	f.serve_complete(t, idx_link)
	f.event(t, 4, 0, operation_not_reported)
	if f.exists("link") {
		t.Error("the legacy completion did not delete the transferred source")
	}
	if !f.exists("file.txt") {
		t.Error("an untransferred source was deleted")
	}
}

// A Status report (x=2) with no operation is "no action", never the unreported marker.
func TestXDND435StatusWithoutOperationAcceptsNothing(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.start_gesture(t)
	f.event(t, 2, 0, 2)
	f.event(t, 2, 0, operation_not_reported)
	if got := f.d.drag_status.accepted_operation; got != 0 {
		t.Errorf("accepted_operation = %d after a status without o, want 0", got)
	}
}
