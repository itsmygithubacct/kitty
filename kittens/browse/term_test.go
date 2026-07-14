package browse

import (
	"image"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func TestPresentUsesUniqueTransientFrameFiles(t *testing.T) {
	frameDir := t.TempDir()
	out, err := os.CreateTemp(t.TempDir(), "browse-output-")
	if err != nil {
		t.Fatal(err)
	}
	defer out.Close()
	b := &Browse{
		term: &Term{out: out, Cols: 4}, wid: "42", frameDir: frameDir,
		lastRGBA: image.NewRGBA(image.Rect(0, 0, 2, 2)),
		imgW:     2, imgH: 2, viewRows: 3,
	}
	for range 10 {
		b.present()
	}
	if _, err := out.Seek(0, 0); err != nil {
		t.Fatal(err)
	}
	wire, err := os.ReadFile(out.Name())
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Count(string(wire), "N=1"); got != 10 {
		t.Fatalf("transient commands = %d, want 10", got)
	}
	entries, err := os.ReadDir(frameDir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 10 {
		t.Fatalf("frame files = %d, want 10", len(entries))
	}
	for i := 1; i <= 10; i++ {
		path := filepath.Join(frameDir,
			"tty-graphics-protocol-kilix-42-full-"+strconv.Itoa(i)+".rgba")
		info, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if info.Size() != 16 || info.Mode().Perm() != 0o600 {
			t.Fatalf("bad frame %s: size=%d mode=%#o", path,
				info.Size(), info.Mode().Perm())
		}
	}
}

func TestPresentNeverTruncatesPublishedFrame(t *testing.T) {
	frameDir := t.TempDir()
	path := filepath.Join(frameDir,
		"tty-graphics-protocol-kilix-42-full-1.rgba")
	if err := os.WriteFile(path, []byte("mapped-frame"), 0o600); err != nil {
		t.Fatal(err)
	}
	out, err := os.CreateTemp(t.TempDir(), "browse-output-")
	if err != nil {
		t.Fatal(err)
	}
	defer out.Close()
	b := &Browse{
		term: &Term{out: out, Cols: 4}, wid: "42", frameDir: frameDir,
		lastRGBA: image.NewRGBA(image.Rect(0, 0, 2, 2)),
		imgW:     2, imgH: 2, viewRows: 3,
	}
	b.present()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "mapped-frame" {
		t.Fatalf("published frame was changed: %q", data)
	}
	if info, err := out.Stat(); err != nil || info.Size() != 0 {
		t.Fatalf("failed frame was announced: info=%v err=%v", info, err)
	}
}

func TestParseWheelAndClick(t *testing.T) {
	tr := &Term{Cols: 100, Rows: 50}
	cases := []struct {
		in                  string
		wantB, wantX, wantY int
		wantPress           bool
	}{
		{"\x1b[<65;900;500M", 65, 900, 500, true}, // wheel down
		{"\x1b[<64;10;20M", 64, 10, 20, true},     // wheel up
		{"\x1b[<0;5;7M", 0, 5, 7, true},           // left press
		{"\x1b[<0;5;7m", 0, 5, 7, false},          // left release
		{"\x1b[<35;300;400M", 35, 300, 400, true}, // motion, no button
		{"\x1b[<288;1;1M", 288, 1, 1, true},       // leave sentinel
		{"\x1b[<64;-2;300M", 64, -2, 300, true},   // pane padding
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

func TestMouseSequencesNeverLeakAsPaste(t *testing.T) {
	burst := []byte("\x1b[<64;-2;300M\x1b[<65;500;-3M\x1b[<35;-1;200M")
	for i := 0; i <= len(burst); i++ {
		tr := &Term{}
		var evs []InputEvent
		evs = append(evs, tr.Feed(burst[:i])...)
		evs = append(evs, tr.Feed(burst[i:])...)
		if len(evs) != 3 {
			t.Fatalf("split %d: got %d events: %+v", i, len(evs), evs)
		}
		for _, ev := range evs {
			if ev.Paste != "" || ev.Mouse == nil {
				t.Fatalf("split %d leaked or missed mouse: %+v", i, ev)
			}
		}
	}
	tr := &Term{}
	var evs []InputEvent
	for _, b := range burst {
		evs = append(evs, tr.Feed([]byte{b})...)
	}
	if len(evs) != 3 {
		t.Fatalf("byte split: got %d events: %+v", len(evs), evs)
	}
	for _, ev := range evs {
		if ev.Paste != "" || ev.Mouse == nil {
			t.Fatalf("byte split leaked or missed mouse: %+v", ev)
		}
	}
}

func TestEscapeControlsNeverLeakAsPaste(t *testing.T) {
	seqs := []string{
		"\x1bP@kitty-cmd{\"ok\":true}\x1b\\",
		"\x1b]52;c;Zm9v\x07",
		"\x1b]0;title\x1b\\",
		"\x1b_Gf=100,a=T;AAAA\x1b\\",
		"\x1b^private\x1b\\",
		"\x1bXstatus\x1b\\",
		"\x1bOP",
	}
	for _, seq := range seqs {
		for i := 0; i <= len(seq); i++ {
			tr := &Term{}
			evs := append(tr.Feed([]byte(seq[:i])), tr.Feed([]byte(seq[i:]))...)
			for _, ev := range evs {
				if ev.Paste != "" {
					t.Fatalf("%q split %d leaked paste: %+v", seq, i, ev)
				}
			}
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

func TestKeyTextGuards(t *testing.T) {
	for _, key := range []int{57399, 57405, 57408, 57409, 57414, 57417, 57424, 57426} {
		for _, mods := range []int{1, 65, 129} {
			tr := &Term{}
			evs := tr.Feed([]byte("\x1b[" + itoa(key) + ";" + itoa(mods) + "u"))
			if len(evs) != 1 || evs[0].Key == nil {
				t.Fatalf("key %d mods %d: %+v", key, mods, evs)
			}
			if evs[0].Key.Text != "" {
				t.Fatalf("key %d mods %d leaked text %q", key, mods, evs[0].Key.Text)
			}
		}
	}
	tr := &Term{}
	if text := tr.Feed([]byte("\x1b[97;65u"))[0].Key.Text; text != "A" {
		t.Fatalf("caps a text = %q, want A", text)
	}
	tr = &Term{}
	if text := tr.Feed([]byte("\x1b[233;129u"))[0].Key.Text; text != "é" {
		t.Fatalf("numlock e-acute text = %q, want é", text)
	}
}

func TestToolbarChrome(t *testing.T) {
	cases := []struct {
		col  int
		want string
	}{
		{0, ""},
		{1, "back"},
		{3, "back"},
		{5, "forward"},
		{7, "forward"},
		{9, "reload"},
		{11, "reload"},
		{13, "url"},
	}
	for _, c := range cases {
		if got := toolbarAction(c.col); got != c.want {
			t.Fatalf("toolbarAction(%d) = %q, want %q", c.col, got, c.want)
		}
	}

	b := &Browse{
		term:      &Term{Rows: 24, Cols: 80},
		title:     "Example",
		url:       "https://example.com",
		statusMsg: "ready",
	}
	if got := b.renderStatus(); !strings.Contains(got, "[<] [>] [R] Example") {
		t.Fatalf("status missing toolbar: %q", got)
	}
	edit := ""
	b.urlEdit = &edit
	if got := b.renderStatus(); !strings.Contains(got, "[<] [>] [R] URL: ") {
		t.Fatalf("url edit status missing toolbar: %q", got)
	}
}

func itoa(n int) string { return strconv.Itoa(n) }
