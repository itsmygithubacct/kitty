// License: GPLv3 Copyright: 2022, Kovid Goyal, <kovid at kovidgoyal.net>

package utils

import (
	"fmt"
	"testing"
)

var _ = fmt.Print

func TestLongestCommon(t *testing.T) {
	check := func(actual, expected string, items ...string) {
		if actual != expected {
			t.Fatalf("Failed with %#v\nExpected: %#v\nActual:   %#v", items, expected, actual)
		}
	}
	p := func(expected string, items ...string) { check(Prefix(items), expected, items...) }
	s := func(expected string, items ...string) { check(Suffix(items), expected, items...) }
	p("abc", "abc", "abcd")
	p("abc", "abcd", "abc")
	p("", "abc", "xy")
	p("", "abcd", "")
	s("def", "abcdef", "def")
	s("def", "def", "abcdef")
	s("bc", "zabc", "xbc")
	s("", "abcdef", "xyz")
	s("", "abcd", "")
}
