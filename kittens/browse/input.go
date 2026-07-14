package browse

// Input translation (terminal → CDP) and the reserved-chord commands.

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"
)

func (b *Browse) onKey(ev *KeyEvent) {
	// pure modifier presses (kitty functional keycodes 57441-57454) must
	// not dirty the page: the repaint would clear kitty's native selection
	if r := []rune(ev.Key); len(r) == 1 && r[0] >= 57441 && r[0] <= 57454 {
		return
	}
	b.lastInput = time.Now()
	b.snapDirty = true
	mods := ev.Mods - 1
	if mods < 0 {
		mods = 0
	}
	ctrl, alt := mods&4 != 0, mods&2 != 0
	plain := !ctrl && !alt && mods&1 == 0
	if b.urlEdit != nil {
		b.urlEditKey(ev)
		return
	}
	switch {
	case ctrl && ev.Key == "l":
		s := ""
		b.urlEdit = &s
		b.glyphDirty = true
		return
	case ctrl && ev.Key == "q":
		panic(quitSignal{})
	case ctrl && ev.Key == "r":
		b.reload()
		return
	case alt && (ev.Key == "ArrowLeft" || ev.Key == "ArrowRight"):
		step := 1
		if ev.Key == "ArrowLeft" {
			step = -1
		}
		b.history(step)
		return
	case plain && ev.Key == "Backspace" && !b.activeElementAcceptsText():
		b.history(-1)
		return
	case ctrl && ev.Key == "c":
		b.copySelection() // and fall through: forward to the page
	}
	b.forwardKey(ev)
}

type quitSignal struct{}

func (b *Browse) forwardKey(ev *KeyEvent) {
	code := ev.Code
	if code == "" {
		code = ev.Key
	}
	p := map[string]any{
		"modifiers": cdpMods(ev.Mods), "key": ev.Key, "code": code,
		"windowsVirtualKeyCode": ev.VK,
	}
	down := map[string]any{"type": "keyDown"}
	for k, v := range p {
		down[k] = v
	}
	if ev.Text != "" {
		down["text"] = ev.Text
	}
	b.cdp.Send("Input.dispatchKeyEvent", down, b.sess)
	up := map[string]any{"type": "keyUp"}
	for k, v := range p {
		up[k] = v
	}
	b.cdp.Send("Input.dispatchKeyEvent", up, b.sess)
}

func (b *Browse) urlEditKey(ev *KeyEvent) {
	switch ev.Key {
	case "Enter":
		url := strings.TrimSpace(*b.urlEdit)
		b.urlEdit = nil
		if url != "" {
			if !strings.Contains(url, "://") {
				if strings.Contains(url, ".") && !strings.Contains(url, " ") {
					url = "https://" + url
				} else {
					url = "https://duckduckgo.com/?q=" + strings.ReplaceAll(url, " ", "+")
				}
			}
			b.url = url
			b.statusMsg = "loading…"
			b.cdp.Send("Page.navigate", map[string]any{"url": url}, b.sess)
		}
	case "Escape":
		b.urlEdit = nil
	case "Backspace":
		if len(*b.urlEdit) > 0 {
			r := []rune(*b.urlEdit)
			s := string(r[:len(r)-1])
			b.urlEdit = &s
		}
	default:
		if ev.Text != "" {
			s := *b.urlEdit + ev.Text
			b.urlEdit = &s
		}
	}
	b.glyphDirty = true
}

func (b *Browse) onPaste(text string) {
	b.lastInput = time.Now()
	b.snapDirty = true
	if b.urlEdit != nil {
		s := *b.urlEdit + strings.ReplaceAll(text, "\n", "")
		b.urlEdit = &s
		b.glyphDirty = true
		return
	}
	b.cdp.Send("Input.insertText", map[string]any{"text": text}, b.sess)
}

func (b *Browse) onMouse(ev *MouseEvent) {
	b.lastInput = time.Now()
	x, y := ev.X, ev.Y
	logf("mouse b=%d x=%d y=%d press=%v", ev.B, x, y, ev.Press)
	if ev.B&256 != 0 { // kitty SGR-pixel leave indicator
		return
	}
	b.curX, b.curY = x, y
	defer b.repaintCursor() // pointer follows on every path, incl. status row
	if y >= b.pageH {       // toolbar/status row
		if ev.Press && ev.B&3 == 0 {
			b.toolbarClick(x)
		}
		return
	}
	mods := 0
	if ev.B&4 != 0 {
		mods |= 8
	}
	if ev.B&8 != 0 {
		mods |= 1
	}
	if ev.B&16 != 0 {
		mods |= 2
	}
	if ev.B&64 != 0 { // wheel: 64 up, 65 down
		delta := 120
		if ev.B&3 == 0 {
			delta = -120
		}
		b.cdp.Send("Input.dispatchMouseEvent", map[string]any{
			"type": "mouseWheel", "x": x, "y": y,
			"deltaX": 0, "deltaY": delta, "modifiers": mods,
		}, b.sess)
		b.snapDirty = true
		return
	}
	btnCode := ev.B & 3
	button := [4]string{"left", "middle", "right", "none"}[btnCode]
	if ev.B&32 != 0 { // motion: during a drag it must carry the held button
		dragBtn := "none"
		switch {
		case b.mouseButtons&1 != 0:
			dragBtn = "left"
		case b.mouseButtons&2 != 0:
			dragBtn = "right"
		case b.mouseButtons&4 != 0:
			dragBtn = "middle"
		}
		b.cdp.Send("Input.dispatchMouseEvent", map[string]any{
			"type": "mouseMoved", "x": x, "y": y,
			"buttons": b.mouseButtons, "modifiers": mods, "button": dragBtn,
		}, b.sess)
		return
	}
	mask := map[string]int{"left": 1, "right": 2, "middle": 4}[button]
	typ := "mouseReleased"
	if ev.Press {
		if time.Since(b.lastClickT) < 400*time.Millisecond &&
			abs(x-b.lastClickX) < 4 && abs(y-b.lastClickY) < 4 {
			b.clickCount++
		} else {
			b.clickCount = 1
		}
		b.lastClickT, b.lastClickX, b.lastClickY = time.Now(), x, y
		b.mouseButtons |= mask
		typ = "mousePressed"
	} else {
		b.mouseButtons &^= mask
	}
	b.cdp.Send("Input.dispatchMouseEvent", map[string]any{
		"type": typ, "x": x, "y": y, "button": button,
		"buttons": b.mouseButtons, "clickCount": b.clickCount, "modifiers": mods,
	}, b.sess)
	b.snapDirty = true
}

func abs(v int) int {
	if v < 0 {
		return -v
	}
	return v
}

// ── commands ────────────────────────────────────────────────────────────

func (b *Browse) reload() {
	b.cdp.Send("Page.reload", nil, b.sess)
	b.statusMsg = "reloading…"
	b.glyphDirty = true
}

func (b *Browse) history(step int) {
	var h struct {
		CurrentIndex int `json:"currentIndex"`
		Entries      []struct {
			ID int `json:"id"`
		} `json:"entries"`
	}
	if err := b.cdp.Call("Page.getNavigationHistory", nil, b.sess, 5*time.Second, &h); err != nil {
		return
	}
	idx := h.CurrentIndex + step
	if idx >= 0 && idx < len(h.Entries) {
		b.cdp.Send("Page.navigateToHistoryEntry", map[string]any{"entryId": h.Entries[idx].ID}, b.sess)
		b.statusMsg = "navigating…"
		b.glyphDirty = true
	}
}

func toolbarAction(col int) string {
	switch {
	case col >= 1 && col < 4:
		return "back"
	case col >= 5 && col < 8:
		return "forward"
	case col >= 9 && col < 12:
		return "reload"
	case col >= toolbarURLStart:
		return "url"
	default:
		return ""
	}
}

func (b *Browse) toolbarClick(x int) {
	cw := b.term.CellW()
	if cw < 1 {
		cw = 1
	}
	switch toolbarAction(int(float64(x) / cw)) {
	case "back":
		b.history(-1)
	case "forward":
		b.history(1)
	case "reload":
		b.reload()
	case "url":
		s := ""
		b.urlEdit = &s
		b.glyphDirty = true
	}
}

func (b *Browse) evalString(expr string) string {
	var res struct {
		Result struct {
			Value string `json:"value"`
		} `json:"result"`
	}
	err := b.cdp.Call("Runtime.evaluate", map[string]any{
		"expression": expr, "returnByValue": true,
	}, b.sess, 3*time.Second, &res)
	if err != nil {
		return ""
	}
	return res.Result.Value
}

func (b *Browse) evalBool(expr string) (bool, bool) {
	var res struct {
		Result struct {
			Value bool `json:"value"`
		} `json:"result"`
	}
	err := b.cdp.Call("Runtime.evaluate", map[string]any{
		"expression": expr, "returnByValue": true,
	}, b.sess, time.Second, &res)
	if err != nil {
		return false, false
	}
	return res.Result.Value, true
}

func (b *Browse) activeElementAcceptsText() bool {
	if editable, ok := b.evalBool(editableFocusJS); ok {
		return editable
	}
	return true
}

func (b *Browse) copySelection() {
	text := b.evalString("window.getSelection().toString()")
	if text == "" {
		return
	}
	b64 := base64.StdEncoding.EncodeToString([]byte(text))
	b.term.Write("\x1b]52;c;" + b64 + "\x07")
	b.statusMsg = fmt.Sprintf("copied %d chars", len([]rune(text)))
	b.glyphDirty = true
}

func (b *Browse) refreshTitle() {
	if t := b.evalString("document.title"); t != "" {
		b.title = t
	} else {
		b.title = b.url
	}
	t := b.title
	if tr := []rune(t); len(tr) > 60 {
		t = string(tr[:60])
	}
	b.term.Write("\x1b]2;" + t + "\x07")
}

func (b *Browse) doResize() {
	b.term.RefreshSize()
	b.computeSize()
	b.setMetrics()
	b.cdp.Send("Page.stopScreencast", nil, b.sess)
	b.startScreencast()
	b.prevGrid = nil
	b.snapDirty, b.glyphDirty = true, true
	logf("resize -> %dx%d (%dx%d cells)", b.pageW, b.pageH, b.term.Cols, b.term.Rows)
}

// ── CDP events ──────────────────────────────────────────────────────────

func (b *Browse) onCDPEvent(m CDPMsg) {
	switch m.Method {
	case "Page.screencastFrame":
		var p struct {
			Data      string             `json:"data"`
			Metadata  map[string]float64 `json:"metadata"`
			SessionID int                `json:"sessionId"`
		}
		if json.Unmarshal(m.Params, &p) != nil {
			return
		}
		b.blit(p.Data, p.Metadata)
		b.adaptResolution()
		// cap ~30fps: the ack is the throttle (CDP sends nothing until
		// acked). Pace WITHOUT sleeping on the main loop — a Sleep here
		// would stall stdin/mouse processing for up to 33ms per frame.
		// Schedule the ack via a timer instead; cdp.Send is mutex-safe.
		sid := p.SessionID
		ack := func() { b.cdp.Send("Page.screencastFrameAck", map[string]any{"sessionId": sid}, b.sess) }
		if dt := time.Since(b.lastFrame); dt < 33*time.Millisecond {
			time.AfterFunc(33*time.Millisecond-dt, ack)
		} else {
			ack()
		}
		b.lastFrame = time.Now()
	case "Page.loadEventFired":
		b.statusMsg = "ready"
		b.snapDirty = true
		b.cdp.Send("Runtime.evaluate", map[string]any{"expression": injectJS}, b.sess)
		b.refreshTitle()
		b.glyphDirty = true
	case "Page.frameNavigated":
		var p struct {
			Frame struct {
				ParentID string `json:"parentId"`
				URL      string `json:"url"`
			} `json:"frame"`
		}
		if json.Unmarshal(m.Params, &p) == nil && p.Frame.ParentID == "" && p.Frame.URL != "" {
			b.url = p.Frame.URL
			b.glyphDirty = true
		}
	}
}

// ── cleanup ─────────────────────────────────────────────────────────────

func (b *Browse) cleanup() {
	b.term.Restore()
	b.cdp.Close()
	os.RemoveAll(b.frameDir)
	if b.tempProfile != "" {
		os.RemoveAll(b.tempProfile)
	}
}
