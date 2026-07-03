package browse

// CDP over --remote-debugging-pipe: NUL-terminated JSON on the child's
// fds 3 (it reads) / 4 (it writes). exec.Cmd.ExtraFiles pins them.

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"sync"
	"time"
)

type CDPMsg struct {
	ID        int             `json:"id,omitempty"`
	Method    string          `json:"method,omitempty"`
	Params    json.RawMessage `json:"params,omitempty"`
	SessionID string          `json:"sessionId,omitempty"`
	Result    json.RawMessage `json:"result,omitempty"`
	Error     *struct {
		Code    int    `json:"code"`
		Message string `json:"message"`
	} `json:"error,omitempty"`
}

type CDP struct {
	proc    *exec.Cmd
	writer  *os.File
	mu      sync.Mutex
	nextID  int
	pending map[int]chan CDPMsg
	Events  chan CDPMsg
	Dead    chan error
}

var chromeCandidates = []string{"google-chrome", "google-chrome-stable", "chromium", "chromium-browser"}

func startCDP(width, height int, profile string) (*CDP, error) {
	var chrome string
	for _, c := range chromeCandidates {
		if p, err := exec.LookPath(c); err == nil {
			chrome = p
			break
		}
	}
	if chrome == "" {
		return nil, fmt.Errorf("no chrome/chromium binary on PATH")
	}
	// child fd 3 = read end of toChrome, child fd 4 = write end of fromChrome
	toChromeR, toChromeW, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	fromChromeR, fromChromeW, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	cmd := exec.Command(chrome,
		"--headless=new", "--remote-debugging-pipe",
		"--no-first-run", "--no-default-browser-check",
		"--hide-scrollbars", "--mute-audio",
		"--autoplay-policy=no-user-gesture-required",
		"--user-data-dir="+profile,
		fmt.Sprintf("--window-size=%d,%d", width, height),
		"about:blank")
	cmd.ExtraFiles = []*os.File{toChromeR, fromChromeW} // fds 3, 4
	cmd.Stdout, cmd.Stderr = nil, nil
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	toChromeR.Close()
	fromChromeW.Close()

	c := &CDP{
		proc: cmd, writer: toChromeW,
		pending: make(map[int]chan CDPMsg),
		Events:  make(chan CDPMsg, 256),
		Dead:    make(chan error, 1),
	}
	go c.readLoop(fromChromeR)
	return c, nil
}

func (c *CDP) readLoop(r *os.File) {
	br := bufio.NewReaderSize(r, 1<<20)
	for {
		raw, err := br.ReadBytes(0)
		if err != nil {
			c.Dead <- fmt.Errorf("chrome closed the CDP pipe: %w", err)
			close(c.Events)
			return
		}
		var m CDPMsg
		if json.Unmarshal(raw[:len(raw)-1], &m) != nil {
			continue
		}
		if m.ID != 0 {
			c.mu.Lock()
			ch := c.pending[m.ID]
			delete(c.pending, m.ID)
			c.mu.Unlock()
			if ch != nil {
				ch <- m
			}
			continue
		}
		// Never block the reader on Events: a full channel would starve
		// result delivery to c.pending (a synchronous Call would hang to its
		// full timeout while the main loop is blocked in that same Call).
		// Events are screencast frames + advisory page events; under a burst
		// (e.g. a page spamming Runtime events) drop the oldest rather than
		// stall the whole loop.
		select {
		case c.Events <- m:
		default:
			select {
			case <-c.Events: // drop oldest
			default:
			}
			select {
			case c.Events <- m:
			default:
			}
		}
	}
}

// Send fires a method without waiting for the result.
func (c *CDP) Send(method string, params any, session string) error {
	c.mu.Lock()
	c.nextID++
	id := c.nextID
	c.mu.Unlock()
	return c.write(id, method, params, session)
}

func (c *CDP) write(id int, method string, params any, session string) error {
	msg := map[string]any{"id": id, "method": method}
	if params != nil {
		msg["params"] = params
	}
	if session != "" {
		msg["sessionId"] = session
	}
	buf, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	_, err = c.writer.Write(append(buf, 0))
	return err
}

// Call fires a method and waits for its result (events keep flowing to
// c.Events via the reader goroutine).
func (c *CDP) Call(method string, params any, session string, timeout time.Duration, result any) error {
	ch := make(chan CDPMsg, 1)
	c.mu.Lock()
	c.nextID++
	id := c.nextID
	c.pending[id] = ch
	c.mu.Unlock()
	if err := c.write(id, method, params, session); err != nil {
		return err
	}
	select {
	case m := <-ch:
		if m.Error != nil {
			return fmt.Errorf("%s: %s", method, m.Error.Message)
		}
		if result != nil && m.Result != nil {
			return json.Unmarshal(m.Result, result)
		}
		return nil
	case <-time.After(timeout):
		c.mu.Lock()
		delete(c.pending, id)
		c.mu.Unlock()
		return fmt.Errorf("%s: CDP timeout", method)
	}
}

func (c *CDP) Close() {
	if c.proc.Process != nil {
		c.proc.Process.Signal(os.Interrupt)
		done := make(chan struct{})
		go func() { c.proc.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			c.proc.Process.Kill()
		}
	}
}
