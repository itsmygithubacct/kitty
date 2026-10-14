// License: GPLv3

package cli

import "testing"

func TestRemoveCommonPrefixRequiresContainment(t *testing.T) {
	group := MatchGroup{Matches: []*Match{
		{Word: "--foo=bar"},
		{Word: "--foo"},
	}}
	if got := group.remove_common_prefix(); got != "" {
		t.Fatalf("Expected no common prefix removal, got %#v", got)
	}
	if group.Matches[0].Word != "--foo=bar" || group.Matches[1].Word != "--foo" {
		t.Fatalf("Mismatched candidates were modified: %#v", group.Matches)
	}

	group = MatchGroup{Matches: []*Match{
		{Word: "--foo=bar"},
		{Word: "--foo=baz"},
	}}
	if got := group.remove_common_prefix(); got != "--foo=" {
		t.Fatalf("Expected valid common prefix removal, got %#v", got)
	}
	if group.Matches[0].Word != "bar" || group.Matches[1].Word != "baz" {
		t.Fatalf("Valid prefix was not removed: %#v", group.Matches)
	}
}
