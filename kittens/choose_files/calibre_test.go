package choose_files

import (
	"bufio"
	"encoding/json"
	"errors"
	"io"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"
)

type calibre_response_harness struct {
	server     *calibre_server_process
	reader     *io.PipeReader
	writer     *io.PipeWriter
	readerDone chan struct{}
}

func new_calibre_response_harness(t *testing.T) *calibre_response_harness {
	t.Helper()
	reader, writer := io.Pipe()
	h := &calibre_response_harness{
		server:     &calibre_server_process{reader: bufio.NewReader(reader)},
		reader:     reader,
		writer:     writer,
		readerDone: make(chan struct{}),
	}
	go func() {
		h.server.read_responses()
		close(h.readerDone)
	}()
	t.Cleanup(func() { h.close_and_wait(t) })
	return h
}

func (h *calibre_response_harness) close_and_wait(t *testing.T) {
	t.Helper()
	_ = h.writer.Close()
	select {
	case <-h.readerDone:
	case <-time.After(time.Second):
		t.Error("Calibre response reader did not stop after the pipe closed")
	}
	_ = h.reader.Close()
}

func (h *calibre_response_harness) write_line(t *testing.T, line string) {
	t.Helper()
	if _, err := io.WriteString(h.writer, line+"\n"); err != nil {
		t.Fatal(err)
	}
}

func (h *calibre_response_harness) write_response(t *testing.T, response CalibreResponse) {
	t.Helper()
	line, err := json.Marshal(response)
	if err != nil {
		t.Fatal(err)
	}
	h.write_line(t, string(line))
}

func receive_calibre_response(t *testing.T, ch <-chan calibre_response_result) calibre_response_result {
	t.Helper()
	response, err := await_calibre_response(ch, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	return response
}

func TestCalibreResponsesRouteOutOfOrderByDistinctPath(t *testing.T) {
	h := new_calibre_response_harness(t)
	first, err := h.server.register_response("/books/first.epub", "cover-first")
	if err != nil {
		t.Fatal(err)
	}
	second, err := h.server.register_response("/books/second.epub", "cover-second")
	if err != nil {
		t.Fatal(err)
	}

	// The second request's metadata-only response arrives first. Path identity,
	// rather than a FIFO guess, must select its owner.
	h.write_response(t, CalibreResponse{Path: "/books/second.epub", Cover: "unknown-cover"})
	h.write_response(t, CalibreResponse{Path: "/books/second.epub"})
	h.write_response(t, CalibreResponse{Path: "/books/first.epub"})
	gotSecond := receive_calibre_response(t, second.channel)
	gotFirst := receive_calibre_response(t, first.channel)
	if gotSecond.response.Path != "/books/second.epub" || gotSecond.response.Cover != "" || gotSecond.matched_by_cover {
		t.Fatalf("second request received the wrong response: %#v", gotSecond)
	}
	if gotFirst.response.Path != "/books/first.epub" || gotFirst.matched_by_cover {
		t.Fatalf("first request received the wrong response: %#v", gotFirst)
	}
}

func TestCalibreUnresolvedSamePathTombstoneRefusesNewRequest(t *testing.T) {
	h := new_calibre_response_harness(t)
	path := "/books/retry.epub"
	late, err := h.server.register_response(path, "cover-late")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = await_calibre_response(late.channel, 0); !errors.Is(err, os.ErrDeadlineExceeded) {
		t.Fatalf("expected request deadline, got %v", err)
	}
	h.server.expire_response("cover-late", late)
	if _, err = h.server.register_response(path, "cover-retry"); err == nil {
		t.Fatal("a new same-path request was accepted while the old timeout tombstone remained")
	}

	// A different-path sentinel orders the reader's tombstone removal before
	// this goroutine registers the retry; Pipe.Write alone does not do that.
	sentinelPath := "/books/tombstone-sentinel.epub"
	sentinel, err := h.server.register_response(sentinelPath, "cover-sentinel")
	if err != nil {
		t.Fatal(err)
	}
	h.write_response(t, CalibreResponse{Path: path})
	h.write_response(t, CalibreResponse{Path: sentinelPath})
	if got := receive_calibre_response(t, sentinel.channel); got.response.Path != sentinelPath {
		t.Fatalf("sentinel received the wrong response: %#v", got)
	}
	retry, err := h.server.register_response(path, "cover-retry")
	if err != nil {
		t.Fatal(err)
	}
	h.write_response(t, CalibreResponse{Path: path})
	got := receive_calibre_response(t, retry.channel)
	if got.response.Path != path || got.matched_by_cover {
		t.Fatalf("retry received the wrong response: %#v", got)
	}
}

func TestCalibreMatchedCoverIsSufficientDespitePathDifference(t *testing.T) {
	h := new_calibre_response_harness(t)
	path, cover := "/books/original.epub", "cover-unique"
	request, err := h.server.register_response(path, cover)
	if err != nil {
		t.Fatal(err)
	}
	h.write_response(t, CalibreResponse{Path: "/normalized/books/original.epub", Cover: cover})
	got := receive_calibre_response(t, request.channel)
	if !got.matched_by_cover {
		t.Fatal("reply was not identified by its unique cover path")
	}
	if err := got.response.validate_request_identity(path, cover, got.matched_by_cover); err != nil {
		t.Fatalf("unique cover identity was rejected because the path representation differed: %v", err)
	}
}

func TestCalibreRequestPathUsesStableJSONWireRepresentation(t *testing.T) {
	path := "/books/é/with \"quotes\" and\\slashes/�.epub"
	payload, err := marshal_calibre_request(path, "cover-name")
	if err != nil {
		t.Fatal(err)
	}
	var wire_request map[string]string
	if err = json.Unmarshal(payload, &wire_request); err != nil {
		t.Fatal(err)
	}
	if wire_request["path"] != path {
		t.Fatalf("JSON wire path changed valid UTF-8 path: got %q, want %q", wire_request["path"], path)
	}
	h := new_calibre_response_harness(t)
	request, err := h.server.register_response(wire_request["path"], "cover-wire")
	if err != nil {
		t.Fatal(err)
	}
	h.write_response(t, CalibreResponse{Path: wire_request["path"]})
	got := receive_calibre_response(t, request.channel)
	if got.response.Path != path || got.matched_by_cover {
		t.Fatalf("decoded JSON wire path did not retain request ownership: %#v", got)
	}
	if _, err = marshal_calibre_request(string([]byte{0xff}), "cover-name"); err == nil {
		t.Fatal("a path that JSON would replace at the wire boundary was accepted")
	}
	if _, err = h.server.register_response(string([]byte{0xff}), "cover-name"); err == nil {
		t.Fatal("a non-UTF-8 path was admitted into response ownership")
	}
	too_long := strings.Repeat("p", max_calibre_wire_path_bytes+1)
	if _, err = marshal_calibre_request(too_long, "cover-name"); err == nil {
		t.Fatal("an over-limit path was admitted to the Calibre wire protocol")
	}
	if _, err = h.server.register_response(too_long, "cover-name"); err == nil {
		t.Fatal("an over-limit path was admitted into response ownership")
	}
}

func TestCalibrePendingOwnershipHasAFixedBound(t *testing.T) {
	server := &calibre_server_process{}
	for i := 0; i < max_calibre_pending_requests; i++ {
		path := "/books/" + strconv.Itoa(i) + ".epub"
		cover := "cover-" + strconv.Itoa(i)
		request, err := server.register_response(path, cover)
		if err != nil {
			t.Fatalf("register request %d: %v", i, err)
		}
		server.expire_response(cover, request)
	}
	if len(server.pending) != max_calibre_pending_requests || len(server.pending_by_path) != max_calibre_pending_requests {
		t.Fatal("timeout tombstones did not retain a consistent bounded ownership index")
	}
	if _, err := server.register_response("/books/over-limit.epub", "cover-over-limit"); err == nil {
		t.Fatal("Calibre accepted new ownership beyond its fixed pending limit")
	}
}

func TestCalibreReaderSkipsMalformedNoiseAndKeepsServing(t *testing.T) {
	h := new_calibre_response_harness(t)
	request, err := h.server.register_response("/books/noise.epub", "cover-noise")
	if err != nil {
		t.Fatal(err)
	}
	h.write_line(t, "plugin diagnostic that is not JSON")
	h.write_response(t, CalibreResponse{Path: "/books/noise.epub", Cover: "cover-noise"})
	got := receive_calibre_response(t, request.channel)
	if got.response.Cover != "cover-noise" || !got.matched_by_cover {
		t.Fatalf("valid response after malformed noise was not routed: %#v", got)
	}
}

func TestCalibreTerminalEOFFailsAndCollectsPendingOwnership(t *testing.T) {
	h := new_calibre_response_harness(t)
	live, err := h.server.register_response("/books/live.epub", "cover-live")
	if err != nil {
		t.Fatal(err)
	}
	timedOut, err := h.server.register_response("/books/timed-out.epub", "cover-timed-out")
	if err != nil {
		t.Fatal(err)
	}
	h.server.expire_response("cover-timed-out", timedOut)
	h.close_and_wait(t)
	if _, err = await_calibre_response(live.channel, time.Second); err == nil {
		t.Fatal("terminal EOF did not fail the live request")
	}
	if len(h.server.pending) != 0 || len(h.server.pending_by_path) != 0 {
		t.Fatal("terminal EOF retained live or timed-out request ownership")
	}
	if _, err = h.server.register_response("/books/later.epub", "cover-later"); err == nil {
		t.Fatal("a request was accepted after terminal stream failure")
	}
}

func TestCalibreResponseMustMatchPathWhenNoCoverMatched(t *testing.T) {
	if err := (CalibreResponse{Path: "/books/wanted.epub"}).validate_request_identity("/books/wanted.epub", "cover-wanted", false); err != nil {
		t.Fatalf("matching metadata-only response path rejected: %v", err)
	}
	if err := (CalibreResponse{Path: "/books/other.epub"}).validate_request_identity("/books/wanted.epub", "cover-wanted", false); err == nil {
		t.Fatal("mismatched metadata-only response path was accepted")
	}
}

func TestCalibreTimeoutKeepsRequestOwnership(t *testing.T) {
	h := new_calibre_response_harness(t)
	late, err := h.server.register_response("/books/late.epub", "late-owner")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := await_calibre_response(late.channel, 0); !errors.Is(err, os.ErrDeadlineExceeded) {
		t.Fatalf("expected deadline, got %v", err)
	}
	h.server.expire_response("late-owner", late)
	current, err := h.server.register_response("/books/current.epub", "current-owner")
	if err != nil {
		t.Fatal(err)
	}
	h.write_response(t, CalibreResponse{Path: "/books/late.epub"})
	h.write_response(t, CalibreResponse{Path: "/books/current.epub"})
	got := receive_calibre_response(t, current.channel)
	if got.response.Path != "/books/current.epub" {
		t.Fatalf("a timed-out owner's late reply was assigned to a current request: %#v", got)
	}
	if _, err := h.server.register_response("/books/late.epub", "next-owner"); err != nil {
		t.Fatalf("a consumed late reply did not retire its ownership tombstone: %v", err)
	}
}
