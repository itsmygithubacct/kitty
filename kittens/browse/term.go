package browse

// Terminal plumbing: raw mode, pixel-accurate size, and parsing of the
// kitty keyboard protocol / SGR-pixel mouse input streams.

import (
	"os"
	"regexp"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

type Term struct {
	in, out *os.File
	saved   unix.Termios
	Rows    int
	Cols    int
	Xpix    int
	Ypix    int
	buf     []byte
}

func newTerm() (*Term, error) {
	t := &Term{in: os.Stdin, out: os.Stdout}
	st, err := unix.IoctlGetTermios(int(t.in.Fd()), unix.TCGETS)
	if err != nil {
		return nil, err
	}
	t.saved = *st
	if err = t.RefreshSize(); err != nil {
		return nil, err
	}
	return t, nil
}

func (t *Term) RefreshSize() error {
	ws, err := unix.IoctlGetWinsize(int(t.in.Fd()), unix.TIOCGWINSZ)
	if err != nil {
		return err
	}
	t.Rows, t.Cols = int(ws.Row), int(ws.Col)
	t.Xpix, t.Ypix = int(ws.Xpixel), int(ws.Ypixel)
	return nil
}

func (t *Term) CellW() float64 { return float64(t.Xpix) / float64(t.Cols) }
func (t *Term) CellH() float64 { return float64(t.Ypix) / float64(t.Rows) }

func (t *Term) Write(s string) {
	b := []byte(s)
	for len(b) > 0 {
		n, err := t.out.Write(b)
		if err != nil && n == 0 {
			return
		}
		b = b[n:]
	}
}

func (t *Term) Enter() {
	raw := t.saved
	raw.Iflag &^= unix.IGNBRK | unix.BRKINT | unix.PARMRK | unix.ISTRIP |
		unix.INLCR | unix.IGNCR | unix.ICRNL | unix.IXON
	raw.Oflag &^= unix.OPOST
	raw.Lflag &^= unix.ECHO | unix.ECHONL | unix.ICANON | unix.ISIG | unix.IEXTEN
	raw.Cflag &^= unix.CSIZE | unix.PARENB
	raw.Cflag |= unix.CS8
	raw.Cc[unix.VMIN], raw.Cc[unix.VTIME] = 1, 0
	unix.IoctlSetTermios(int(t.in.Fd()), unix.TCSETS, &raw)
	// alt screen, hide cursor, no autowrap, kbd protocol (1|4|8), mouse:
	// drag tracking + SGR + SGR-pixels, bracketed paste
	t.Write("\x1b[?1049h\x1b[2J\x1b[?25l\x1b[?7l\x1b[>13u" +
		"\x1b[?1002h\x1b[?1006h\x1b[?1016h\x1b[?2004h")
}

func (t *Term) Restore() {
	t.Write("\x1b[<u\x1b[?1002l\x1b[?1006l\x1b[?1016l\x1b[?2004l" +
		"\x1b[?7h\x1b_Ga=d,d=A\x1b\\\x1b[?25h\x1b[?1049l")
	unix.IoctlSetTermios(int(t.in.Fd()), unix.TCSETS, &t.saved)
}

// ── input events ────────────────────────────────────────────────────────

type KeyEvent struct {
	Key  string // "a", "Enter", "ArrowLeft", …
	Code string
	VK   int
	Mods int // kitty mods field (mods-1 = bitmask)
	Text string
}

type MouseEvent struct {
	B     int
	X, Y  int
	Press bool
}

type InputEvent struct {
	Key   *KeyEvent
	Mouse *MouseEvent
	Paste string
}

var csiRe = regexp.MustCompile(`^\x1b\[([\x30-\x3f]*)([\x20-\x2f]*)([\x40-\x7e])`)

type spec struct{ key, code string; vk int }

var specialCSI = map[byte]spec{
	'A': {"ArrowUp", "ArrowUp", 38}, 'B': {"ArrowDown", "ArrowDown", 40},
	'C': {"ArrowRight", "ArrowRight", 39}, 'D': {"ArrowLeft", "ArrowLeft", 37},
	'H': {"Home", "Home", 36}, 'F': {"End", "End", 35},
}
var specialTilde = map[int]spec{
	2: {"Insert", "Insert", 45}, 3: {"Delete", "Delete", 46},
	5: {"PageUp", "PageUp", 33}, 6: {"PageDown", "PageDown", 34},
}
var specialU = map[int]spec{
	13: {"Enter", "Enter", 13}, 9: {"Tab", "Tab", 9},
	127: {"Backspace", "Backspace", 8}, 27: {"Escape", "Escape", 27},
}

// Feed appends raw bytes and returns completed input events.
func (t *Term) Feed(data []byte) []InputEvent {
	t.buf = append(t.buf, data...)
	var events []InputEvent
	for len(t.buf) > 0 {
		if t.buf[0] == 0x1b && len(t.buf) >= 2 && t.buf[1] == '[' {
			m := csiRe.FindSubmatch(t.buf)
			if m == nil {
				if len(t.buf) > 64 { // garbage: resync
					t.buf = t.buf[1:]
					continue
				}
				break // incomplete, wait for more bytes
			}
			seq := len(m[0])
			params, final := string(m[1]), m[3][0]
			if params == "200" && final == '~' { // bracketed paste
				end := strings.Index(string(t.buf[seq:]), "\x1b[201~")
				if end < 0 {
					break // wait for the paste terminator
				}
				events = append(events, InputEvent{Paste: string(t.buf[seq : seq+end])})
				t.buf = t.buf[seq+end+6:]
				continue
			}
			t.buf = t.buf[seq:]
			if ev := parseCSI(params, final); ev != nil {
				events = append(events, *ev)
			}
		} else if t.buf[0] == 0x1b {
			if len(t.buf) == 1 {
				break
			}
			t.buf = t.buf[1:] // stray ESC
		} else {
			nxt := strings.IndexByte(string(t.buf), 0x1b)
			var chunk []byte
			if nxt < 0 {
				chunk, t.buf = t.buf, nil
			} else {
				chunk, t.buf = t.buf[:nxt], t.buf[nxt:]
			}
			events = append(events, InputEvent{Paste: string(chunk)})
		}
	}
	return events
}

func atoiDef(s string, def int) int {
	if s == "" {
		return def
	}
	if n, err := strconv.Atoi(s); err == nil {
		return n
	}
	return def
}

func parseCSI(params string, final byte) *InputEvent {
	if (final == 'M' || final == 'm') && strings.HasPrefix(params, "<") {
		p := strings.Split(params[1:], ";")
		if len(p) != 3 {
			return nil
		}
		return &InputEvent{Mouse: &MouseEvent{
			B: atoiDef(p[0], 0), X: atoiDef(p[1], 1) - 1, Y: atoiDef(p[2], 1) - 1,
			Press: final == 'M',
		}}
	}
	parts := strings.Split(params, ";")
	mods := 1
	if len(parts) > 1 {
		mods = atoiDef(strings.Split(parts[1], ":")[0], 1)
	}
	switch {
	case final == '~':
		num := atoiDef(strings.Split(parts[0], ":")[0], 0)
		if s, ok := specialTilde[num]; ok {
			return &InputEvent{Key: &KeyEvent{Key: s.key, Code: s.code, VK: s.vk, Mods: mods}}
		}
		return nil
	case specialCSI[final].key != "":
		s := specialCSI[final]
		return &InputEvent{Key: &KeyEvent{Key: s.key, Code: s.code, VK: s.vk, Mods: mods}}
	case final == 'u':
		nums := strings.Split(parts[0], ":")
		key := atoiDef(nums[0], 0)
		shifted := 0
		if len(nums) > 1 {
			shifted = atoiDef(nums[1], 0)
		}
		if s, ok := specialU[key]; ok {
			text := ""
			if key == 13 {
				text = "\r"
			} else if key == 9 {
				text = "\t"
			}
			return &InputEvent{Key: &KeyEvent{Key: s.key, Code: s.code, VK: s.vk, Mods: mods, Text: text}}
		}
		mm := mods - 1
		if mm < 0 {
			mm = 0
		}
		ch := rune(key)
		if mm&1 != 0 && shifted > 0 {
			ch = rune(shifted)
		}
		text := ""
		if mm&^1 == 0 && key >= 32 {
			text = string(ch)
		}
		vk := 0
		up := strings.ToUpper(string(ch))
		if up != "" {
			vk = int(up[0])
		}
		return &InputEvent{Key: &KeyEvent{Key: string(ch), Code: "", VK: vk, Mods: mods, Text: text}}
	}
	return nil
}

// kitty mods (mods-1: 1 shift, 2 alt, 4 ctrl, 8 super) → CDP modifiers
// (1 alt, 2 ctrl, 4 meta, 8 shift)
func cdpMods(kittyMods int) int {
	m := kittyMods - 1
	if m < 0 {
		m = 0
	}
	out := 0
	if m&2 != 0 {
		out |= 1
	}
	if m&4 != 0 {
		out |= 2
	}
	if m&8 != 0 {
		out |= 4
	}
	if m&1 != 0 {
		out |= 8
	}
	return out
}
