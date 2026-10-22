package choose_files

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	"unicode/utf8"

	"github.com/kovidgoyal/kitty/tools/icons"
	"github.com/kovidgoyal/kitty/tools/utils"
	"github.com/kovidgoyal/kitty/tools/utils/humanize"
	"github.com/kovidgoyal/kitty/tools/utils/images"
)

var _ = fmt.Print
var calibre_needs_cleanup atomic.Bool
var calibre_server_load_finished atomic.Bool

type calibre_server_process struct {
	proc            *exec.Cmd
	stdout          io.ReadCloser
	stdin           io.WriteCloser
	reader          *bufio.Reader
	request_mutex   sync.Mutex
	response_mutex  sync.Mutex
	pending         map[string]*calibre_pending_request
	pending_by_path map[string]string
	response_error  error
	file_extensions *utils.Set[string]
}

type CalibreMetadata struct {
	Title        string    `json:"title"`
	Authors      []string  `json:"authors"`
	Series       string    `json:"series"`
	Series_index float64   `json:"series_index"`
	Tags         []string  `json:"tags"`
	Rating       float64   `json:"rating"`
	Published    time.Time `json:"pubdate"`
	Timestamp    time.Time `json:"timestamp"`
}

type CalibreResponse struct {
	Path      string          `json:"path"`
	Filetypes []string        `json:"filetypes"`
	Cover     string          `json:"cover"`
	Error     string          `json:"error"`
	Metadata  CalibreMetadata `json:"metadata"`
}

type calibre_response_result struct {
	line             string
	response         CalibreResponse
	matched_by_cover bool
	err              error
}

type calibre_pending_request struct {
	path    string
	channel chan calibre_response_result
}

// Timed-out requests keep ownership until a reply or terminal stream failure.
// Bound both the number of tombstones and the strings each one retains.
const max_calibre_pending_requests = 256
const max_calibre_wire_path_bytes = 4096

func ReadLineWithTimeout(r io.Reader, timeout time.Duration) (string, error) {
	return read_line_with_timeout(bufio.NewReader(r), timeout)
}

func read_line_with_timeout(r *bufio.Reader, timeout time.Duration) (string, error) {
	type result struct {
		line string
		err  error
	}
	ch := make(chan result, 1)

	go func() {
		line, err := r.ReadString('\n')
		ch <- result{strings.TrimRight(line, "\n"), err}
	}()

	select {
	case res := <-ch:
		return res.line, res.err
	case <-time.After(timeout):
		return "", os.ErrDeadlineExceeded
	}
}

func read_response_with_timeout(r *bufio.Reader, timeout time.Duration) (string, CalibreResponse, error) {
	type result struct {
		line     string
		response CalibreResponse
		err      error
	}
	ch := make(chan result, 1)
	go func() {
		for {
			line, err := r.ReadString('\n')
			if err != nil {
				ch <- result{err: err}
				return
			}
			line = strings.TrimRight(line, "\n")
			var response CalibreResponse
			if json.Unmarshal([]byte(line), &response) != nil {
				continue
			}
			ch <- result{line: line, response: response}
			return
		}
	}()
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case ans := <-ch:
		return ans.line, ans.response, ans.err
	case <-timer.C:
		return "", CalibreResponse{}, os.ErrDeadlineExceeded
	}
}

func marshal_calibre_request(path, cover string) ([]byte, error) {
	if err := validate_calibre_wire_path(path); err != nil {
		return nil, err
	}
	if err := validate_calibre_wire_path(cover); err != nil {
		return nil, fmt.Errorf("invalid Calibre cover path: %w", err)
	}
	return json.Marshal(map[string]string{"path": path, "cover": cover})
}

func validate_calibre_wire_path(path string) error {
	if !utf8.ValidString(path) {
		return fmt.Errorf("Calibre paths must be valid UTF-8")
	}
	if len(path) > max_calibre_wire_path_bytes {
		return fmt.Errorf("Calibre path exceeds the %d-byte ownership limit", max_calibre_wire_path_bytes)
	}
	return nil
}

func (c *calibre_server_process) register_response(path, cover string) (*calibre_pending_request, error) {
	c.response_mutex.Lock()
	defer c.response_mutex.Unlock()
	if c.response_error != nil {
		return nil, c.response_error
	}
	if path == "" || cover == "" {
		return nil, fmt.Errorf("Calibre requests require non-empty path and cover identities")
	}
	if err := validate_calibre_wire_path(path); err != nil {
		return nil, err
	}
	if err := validate_calibre_wire_path(cover); err != nil {
		return nil, fmt.Errorf("invalid Calibre cover path: %w", err)
	}
	if c.pending == nil {
		c.pending = make(map[string]*calibre_pending_request)
	}
	if c.pending_by_path == nil {
		c.pending_by_path = make(map[string]string)
	}
	if cover, exists := c.pending_by_path[path]; exists {
		return nil, fmt.Errorf("a Calibre response is still unresolved for path %q (cover %q)", path, cover)
	}
	if len(c.pending) >= max_calibre_pending_requests {
		return nil, fmt.Errorf("Calibre response ownership limit reached (%d unresolved requests)", max_calibre_pending_requests)
	}
	if _, exists := c.pending[cover]; exists {
		return nil, fmt.Errorf("a Calibre response is already pending for cover %q", cover)
	}
	path, cover = strings.Clone(path), strings.Clone(cover)
	request := &calibre_pending_request{
		path: path, channel: make(chan calibre_response_result, 1),
	}
	c.pending[cover] = request
	c.pending_by_path[path] = cover
	return request, nil
}

func (c *calibre_server_process) remove_response_locked(cover string, request *calibre_pending_request) {
	if c.pending[cover] != request {
		return
	}
	delete(c.pending, cover)
	if c.pending_by_path[request.path] == cover {
		delete(c.pending_by_path, request.path)
	}
}

func (c *calibre_server_process) remove_response(cover string, request *calibre_pending_request) {
	c.response_mutex.Lock()
	c.remove_response_locked(cover, request)
	c.response_mutex.Unlock()
}

func (c *calibre_server_process) expire_response(cover string, request *calibre_pending_request) {
	c.response_mutex.Lock()
	if c.pending[cover] == request {
		request.channel = nil
	}
	c.response_mutex.Unlock()
}

func (c *calibre_server_process) fail_responses(err error) {
	c.response_mutex.Lock()
	defer c.response_mutex.Unlock()
	if c.response_error == nil {
		c.response_error = err
	}
	for cover, request := range c.pending {
		c.remove_response_locked(cover, request)
		if request.channel != nil {
			request.channel <- calibre_response_result{err: c.response_error}
		}
	}
}

func (c *calibre_server_process) read_responses() {
	for {
		line, err := c.reader.ReadString('\n')
		if err != nil {
			c.fail_responses(fmt.Errorf("error reading from calibre metadata server: %w", err))
			return
		}
		line = strings.TrimRight(line, "\n")
		var response CalibreResponse
		if err = json.Unmarshal([]byte(line), &response); err != nil {
			continue
		}
		c.response_mutex.Lock()
		cover := ""
		var request *calibre_pending_request
		matched_by_cover := false
		if response.Cover != "" {
			cover = response.Cover
			request = c.pending[cover]
			matched_by_cover = request != nil
		} else if cover = c.pending_by_path[response.Path]; cover != "" {
			request = c.pending[cover]
		}
		var response_channel chan calibre_response_result
		if request != nil {
			c.remove_response_locked(cover, request)
			response_channel = request.channel
		}
		c.response_mutex.Unlock()
		if response_channel != nil {
			response_channel <- calibre_response_result{line: line, response: response, matched_by_cover: matched_by_cover}
		}
	}
}

func await_calibre_response(ch <-chan calibre_response_result, timeout time.Duration) (calibre_response_result, error) {
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case response := <-ch:
		return response, response.err
	case <-timer.C:
		return calibre_response_result{}, os.ErrDeadlineExceeded
	}
}

func (r CalibreResponse) validate_request_path(path string) error {
	if r.Path != path {
		return fmt.Errorf("calibre metadata server returned metadata for %q while requesting %q", r.Path, path)
	}
	return nil
}

func (r CalibreResponse) validate_request_identity(path, cover string, matched_by_cover bool) error {
	if matched_by_cover {
		if r.Cover != cover {
			return fmt.Errorf("calibre metadata server returned cover %q while requesting %q", r.Cover, cover)
		}
		return nil
	}
	if r.Cover != "" {
		return fmt.Errorf("calibre metadata server returned unmatched cover %q", r.Cover)
	}
	return r.validate_request_path(path)
}

var calibre_server = sync.OnceValues(func() (ans *calibre_server_process, err error) {
	defer func() { calibre_server_load_finished.Store(true) }()
	ans = &calibre_server_process{}
	ans.proc = exec.Command("calibre-debug", "-c", "from calibre.ebooks.metadata.cli import *; simple_metadata_server()")
	ans.proc.Stderr = nil
	if ans.stdout, err = ans.proc.StdoutPipe(); err != nil {
		return nil, err
	}
	if ans.stdin, err = ans.proc.StdinPipe(); err != nil {
		ans.stdout.Close()
		return nil, err
	}
	set_process_session_leader(ans.proc)
	if err = ans.proc.Start(); err != nil {
		err = fmt.Errorf("calibre-debug executable not found in PATH, you must install the calibre program to preview these file types: %w", err)
		return
	}
	calibre_needs_cleanup.Store(true)
	ans.reader = bufio.NewReader(ans.stdout)
	payload, _ := marshal_calibre_request("", "")
	if _, err = ans.stdin.Write(append(payload, '\n')); err != nil {
		err = fmt.Errorf("error writing to calibre metadata server: %w", err)
		return
	}
	_, r, err := read_response_with_timeout(ans.reader, 2*time.Second)
	if err != nil {
		if errors.Is(err, os.ErrDeadlineExceeded) {
			err = fmt.Errorf("timed out waiting for response from calibre metadata server, make sure you are using calibre version >= 8.16")
		} else {
			err = fmt.Errorf("error reading from calibre metadata server: %w", err)
		}
		return
	}
	ans.file_extensions = utils.NewSet[string](len(r.Filetypes))
	for _, ft := range r.Filetypes {
		ans.file_extensions.Add("." + ft)
	}
	go ans.read_responses()
	return
})

func calibre_cleanup() {
	if !calibre_needs_cleanup.Load() {
		return
	}
	calibre_needs_cleanup.Store(false)
	calibre, _ := calibre_server()
	if calibre.stdin != nil {
		calibre.stdin.Close()
	}
	if calibre.stdout != nil {
		calibre.stdout.Close()
	}
	if calibre.proc != nil {
		calibre.proc.Wait()
	}
}

func IsSupportedByCalibre(path string) bool {
	ext := strings.ToLower(filepath.Ext(filepath.Base(path)))
	if len(ext) > 1 {
		if calibre_server_load_finished.Load() {
			if calibre, err := calibre_server(); err == nil {
				return calibre.file_extensions.Has(ext)
			}
		} else {
			// we dont want to delay the render loop waiting for data from
			// calibre-server as it causes flicker because the atomic update
			// timeout expires, so use a default set of extensions.
			go func() { _, _ = calibre_server() }()
			return slices.Contains([]string{"cb7", "azw3", "kepub", "zip", "htmlz", "rar", "lrf", "cbr", "tpz", "azw1", "mobi", "lit", "pdf", "updb", "pml", "pobi", "fbz", "azw", "fb2", "cbc", "rtf", "snb", "opf", "txt", "epub", "oebzip", "txtz", "imp", "docx", "odt", "pdb", "rb", "prc", "html", "chm", "pmlz", "cbz", "lrx", "azw4"}, ext[1:])
		}
	}
	return false
}

const CALIBRE_METADATA_KEY = "calibre-metadata.json"

type calibre_renderer int

func (c calibre_renderer) Unmarshall(m map[string]string) (any, error) {
	data, err := os.ReadFile(m[CALIBRE_METADATA_KEY])
	if err != nil {
		return nil, err
	}
	var ans CalibreResponse
	if err = json.Unmarshal(data, &ans); err != nil {
		return nil, err
	}
	return &ans, nil
}

func (c calibre_renderer) Render(path string) (m map[string][]byte, mi metadata, img *images.ImageData, err error) {
	cpath, err := os.CreateTemp("", "")
	if err != nil {
		return
	}
	defer func() {
		cpath.Close()
		os.Remove(cpath.Name())
	}()
	calibre, err := calibre_server()
	if err != nil {
		return
	}
	payload, err := marshal_calibre_request(path, cpath.Name())
	if err != nil {
		return
	}
	calibre.request_mutex.Lock()
	request, err := calibre.register_response(path, cpath.Name())
	if err != nil {
		calibre.request_mutex.Unlock()
		return
	}
	_, err = calibre.stdin.Write(append(payload, '\n'))
	if err != nil {
		calibre.fail_responses(fmt.Errorf("error writing to calibre metadata server: %w", err))
	}
	calibre.request_mutex.Unlock()
	if err != nil {
		return
	}
	response, err := await_calibre_response(request.channel, 30*time.Second)
	if err != nil {
		if errors.Is(err, os.ErrDeadlineExceeded) {
			calibre.expire_response(cpath.Name(), request)
		} else {
			calibre.remove_response(cpath.Name(), request)
		}
		return
	}
	lb := []byte(response.line)
	reply := response.response
	if err = reply.validate_request_identity(path, cpath.Name(), response.matched_by_cover); err != nil {
		return
	}
	if reply.Cover == cpath.Name() {
		var ip ImagePreviewRenderer
		if m, mi, img, err = ip.Render(cpath.Name()); err != nil {
			return
		}
	} else {
		m = make(map[string][]byte)
	}
	mi.custom = &reply
	m[CALIBRE_METADATA_KEY] = lb
	return
}

func (c calibre_renderer) ShowMetadata(h *Handler, s ShowData) (offset int) {
	w := func(text string, center bool) {
		if s.height > offset {
			offset += h.render_wrapped_text_in_region(text, s.x, s.y+offset, s.width, s.height-offset, center)
		}
	}
	ext := filepath.Ext(s.abspath)
	text := fmt.Sprintf("%s: %s", ext, humanize.Bytes(uint64(s.metadata.Size())))
	icon := icons.IconForPath(s.abspath)
	w(icon+"  "+text, true)
	r := s.custom_metadata.custom.(*CalibreResponse)
	w("Title: "+r.Metadata.Title, false)
	w("Authors: "+strings.Join(r.Metadata.Authors, " & "), false)
	if r.Metadata.Series != "" {
		w(fmt.Sprintf("Series: Book %g of %s", r.Metadata.Series_index, r.Metadata.Series), false)
	}
	if len(r.Metadata.Tags) > 0 {
		w("Tags: "+strings.Join(r.Metadata.Authors, ", "), false)
	}
	return
}

func (c calibre_renderer) String() string {
	return "Calibre"
}

func NewCalibrePreview(
	abspath string, metadata fs.FileInfo, opts Settings, WakeupMainThread func() bool,
) Preview {
	c := calibre_renderer(0)
	if ans, err := NewImagePreview(abspath, metadata, opts, WakeupMainThread, c); err == nil {
		return ans
	} else {
		return NewErrorPreview(err)
	}
}
