package browse

import "testing"

func TestParseWheelAndClick(t *testing.T) {
	tr := &Term{Cols: 100, Rows: 50}
	cases := []struct {
		in            string
		wantB, wantX, wantY int
		wantPress     bool
	}{
		{"\x1b[<65;900;500M", 65, 899, 499, true},   // wheel down
		{"\x1b[<64;10;20M", 64, 9, 19, true},        // wheel up
		{"\x1b[<0;5;7M", 0, 4, 6, true},             // left press
		{"\x1b[<0;5;7m", 0, 4, 6, false},            // left release
		{"\x1b[<35;300;400M", 35, 299, 399, true},   // motion, no button
		{"\x1b[<288;1;1M", 288, 0, 0, true},         // leave sentinel
	}
	for _, c := range cases {
		evs := tr.Feed([]byte(c.in))
		if len(evs) != 1 || evs[0].Mouse == nil {
			t.Fatalf("%q: got %d events (want 1 mouse)", c.in, len(evs))
		}
		m := evs[0].Mouse
		if m.B != c.wantB || m.X != c.wantX || m.Y != c.wantY || m.Press != c.wantPress {
			t.Errorf("%q: got b=%d x=%d y=%d press=%v; want b=%d x=%d y=%d press=%v",
				c.in, m.B, m.X, m.Y, m.Press, c.wantB, c.wantX, c.wantY, c.wantPress)
		}
	}
}

func TestParseKeyBasics(t *testing.T) {
	tr := &Term{Cols: 100, Rows: 50}
	// 'a' key in kitty keyboard protocol: CSI 97 u
	evs := tr.Feed([]byte("\x1b[97u"))
	if len(evs) != 1 || evs[0].Key == nil || evs[0].Key.Text != "a" {
		t.Fatalf("plain 'a': got %+v", evs)
	}
	// Ctrl+L: CSI 108 ; 5 u  (mods=5 -> ctrl)
	evs = tr.Feed([]byte("\x1b[108;5u"))
	if len(evs) != 1 || evs[0].Key == nil || evs[0].Key.Key != "l" {
		t.Fatalf("ctrl+l: got %+v", evs)
	}
	if m := evs[0].Key.Mods; m != 5 {
		t.Errorf("ctrl+l mods: got %d want 5", m)
	}
}
