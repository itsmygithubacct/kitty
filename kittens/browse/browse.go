package browse

// kilix browse: current Chrome rendered inside a kitty pane.
//   pixel layer: CDP screencast frames blitted via the kitty graphics
//     protocol at z=-1 (below text) through private Kilix session files
//   glyph layer: page text drawn as real terminal cells (selectable),
//     harvested from DOMSnapshot; the page's own text ink is transparent
// Reference implementation:
// the public Kilix browse configuration.

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"image"
	"image/draw"
	"image/jpeg"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/kovidgoyal/kitty/tools/wcswidth"
)

// NB: -webkit-text-fill-color INHERITS, so excluding form fields from the
// selector is not enough — the transparent value flows down from the parent
// and typed text becomes invisible. Fields (and their placeholders) must be
// explicitly reset to their own ink.
const transparentCSS = `*:not(input):not(textarea):not(select)` +
	`{-webkit-text-fill-color:transparent !important;text-shadow:none !important}` +
	`input,textarea,select{-webkit-text-fill-color:currentColor !important}` +
	`input::placeholder,textarea::placeholder{-webkit-text-fill-color:currentColor !important}` +
	`::selection{background:rgba(52,101,164,0.55)}`

var injectJS = `(function(){var s=document.createElement('style');` +
	`s.textContent=` + mustJSON(transparentCSS) + `;` +
	`(document.head||document.documentElement).appendChild(s);})()`

const editableFocusJS = `(() => {
  const e = document.activeElement;
  if (!e) return false;
  if (e.isContentEditable) return true;
  const tag = (e.tagName || "").toLowerCase();
  if (tag === "textarea") return !e.readOnly && !e.disabled;
  if (tag !== "input") return e.getAttribute && e.getAttribute("role") === "textbox";
  const type = (e.getAttribute("type") || "text").toLowerCase();
  return !["button", "checkbox", "color", "file", "hidden", "image",
           "radio", "range", "reset", "submit"].includes(type)
         && !e.readOnly && !e.disabled;
})()`

const toolbarPrefix = " [<] [>] [R] "
const toolbarURLStart = len(toolbarPrefix)

func mustJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

type attr struct {
	fg              [3]uint8
	bold, ital, und bool
}

type cell struct {
	r rune // 0 = empty, -1 = wide continuation
	a attr
}

type run struct {
	x, y, w, h float64
	text       string
	a          attr
}

type Browse struct {
	term        *Term
	cdp         *CDP
	sess        string
	url, title  string
	wid         string
	seq         int
	viewRows    int
	pageW       int
	pageH       int
	tempProfile string
	frameDir    string

	runs             []run
	scrollX, scrollY float64
	halfRes          bool // sustained animation: screencast at half size
	frameTimes       []time.Time
	cursor           bool // draw a software pointer (headless Chrome has none)
	curX, curY       int  // pointer position, page pixels
	lastRGBA         *image.RGBA
	imgW, imgH       int
	lastCurPaint     time.Time
	savedPatch       []uint8 // pixels under the stamped cursor
	savedRect        image.Rectangle
	snapDirty        bool
	glyphDirty       bool
	lastInput        time.Time
	lastSnap         time.Time
	lastFrame        time.Time
	frames           int
	statusMsg        string
	urlEdit          *string
	mouseButtons     int
	lastClickT       time.Time
	lastClickX       int
	lastClickY       int
	clickCount       int
	prevGrid         [][]cell
	prevStatus       string
}

var rgbRe = regexp.MustCompile(`rgba?\((\d+),\s*(\d+),\s*(\d+)`)

func logf(format string, a ...any) {
	if p := os.Getenv("KILIX_BROWSE_LOG"); p != "" {
		f, err := os.OpenFile(
			p, os.O_APPEND|os.O_CREATE|os.O_WRONLY|syscall.O_NOFOLLOW, 0o600)
		if err == nil {
			_ = f.Chmod(0o600)
			fmt.Fprintf(f, "[%.3f] "+format+"\n", append([]any{float64(time.Now().UnixMilli()) / 1000}, a...)...)
			f.Close()
		}
	}
}

func ensurePrivateDir(path string) error {
	if err := os.MkdirAll(path, 0o700); err != nil {
		return err
	}
	return os.Chmod(path, 0o700)
}

func newBrowse(url string, incognito, cursor bool) (*Browse, error) {
	t, err := newTerm()
	if err != nil {
		return nil, err
	}
	if t.Xpix == 0 {
		return nil, fmt.Errorf("terminal does not report pixel size (run inside kilix)")
	}
	b := &Browse{
		term: t, url: url, title: url, statusMsg: "loading…",
		snapDirty: true, glyphDirty: true, cursor: cursor,
	}
	b.wid = os.Getenv("KITTY_WINDOW_ID")
	if b.wid == "" {
		b.wid = strconv.Itoa(os.Getpid())
	}
	b.frameDir = filepath.Join(sessionDir(), "graphics",
		fmt.Sprintf("browse-%s-%d", b.wid, os.Getpid()))
	if err = ensurePrivateDir(b.frameDir); err != nil {
		t.Restore()
		return nil, err
	}
	cleanupStorage := func() {
		_ = os.RemoveAll(b.frameDir)
		if b.tempProfile != "" {
			_ = os.RemoveAll(b.tempProfile)
		}
	}
	b.computeSize()
	b.curX, b.curY = b.pageW/2, b.pageH/2
	var extra []string
	var profile string
	if incognito {
		// a throwaway profile, deleted on exit: no history, cookies or cache
		// survive the session (Chrome's own --incognito on top for good measure)
		profileRoot := filepath.Join(sessionDir(), "browse-profiles")
		if err = ensurePrivateDir(profileRoot); err == nil {
			profile, err = os.MkdirTemp(profileRoot, "kilix-browse-incognito-")
		}
		if err != nil {
			cleanupStorage()
			t.Restore()
			return nil, err
		}
		b.tempProfile = profile
		extra = append(extra, "--incognito")
	} else {
		profile = filepath.Join(stateDir(), "browse-profile")
		if err = ensurePrivateDir(profile); err != nil {
			cleanupStorage()
			t.Restore()
			return nil, err
		}
		// Chrome refuses to share a profile: fall back to a disposable one
		lock := filepath.Join(profile, "SingletonLock")
		switch profileLockState(lock) {
		case "stale":
			removeStaleProfileSingletons(profile)
		case "live", "unknown":
			profileRoot := filepath.Join(sessionDir(), "browse-profiles")
			if err = ensurePrivateDir(profileRoot); err != nil {
				cleanupStorage()
				t.Restore()
				return nil, err
			}
			profile, err = os.MkdirTemp(profileRoot, "kilix-browse-shared-profile-")
			if err != nil {
				cleanupStorage()
				t.Restore()
				return nil, err
			}
			b.tempProfile = profile
		}
	}
	b.cdp, err = startCDP(b.pageW, b.pageH, profile, extra...)
	if err != nil {
		cleanupStorage()
		t.Restore()
		return nil, err
	}
	return b, nil
}

func stateDir() string {
	if d := os.Getenv("KILIX_STATE_DIRECTORY"); d != "" {
		return d
	}
	if d := os.Getenv("KILIX_STORAGE_HOME"); d != "" {
		return filepath.Join(d, "state")
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".local", "gpu_terminal", "kilix", "state")
}

func sessionDir() string {
	if d := os.Getenv("KILIX_SESSION_HOME"); d != "" {
		return d
	}
	if d := os.Getenv("KILIX_STORAGE_HOME"); d != "" {
		return filepath.Join(d, "session")
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".local", "gpu_terminal", "kilix", "session")
}

func profileLockState(lock string) string {
	target, err := os.Readlink(lock)
	if os.IsNotExist(err) {
		return "absent"
	}
	if err != nil {
		return "unknown"
	}
	i := strings.LastIndex(target, "-")
	if i < 0 {
		return "unknown"
	}
	pid, err := strconv.Atoi(target[i+1:])
	if err != nil {
		return "unknown"
	}
	err = syscall.Kill(pid, 0)
	if err == nil || err == syscall.EPERM {
		return "live"
	}
	if err == syscall.ESRCH {
		return "stale"
	}
	return "unknown"
}

func removeStaleProfileSingletons(profile string) {
	if profileLockState(filepath.Join(profile, "SingletonLock")) != "stale" {
		return
	}
	for _, name := range []string{"SingletonLock", "SingletonCookie", "SingletonSocket"} {
		path := filepath.Join(profile, name)
		if info, err := os.Lstat(path); err == nil && info.Mode()&os.ModeSymlink != 0 {
			_ = os.Remove(path)
		}
	}
}

func (b *Browse) computeSize() {
	b.viewRows = b.term.Rows - 1 // last row = status
	b.pageW = int(float64(b.term.Cols) * b.term.CellW())
	b.pageH = int(float64(b.viewRows) * b.term.CellH())
}

// ── CDP session ─────────────────────────────────────────────────────────

func (b *Browse) start() error {
	var tgt struct {
		TargetID string `json:"targetId"`
	}
	if err := b.cdp.Call("Target.createTarget", map[string]any{"url": "about:blank"}, "", 30*time.Second, &tgt); err != nil {
		return err
	}
	var att struct {
		SessionID string `json:"sessionId"`
	}
	if err := b.cdp.Call("Target.attachToTarget",
		map[string]any{"targetId": tgt.TargetID, "flatten": true}, "", 30*time.Second, &att); err != nil {
		return err
	}
	b.sess = att.SessionID
	for _, m := range []string{"Page.enable", "Runtime.enable", "DOMSnapshot.enable"} {
		if err := b.cdp.Call(m, nil, b.sess, 30*time.Second, nil); err != nil {
			return err
		}
	}
	b.cdp.Call("Page.addScriptToEvaluateOnNewDocument",
		map[string]any{"source": injectJS}, b.sess, 10*time.Second, nil)
	b.setMetrics()
	b.cdp.Send("Page.navigate", map[string]any{"url": b.url}, b.sess)
	return b.startScreencast()
}

func (b *Browse) setMetrics() error {
	return b.cdp.Call("Emulation.setDeviceMetricsOverride", map[string]any{
		"width": b.pageW, "height": b.pageH, "deviceScaleFactor": 1, "mobile": false,
	}, b.sess, 10*time.Second, nil)
}

func (b *Browse) startScreencast() error {
	w, h := b.pageW, b.pageH
	if b.halfRes {
		w, h = w/2, h/2
	}
	return b.cdp.Call("Page.startScreencast", map[string]any{
		"format": "jpeg", "quality": 80,
		"maxWidth": w, "maxHeight": h, "everyNthFrame": 1,
	}, b.sess, 10*time.Second, nil)
}

// Sustained animation (video) makes JPEG decode the CPU hog. Drop the
// screencast to half resolution while frames stream (quarter the decode
// work; kitty GPU-scales the placement back to the full pane) and return
// to full resolution once the page goes still.
func (b *Browse) adaptResolution() {
	now := time.Now()
	b.frameTimes = append(b.frameTimes, now)
	for len(b.frameTimes) > 0 && now.Sub(b.frameTimes[0]) > time.Second {
		b.frameTimes = b.frameTimes[1:]
	}
	fps := len(b.frameTimes)
	if !b.halfRes && fps >= 15 {
		b.halfRes = true
		b.cdp.Send("Page.stopScreencast", nil, b.sess)
		b.startScreencast()
		logf("adaptive: half-res (fps=%d)", fps)
	} else if b.halfRes && fps <= 3 {
		b.halfRes = false
		b.cdp.Send("Page.stopScreencast", nil, b.sess)
		b.startScreencast()
		logf("adaptive: full-res")
	}
}

// ── pixel layer ─────────────────────────────────────────────────────────

func (b *Browse) blit(b64jpeg string, meta map[string]float64) {
	raw, err := base64.StdEncoding.DecodeString(b64jpeg)
	if err != nil {
		return
	}
	img, err := jpeg.Decode(bytes.NewReader(raw))
	if err != nil {
		return
	}
	bounds := img.Bounds()
	rgba, ok := img.(*image.RGBA)
	if !ok {
		rgba = image.NewRGBA(bounds)
		draw.Draw(rgba, bounds, img, bounds.Min, draw.Src)
	}
	b.lastRGBA, b.imgW, b.imgH = rgba, bounds.Dx(), bounds.Dy()
	b.savedPatch = nil // fresh frame: nothing stamped on it yet
	if b.cursor {
		b.stampCursor()
	}
	b.present()
	sx, sy := meta["scrollOffsetX"], meta["scrollOffsetY"]
	if sx != b.scrollX || sy != b.scrollY {
		b.scrollX, b.scrollY = sx, sy
		b.glyphDirty = true
	}
	b.frames++
	if b.frames == 1 || b.frames%60 == 0 {
		logf("frames=%d size=%dx%d scroll=%.0f", b.frames, b.imgW, b.imgH, b.scrollY)
	}
}

func (b *Browse) present() {
	b.seq++
	path := filepath.Join(b.frameDir,
		fmt.Sprintf("tty-graphics-protocol-kilix-%s-full-%d.rgba", b.wid, b.seq))
	f, err := os.OpenFile(path,
		os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o600)
	if err != nil {
		return
	}
	_, writeErr := f.Write(b.lastRGBA.Pix)
	closeErr := f.Close()
	if writeErr != nil || closeErr != nil {
		_ = os.Remove(path)
		return
	}
	payload := base64.StdEncoding.EncodeToString([]byte(path))
	// c/r pin the placement to the full pane rect so kitty GPU-scales
	// half-res frames back up; at full resolution it is a 1:1 no-op.
	b.term.Write(fmt.Sprintf("\x1b[H\x1b_Ga=T,i=1,p=1,z=-1,t=t,f=32,N=1,s=%d,v=%d,c=%d,r=%d,q=2,C=1;%s\x1b\\",
		b.imgW, b.imgH, b.term.Cols, b.viewRows, payload))
}

// ── software mouse pointer ──────────────────────────────────────────────
// Headless Chrome renders no pointer, so browse stamps a classic arrow onto
// the frame itself; the pixels underneath are saved and restored, so pointer
// motion never needs a fresh screencast frame.

var cursorArt = []string{
	"k          ",
	"kk         ",
	"kwk        ",
	"kwwk       ",
	"kwwwk      ",
	"kwwwwk     ",
	"kwwwwwk    ",
	"kwwwwwwk   ",
	"kwwwwwwwk  ",
	"kwwwwwwwwk ",
	"kwwwwwkkkkk",
	"kwwkwwk    ",
	"kwk kwwk   ",
	"kk  kwwk   ",
	"k    kwwk  ",
	"     kwwk  ",
	"      kk   ",
}

func (b *Browse) stampCursor() {
	if b.lastRGBA == nil || b.pageW == 0 || b.pageH == 0 {
		return
	}
	// page coords → image coords (differ while the screencast is half-res)
	sx := b.curX * b.imgW / b.pageW
	sy := b.curY * b.imgH / b.pageH
	r := image.Rect(sx, sy, sx+len(cursorArt[0]), sy+len(cursorArt)).
		Intersect(b.lastRGBA.Bounds())
	if r.Empty() {
		b.savedPatch = nil
		return
	}
	b.savedRect = r
	b.savedPatch = make([]uint8, 0, r.Dx()*r.Dy()*4)
	for y := r.Min.Y; y < r.Max.Y; y++ {
		o := b.lastRGBA.PixOffset(r.Min.X, y)
		b.savedPatch = append(b.savedPatch, b.lastRGBA.Pix[o:o+r.Dx()*4]...)
	}
	for dy, rowArt := range cursorArt {
		for dx, c := range rowArt {
			if c == ' ' {
				continue
			}
			x, y := sx+dx, sy+dy
			if !image.Pt(x, y).In(r) {
				continue
			}
			o := b.lastRGBA.PixOffset(x, y)
			v := uint8(0)
			if c == 'w' {
				v = 255
			}
			b.lastRGBA.Pix[o], b.lastRGBA.Pix[o+1], b.lastRGBA.Pix[o+2], b.lastRGBA.Pix[o+3] = v, v, v, 255
		}
	}
}

func (b *Browse) unstampCursor() {
	if b.savedPatch == nil || b.lastRGBA == nil {
		return
	}
	r := b.savedRect
	for y, i := r.Min.Y, 0; y < r.Max.Y; y++ {
		o := b.lastRGBA.PixOffset(r.Min.X, y)
		copy(b.lastRGBA.Pix[o:o+r.Dx()*4], b.savedPatch[i:i+r.Dx()*4])
		i += r.Dx() * 4
	}
	b.savedPatch = nil
}

// repaintCursor re-presents the last frame with the pointer at its new
// position — throttled, because SGR-pixel motion arrives per pixel.
func (b *Browse) repaintCursor() {
	if !b.cursor || b.lastRGBA == nil {
		return
	}
	if time.Since(b.lastCurPaint) < 25*time.Millisecond {
		return
	}
	b.lastCurPaint = time.Now()
	b.unstampCursor()
	b.stampCursor()
	b.present()
}

// ── glyph layer ─────────────────────────────────────────────────────────

type snapDoc struct {
	Layout struct {
		Bounds [][]float64 `json:"bounds"`
		Text   []int       `json:"text"`
		Styles [][]int     `json:"styles"`
	} `json:"layout"`
}

func (b *Browse) snapshot() {
	var snap struct {
		Documents []snapDoc `json:"documents"`
		Strings   []string  `json:"strings"`
	}
	err := b.cdp.Call("DOMSnapshot.captureSnapshot", map[string]any{
		"computedStyles": []string{"color", "font-weight", "font-style", "text-decoration-line"},
	}, b.sess, 5*time.Second, &snap)
	if err != nil {
		logf("snapshot failed: %v", err)
		return
	}
	var runs []run
	for _, doc := range snap.Documents {
		lay := doc.Layout
		for i, ti := range lay.Text {
			if ti < 0 || ti >= len(snap.Strings) || i >= len(lay.Bounds) {
				continue
			}
			text := snap.Strings[ti]
			if strings.TrimSpace(text) == "" {
				continue
			}
			bb := lay.Bounds[i]
			if len(bb) < 4 {
				continue
			}
			a := attr{fg: [3]uint8{211, 215, 207}}
			if i < len(lay.Styles) && len(lay.Styles[i]) > 0 {
				vals := make([]string, len(lay.Styles[i]))
				for j, si := range lay.Styles[i] {
					if si >= 0 && si < len(snap.Strings) {
						vals[j] = snap.Strings[si]
					}
				}
				if m := rgbRe.FindStringSubmatch(vals[0]); m != nil {
					r, _ := strconv.Atoi(m[1])
					g, _ := strconv.Atoi(m[2])
					bl, _ := strconv.Atoi(m[3])
					a.fg = [3]uint8{uint8(r), uint8(g), uint8(bl)}
				}
				if len(vals) > 1 && vals[1] != "" && vals[1][0] >= '0' && vals[1][0] <= '9' {
					if wgt, err := strconv.Atoi(strings.Fields(vals[1])[0]); err == nil {
						a.bold = wgt >= 600
					}
				}
				if len(vals) > 2 {
					a.ital = vals[2] == "italic"
				}
				if len(vals) > 3 {
					a.und = strings.Contains(vals[3], "underline")
				}
			}
			runs = append(runs, run{bb[0], bb[1], bb[2], bb[3], text, a})
		}
	}
	b.runs = runs
	b.lastSnap = time.Now()
	b.snapDirty = false
	b.glyphDirty = true
	logf("snapshot: %d runs", len(runs))
}

// charWidth must agree with kitty or rows drift and the erase-diff misses.
func charWidth(r rune) int {
	if r < 32 || r == 127 {
		return -1 // control char: sanitize to a space
	}
	w := wcswidth.Runewidth(r)
	if w < 0 {
		w = 0
	}
	return w
}

func (b *Browse) renderGlyphs() {
	rows, cols := b.viewRows, b.term.Cols
	cw, ch := b.term.CellW(), b.term.CellH()
	grid := make([][]cell, rows)
	for r := range grid {
		grid[r] = make([]cell, cols)
	}
	for _, ru := range b.runs {
		sy := ru.y - b.scrollY
		sx := ru.x - b.scrollX
		row := int((sy + ru.h/2) / ch)
		if row < 0 || row >= rows {
			continue
		}
		col := int(sx/cw + 0.5)
		for _, r := range ru.text {
			if col >= cols {
				break
			}
			w := charWidth(r)
			if w == -1 {
				r, w = ' ', 1
			} else if w == 0 {
				continue
			}
			if col >= 0 {
				grid[row][col] = cell{r: r, a: ru.a}
				if w == 2 && col+1 < cols {
					grid[row][col+1] = cell{r: -1, a: ru.a}
				}
			}
			col += w
		}
	}
	status := b.renderStatus()
	if gridsEqual(grid, b.prevGrid) && status == b.prevStatus {
		// identical content: never rewrite (a no-op repaint still clears
		// kitty's native selection)
		b.glyphDirty = false
		return
	}
	b.prevStatus = status

	var out strings.Builder
	out.WriteString("\x1b[?2026h")
	prev := b.prevGrid
	for r := 0; r < rows; r++ {
		fmt.Fprintf(&out, "\x1b[%d;1H\x1b[0m", r+1)
		var cur *attr
		col := 0
		for col < cols {
			c := grid[r][col]
			if c.r == 0 {
				if cur != nil {
					out.WriteString("\x1b[0m")
					cur = nil
				}
				// blank cells that had a glyph last repaint; jump the rest
				start := col
				for col < cols && grid[r][col].r == 0 {
					col++
				}
				emitGap(&out, prev, r, start, col)
				continue
			}
			if c.r == -1 { // orphaned wide continuation
				out.WriteString("\x1b[0m ")
				cur = nil
				col++
				continue
			}
			w := charWidth(c.r)
			if col+w > cols { // wide char can't fit in the last column
				out.WriteString("\x1b[0m ")
				cur = nil
				col++
				continue
			}
			if cur == nil || *cur != c.a {
				sgr := fmt.Sprintf("\x1b[0;38;2;%d;%d;%d", c.a.fg[0], c.a.fg[1], c.a.fg[2])
				if c.a.bold {
					sgr += ";1"
				}
				if c.a.ital {
					sgr += ";3"
				}
				if c.a.und {
					sgr += ";4"
				}
				out.WriteString(sgr + "m")
				a := c.a
				cur = &a
			}
			out.WriteRune(c.r)
			col += w
		}
	}
	out.WriteString(status)
	out.WriteString("\x1b[?2026l")
	b.term.Write(out.String())
	b.prevGrid = grid
	b.glyphDirty = false
}

func emitGap(out *strings.Builder, prev [][]cell, row, start, end int) {
	// RLE over "had a glyph before" (erase with space) vs "untouched" (jump)
	col := start
	for col < end {
		erase := prev != nil && row < len(prev) && col < len(prev[row]) && prev[row][col].r != 0
		n := 0
		for col < end {
			e := prev != nil && row < len(prev) && col < len(prev[row]) && prev[row][col].r != 0
			if e != erase {
				break
			}
			n++
			col++
		}
		if erase {
			out.WriteString(strings.Repeat(" ", n))
		} else {
			fmt.Fprintf(out, "\x1b[%dC", n)
		}
	}
}

func gridsEqual(a, b [][]cell) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if len(a[i]) != len(b[i]) {
			return false
		}
		for j := range a[i] {
			if a[i][j] != b[i][j] {
				return false
			}
		}
	}
	return true
}

func (b *Browse) renderStatus() string {
	var body string
	if b.urlEdit != nil {
		body = toolbarPrefix + "URL: " + *b.urlEdit + "▏"
	} else {
		title := b.title
		if tr := []rune(title); len(tr) > 40 {
			title = string(tr[:40])
		}
		body = fmt.Sprintf("%s%s — %s  [%s]", toolbarPrefix, title, b.url, b.statusMsg)
	}
	r := []rune(body)
	if len(r) > b.term.Cols {
		r = r[:b.term.Cols]
	}
	body = string(r) + strings.Repeat(" ", b.term.Cols-len(r))
	return fmt.Sprintf("\x1b[%d;1H\x1b[0;7m%s\x1b[0m", b.term.Rows, body)
}
