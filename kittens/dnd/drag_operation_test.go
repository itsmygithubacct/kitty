package dnd

import (
	"fmt"
	"math"
	"testing"

	"github.com/kovidgoyal/kitty/tools/tui/loop"
)

// These tests are about the "o" of the terminal's t=e messages as it reaches
// on_drag_event. The kitten has to tell a message with no "o" at all (a terminal
// without the completion report) from one that reports a value, and a reported
// value that is not 0, 1 or 2 is a bogus report that must be read as a refusal.
// In particular no value that the wire can carry may equal the internal
// "unreported" marker, or an accepted Move followed by a bogus explicit report
// would take the legacy fallback and delete the sources.

// wire_op is what loop.handle_dnd hands the kitten for a t=e message:
// tools/tui/loop TestDndOperationWireValues pins the decoder side of every row.
type wire_op struct {
	name string
	has  bool
	op   int
}

func (w wire_op) command(x, y int) loop.DndCommand {
	return loop.DndCommand{Type: 'e', X: x, Y: y, Has_operation: w.has, Operation: w.op}
}

var wire_ops = []wire_op{
	{"absent", false, 0},
	{"absent with stale value", false, 2}, // a decoder never does this; the presence bit alone decides
	{"o=0", true, 0},
	{"o=1", true, 1},
	{"o=2", true, 2},
	{"o=-1", true, -1},
	{"o=-2", true, -2},
	{"o=3", true, 3},
	{"o=4", true, 4},
	{"o=99", true, 99},
	{"o=MaxInt32", true, math.MaxInt32},
	{"o=MaxInt32+1", true, math.MaxInt32 + 1},
	{"o=MaxInt64", true, math.MaxInt64},
	{"o=MinInt64", true, math.MinInt64},
	{"o=MinInt64+1", true, math.MinInt64 + 1},
	{"o=unparsable or overflowing (decodes to 0)", true, 0},
}

// expected_operation is an independent statement of the contract: the operation
// a message reports, with absent as -1 here (the test's own marker, not the
// kitten's).
func expected_operation(w wire_op) int {
	if !w.has {
		return -1
	}
	if w.op == 0 || w.op == 1 || w.op == 2 {
		return w.op
	}
	return 0
}

func TestXDND5ReportedOperationNeverEqualsTheUnreportedMarker(t *testing.T) {
	for _, w := range wire_ops {
		got := drag_event_operation(w.command(4, 0))
		if w.has && got == operation_not_reported {
			t.Errorf("%s: a reported operation reads as unreported", w.name)
		}
		if !w.has && got != operation_not_reported {
			t.Errorf("%s: a message with no o reads as %d, not unreported", w.name, got)
		}
		if want := expected_operation(w); w.has && got != want {
			t.Errorf("%s: operation %d, want %d", w.name, got, want)
		}
	}
	// the internal API too: no negative or out-of-range value other than the marker is the marker
	for _, v := range []int{-1, -2, math.MinInt + 1, 3, 99, math.MaxInt} {
		if got := reported_operation(v); got != 0 {
			t.Errorf("reported_operation(%d) = %d, want 0", v, got)
		}
	}
}

// The matrix: (accepted at x=2) x (completion report) x (canceled), each a
// production-path gesture in which the target read a file and a directory tree.
func TestXDND5OperationMatrixThroughTheProductionPath(t *testing.T) {
	cells := 0
	for _, accepted := range wire_ops {
		for _, final := range wire_ops {
			for _, canceled := range []int{0, 1} {
				cells++
				name := fmt.Sprintf("accepted[%s] final[%s] canceled=%d", accepted.name, final.name, canceled)
				f := new_finish_fixture(t, "drag-finish")
				f.start_gesture(t)
				f.event(t, 1, 0, 0)
				if err := f.d.on_drag_event(2, 0, drag_event_operation(accepted.command(2, 0))); err != nil {
					t.Fatal(err)
				}
				f.event(t, 3, 0, 0)
				f.serve_complete(t, idx_file)
				f.serve_complete(t, idx_tree)
				if err := f.d.on_drag_event(4, canceled, drag_event_operation(final.command(4, canceled))); err != nil {
					t.Fatal(err)
				}
				acc := max(expected_operation(accepted), 0)
				eff := expected_operation(final)
				if eff < 0 {
					eff = acc
				}
				move := canceled == 0 && eff == 2 && acc == 2
				completed := canceled == 0 && eff != 0
				if got := !f.exists("file.txt") && !f.exists("tree"); got != move {
					t.Errorf("%s: sources deleted=%v, want %v", name, got, move)
				}
				if !move && !(f.exists("file.txt") && f.exists("tree") && f.exists("link") && f.exists("empty") && f.exists("unsent.txt")) {
					t.Errorf("%s: a source was deleted by a gesture that is not a completed Move", name)
				}
				if exited := !loop_keep_going(f.d.lp); exited != completed {
					t.Errorf("%s: --exit-on drag-finish exited=%v, want %v", name, exited, completed)
				}
				if offered := f.d.drag_sources != nil; offered == move {
					t.Errorf("%s: offering sources=%v after a gesture with move=%v", name, offered, move)
				}
				if !move {
					f.assert_no_marks(t, name)
				}
			}
		}
	}
	if cells != len(wire_ops)*len(wire_ops)*2 {
		t.Fatalf("ran %d cells", cells)
	}
}

// RV4-1 as the reviewer found it: an accepted Move, the target read everything,
// then a completion that explicitly reports -1. It must behave as a refusal.
func TestXDND5ExplicitNegativeOperationAfterAcceptedMoveKeepsEverything(t *testing.T) {
	for _, o := range []int{-1, -2, math.MinInt, math.MinInt + 1} {
		f := new_finish_fixture(t, "drag-finish")
		f.start_gesture(t)
		f.target_drops(t, 2)
		for _, i := range []int{idx_file, idx_tree, idx_empty, idx_link} {
			f.serve_complete(t, i)
		}
		cmd := loop.DndCommand{Type: 'e', X: 4, Has_operation: true, Operation: o}
		if err := f.d.on_drag_event(cmd.X, cmd.Y, drag_event_operation(cmd)); err != nil {
			t.Fatal(err)
		}
		f.assert_nothing_removed(t)
		if !loop_keep_going(f.d.lp) {
			t.Errorf("o=%d ended the kitten as a successful drag-finish", o)
		}
		f.assert_no_marks(t, fmt.Sprintf("after an explicit o=%d", o))
	}
}

// A reviewer-style direct call: a bogus value given straight to on_drag_event
// (-1 among them) is a reported value, not the marker, and so a refusal.
func TestXDND5DirectBogusOperationIsARefusalNotTheUnreportedMarker(t *testing.T) {
	for _, o := range []int{-1, -2, math.MinInt + 1, 3, 4, 99, math.MaxInt} {
		f := new_finish_fixture(t, "drag-finish")
		f.start_gesture(t)
		f.target_drops(t, 2)
		f.serve_complete(t, idx_link)
		f.event(t, 4, 0, o)
		f.assert_nothing_removed(t)
		if !loop_keep_going(f.d.lp) {
			t.Errorf("o=%d passed directly to on_drag_event ended the kitten as a successful drag-finish", o)
		}
	}
}

// A bogus status (x=2) accepts nothing, so a legacy completion after it cannot move.
func TestXDND5BogusStatusOperationAcceptsNothing(t *testing.T) {
	for _, w := range wire_ops {
		if !w.has || w.op == 0 || w.op == 1 || w.op == 2 {
			continue
		}
		f := new_finish_fixture(t, "esc-key")
		f.start_gesture(t)
		if err := f.d.on_drag_event(2, 0, drag_event_operation(w.command(2, 0))); err != nil {
			t.Fatal(err)
		}
		if got := f.d.drag_status.accepted_operation; got != 0 {
			t.Errorf("%s: accepted_operation %d, want 0", w.name, got)
		}
		f.event(t, 3, 0, 0)
		f.serve_complete(t, idx_file)
		f.event(t, 4, 0, operation_not_reported)
		f.assert_nothing_removed(t)
	}
}

// RV4-2, pinned. A legacy terminal that never sent x=2:o (so nothing was ever
// accepted) and sends x=4:y=0 with no "o" after a drop: the action is unknown,
// which is not a completed Copy or Move. Nothing is deleted and
// --exit-on drag-finish does not fire. (r3 fired here: this is a documented
// change, see docs/dnd-protocol.rst.)
func TestXDND5LegacyCompletionWithNoOperationAtAllDoesNotExitOnDragFinish(t *testing.T) {
	f := new_finish_fixture(t, "drag-finish")
	f.start_gesture(t)
	f.event(t, 3, 0, 0)
	f.serve_complete(t, idx_file)
	f.event(t, 4, 0, operation_not_reported)
	if !loop_keep_going(f.d.lp) {
		t.Error("an unknown action ended the kitten as a successful drag-finish")
	}
	f.assert_nothing_removed(t)
	if f.d.drag_sources == nil {
		t.Error("the sources stopped being offered after a completion with unknown action")
	}
}

// ...while a legacy terminal that did report the accepted action in x=2 still
// completes: Copy exits without deleting, Move deletes.
func TestXDND5LegacyCompletionAfterAcceptedActionStillCompletes(t *testing.T) {
	for _, accepted := range []int{1, 2} {
		f := new_finish_fixture(t, "drag-finish")
		f.start_gesture(t)
		f.target_drops(t, accepted)
		f.serve_complete(t, idx_file)
		f.event(t, 4, 0, operation_not_reported)
		if loop_keep_going(f.d.lp) {
			t.Errorf("accepted=%d: a completed legacy drag did not exit", accepted)
		}
		if gone := !f.exists("file.txt"); gone != (accepted == 2) {
			t.Errorf("accepted=%d: file deleted=%v", accepted, gone)
		}
	}
}

// Explicit refusal and bogus reports do not end the kitten, keep the offer, clear
// the marks, and the next Move deletes exactly its own item.
func TestXDND5BogusCompletionThenMoveDeletesOnlyTheSecondGesturesItems(t *testing.T) {
	for _, final := range wire_ops {
		if !final.has || final.op == 1 || final.op == 2 {
			continue
		}
		f := new_finish_fixture(t, "drag-finish")
		f.d.allow_drops = true
		f.start_gesture(t)
		f.target_drops(t, 2)
		for _, i := range []int{idx_file, idx_tree, idx_empty, idx_link} {
			f.serve_complete(t, i)
		}
		f.event(t, 4, 0, drag_event_operation(final.command(4, 0)))
		if !loop_keep_going(f.d.lp) {
			t.Fatalf("%s: ended the kitten", final.name)
		}
		f.assert_nothing_removed(t)
		f.assert_no_marks(t, final.name)

		f.start_gesture(t)
		f.target_drops(t, 2)
		f.serve_complete(t, idx_empty)
		f.event(t, 4, 0, 2)
		if f.exists("empty") {
			t.Errorf("%s: the Move's own item was not removed", final.name)
		}
		for _, name := range []string{"file.txt", "tree", "link", "unsent.txt"} {
			if !f.exists(name) {
				t.Errorf("%s: %s was deleted although the Move did not transfer it", final.name, name)
			}
		}
	}
}

// Final-write ordering is unchanged: a request whose last write completion is
// still pending when the finish arrives counts for a valid Move, and a bogus
// report neither deletes it nor lets the late write completion delete it.
func TestXDND5FinalWritePendingWithBogusOperationDeletesNothingEvenAfterTheLateWrite(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.start_gesture(t)
	f.target_drops(t, 2)
	f.serve_started(t, idx_link)
	id := f.d.drag_status.remote_item_write_id
	f.event(t, 4, 0, drag_event_operation(loop.DndCommand{Type: 'e', X: 4, Has_operation: true, Operation: -1}))
	if err := f.d.on_send_done(id); err != nil {
		t.Fatal(err)
	}
	f.assert_nothing_removed(t)
	f.assert_no_marks(t, "after the late write completion")
}

func TestXDND5FinalWritePendingWithValidMoveStillDeletes(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.start_gesture(t)
	f.target_drops(t, 2)
	f.serve_started(t, idx_link)
	f.event(t, 4, 0, drag_event_operation(loop.DndCommand{Type: 'e', X: 4, Has_operation: true, Operation: 2}))
	if !f.exists("link") {
		t.Fatal("the symlink was deleted before its final write callback")
	}
	id := f.d.drag_status.remote_item_write_id
	if err := f.d.on_send_done(id); err != nil {
		t.Fatal(err)
	}
	if f.exists("link") {
		t.Error("the confirmed final write did not complete the Move")
	}
}

// Direct callers too: a bogus value on x=2 accepts nothing, and a legacy completion after it cannot move.
func TestXDND5DirectBogusStatusOperationAcceptsNothing(t *testing.T) {
	for _, o := range []int{-1, -2, 3, 99, math.MaxInt, math.MinInt + 1} {
		f := new_finish_fixture(t, "esc-key")
		f.start_gesture(t)
		f.event(t, 2, 0, o)
		if got := f.d.drag_status.accepted_operation; got != 0 {
			t.Errorf("x=2 with o=%d: accepted_operation %d, want 0", o, got)
		}
	}
}
