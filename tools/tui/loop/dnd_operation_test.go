package loop

import "testing"

// The dnd kitten has to tell a terminal that reports no operation at all from one
// that reports o=0, which the protocol defines as refusal or cancellation.
func TestDndOperationPresenceIsDecoded(t *testing.T) {
	cases := []struct {
		raw     string
		has     bool
		op      int
		chunked bool
	}{
		{"t=e:x=4:y=0:o=2", true, 2, false},
		{"t=e:x=4:y=0:o=1", true, 1, false},
		{"t=e:x=4:y=0:o=0", true, 0, false},
		{"t=e:x=4:y=1:o=0", true, 0, false},
		{"t=e:x=4:y=0", false, 0, false},
		{"t=e:x=4", false, 0, false},
		{"t=e:x=2:o=2", true, 2, false},
		// reported but unusable: still a report, so never mistaken for "unreported"
		{"t=e:x=4:y=0:o=", true, 0, false},
		{"t=e:x=4:y=0:o=zz", true, 0, false},
		{"t=m:o=1;aGk", true, 1, false},
	}
	for _, c := range cases {
		t.Run(c.raw, func(t *testing.T) {
			lp, err := New()
			if err != nil {
				t.Fatal(err)
			}
			var got *DndCommand
			lp.OnDnDData = func(cmd DndCommand) error { got = &cmd; return nil }
			if err = lp.handle_dnd([]byte(c.raw)); err != nil {
				t.Fatal(err)
			}
			if got == nil {
				t.Fatal("no command delivered")
			}
			if got.Has_operation != c.has || got.Operation != c.op {
				t.Errorf("got Has_operation=%v Operation=%d, want %v and %d", got.Has_operation, got.Operation, c.has, c.op)
			}
		})
	}
}

// The first chunk's metadata describes the whole chunked command.
func TestDndOperationPresenceSurvivesChunking(t *testing.T) {
	lp, err := New()
	if err != nil {
		t.Fatal(err)
	}
	var got []DndCommand
	lp.OnDnDData = func(cmd DndCommand) error { got = append(got, cmd); return nil }
	for _, raw := range []string{"t=m:o=0:m=1;aGk", "m=0;aGk"} {
		if err = lp.handle_dnd([]byte(raw)); err != nil {
			t.Fatal(err)
		}
	}
	if len(got) != 2 || !got[0].Has_operation || !got[1].Has_operation {
		t.Errorf("chunks lost the operation report: %+v", got)
	}
}

// Every value that a terminal could put after "o=" and what the decoder makes of
// it. The dnd kitten reads anything but 0, 1 and 2 as a refusal, and only a
// missing key as "unreported": kittens/dnd TestXDND5ReportedOperationNeverEqualsTheUnreportedMarker.
func TestDndOperationWireValues(t *testing.T) {
	cases := []struct {
		raw string
		has bool
		op  int
	}{
		{"t=e:x=4:y=0:o=-1", true, -1},
		{"t=e:x=4:y=0:o=-2", true, -2},
		{"t=e:x=4:y=0:o=-0", true, 0},
		{"t=e:x=4:y=0:o=3", true, 3},
		{"t=e:x=4:y=0:o=99", true, 99},
		{"t=e:x=4:y=0:o=2147483647", true, 2147483647},
		{"t=e:x=4:y=0:o=2147483648", true, 2147483648},
		{"t=e:x=4:y=0:o=9223372036854775807", true, 9223372036854775807},
		{"t=e:x=4:y=0:o=-9223372036854775808", true, -9223372036854775808},
		{"t=e:x=4:y=0:o=9223372036854775808", true, 0},
		{"t=e:x=4:y=0:o=-9223372036854775809", true, 0},
		{"t=e:x=4:y=0:o=99999999999999999999", true, 0},
		{"t=e:x=4:y=0:o=1.5", true, 0},
		{"t=e:x=4:y=0:o= 2", true, 0},
		{"t=e:x=4:y=0:o=0x2", true, 0},
		{"t=e:x=4:y=0:o=+2", true, 2},
		{"t=e:x=4:y=0:o=zz", true, 0},
		{"t=e:x=4:y=0:o=", true, 0},
		{"t=e:x=4:y=0:o=2:o=0", true, 0},
		{"t=e:x=4:y=0:o", false, 0},
		{"t=e:x=4:y=0:oo=2", false, 0},
		{"t=e:x=4:y=0", false, 0},
		{"t=e:x=4:y=0;o=2", false, 0},
	}
	for _, c := range cases {
		t.Run(c.raw, func(t *testing.T) {
			lp, err := New()
			if err != nil {
				t.Fatal(err)
			}
			var got *DndCommand
			lp.OnDnDData = func(cmd DndCommand) error { got = &cmd; return nil }
			if err = lp.handle_dnd([]byte(c.raw)); err != nil {
				t.Fatal(err)
			}
			if got == nil {
				t.Fatal("no command delivered")
			}
			if got.Has_operation != c.has || got.Operation != c.op {
				t.Errorf("got Has_operation=%v Operation=%d, want %v and %d", got.Has_operation, got.Operation, c.has, c.op)
			}
		})
	}
}

// Duplicates are fail-closed, including a malformed tail after valid Move.
func TestAggregateR2DuplicateOperationRefuses(t *testing.T) {
 for _, raw := range []string{"t=e:x=4:y=0:o=2:o=zz", "t=e:x=4:y=0:o=2:o=", "t=e:x=4:y=0:o=2:o=9223372036854775808", "t=e:x=4:y=0:o=2:o=2", "t=e:x=4:y=0:o=zz:o=2", "t=e:x=2:o=2:o=2"} {
  lp, err := New(); if err != nil { t.Fatal(err) }; var got *DndCommand
  lp.OnDnDData = func(c DndCommand) error { got = &c; return nil }
  if err = lp.handle_dnd([]byte(raw)); err != nil { t.Fatal(err) }
  if got == nil || !got.Has_operation || got.Operation != 0 { t.Fatalf("ambiguous operation admitted: %s: %+v", raw, got) }
 }
}
