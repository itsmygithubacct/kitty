package dnd

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"unsafe"

	"github.com/kovidgoyal/kitty/tools/tui/loop"
)

// loop.Loop has no exported way to ask whether Quit() was called, so read and
// reset its private keep_going flag.
func loop_keep_going(lp *loop.Loop) bool {
	return reflect.ValueOf(lp).Elem().FieldByName("keep_going").Bool()
}

func set_loop_keep_going(lp *loop.Loop, v bool) {
	f := reflect.ValueOf(lp).Elem().FieldByName("keep_going")
	reflect.NewAt(f.Type(), unsafe.Pointer(f.UnsafeAddr())).Elem().SetBool(v)
}

// Rendering wraps paragraphs to the screen width, which is zero without a tty.
func set_loop_screen_size(lp *loop.Loop, width, height uint) {
	f := reflect.ValueOf(lp).Elem().FieldByName("screen_size")
	sz := reflect.NewAt(f.Type(), unsafe.Pointer(f.UnsafeAddr())).Elem()
	sz.FieldByName("WidthCells").SetUint(uint64(width))
	sz.FieldByName("HeightCells").SetUint(uint64(height))
	upd := sz.FieldByName("updated")
	reflect.NewAt(upd.Type(), unsafe.Pointer(upd.UnsafeAddr())).Elem().SetBool(true)
}

type finish_fixture struct {
	d     *dnd
	dir   string
	items []uri_list_item
}

// new_finish_fixture creates a dnd with five uri-list sources on disk covering
// every kind the kitten can send to a remote client: a file, a directory tree,
// an empty directory, a symlink and a never-requested file.
func new_finish_fixture(t *testing.T, exit_on string) *finish_fixture {
	t.Helper()
	dir := t.TempDir()
	buildTree(t, dir, map[string]string{"file.txt": "data", "tree/sub/f": "x", "unsent.txt": "keep"}, map[string]string{"link": "file.txt"})
	if err := os.Mkdir(filepath.Join(dir, "empty"), 0o755); err != nil {
		t.Fatal(err)
	}
	var items []uri_list_item
	for _, name := range []string{"file.txt", "tree", "empty", "link", "unsent.txt"} {
		p := filepath.Join(dir, name)
		st, err := os.Lstat(p)
		if err != nil {
			t.Fatal(err)
		}
		items = append(items, uri_list_item{path: p, human_name: name, metadata: st})
	}
	lp, err := loop.New()
	if err != nil {
		t.Fatal(err)
	}
	set_loop_keep_going(lp, true)
	set_loop_screen_size(lp, 80, 24)
	d := &dnd{
		opts: &Options{ExitOn: exit_on}, lp: lp, in_test_mode: true,
		drag_sources: map[string]*drag_source{"text/uri-list": {mime_type: "text/uri-list", uri_list: items}},
		allow_drags:  true,
	}
	return &finish_fixture{d: d, dir: dir, items: items}
}

// sent marks the first four items as sent and completely transferred, which is
// the state a remote client's data requests leave behind.
func (f *finish_fixture) sent() {
	ds := f.d.drag_sources["text/uri-list"]
	for i := 0; i < 4; i++ {
		ds.uri_list[i].was_requested = true
		mark_transfer_complete(&ds.uri_list[i], true)
	}
	ds.uri_list[4].was_requested = false
}

func (f *finish_fixture) exists(name string) bool {
	_, err := os.Lstat(filepath.Join(f.dir, name))
	return err == nil
}

func (f *finish_fixture) assert_all_sent_removed(t *testing.T) {
	t.Helper()
	for _, name := range []string{"file.txt", "tree", "empty", "link"} {
		if f.exists(name) {
			t.Errorf("%s was sent and the Move completed but the source still exists", name)
		}
	}
	if !f.exists("unsent.txt") {
		t.Error("a source that was never requested by the target was deleted")
	}
	if f.d.drag_sources != nil || f.d.allow_drags {
		t.Error("a completed Move should stop offering the dragged sources")
	}
}

func (f *finish_fixture) assert_nothing_removed(t *testing.T) {
	t.Helper()
	for _, name := range []string{"file.txt", "tree", "empty", "link", "unsent.txt"} {
		if !f.exists(name) {
			t.Errorf("%s was deleted although the drag did not complete as a Move", name)
		}
	}
	if f.d.drag_sources == nil || !f.d.allow_drags {
		t.Error("the sources must stay offered unless a Move completed")
	}
}

// Terminal reports x=4:y=0:o=2 after a dropped Move: every source of every kind
// that was fully sent is removed, whichever path sent it.
func TestXDND63CompletedMoveDeletesEverySentSourceKind(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.sent()
	f.d.drag_status = drag_status{active: true, dropped: true, accepted_operation: 2}
	if err := f.d.on_drag_event(4, 0, 2); err != nil {
		t.Fatal(err)
	}
	f.assert_all_sent_removed(t)
}

// A terminal that sends no "o" at all in its completion report (the base
// protocol's t=e:x=4:y=0) leaves the operation unreported. The operation the
// target last accepted (x=2) then decides. An explicit o=0 is not this case.
func TestXDND63CompletedMoveWithUnreportedOperationUsesAcceptedOperation(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.sent()
	f.d.drag_status = drag_status{active: true, dropped: true, accepted_operation: 2}
	if err := f.d.on_drag_event(4, 0, operation_not_reported); err != nil {
		t.Fatal(err)
	}
	f.assert_all_sent_removed(t)
}

// The last write-completion event of a request may be delivered after the
// terminal's finish report. Nothing is left to queue, so the request counts.
func TestXDND63CompletedMoveCountsRequestWithEverythingQueued(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.sent()
	ds := f.d.drag_sources["text/uri-list"]
	mark_transfer_complete(&ds.uri_list[3], false) // symlink: its only message is queued, write-done not yet delivered
	f.d.drag_status = drag_status{active: true, dropped: true, accepted_operation: 2, remote_data_requests: []int{3}, remote_item_write_id: 7}
	if err := f.d.on_drag_event(4, 0, 2); err != nil {
		t.Fatal(err)
	}
	f.assert_nothing_removed(t)
	if f.d.drag_status.deferred_finish == nil {
		t.Fatal("the final queued write must hold the finish")
	}
	if err := f.d.on_send_done(7); err != nil {
		t.Fatal(err)
	}
	f.assert_all_sent_removed(t)
}

// A request that still has items to send is not complete and must not be deleted.
func TestXDND63CompletedMoveKeepsPartiallySentSource(t *testing.T) {
	f := new_finish_fixture(t, "esc-key")
	f.sent()
	ds := f.d.drag_sources["text/uri-list"]
	mark_transfer_complete(&ds.uri_list[1], false)
	f.d.drag_status = drag_status{
		active: true, dropped: true, accepted_operation: 2, remote_data_requests: []int{1},
		remote_items: []*remote_data_item{{path: filepath.Join(f.dir, "tree", "sub", "f"), idx_in_uri_list: 1}},
	}
	if err := f.d.on_drag_event(4, 0, 2); err != nil {
		t.Fatal(err)
	}
	if !f.exists("tree/sub/f") || !f.exists("tree") {
		t.Error("a source whose transfer had unsent items was deleted")
	}
	if !f.exists("file.txt") {
		t.Error("an incomplete gesture deleted a fully transferred source")
	}
}

func TestXDND63NoDeletionUnlessDroppedAndNotCanceledAndMove(t *testing.T) {
	cases := []struct {
		name                     string
		dropped                  bool
		accepted_operation, y, o int
	}{
		{"canceled after drop", true, 2, 1, 2},
		{"never dropped", false, 2, 0, 2},
		{"final operation is copy", true, 2, 0, 1},
		{"accepted copy, unreported final", true, 1, 0, operation_not_reported},
		{"nothing accepted, unreported final", true, 0, 0, operation_not_reported},
		{"nothing accepted, explicit refusal", true, 0, 0, 0},
		{"accepted move, explicit refusal", true, 2, 0, 0},
		{"accepted copy, explicit refusal", true, 1, 0, 0},
		{"canceled, explicit refusal", true, 2, 1, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := new_finish_fixture(t, "esc-key")
			f.sent()
			f.d.drag_status = drag_status{active: true, dropped: c.dropped, accepted_operation: c.accepted_operation}
			if err := f.d.on_drag_event(4, c.y, c.o); err != nil {
				t.Fatal(err)
			}
			f.assert_nothing_removed(t)
		})
	}
}

// --exit-on drag-finish: "successful completion of a drag", copy or move.
func TestXDND63ExitOnDragFinishAfterSuccessfulCopy(t *testing.T) {
	for _, o := range []int{1, operation_not_reported} { // reported copy, and unreported with accepted copy
		f := new_finish_fixture(t, "drag-finish")
		f.d.drag_status = drag_status{active: true, dropped: true, accepted_operation: 1}
		if err := f.d.on_drag_event(4, 0, o); err != nil {
			t.Fatal(err)
		}
		if loop_keep_going(f.d.lp) {
			t.Errorf("o=%d: a successful Copy did not exit although --exit-on drag-finish was given", o)
		}
		f.assert_nothing_removed(t)
	}
}

func TestXDND63ExitOnDragFinishAfterSuccessfulMove(t *testing.T) {
	f := new_finish_fixture(t, "drag-finish")
	f.sent()
	f.d.allow_drops = true // otherwise a completed Move quits by itself
	f.d.drag_status = drag_status{active: true, dropped: true, accepted_operation: 2}
	if err := f.d.on_drag_event(4, 0, 2); err != nil {
		t.Fatal(err)
	}
	if loop_keep_going(f.d.lp) {
		t.Error("a successful Move did not exit although --exit-on drag-finish was given")
	}
	f.assert_all_sent_removed(t)
}

func TestXDND63NoExitWithoutDragFinishOrWithoutSuccess(t *testing.T) {
	cases := []struct {
		name    string
		exit_on string
		dropped bool
		y       int
	}{
		{"drag-finish not requested", "esc-key", true, 0},
		{"canceled", "drag-finish", true, 1},
		{"never dropped", "drag-finish", false, 0},
		{"explicit refusal", "drag-finish", true, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := new_finish_fixture(t, c.exit_on)
			o := 1
			if c.name == "explicit refusal" {
				o = 0
			}
			f.d.drag_status = drag_status{active: true, dropped: c.dropped, accepted_operation: 1}
			if err := f.d.on_drag_event(4, c.y, o); err != nil {
				t.Fatal(err)
			}
			if !loop_keep_going(f.d.lp) {
				t.Error("the kitten exited although the drag did not complete successfully with --exit-on drag-finish")
			}
		})
	}
}
