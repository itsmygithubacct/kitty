package choose_files

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/kovidgoyal/kitty/tools/disk_cache"
	"github.com/kovidgoyal/kitty/tools/utils/images"
)

type xdndPreviewErrorRenderer struct{}

func (xdndPreviewErrorRenderer) Unmarshall(map[string]string) (any, error) { return nil, nil }
func (xdndPreviewErrorRenderer) Render(string) (map[string][]byte, metadata, *images.ImageData, error) {
	return map[string][]byte{"xdnd": []byte("ok")}, metadata{}, nil, nil
}
func (xdndPreviewErrorRenderer) ShowMetadata(*Handler, ShowData) int { return 0 }
func (xdndPreviewErrorRenderer) String() string                      { return "xdnd-test" }

// The completion path must close the channel it was started with. The consumer
// (ImagePreview.Render) clears p.render_channel as soon as it has received the
// result, so a completion that re-reads the field closes nil and panics.
//
// This is a scheduling race, so the test pins the schedule instead of hoping for
// it: with a single P, the producer parks in its send until the consumer takes
// the value, and the consumer then keeps running (the woken producer waits for a
// P) long enough to clear the field before the producer's deferred close runs.
// Without this the unfixed code only fails under the race detector.
func TestXDND54RenderCompletionUsesTheChannelCapturedBeforeConsumption(t *testing.T) {
	defer runtime.GOMAXPROCS(runtime.GOMAXPROCS(1))
	path := filepath.Join(t.TempDir(), "not-an-image")
	if err := os.WriteFile(path, []byte("sample"), 0600); err != nil {
		t.Fatal(err)
	}
	dc, err := disk_cache.NewDiskCache(filepath.Join(t.TempDir(), "cache"), 1024)
	if err != nil {
		t.Fatal(err)
	}
	for attempt := 0; attempt < 20; attempt++ {
		p := &ImagePreview{
			abspath: path + string(rune('a'+attempt)), disk_cache: dc, renderer: xdndPreviewErrorRenderer{},
			render_channel: make(chan render_data), WakeupMainThread: func() bool { return true },
		}
		if err := os.WriteFile(p.abspath, []byte("sample"), 0600); err != nil {
			t.Fatal(err)
		}
		done := make(chan any, 1)
		channel := p.render_channel
		go func() {
			defer func() { done <- recover() }()
			p.start_rendering(channel)
		}()
		// Let the producer render and park in its send; nobody is receiving yet.
		time.Sleep(50 * time.Millisecond)
		result := <-p.render_channel
		// ImagePreview.Render clears the field right after consuming the result.
		p.render_channel = nil
		if result.err != nil {
			t.Fatalf("successful renderer unexpectedly failed: %v", result.err)
		}
		if panicValue := <-done; panicValue != nil {
			t.Fatalf("attempt %d: completion panicked after its consumer cleared the channel: %v", attempt, panicValue)
		}
		if !p.ready.Load() {
			t.Fatal("preview was not marked ready")
		}
	}
}
