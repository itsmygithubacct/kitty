package choose_fonts

import (
	"errors"
	"testing"

	"github.com/kovidgoyal/kitty/tools/tui/loop"
)

func new_click_test_font_list() (*FontList, *handler) {
	h := &handler{}
	listing := &FontList{family_list: FamilyList{families: []string{"Alpha", "Beta"}}}
	listing.handler = h
	h.current_pane = listing
	return listing, h
}

func TestFontListFamilyClickUsesActivePaneAndSelectsValidatedFamily(t *testing.T) {
	listing, h := new_click_test_font_list()
	redraws, refusals := 0, 0
	if h.state != SCANNING_FAMILIES {
		t.Fatalf("test expected the handler's zero state, got %v", h.state)
	}
	err := listing.handle_click("family-chosen:Beta", func() error {
		redraws++
		return nil
	}, func() { refusals++ })
	if err != nil {
		t.Fatal(err)
	}
	if got := listing.family_list.CurrentFamily(); got != "Beta" {
		t.Fatalf("active family click selected %q, want Beta", got)
	}
	if redraws != 1 || refusals != 0 {
		t.Fatalf("active valid click effects: redraws=%d refusals=%d, want 1 and 0", redraws, refusals)
	}
}

func TestFontListFamilyClickRefusesUnknownFamily(t *testing.T) {
	listing, _ := new_click_test_font_list()
	redraws, refusals := 0, 0
	err := listing.handle_click("family-chosen:Missing", func() error {
		redraws++
		return nil
	}, func() { refusals++ })
	if err != nil {
		t.Fatal(err)
	}
	if got := listing.family_list.CurrentFamily(); got != "Alpha" {
		t.Fatalf("unknown family changed selection to %q", got)
	}
	if redraws != 0 || refusals != 1 {
		t.Fatalf("unknown family effects: redraws=%d refusals=%d, want 0 and 1", redraws, refusals)
	}
}

func TestFontListFamilyClickRejectsInactiveListingPane(t *testing.T) {
	listing, h := new_click_test_font_list()
	h.current_pane = &h.faces
	redraws, refusals := 0, 0
	err := listing.handle_click("family-chosen:Beta", func() error {
		redraws++
		return nil
	}, func() { refusals++ })
	if err != nil {
		t.Fatal(err)
	}
	if got := listing.family_list.CurrentFamily(); got != "Alpha" {
		t.Fatalf("inactive listing click changed selection to %q", got)
	}
	if redraws != 0 || refusals != 0 {
		t.Fatalf("inactive click effects: redraws=%d refusals=%d, want 0 and 0", redraws, refusals)
	}
}

func TestFontListFamilyClickKeepsMalformedIDRefusal(t *testing.T) {
	listing, _ := new_click_test_font_list()
	if err := listing.handle_click("malformed", func() error { return nil }, func() {}); err == nil {
		t.Fatal("malformed click id was accepted")
	}
}

func navigation_test_key(name string) *loop.KeyEvent {
	return &loop.KeyEvent{Type: loop.PRESS, Key: loop.ParseShortcut(name).KeyName}
}

func TestFontListPageDownUsesNoWrapAtExactEnd(t *testing.T) {
	listing := &FontList{family_list: FamilyList{families: []string{"A", "B", "C"}, current_idx: 2}}
	event := navigation_test_key("page_down")
	screen_size_calls, redraws, refusals := 0, 0, 0
	handled, err := listing.handle_navigation_key(event, func() (loop.ScreenSize, error) {
		screen_size_calls++
		return loop.ScreenSize{HeightCells: 4}, nil // Page Down delta is +1: exactly Len().
	}, func(delta int, allowWrapping bool) error {
		return listing.next_with_feedback(delta, allowWrapping, func() error {
			redraws++
			return nil
		}, func() { refusals++ })
	})
	if err != nil {
		t.Fatal(err)
	}
	if !handled || !event.Handled {
		t.Fatal("Page Down was not consumed by the listing navigation handler")
	}
	if screen_size_calls != 1 {
		t.Fatalf("Page Down read screen size %d times, want once", screen_size_calls)
	}
	if got := listing.family_list.CurrentFamily(); got != "C" {
		t.Fatalf("no-wrap Page Down changed selection to %q, want C", got)
	}
	if redraws != 0 || refusals != 1 {
		t.Fatalf("end-of-list Page Down effects: redraws=%d refusals=%d, want 0 and 1", redraws, refusals)
	}
}

func TestFontListPageDownCanLandOnLastValidFamily(t *testing.T) {
	listing := &FontList{family_list: FamilyList{families: []string{"A", "B", "C"}, current_idx: 1}}
	event := navigation_test_key("page_down")
	redraws, refusals := 0, 0
	handled, err := listing.handle_navigation_key(event, func() (loop.ScreenSize, error) {
		return loop.ScreenSize{HeightCells: 4}, nil
	}, func(delta int, allowWrapping bool) error {
		return listing.next_with_feedback(delta, allowWrapping, func() error {
			redraws++
			return nil
		}, func() { refusals++ })
	})
	if err != nil {
		t.Fatal(err)
	}
	if !handled || listing.family_list.CurrentFamily() != "C" || redraws != 1 || refusals != 0 {
		t.Fatalf("Page Down last-valid result: handled=%v family=%q redraws=%d refusals=%d", handled, listing.family_list.CurrentFamily(), redraws, refusals)
	}
}

func TestFontListArrowNavigationRetainsWrapping(t *testing.T) {
	listing := &FontList{family_list: FamilyList{families: []string{"A", "B", "C"}, current_idx: 2}}
	event := navigation_test_key("down")
	screen_size_calls, redraws, refusals := 0, 0, 0
	handled, err := listing.handle_navigation_key(event, func() (loop.ScreenSize, error) {
		screen_size_calls++
		return loop.ScreenSize{}, nil
	}, func(delta int, allowWrapping bool) error {
		return listing.next_with_feedback(delta, allowWrapping, func() error {
			redraws++
			return nil
		}, func() { refusals++ })
	})
	if err != nil {
		t.Fatal(err)
	}
	if !handled || !event.Handled || listing.family_list.CurrentFamily() != "A" {
		t.Fatalf("Down did not wrap from the final family: handled=%v family=%q", handled, listing.family_list.CurrentFamily())
	}
	if screen_size_calls != 0 || redraws != 1 || refusals != 0 {
		t.Fatalf("Down effects: screen-size calls=%d redraws=%d refusals=%d", screen_size_calls, redraws, refusals)
	}
}

func TestFontListNavigationReturnsScreenSizeFailure(t *testing.T) {
	listing := &FontList{family_list: FamilyList{families: []string{"A"}}}
	event := navigation_test_key("page_down")
	wantErr := errors.New("screen size unavailable")
	handled, err := listing.handle_navigation_key(event, func() (loop.ScreenSize, error) {
		return loop.ScreenSize{}, wantErr
	}, func(int, bool) error { return nil })
	if !handled || !errors.Is(err, wantErr) {
		t.Fatalf("screen size failure was not returned: handled=%v err=%v", handled, err)
	}
}
