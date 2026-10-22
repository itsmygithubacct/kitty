package choose_files

import (
	"errors"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"github.com/kovidgoyal/kitty/tools/disk_cache"
	"github.com/kovidgoyal/kitty/tools/utils/images"
)

type channelClearingPreviewRenderer struct {
	preview *ImagePreview
}

func (r channelClearingPreviewRenderer) Unmarshall(map[string]string) (any, error) {
	return nil, nil
}

func (r channelClearingPreviewRenderer) Render(string) (map[string][]byte, metadata, *images.ImageData, error) {
	r.preview.render_channel = nil
	return nil, metadata{}, nil, errors.New("fixture render failed")
}

func (r channelClearingPreviewRenderer) ShowMetadata(*Handler, ShowData) int { return 0 }
func (r channelClearingPreviewRenderer) String() string                      { return "channel-clearing fixture" }

func TestImagePreviewWorkerClosesItsCapturedChannel(t *testing.T) {
	input := filepath.Join(t.TempDir(), "preview-input")
	if err := os.WriteFile(input, []byte("fixture"), 0o600); err != nil {
		t.Fatal(err)
	}
	cache, err := disk_cache.NewDiskCache(filepath.Join(t.TempDir(), "cache"), 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	var wakeups atomic.Int32
	p := &ImagePreview{
		abspath: input, disk_cache: cache,
		WakeupMainThread: func() bool { wakeups.Add(1); return true },
	}
	p.renderer = channelClearingPreviewRenderer{preview: p}
	channel := make(chan render_data, 1)
	p.render_channel = channel
	done := make(chan struct{})
	go func() {
		p.start_rendering(channel)
		close(done)
	}()

	select {
	case result := <-channel:
		if result.err == nil || result.err.Error() != "fixture render failed" {
			t.Fatalf("render error was not delivered: %#v", result.err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("worker did not deliver its result on the captured channel")
	}
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("worker did not finish after the consumer cleared render_channel")
	}
	if !p.ready.Load() {
		t.Fatal("worker did not publish readiness")
	}
	if got := wakeups.Load(); got != 1 {
		t.Fatalf("wakeup count = %d, want 1", got)
	}
	if _, open := <-channel; open {
		t.Fatal("worker did not close its captured channel")
	}
}
