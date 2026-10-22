// License: GPLv3

package dnd

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/kovidgoyal/kitty/tools/tui/loop"
)

func newRemoteDragCompletionTest(t *testing.T) (*dnd, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "source.txt")
	if err := os.WriteFile(path, []byte("source contents"), 0600); err != nil {
		t.Fatal(err)
	}
	metadata, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	lp, err := loop.New()
	if err != nil {
		t.Fatal(err)
	}
	return &dnd{
		opts: &Options{ExitOn: "drag-finish"},
		lp:   lp,
		drag_sources: map[string]*drag_source{
			"text/uri-list": {uri_list: []uri_list_item{{path: path, metadata: metadata}}},
		},
		allow_drags: true,
		allow_drops: true,
		drag_status: drag_status{accepted_operation: move_on_drop, dropped: true},
	}, path
}

func requestAndCompleteRemoteURITransfer(t *testing.T, d *dnd) {
	t.Helper()
	if err := d.on_drag_remote_data_request(0); err != nil {
		t.Fatalf("on_drag_remote_data_request: %v", err)
	}
	item := &d.drag_sources["text/uri-list"].uri_list[0]
	if item.transfer_completed {
		t.Fatal("URI transfer completed before its queued writes")
	}
	for i := 0; i < 4 && d.drag_status.remote_item_write_id != 0; i++ {
		id := d.drag_status.remote_item_write_id
		if err := d.on_send_done(id); err != nil {
			t.Fatalf("on_send_done: %v", err)
		}
	}
	if d.drag_status.remote_item_write_id != 0 {
		t.Fatal("URI transfer did not drain its write completions")
	}
	if !item.transfer_completed {
		t.Fatal("URI transfer was not marked complete after its final write")
	}
}

func assertSourceExists(t *testing.T, path string) {
	t.Helper()
	if _, err := os.Lstat(path); err != nil {
		t.Fatalf("source path was not retained: %v", err)
	}
}

func TestNewDragClearsPriorURITransferState(t *testing.T) {
	d, _ := newRemoteDragCompletionTest(t)
	item := &d.drag_sources["text/uri-list"].uri_list[0]
	item.was_requested = true
	item.transfer_completed = true
	d.reset_uri_transfer_tracking()
	if item.was_requested || item.transfer_completed {
		t.Fatal("new drag retained URI transfer state from an earlier drag")
	}
}

func TestRemoteMoveRetainsSourceUntilURITransferCompletes(t *testing.T) {
	d, path := newRemoteDragCompletionTest(t)
	d.lp.Quit(17)
	if err := d.on_drag_remote_data_request(0); err != nil {
		t.Fatalf("on_drag_remote_data_request: %v", err)
	}
	if err := d.on_drag_finished(0, move_on_drop); err != nil {
		t.Fatalf("on_drag_finished: %v", err)
	}
	assertSourceExists(t, path)
	if got := d.lp.ExitCode(); got != 17 {
		t.Fatalf("incomplete transfer triggered drag-finish exit: got %d", got)
	}
}

func TestCanceledRemoteMoveRetainsCompletedSource(t *testing.T) {
	d, path := newRemoteDragCompletionTest(t)
	requestAndCompleteRemoteURITransfer(t, d)
	d.lp.Quit(17)
	if err := d.on_drag_finished(1, move_on_drop); err != nil {
		t.Fatalf("on_drag_finished: %v", err)
	}
	assertSourceExists(t, path)
	if got := d.lp.ExitCode(); got != 17 {
		t.Fatalf("canceled drag triggered drag-finish exit: got %d", got)
	}
}

func TestRefusedRemoteDropRetainsCompletedSource(t *testing.T) {
	d, path := newRemoteDragCompletionTest(t)
	requestAndCompleteRemoteURITransfer(t, d)
	d.lp.Quit(17)
	if err := d.on_drag_finished(0, 0); err != nil {
		t.Fatalf("on_drag_finished: %v", err)
	}
	assertSourceExists(t, path)
	if got := d.lp.ExitCode(); got != 17 {
		t.Fatalf("refused drag triggered drag-finish exit: got %d", got)
	}
}

func TestFinalCopyDoesNotRemoveSourceAndCanFinish(t *testing.T) {
	d, path := newRemoteDragCompletionTest(t)
	requestAndCompleteRemoteURITransfer(t, d)
	d.lp.Quit(17)
	if err := d.on_drag_finished(0, copy_on_drop); err != nil {
		t.Fatalf("on_drag_finished: %v", err)
	}
	assertSourceExists(t, path)
	if got := d.lp.ExitCode(); got != 0 {
		t.Fatalf("successful copy did not trigger drag-finish exit: got %d", got)
	}
	if d.drag_sources == nil || !d.allow_drags {
		t.Fatal("successful copy unexpectedly stopped offering the source")
	}
}

func TestCompletedRemoteMoveRemovesRequestedSource(t *testing.T) {
	d, path := newRemoteDragCompletionTest(t)
	requestAndCompleteRemoteURITransfer(t, d)
	if err := d.on_drag_finished(0, move_on_drop); err != nil {
		t.Fatalf("on_drag_finished: %v", err)
	}
	if _, err := os.Lstat(path); !os.IsNotExist(err) {
		t.Fatalf("completed move did not remove requested source: %v", err)
	}
	if d.drag_sources != nil || d.allow_drags {
		t.Fatal("completed move did not stop offering the source")
	}
}

// queueRemoteURITransferUntilFinalWrite requests the only URI entry and
// completes every write except the one for the last terminator, returning that
// write's id. Tests then choose whether the terminal's finish event or the
// writer's completion callback is handled first.
func queueRemoteURITransferUntilFinalWrite(t *testing.T, d *dnd) loop.IdType {
	t.Helper()
	if err := d.on_drag_remote_data_request(0); err != nil {
		t.Fatalf("on_drag_remote_data_request: %v", err)
	}
	for i := 0; i < 8; i++ {
		if d.only_final_write_pending() {
			return d.drag_status.remote_item_write_id
		}
		id := d.drag_status.remote_item_write_id
		if id == 0 {
			t.Fatal("URI transfer stopped writing before its final terminator")
		}
		if err := d.on_send_done(id); err != nil {
			t.Fatalf("on_send_done: %v", err)
		}
	}
	t.Fatal("URI transfer never reached its final terminator write")
	return 0
}

func assertSourceRemoved(t *testing.T, path string) {
	t.Helper()
	if _, err := os.Lstat(path); !os.IsNotExist(err) {
		t.Fatalf("source path was not removed: %v", err)
	}
}

func TestMoveFinishBeforeFinalWriteDoneWaitsAndThenRemovesSource(t *testing.T) {
	d, path := newRemoteDragCompletionTest(t)
	id := queueRemoteURITransferUntilFinalWrite(t, d)
	d.lp.Quit(17)
	item := &d.drag_sources["text/uri-list"].uri_list[0]
	if err := d.on_drag_finished(0, move_on_drop); err != nil {
		t.Fatalf("on_drag_finished: %v", err)
	}
	if d.drag_status.deferred_finish == nil || !d.drag_status.dropped {
		t.Fatal("successful finish before the final write completion was not held")
	}
	if item.transfer_completed {
		t.Fatal("transfer marked complete before the write completion callback")
	}
	assertSourceExists(t, path)
	if got := d.lp.ExitCode(); got != 17 {
		t.Fatalf("held finish decided early: exit %d", got)
	}
	if err := d.on_send_done(id); err != nil {
		t.Fatalf("on_send_done: %v", err)
	}
	assertSourceRemoved(t, path)
	if d.drag_status.deferred_finish != nil || d.drag_sources != nil || d.allow_drags {
		t.Fatal("held finish was not fully resolved by the write completion")
	}
	if got := d.lp.ExitCode(); got != 0 {
		t.Fatalf("resolved move did not trigger drag-finish exit: got %d", got)
	}
}

func TestMoveFinishAfterFinalWriteDoneRemovesSourceAsBefore(t *testing.T) {
	d, path := newRemoteDragCompletionTest(t)
	id := queueRemoteURITransferUntilFinalWrite(t, d)
	if err := d.on_send_done(id); err != nil {
		t.Fatalf("on_send_done: %v", err)
	}
	if err := d.on_drag_finished(0, move_on_drop); err != nil {
		t.Fatalf("on_drag_finished: %v", err)
	}
	if d.drag_status.deferred_finish != nil {
		t.Fatal("finish after the write completion was held")
	}
	assertSourceRemoved(t, path)
}

func TestCopyFinishBeforeFinalWriteDoneWaitsThenFinishesAndKeepsSource(t *testing.T) {
	d, path := newRemoteDragCompletionTest(t)
	id := queueRemoteURITransferUntilFinalWrite(t, d)
	d.lp.Quit(17)
	if err := d.on_drag_finished(0, copy_on_drop); err != nil {
		t.Fatalf("on_drag_finished: %v", err)
	}
	if got := d.lp.ExitCode(); got != 17 {
		t.Fatalf("held copy decided early: exit %d", got)
	}
	if err := d.on_send_done(id); err != nil {
		t.Fatalf("on_send_done: %v", err)
	}
	assertSourceExists(t, path)
	if got := d.lp.ExitCode(); got != 0 {
		t.Fatalf("resolved copy did not trigger drag-finish exit: got %d", got)
	}
	if d.drag_sources == nil || !d.allow_drags {
		t.Fatal("resolved copy stopped offering the source")
	}
}

func TestCanceledOrRefusedFinishBeforeFinalWriteDoneIsNeverHeldOrMoved(t *testing.T) {
	for _, tc := range []struct{ canceled, operation int }{{1, move_on_drop}, {0, 0}, {1, 0}} {
		d, path := newRemoteDragCompletionTest(t)
		id := queueRemoteURITransferUntilFinalWrite(t, d)
		d.lp.Quit(17)
		if err := d.on_drag_finished(tc.canceled, tc.operation); err != nil {
			t.Fatalf("on_drag_finished(%d, %d): %v", tc.canceled, tc.operation, err)
		}
		if d.drag_status.deferred_finish != nil {
			t.Fatalf("finish (%d, %d) that cannot succeed was held", tc.canceled, tc.operation)
		}
		if err := d.on_send_done(id); err != nil {
			t.Fatalf("late on_send_done: %v", err)
		}
		assertSourceExists(t, path)
		if got := d.lp.ExitCode(); got != 17 {
			t.Fatalf("finish (%d, %d) triggered drag-finish exit: got %d", tc.canceled, tc.operation, got)
		}
	}
}

func TestSuccessfulFinishDuringStreamingIsNotHeldAndKeepsSource(t *testing.T) {
	d, path := newRemoteDragCompletionTest(t)
	d.lp.Quit(17)
	if err := d.on_drag_remote_data_request(0); err != nil {
		t.Fatalf("on_drag_remote_data_request: %v", err)
	}
	if d.only_final_write_pending() {
		t.Fatal("test needs a transfer that is still streaming file data")
	}
	if err := d.on_drag_finished(0, move_on_drop); err != nil {
		t.Fatalf("on_drag_finished: %v", err)
	}
	if d.drag_status.deferred_finish != nil {
		t.Fatal("a finish while file data was still being queued was held")
	}
	assertSourceExists(t, path)
	if got := d.lp.ExitCode(); got != 17 {
		t.Fatalf("early move finish triggered drag-finish exit: got %d", got)
	}
}

func TestResetDragDiscardsAHeldFinish(t *testing.T) {
	d, path := newRemoteDragCompletionTest(t)
	id := queueRemoteURITransferUntilFinalWrite(t, d)
	if err := d.on_drag_finished(0, move_on_drop); err != nil {
		t.Fatalf("on_drag_finished: %v", err)
	}
	d.finish_drag("EIO")
	if d.drag_status.deferred_finish != nil {
		t.Fatal("reset left a held finish behind")
	}
	if err := d.on_send_done(id); err != nil {
		t.Fatalf("late on_send_done: %v", err)
	}
	assertSourceExists(t, path)
}

func TestFinishWhileALaterRequestIsStillQueuedIsNotHeld(t *testing.T) {
	d, first := newRemoteDragCompletionTest(t)
	second := filepath.Join(filepath.Dir(first), "second.txt")
	if err := os.WriteFile(second, []byte("second contents"), 0600); err != nil {
		t.Fatal(err)
	}
	metadata, err := os.Lstat(second)
	if err != nil {
		t.Fatal(err)
	}
	ds := d.drag_sources["text/uri-list"]
	ds.uri_list = append(ds.uri_list, uri_list_item{path: second, metadata: metadata})
	for _, idx := range []int{0, 1} {
		if err := d.on_drag_remote_data_request(idx); err != nil {
			t.Fatalf("on_drag_remote_data_request(%d): %v", idx, err)
		}
	}
	for i := 0; i < 8; i++ {
		id := d.drag_status.remote_item_write_id
		if id == 0 {
			t.Fatal("first URI transfer stopped writing before its terminator")
		}
		if d.drag_status.current_remote_file == nil && len(d.drag_status.remote_items) == 0 {
			break
		}
		if err := d.on_send_done(id); err != nil {
			t.Fatalf("on_send_done: %v", err)
		}
	}
	if d.drag_status.current_remote_file != nil || len(d.drag_status.remote_data_requests) != 2 {
		t.Fatal("test needs the first terminator pending with the second request still queued")
	}
	d.lp.Quit(17)
	if err := d.on_drag_finished(0, move_on_drop); err != nil {
		t.Fatalf("on_drag_finished: %v", err)
	}
	if d.drag_status.deferred_finish != nil {
		t.Fatal("a finish with a later URI request still queued was held")
	}
	assertSourceExists(t, first)
	assertSourceExists(t, second)
	if got := d.lp.ExitCode(); got != 17 {
		t.Fatalf("early move finish triggered drag-finish exit: got %d", got)
	}
}

func TestFinishWhileDirectoryChildrenAreQueuedIsNotHeld(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "tree")
	if err := os.Mkdir(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "child.txt"), []byte("child contents"), 0600); err != nil {
		t.Fatal(err)
	}
	metadata, err := os.Lstat(dir)
	if err != nil {
		t.Fatal(err)
	}
	lp, err := loop.New()
	if err != nil {
		t.Fatal(err)
	}
	d := &dnd{
		opts: &Options{ExitOn: "drag-finish"},
		lp:   lp,
		drag_sources: map[string]*drag_source{
			"text/uri-list": {uri_list: []uri_list_item{{path: dir, metadata: metadata}}},
		},
		allow_drags: true,
		allow_drops: true,
		drag_status: drag_status{accepted_operation: move_on_drop, dropped: true},
	}
	if err := d.on_drag_remote_data_request(0); err != nil {
		t.Fatalf("on_drag_remote_data_request: %v", err)
	}
	if d.drag_status.remote_item_write_id == 0 || len(d.drag_status.remote_items) != 1 {
		t.Fatal("test needs the directory listing terminator pending with its child still queued")
	}
	d.lp.Quit(17)
	if err := d.on_drag_finished(0, move_on_drop); err != nil {
		t.Fatalf("on_drag_finished: %v", err)
	}
	if d.drag_status.deferred_finish != nil {
		t.Fatal("a finish with directory children still queued was held")
	}
	if _, err := os.Lstat(dir); err != nil {
		t.Fatalf("directory source was not retained: %v", err)
	}
	if got := d.lp.ExitCode(); got != 17 {
		t.Fatalf("early move finish triggered drag-finish exit: got %d", got)
	}
}

func TestFinishForADragThatWasNeverDroppedIsNeverHeld(t *testing.T) {
	d, path := newRemoteDragCompletionTest(t)
	id := queueRemoteURITransferUntilFinalWrite(t, d)
	d.drag_status.dropped = false
	d.lp.Quit(17)
	if err := d.on_drag_finished(0, move_on_drop); err != nil {
		t.Fatalf("on_drag_finished: %v", err)
	}
	if d.drag_status.deferred_finish != nil {
		t.Fatal("a finish for a drag that was never dropped was held")
	}
	if err := d.on_send_done(id); err != nil {
		t.Fatalf("late on_send_done: %v", err)
	}
	assertSourceExists(t, path)
	if got := d.lp.ExitCode(); got != 17 {
		t.Fatalf("undropped finish triggered drag-finish exit: got %d", got)
	}
}
