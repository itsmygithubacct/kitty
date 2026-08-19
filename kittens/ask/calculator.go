// License: GPLv3

package ask

import (
	"math"
	"strconv"
	"strings"

	"github.com/kovidgoyal/kitty/tools/cli/markup"
	"github.com/kovidgoyal/kitty/tools/tui/loop"
	"github.com/kovidgoyal/kitty/tools/wcswidth"
)

const calculatorError = "Cannot divide by zero"

type calculatorState struct {
	acc, memory float64
	op          string
	entry       string
	fresh       bool
	err         string
}

func newCalculatorState() *calculatorState {
	return &calculatorState{entry: "0", fresh: true}
}

func formatCalculatorNumber(v float64) string {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return calculatorError
	}
	if v == math.Trunc(v) && math.Abs(v) < 1e16 {
		return strconv.FormatInt(int64(v), 10)
	}
	return strconv.FormatFloat(v, 'g', 12, 64)
}

func (s *calculatorState) value() float64 {
	v, err := strconv.ParseFloat(s.entry, 64)
	if err != nil {
		return 0
	}
	return v
}

func calculate(a float64, op string, b float64) (float64, bool) {
	switch op {
	case "+":
		return a + b, true
	case "-":
		return a - b, true
	case "*":
		return a * b, true
	case "/":
		if b == 0 {
			return 0, false
		}
		return a / b, true
	}
	return b, true
}

func (s *calculatorState) fail(msg string) { s.err, s.op, s.fresh = msg, "", true }
func (s *calculatorState) set(v float64)   { s.entry, s.fresh = formatCalculatorNumber(v), true }

func (s *calculatorState) press(key string) {
	if key == "C" {
		s.acc, s.op, s.entry, s.fresh, s.err = 0, "", "0", true, ""
		return
	}
	if key == "CE" {
		s.entry, s.fresh, s.err = "0", true, ""
		return
	}
	if s.err != "" && key != "MC" {
		return
	}
	if len(key) == 1 && key[0] >= '0' && key[0] <= '9' {
		if s.fresh || s.entry == "0" {
			s.entry = key
		} else if len(s.entry) < 16 {
			s.entry += key
		}
		s.fresh = false
		return
	}
	switch key {
	case ".":
		if s.fresh {
			s.entry, s.fresh = "0.", false
		} else if !strings.Contains(s.entry, ".") {
			s.entry += "."
		}
	case "BS":
		if !s.fresh {
			s.entry = strings.TrimSuffix(s.entry, s.entry[len(s.entry)-1:])
			if s.entry == "" || s.entry == "-" {
				s.entry = "0"
			}
		}
	case "+/-":
		if s.entry != "0" && s.entry != "0." {
			if strings.HasPrefix(s.entry, "-") {
				s.entry = s.entry[1:]
			} else {
				s.entry = "-" + s.entry
			}
		}
	case "+", "-", "*", "/":
		cur := s.value()
		if s.op != "" && !s.fresh {
			if v, ok := calculate(s.acc, s.op, cur); ok {
				s.acc, s.entry = v, formatCalculatorNumber(v)
			} else {
				s.fail(calculatorError)
				return
			}
		} else {
			s.acc = cur
		}
		s.op, s.fresh = key, true
	case "=":
		if s.op == "" {
			s.fresh = true
			return
		}
		if v, ok := calculate(s.acc, s.op, s.value()); ok {
			s.acc, s.entry, s.op, s.fresh = v, formatCalculatorNumber(v), "", true
		} else {
			s.fail(calculatorError)
		}
	case "sqrt":
		if s.value() < 0 {
			s.fail("Invalid input")
		} else {
			s.set(math.Sqrt(s.value()))
		}
	case "1/x":
		if s.value() == 0 {
			s.fail(calculatorError)
		} else {
			s.set(1 / s.value())
		}
	case "%":
		s.set(s.acc * s.value() / 100)
	case "MC":
		s.memory = 0
	case "MR":
		s.set(s.memory)
	case "MS":
		s.memory = s.value()
	case "M+":
		s.memory += s.value()
	}
}

type calculatorButton struct {
	label, key     string
	x0, y0, x1, y1 int
}

var calculatorKeys = [][]calculatorButton{
	{{"MC", "MC", 0, 0, 0, 0}, {"MR", "MR", 0, 0, 0, 0}, {"MS", "MS", 0, 0, 0, 0}, {"M+", "M+", 0, 0, 0, 0}, {"⌫", "BS", 0, 0, 0, 0}},
	{{"CE", "CE", 0, 0, 0, 0}, {"C", "C", 0, 0, 0, 0}, {"√", "sqrt", 0, 0, 0, 0}, {"%", "%", 0, 0, 0, 0}, {"1/x", "1/x", 0, 0, 0, 0}},
	{{"7", "7", 0, 0, 0, 0}, {"8", "8", 0, 0, 0, 0}, {"9", "9", 0, 0, 0, 0}, {"÷", "/", 0, 0, 0, 0}, {"×", "*", 0, 0, 0, 0}},
	{{"4", "4", 0, 0, 0, 0}, {"5", "5", 0, 0, 0, 0}, {"6", "6", 0, 0, 0, 0}, {"−", "-", 0, 0, 0, 0}, {"+", "+", 0, 0, 0, 0}},
	{{"1", "1", 0, 0, 0, 0}, {"2", "2", 0, 0, 0, 0}, {"3", "3", 0, 0, 0, 0}, {"0", "0", 0, 0, 0, 0}, {".", ".", 0, 0, 0, 0}},
	{{"±", "+/-", 0, 0, 0, 0}, {"=", "=", 0, 0, 0, 0}},
}

func GetCalculator(o *Options) (response string, err error) {
	lp, err := loop.New()
	if err != nil {
		return "", err
	}
	lp.MouseTrackingMode(loop.FULL_MOUSE_TRACKING)
	state := newCalculatorState()
	buttons := make([]calculatorButton, 0, 24)
	hovered := ""
	m := markup.New(true)

	draw := func() error {
		lp.StartAtomicUpdate()
		defer lp.EndAtomicUpdate()
		lp.ClearScreen()
		buttons = buttons[:0]
		sz, err := lp.ScreenSize()
		if err != nil {
			return err
		}
		w, h := int(sz.WidthCells), int(sz.HeightCells)
		buttonW, gap, panelW := 9, 2, 5*9+4*2
		startX, startY := max(1, (w-panelW)/2), max(0, (h-24)/2)
		display := state.entry
		if state.err != "" {
			display = state.err
		}
		if state.memory != 0 {
			display = "M  " + display
		}
		if w < panelW+2 || h < 24 {
			lp.MoveCursorTo(1, 1)
			lp.QueueWriteString("Calculator needs at least 55 columns × 24 rows.\r\nResize the pane or press Esc to close.")
			return nil
		}
		lp.MoveCursorTo(startX+1, startY+1)
		lp.QueueWriteString(m.Bold("Calculator"))
		shown, _ := wcswidth.TruncateToVisualLengthWithWidth(display, panelW-4)
		// The inner display is panelW-2 cells wide. Reserve its last cell as
		// right padding so the closing border stays aligned with both corners.
		pad := max(0, panelW-3-wcswidth.Stringwidth(shown))
		lp.MoveCursorTo(startX+1, startY+3)
		lp.QueueWriteString("╭" + strings.Repeat("─", panelW-2) + "╮")
		lp.MoveCursorTo(startX+1, startY+4)
		lp.QueueWriteString("│" + strings.Repeat(" ", pad) + m.Bold(shown) + " │")
		lp.MoveCursorTo(startX+1, startY+5)
		lp.QueueWriteString("╰" + strings.Repeat("─", panelW-2) + "╯")
		for row, source := range calculatorKeys {
			for col, sourceButton := range source {
				x := startX + col*(buttonW+gap)
				y := startY + 6 + row*3
				b := sourceButton
				b.x0, b.y0, b.x1, b.y1 = x, y, x+buttonW-1, y+2
				buttons = append(buttons, b)
				frame := func(s string) string {
					if hovered == b.key {
						return m.Yellow(s)
					}
					return s
				}
				label := b.label
				left := (buttonW - 2 - wcswidth.Stringwidth(label)) / 2
				right := buttonW - 2 - left - wcswidth.Stringwidth(label)
				lp.MoveCursorTo(x+1, y+1)
				lp.QueueWriteString(frame("╭" + strings.Repeat("─", buttonW-2) + "╮"))
				lp.MoveCursorTo(x+1, y+2)
				lp.QueueWriteString(frame("│") + strings.Repeat(" ", left) + m.Green(label) + strings.Repeat(" ", right) + frame("│"))
				lp.MoveCursorTo(x+1, y+3)
				lp.QueueWriteString(frame("╰" + strings.Repeat("─", buttonW-2) + "╯"))
			}
		}
		return nil
	}
	buttonAt := func(x, y int) string {
		for _, b := range buttons {
			if x >= b.x0 && x <= b.x1 && y >= b.y0 && y <= b.y1 {
				return b.key
			}
		}
		return ""
	}
	lp.OnInitialize = func() (string, error) {
		lp.SetCursorVisible(false)
		if o.Title != "" {
			lp.SetWindowTitle(o.Title)
		}
		return "", draw()
	}
	lp.OnFinalize = func() string { lp.SetCursorVisible(true); return "" }
	lp.OnText = func(text string, _, _ bool) error {
		for _, r := range text {
			key := string(r)
			if r == 'c' || r == 'C' {
				key = "C"
			}
			if strings.ContainsRune("0123456789.+-*/%=cC", r) {
				state.press(key)
				_ = draw()
			}
		}
		return nil
	}
	lp.OnKeyEvent = func(ev *loop.KeyEvent) error {
		switch {
		case ev.MatchesPressOrRepeat("esc") || ev.MatchesPressOrRepeat("ctrl+c"):
			ev.Handled = true
			lp.Quit(0)
		case ev.MatchesPressOrRepeat("enter") || ev.MatchesPressOrRepeat("kp_enter"):
			ev.Handled = true
			state.press("=")
			_ = draw()
		case ev.MatchesPressOrRepeat("backspace") || ev.MatchesPressOrRepeat("delete"):
			ev.Handled = true
			state.press("BS")
			_ = draw()
		}
		return nil
	}
	lp.OnMouseEvent = func(ev *loop.MouseEvent) error {
		key := buttonAt(ev.Cell.X, ev.Cell.Y)
		if key != hovered {
			hovered = key
			_ = draw()
		}
		if key != "" {
			if _, ok := lp.CurrentPointerShape(); !ok {
				lp.PushPointerShape(loop.POINTER_POINTER)
			}
		} else if _, ok := lp.CurrentPointerShape(); ok {
			lp.PopPointerShape()
		}
		if ev.Event_type == loop.MOUSE_CLICK && key != "" {
			state.press(key)
			_ = draw()
		}
		return nil
	}
	lp.OnResize = func(_, _ loop.ScreenSize) error { return draw() }
	if err = lp.Run(); err != nil {
		return "", err
	}
	return state.entry, nil
}
