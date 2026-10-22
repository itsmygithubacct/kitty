package themes

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"unsafe"

	"github.com/kovidgoyal/kitty/tools/themes"
	"github.com/kovidgoyal/kitty/tools/tui/loop"
)

// loop.Loop only exposes its queued terminal output to the loop itself, so read
// the private queue and flags the way the loop would.
func pending_output(lp *loop.Loop) string {
	pw := reflect.ValueOf(lp).Elem().FieldByName("pending_writes")
	var sb strings.Builder
	for i := range pw.Len() {
		e := pw.Index(i)
		sb.WriteString(e.FieldByName("str").String())
		sb.Write(e.FieldByName("bytes").Bytes())
	}
	return sb.String()
}

func set_private(v reflect.Value) reflect.Value {
	return reflect.NewAt(v.Type(), unsafe.Pointer(v.UnsafeAddr())).Elem()
}

func new_accepting_handler(t *testing.T) *handler {
	t.Helper()
	path := filepath.Join(t.TempDir(), "xdnd-test-theme.conf")
	conf := "## name: XDND Test Theme\n## author: test\n## license: MIT\n## blurb: fixture\nforeground #eeeeee\nbackground #111111\n"
	if err := os.WriteFile(path, []byte(conf), 0o600); err != nil {
		t.Fatal(err)
	}
	all := (&themes.Themes{}).Copy()
	if _, err := all.AddFromFile(path); err != nil {
		t.Fatal(err)
	}
	all = all.Filtered(func(*themes.Theme) bool { return true })
	if all.Len() != 1 {
		t.Fatalf("expected one fixture theme, got %d", all.Len())
	}
	lp, err := loop.New()
	if err != nil {
		t.Fatal(err)
	}
	lpv := reflect.ValueOf(lp).Elem()
	set_private(lpv.FieldByName("keep_going")).SetBool(true)
	sz := set_private(lpv.FieldByName("screen_size"))
	sz.FieldByName("WidthCells").SetUint(80)
	sz.FieldByName("HeightCells").SetUint(24)
	set_private(sz.FieldByName("updated")).SetBool(true)
	h := &handler{lp: lp, opts: &Options{ConfigFileName: "kitty.conf"}, cached_data: &CachedData{Category: "All"}, state: ACCEPTING, themes_list: &ThemesList{}}
	h.themes_list.UpdateThemes(all)
	return h
}

func loop_is_running(lp *loop.Loop) bool {
	return reflect.ValueOf(lp).Elem().FieldByName("keep_going").Bool()
}

// A failed save keeps the picker open. The error must be shown by redrawing the
// whole screen, not by printing the menu a second time below the first one.
func TestXDND61FailedThemeSaveRedrawsTheWholeScreenWithTheError(t *testing.T) {
	h := new_accepting_handler(t)
	h.draw_screen()
	ev := &loop.KeyEvent{}
	if err := h.finish_theme_save(ev, errors.New("disk full")); err != nil {
		t.Fatal(err)
	}
	if !ev.Handled {
		t.Error("the key event that triggered the save must be consumed")
	}
	if !loop_is_running(h.lp) {
		t.Error("a failed save must not quit the picker")
	}
	out := pending_output(h.lp)
	const clear = "\x1b[H\x1b[2J"
	last := strings.LastIndex(out, clear)
	if last < 0 || strings.Count(out, clear) != 2 {
		t.Fatalf("expected the first and the failure redraw to each clear the screen, found %d clears", strings.Count(out, clear))
	}
	redraw := out[last:]
	if n := strings.Count(redraw, "You have chosen the"); n != 1 {
		t.Errorf("after the last screen clear the menu appears %d times, want exactly 1", n)
	}
	if !strings.Contains(redraw, "Could not save theme: disk full") {
		t.Errorf("the save error is not shown in the redraw: %q", redraw)
	}
	if strings.Contains(out[:last], "Could not save theme") {
		t.Error("the error appeared before the failure")
	}
}

func TestXDND61SuccessfulThemeSaveQuitsAndClearsTheError(t *testing.T) {
	h := new_accepting_handler(t)
	h.save_error = "earlier failure"
	ev := &loop.KeyEvent{}
	if err := h.finish_theme_save(ev, nil); err != nil {
		t.Fatal(err)
	}
	if loop_is_running(h.lp) {
		t.Error("a successful save must quit the picker")
	}
	if h.save_error != "" {
		t.Errorf("stale error kept: %q", h.save_error)
	}
	if !ev.Handled {
		t.Error("the key event must be consumed")
	}
}
