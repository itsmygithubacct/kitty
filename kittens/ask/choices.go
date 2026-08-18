// License: GPLv3 Copyright: 2023, Kovid Goyal, <kovid at kovidgoyal.net>

package ask

import (
	"fmt"
	"github.com/kovidgoyal/kitty/tools/cli/markup"
	"github.com/kovidgoyal/kitty/tools/tty"
	"github.com/kovidgoyal/kitty/tools/tui/loop"
	"github.com/kovidgoyal/kitty/tools/utils"
	"github.com/kovidgoyal/kitty/tools/utils/style"
	"github.com/kovidgoyal/kitty/tools/wcswidth"
	"io"
	"os"
	"regexp"
	"strings"
	"unicode"
)

var _ = fmt.Print

type Choice struct {
	text          string
	idx           int
	color, letter string
}

func (self Choice) prefix() string {
	return string([]rune(self.text)[:self.idx])
}

func (self Choice) display_letter() string {
	return string([]rune(self.text)[self.idx])
}

func (self Choice) suffix() string {
	return string([]rune(self.text)[self.idx+1:])
}

type Range struct {
	start, end, y int
}

func (self *Range) has_point(x, y int) bool {
	return y == self.y && self.start <= x && x <= self.end
}

func truncate_at_space(text string, width int) (string, string) {
	truncated, p := wcswidth.TruncateToVisualLengthWithWidth(text, width)
	if len(truncated) == len(text) {
		return text, ""
	}
	i := strings.LastIndexByte(truncated, ' ')
	if i > 0 && p-i < 12 {
		p = i + 1
	}
	return text[:p], text[p:]
}

func extra_for(width, screen_width int) int {
	return max(0, screen_width-width)/2 + 1
}

var debugprintln = tty.DebugPrintln
var _ = debugprintln

func GetChoices(o *Options) (response string, err error) {
	response = ""
	lp, err := loop.New()
	if err != nil {
		return "", err
	}
	lp.MouseTrackingMode(loop.FULL_MOUSE_TRACKING)

	prefix_style_pat := regexp.MustCompile("^(?:\x1b\\[[^m]*?m)+")
	choice_order := make([]Choice, 0, len(o.Choices))
	clickable_ranges := make(map[string][]Range, 16)
	allowed := utils.NewSet[string](max(2, len(o.Choices)))
	response_on_accept := o.Default
	switch o.Type {
	case "yesno":
		allowed.AddItems("y", "n")
		if !allowed.Has(response_on_accept) {
			response_on_accept = "y"
		}
	case "choices":
		first_choice := ""
		for i, x := range o.Choices {
			letter, text, _ := strings.Cut(x, ":")
			color := ""
			if strings.Contains(letter, ";") {
				letter, color, _ = strings.Cut(letter, ";")
			}
			letter = strings.ToLower(letter)
			idx := strings.Index(strings.ToLower(text), letter)
			if idx < 0 {
				return "", fmt.Errorf("The choice letter %#v is not present in the choice text: %#v", letter, text)
			}
			idx = len([]rune(strings.ToLower(text)[:idx]))
			allowed.Add(letter)
			c := Choice{text: text, idx: idx, color: color, letter: letter}
			choice_order = append(choice_order, c)
			if i == 0 {
				first_choice = letter
			}
		}
		if !allowed.Has(response_on_accept) {
			response_on_accept = first_choice
		}
	}
	message := o.Message
	hidden_text_start_pos := -1
	hidden_text_end_pos := -1
	hidden_text := ""
	m := markup.New(true)
	replacement_text := fmt.Sprintf("Press %s or click to show", m.Green(o.UnhideKey))
	replacement_range := Range{-1, -1, -1}
	if message != "" && o.HiddenTextPlaceholder != "" {
		hidden_text_start_pos = strings.Index(message, o.HiddenTextPlaceholder)
		if hidden_text_start_pos > -1 {
			raw, err := io.ReadAll(os.Stdin)
			if err != nil {
				return "", fmt.Errorf("Failed to read hidden text from STDIN: %w", err)
			}
			hidden_text = strings.TrimRightFunc(utils.UnsafeBytesToString(raw), unicode.IsSpace)
			hidden_text_end_pos = hidden_text_start_pos + len(replacement_text)
			suffix := message[hidden_text_start_pos+len(o.HiddenTextPlaceholder):]
			message = message[:hidden_text_start_pos] + replacement_text + suffix
		}
	}

	draw_long_text := func(screen_width int, text string, msg_lines []string) []string {
		if screen_width < 3 {
			return msg_lines
		}
		if text == "" {
			msg_lines = append(msg_lines, "")
		} else {
			width := screen_width - 2
			prefix := prefix_style_pat.FindString(text)
			for text != "" {
				var t string
				t, text = truncate_at_space(text, width)
				t = strings.TrimSpace(t)
				msg_lines = append(msg_lines, strings.Repeat(" ", extra_for(wcswidth.Stringwidth(t), width))+m.Bold(prefix+t))
			}
		}
		return msg_lines
	}

	ctx := style.Context{AllowEscapeCodes: true}
	is_kilix_menu := strings.HasPrefix(o.Name, "kilix-start-")
	selected_choice := 0

	draw_kilix_menu := func(y, screen_width, screen_height int) {
		clickable_ranges = map[string][]Range{}
		panel_width := 12
		for _, choice := range choice_order {
			// Two border cells and one padding cell on either side. The old
			// +2 measurement reserved +4 while drawing, clipping the longest
			// label by exactly two characters.
			panel_width = max(panel_width, wcswidth.Stringwidth(choice.text)+4)
		}
		panel_width = min(panel_width, max(4, screen_width-1))
		// Match the shipped clickable chrome: dark inactive-tab surface,
		// Tango blue frame/selection, and high-contrast white labels.
		border := "\x1b[38;2;52;101;164m\x1b[48;2;46;52;54m"
		title := " " + o.Title + " "
		if wcswidth.Stringwidth(title) > panel_width {
			title, _ = wcswidth.TruncateToVisualLengthWithWidth(title, panel_width)
		}
		lp.MoveCursorTo(1, y+1)
		lp.QueueWriteString("\x1b[48;2;52;101;164m\x1b[38;2;255;255;255m" +
			title + strings.Repeat(" ", max(0, panel_width-wcswidth.Stringwidth(title))) + "\x1b[0m\r\n")
		lp.QueueWriteString(border + "┌" + strings.Repeat("─", panel_width-2) + "┐\x1b[0m\r\n")
		for i, choice := range choice_order {
			selected := i == selected_choice
			palette := "\x1b[38;2;255;255;255m\x1b[48;2;46;52;54m"
			accelerator := "\x1b[38;2;114;159;207m\x1b[48;2;46;52;54m\x1b[1m"
			if selected {
				palette = "\x1b[38;2;255;255;255m\x1b[48;2;52;101;164m"
				accelerator = "\x1b[38;2;255;255;255m\x1b[48;2;52;101;164m\x1b[1m"
			}
			label := choice.prefix() + accelerator + choice.display_letter()
			label += "\x1b[22m" + palette + choice.suffix()
			plain_width := wcswidth.Stringwidth(label)
			if plain_width > panel_width-4 {
				label, _ = wcswidth.TruncateToVisualLengthWithWidth(label, panel_width-4)
				plain_width = wcswidth.Stringwidth(label)
			}
			row := border + "│" + palette + " " + label + strings.Repeat(" ", max(0, panel_width-3-plain_width)) + border + "│\x1b[0m"
			lp.QueueWriteString(row)
			row_y := y + i + 2
			clickable_ranges[choice.letter] = []Range{{0, panel_width - 1, row_y}}
			if row_y+1 < screen_height {
				lp.QueueWriteString("\r\n")
			}
		}
		lp.QueueWriteString(border + "└" + strings.Repeat("─", panel_width-2) + "┘\x1b[0m")
	}

	draw_choice_boxes := func(y, screen_width, _ int, choices ...Choice) {
		clickable_ranges = map[string][]Range{}
		width := screen_width - 2
		current_line_length := 0
		type Item struct{ letter, text string }
		type Line = []Item
		var current_line Line
		lines := make([]Line, 0, 32)
		sep := "  "
		sep_sz := len(sep) + 2 // for the borders

		for _, choice := range choices {
			clickable_ranges[choice.letter] = make([]Range, 0, 4)
			text := " " + choice.prefix()
			color := choice.color
			if choice.color == "" {
				color = "green"
			}
			text += ctx.SprintFunc("fg=" + color)(choice.display_letter())
			text += choice.suffix() + " "
			sz := wcswidth.Stringwidth(text)
			if sz+sep_sz+current_line_length > width {
				lines = append(lines, current_line)
				current_line = nil
				current_line_length = 0
			}
			current_line = append(current_line, Item{choice.letter, text})
			current_line_length += sz + sep_sz
		}
		if len(current_line) > 0 {
			lines = append(lines, current_line)
		}

		highlight := func(text string) string {
			return m.Yellow(text)
		}

		top := func(text string, highlight_frame bool) (ans string) {
			ans = "╭" + strings.Repeat("─", wcswidth.Stringwidth(text)) + "╮"
			if highlight_frame {
				ans = highlight(ans)
			}
			return
		}

		middle := func(text string, highlight_frame bool) (ans string) {
			f := "│"
			if highlight_frame {
				f = highlight(f)
			}
			return f + text + f
		}

		bottom := func(text string, highlight_frame bool) (ans string) {
			ans = "╰" + strings.Repeat("─", wcswidth.Stringwidth(text)) + "╯"
			if highlight_frame {
				ans = highlight(ans)
			}
			return
		}

		print_line := func(add_borders func(string, bool) string, is_last bool, items ...Item) {
			type Position struct {
				letter  string
				x, size int
			}
			texts := make([]string, 0, 8)
			positions := make([]Position, 0, 8)
			x := 0
			for _, item := range items {
				text := item.text
				positions = append(positions, Position{item.letter, x, wcswidth.Stringwidth(text) + 2})
				text = add_borders(text, item.letter == response_on_accept)
				text += sep
				x += wcswidth.Stringwidth(text)
				texts = append(texts, text)
			}
			line := strings.TrimRightFunc(strings.Join(texts, ""), unicode.IsSpace)
			offset := extra_for(wcswidth.Stringwidth(line), width)
			for _, pos := range positions {
				x = pos.x
				x += offset
				clickable_ranges[pos.letter] = append(clickable_ranges[pos.letter], Range{x, x + pos.size - 1, y})
			}
			end := "\r\n"
			if is_last {
				end = ""
			}
			lp.QueueWriteString(strings.Repeat(" ", offset) + line + end)
			y++
		}
		lp.AllowLineWrapping(false)
		defer func() { lp.AllowLineWrapping(true) }()
		for i, boxed_line := range lines {
			print_line(top, false, boxed_line...)
			print_line(middle, false, boxed_line...)
			is_last := i == len(lines)-1
			print_line(bottom, is_last, boxed_line...)
		}
	}

	draw_yesno := func(y, screen_width, screen_height int) {
		yes := m.Green("Y") + "es"
		no := m.BrightRed("N") + "o"
		if y+3 <= screen_height {
			draw_choice_boxes(y, screen_width, screen_height, Choice{"Yes", 0, "green", "y"}, Choice{"No", 0, "red", "n"})
		} else {
			sep := strings.Repeat(" ", 3)
			text := yes + sep + no
			w := wcswidth.Stringwidth(text)
			x := extra_for(w, screen_width-2)
			nx := x + wcswidth.Stringwidth(yes) + len(sep)
			clickable_ranges = map[string][]Range{
				"y": {{x, x + wcswidth.Stringwidth(yes) - 1, y}},
				"n": {{nx, nx + wcswidth.Stringwidth(no) - 1, y}},
			}
			lp.QueueWriteString(strings.Repeat(" ", x) + text)
		}
	}

	draw_choice := func(y, screen_width, screen_height int) {
		if y+3 <= screen_height {
			draw_choice_boxes(y, screen_width, screen_height, choice_order...)
			return
		}
		clickable_ranges = map[string][]Range{}
		current_line := ""
		current_ranges := map[string]int{}
		width := screen_width - 2

		commit_line := func(add_newline bool) {
			x := extra_for(wcswidth.Stringwidth(current_line), width)
			text := strings.Repeat(" ", x) + current_line
			if add_newline {
				lp.Println(text)
			} else {
				lp.QueueWriteString(text)
			}
			for letter, sz := range current_ranges {
				clickable_ranges[letter] = []Range{{x, x + sz - 3, y}}
				x += sz
			}
			current_ranges = map[string]int{}
			y++
			current_line = ""
		}
		for _, choice := range choice_order {
			text := choice.prefix()
			spec := ""
			if choice.color != "" {
				spec = "fg=" + choice.color
			} else {
				spec = "fg=green"
			}
			if choice.letter == response_on_accept {
				spec += " u=straight"
			}
			text += ctx.SprintFunc(spec)(choice.display_letter())
			text += choice.suffix()
			text += "  "
			sz := wcswidth.Stringwidth(text)
			if sz+wcswidth.Stringwidth(current_line) >= width {
				commit_line(true)
			}
			current_line += text
			current_ranges[choice.letter] = sz
		}
		if current_line != "" {
			commit_line(false)
		}
	}

	draw_screen := func() error {
		lp.StartAtomicUpdate()
		defer lp.EndAtomicUpdate()
		lp.ClearScreen()
		msg_lines := make([]string, 0, 8)
		sz, err := lp.ScreenSize()
		if err != nil {
			return err
		}
		if is_kilix_menu {
			height := len(choice_order) + 3
			y := 0
			if strings.HasSuffix(o.Name, "-up") {
				y = max(0, int(sz.HeightCells)-height)
			}
			draw_kilix_menu(y, int(sz.WidthCells), int(sz.HeightCells))
			return nil
		}
		if message != "" {
			scanner := utils.NewLineScanner(message)
			for scanner.Scan() {
				msg_lines = draw_long_text(int(sz.WidthCells), scanner.Text(), msg_lines)
			}
		}
		y := int(sz.HeightCells) - len(msg_lines)
		y = max(0, (y/2)-2)
		lp.QueueWriteString(strings.Repeat("\r\n", y))
		for _, line := range msg_lines {
			if replacement_text != "" {
				before, _, ok := strings.Cut(line, replacement_text)
				if ok {
					x := wcswidth.Stringwidth(before)
					replacement_range = Range{x, x + wcswidth.Stringwidth(replacement_text), y}
				}
			}
			lp.Println(line)
			y++
		}
		if sz.HeightCells > 2 {
			lp.Println()
			y++
		}
		switch o.Type {
		case "yesno":
			draw_yesno(y, int(sz.WidthCells), int(sz.HeightCells))
		case "choices":
			draw_choice(y, int(sz.WidthCells), int(sz.HeightCells))
		}
		return nil
	}

	unhide := func() {
		if hidden_text != "" && message != "" {
			message = message[:hidden_text_start_pos] + hidden_text + message[hidden_text_end_pos:]
			hidden_text = ""
			_ = draw_screen()
		}
	}

	lp.OnInitialize = func() (string, error) {
		lp.SetCursorVisible(false)
		if o.Title != "" {
			lp.SetWindowTitle(o.Title)
		}
		return "", draw_screen()
	}

	lp.OnFinalize = func() string {
		lp.SetCursorVisible(true)
		return ""
	}

	lp.OnText = func(text string, from_key_event, in_bracketed_paste bool) error {
		text = strings.ToLower(text)
		if allowed.Has(text) {
			response = text
			lp.Quit(0)
		} else if hidden_text != "" && text == o.UnhideKey {
			unhide()
		} else if o.Type == "yesno" {
			lp.Quit(1)
		}
		return nil
	}

	lp.OnKeyEvent = func(ev *loop.KeyEvent) error {
		if ev.MatchesPressOrRepeat("esc") || ev.MatchesPressOrRepeat("ctrl+c") {
			ev.Handled = true
			lp.Quit(1)
		} else if is_kilix_menu && (ev.MatchesPressOrRepeat("up") || ev.MatchesPressOrRepeat("down") ||
			ev.MatchesPressOrRepeat("home") || ev.MatchesPressOrRepeat("end")) {
			ev.Handled = true
			if ev.MatchesPressOrRepeat("up") {
				selected_choice = (selected_choice + len(choice_order) - 1) % len(choice_order)
			} else if ev.MatchesPressOrRepeat("down") {
				selected_choice = (selected_choice + 1) % len(choice_order)
			} else if ev.MatchesPressOrRepeat("home") {
				selected_choice = 0
			} else {
				selected_choice = len(choice_order) - 1
			}
			response_on_accept = choice_order[selected_choice].letter
			_ = draw_screen()
		} else if is_kilix_menu && ev.MatchesPressOrRepeat("left") {
			ev.Handled = true
			for _, choice := range choice_order {
				if strings.Contains(choice.text, "Back") {
					response = choice.letter
					lp.Quit(0)
					break
				}
			}
		} else if ev.MatchesPressOrRepeat("enter") || ev.MatchesPressOrRepeat("kp_enter") ||
			(is_kilix_menu && ev.MatchesPressOrRepeat("right")) {
			ev.Handled = true
			response = response_on_accept
			lp.Quit(0)
		}
		return nil
	}

	lp.OnMouseEvent = func(ev *loop.MouseEvent) error {
		on_letter := ""
		for letter, ranges := range clickable_ranges {
			for _, r := range ranges {
				if r.has_point(ev.Cell.X, ev.Cell.Y) {
					on_letter = letter
					break
				}
			}
		}
		if on_letter != "" {
			if s, has_shape := lp.CurrentPointerShape(); !has_shape && s != loop.POINTER_POINTER {
				lp.PushPointerShape(loop.POINTER_POINTER)
			}
		} else {
			if _, has_shape := lp.CurrentPointerShape(); has_shape {
				lp.PopPointerShape()
			}
		}

		if ev.Event_type == loop.MOUSE_CLICK {
			if on_letter != "" {
				response = on_letter
				lp.Quit(0)
				return nil
			}
			if hidden_text != "" && replacement_range.has_point(ev.Cell.X, ev.Cell.Y) {
				unhide()
			}
		}
		return nil
	}

	lp.OnResize = func(old, news loop.ScreenSize) error {
		return draw_screen()
	}

	err = lp.Run()
	if err != nil {
		return "", err
	}
	ds := lp.DeathSignalName()
	if ds != "" {
		fmt.Println("Killed by signal: ", ds)
		lp.KillIfSignalled()
		return "", fmt.Errorf("Filled by signal: %s", ds)
	}
	return response, nil
}
