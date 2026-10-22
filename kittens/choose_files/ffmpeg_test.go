package choose_files

import (
	"errors"
	"testing"

	"github.com/kovidgoyal/kitty/tools/utils/images"
)

func TestFFmpegWorkersPublishResultsBeforeCompletion(t *testing.T) {
	release := make(chan struct{})
	started := make(chan struct{}, 2)
	wantMetadata := FFMpegMetadata{Format: FFMpegFormat{Duration: "3.5"}}
	wantMetadataError := errors.New("metadata worker error")
	wantImage := &images.ImageData{}
	wantThumbnailError := errors.New("thumbnail worker error")
	type result struct {
		metadata       FFMpegMetadata
		metadataError  error
		img            *images.ImageData
		thumbnailError error
	}
	done := make(chan result, 1)
	go func() {
		metadata, metadataError, img, thumbnailError := run_ffmpeg_workers(
			func() (FFMpegMetadata, error) {
				started <- struct{}{}
				<-release
				return wantMetadata, wantMetadataError
			},
			func() (*images.ImageData, error) {
				started <- struct{}{}
				<-release
				return wantImage, wantThumbnailError
			},
		)
		done <- result{metadata, metadataError, img, thumbnailError}
	}()
	<-started
	<-started
	close(release)
	got := <-done
	if got.metadata.Format.Duration != wantMetadata.Format.Duration || got.metadataError != wantMetadataError {
		t.Fatalf("metadata result was not published before completion: %#v", got)
	}
	if got.img != wantImage || got.thumbnailError != wantThumbnailError {
		t.Fatalf("thumbnail result was not published before completion: %#v", got)
	}
}
