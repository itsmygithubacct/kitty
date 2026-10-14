package dnd

import (
	"fmt"
	"math/rand"
	"testing"
)

// A model-based probe, independent of the hand-written expectations in the other
// tests: random sequences of gestures run through the production path, and an
// independent model says which sources are gone after each one. The model knows
// only the contract: a gesture ends in a Move that deletes exactly the items the
// target fully read during that gesture; a Copy, refusal, cancellation or an
// abandoned gesture deletes nothing; after a Move the sources are no longer offered.
func TestXDND435RandomGestureSequencesDeleteOnlyWhatTheFinalMoveTransferred(t *testing.T) {
	names := []string{"file.txt", "tree", "empty", "link", "unsent.txt"}
	for seed := int64(1); seed <= 300; seed++ {
		rng := rand.New(rand.NewSource(seed))
		f := new_finish_fixture(t, "esc-key")
		var log []string
		gone := map[string]bool{}
		for g := 0; g < 6; g++ {
			f.start_gesture(t)
			ended := []string{"copy", "move", "refuse", "cancel", "legacy-move", "abandon", "partial-move"}[rng.Intn(7)]
			accepted := 2
			if ended == "copy" {
				accepted = 1
			}
			f.target_drops(t, accepted)
			read := map[string]bool{}
			partial := -1 // a request whose writes are still pending would be finished by any later request's drain, so it goes last
			for i, n := range names[:4] {
				if rng.Intn(2) == 0 {
					continue
				}
				if ended == "partial-move" && n == "tree" {
					partial = i
					continue
				}
				f.serve_complete(t, i)
				read[n] = true
			}
			if partial >= 0 {
				f.serve_started(t, partial)
			}
			log = append(log, fmt.Sprintf("%s read=%v", ended, read))
			switch ended {
			case "copy":
				f.event(t, 4, 0, 1)
			case "move", "partial-move":
				f.event(t, 4, 0, 2)
			case "legacy-move":
				f.event(t, 4, 0, operation_not_reported)
			case "refuse":
				f.event(t, 4, 0, 0)
			case "cancel":
				f.event(t, 4, 1, 0)
			case "abandon":
				f.d.finish_drag("EIO")
			}
			// A pending request makes the entire Move incomplete. Every source
			// must survive; only a completed gesture may delete its read items.
			moved := ended == "move" || ended == "legacy-move" || (ended == "partial-move" && partial < 0)
			if moved {
				for n := range read {
					gone[n] = true
				}
			}
			for _, n := range names {
				if f.exists(n) == gone[n] {
					t.Fatalf("seed %d gesture %d (%v): %s exists=%v, model says gone=%v", seed, g, log, n, f.exists(n), gone[n])
				}
			}
			if moved {
				break // a Move stops offering the sources
			}
		}
	}
}
