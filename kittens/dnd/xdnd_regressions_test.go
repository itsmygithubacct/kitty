package dnd

import (
	"strings"
	"testing"
)

func TestXDND62DropDataRejectsOnePastLastMimeIndex(t *testing.T) {
	d := &dnd{drop_status: drop_status{offered_mimes: []string{"text/plain"}}}
	err := d.on_drop_data(DC{X: 2})
	if err == nil || !strings.Contains(err.Error(), "outside the offered MIMEs list") {
		t.Fatalf("one-past-the-end MIME index should be rejected, got %v", err)
	}
}

func TestXDND62DropDataRejectsMimeWithoutAcceptedDestination(t *testing.T) {
	// The index is in range, but no destination was registered for the MIME
	// type: the terminal must get an error, not crash the kitten.
	d := &dnd{drop_status: drop_status{offered_mimes: []string{"text/plain"}, accepted_mimes: []string{"text/plain"}, reading_data: true}}
	var err error
	func() {
		defer func() {
			if r := recover(); r != nil {
				t.Fatalf("drop data for a MIME without a destination panicked: %v", r)
			}
		}()
		err = d.on_drop_data(DC{X: 1, Payload: []byte("data"), Has_more: true})
	}()
	if err == nil || !strings.Contains(err.Error(), "with no accepted destination") {
		t.Fatalf("expected an error for a MIME without a destination, got %v", err)
	}
	// The same must hold for the end-of-data message, which dereferences the destination first.
	if err = d.on_drop_data(DC{X: 1}); err == nil || !strings.Contains(err.Error(), "with no accepted destination") {
		t.Fatalf("expected an error for end-of-data without a destination, got %v", err)
	}
}
