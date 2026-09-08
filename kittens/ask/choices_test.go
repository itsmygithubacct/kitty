package ask

import "testing"

func TestKilixMenuVisibleChoices(t *testing.T) {
	for _, tc := range [][5]int{
		{0, 13, 16, 0, 13}, {12, 13, 16, 0, 13},
		{0, 13, 7, 0, 4}, {7, 13, 7, 4, 8}, {12, 13, 7, 9, 13},
		{12, 13, 4, 12, 13}, {0, 0, 8, 0, 0},
	} {
		first, last := kilixMenuVisibleChoices(tc[0], tc[1], tc[2])
		if first != tc[3] || last != tc[4] {
			t.Fatalf("selection/count/rows %v: got [%d,%d), want [%d,%d)", tc[:3], first, last, tc[3], tc[4])
		}
	}
}
