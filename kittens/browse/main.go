package browse

// kilix browse — Chrome in a kitty pane. See browse.go for the design.

import (
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/kovidgoyal/kitty/tools/cli"
)

func EntryPoint(root *cli.Command) {
	root.AddSubCommand(&cli.Command{
		Name:             "browse",
		ShortDescription: "Browse the web inside the terminal",
		Usage:            "[url]",
		HelpText: "Render Chrome inside the kitty/kilix pane: page pixels stream at full " +
			"resolution via the graphics protocol while page text is drawn as real, " +
			"selectable terminal glyphs. Requires google-chrome or chromium on PATH. " +
			"Keys: Ctrl+L url bar, Alt+Left/Right history, Ctrl+R reload, Ctrl+C copy " +
			"selection, Ctrl+Q quit. Shift+drag selects glyph text natively.",
		Run: func(cmd *cli.Command, args []string) (int, error) {
			url := "https://example.com"
			if len(args) > 0 {
				url = args[0]
			}
			if !strings.Contains(url, "://") {
				url = "https://" + url
			}
			return runBrowse(url)
		},
	})
}

func runBrowse(url string) (rc int, err error) {
	b, err := newBrowse(url)
	if err != nil {
		return 1, err
	}
	quit := make(chan struct{})
	defer func() {
		if r := recover(); r != nil {
			if _, isQuit := r.(quitSignal); !isQuit {
				b.cleanup()
				panic(r) // real crash: restore first, then propagate
			}
		}
		close(quit)
		b.cleanup()
		if err != nil {
			fmt.Fprintln(os.Stderr, "kilix browse:", err)
			rc = 1
		}
	}()

	// Register signal handlers BEFORE entering raw mode. Otherwise a
	// SIGTERM/SIGINT landing during b.start() (which makes several CDP
	// round-trips with 30s timeouts while Chrome boots) would take Go's
	// default disposition — terminate WITHOUT running our deferred
	// cleanup — leaving the tty in raw/alt-screen/mouse mode.
	winch := make(chan os.Signal, 1)
	signal.Notify(winch, syscall.SIGWINCH)
	term := make(chan os.Signal, 1)
	signal.Notify(term, syscall.SIGTERM, syscall.SIGINT)

	b.term.Enter()
	if err = b.start(); err != nil {
		return 1, err
	}
	// If a term signal arrived during startup, honor it now (cleanup runs
	// via the defer above).
	select {
	case <-term:
		return 0, nil
	default:
	}

	stdin := make(chan []byte, 16)
	go func() {
		for {
			buf := make([]byte, 65536)
			n, rerr := b.term.in.Read(buf)
			if n > 0 {
				select {
				case stdin <- buf[:n]:
				case <-quit:
					return
				}
			}
			if rerr != nil {
				return
			}
		}
	}()
	tick := time.NewTicker(200 * time.Millisecond)
	defer tick.Stop()

	for {
		select {
		case m, ok := <-b.cdp.Events:
			if !ok {
				err = <-b.cdp.Dead
				return 1, err
			}
			b.onCDPEvent(m)
			// drain any queued events before rendering
			for {
				select {
				case m, ok := <-b.cdp.Events:
					if !ok {
						err = <-b.cdp.Dead
						return 1, err
					}
					b.onCDPEvent(m)
					continue
				default:
				}
				break
			}
		case data := <-stdin:
			for _, ev := range b.term.Feed(data) {
				switch {
				case ev.Key != nil:
					b.onKey(ev.Key)
				case ev.Mouse != nil:
					b.onMouse(ev.Mouse)
				case ev.Paste != "":
					b.onPaste(ev.Paste)
				}
			}
		case <-winch:
			b.doResize()
		case <-term:
			return 0, nil
		case <-tick.C:
		}
		now := time.Now()
		if b.snapDirty && now.Sub(b.lastSnap) > 500*time.Millisecond &&
			now.Sub(b.lastInput) > 250*time.Millisecond {
			b.snapshot()
		}
		b.renderGlyphs()
	}
}
