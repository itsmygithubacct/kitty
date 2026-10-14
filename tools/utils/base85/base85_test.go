package base85

import (
	"fmt"
	"testing"

	"github.com/google/go-cmp/cmp"
)

var _ = fmt.Print

type pair struct {
	src, encoded string
}

func TestBase85(t *testing.T) {
	dst, dst2 := [8]byte{}, [8]byte{}
	decode := decoder_array()
	var el int
	for _, x := range []string{"M", "Ma", "Man", "Man "} {
		a := []byte(x)
		if el = encodeChunk(dst[:], a); el != EncodedLen(len(a)) {
			t.Fatalf("Encoded len for %#v wrong: %d != %d", x, el, EncodedLen(len(a)))
		}
		encoded := dst[:el]
		dl, bad_idx := decodeChunk(decode, dst2[:], encoded)
		if bad_idx != 0 {
			t.Fatalf("Decode for %#v returned bad data at: %d (%#v)", x, bad_idx, encoded)
		}
		if dl != DecodedLen(len(encoded)) {
			t.Fatalf("Decoded len for %#v wrong: %d != %d", x, dl, DecodedLen(len(a)))
		}
		decoded := string(dst2[:dl])
		if diff := cmp.Diff(x, decoded); diff != "" {
			t.Fatalf("Roundtrip failed for %#v: %s", x, diff)
		}
	}
	for _, p := range []pair{
		{"M", "O#"},
		{"Manual", "O<`_zVQc"},
		{
			"Man is distinguished, not only by his reason, but by this singular passion from " +
				"other animals, which is a lust of the mind, that by a perseverance of delight in " +
				"the continued and indefatigable generation of knowledge, exceeds the short " +
				"vehemence of any carnal pleasure.",
			"O<`^zX>%ZCX>)XGZfA9Ab7*B`EFf-gbRchTY<VDJc_3(Mb0BhMVRLV8EFfZabRc4RAarPHb0BkRZfA9DVR9gFVRLh7Z*CxFa&K)QZ**v7av))DX>DO_b1WctXlY|;AZc?TVIXXEb95kYW*~HEWgu;7Ze%PVbZB98AYyqSVIXj2a&u*NWpZI|V`U(3W*}r`Y-wj`bRcPNAarPDAY*TCbZKsNWn>^>Ze$>7Ze(R<VRUI{VPb4$AZKN6WpZJ3X>V>IZ)PBCZf|#NWn^b%EFfigV`XJzb0BnRWgv5CZ*p`Xc4cT~ZDnp_Wgu^6AYpEKAY);2ZeeU7aBO8^b9HiME&",
		},
	} {
		q := EncodeToString([]byte(p.src))
		if diff := cmp.Diff(p.encoded, q); diff != "" {
			t.Fatalf("Incorrect encoding of: %#v\n%s", p.src, diff)
		}
		sc, err := DecodeString(q)
		if err != nil {
			t.Fatalf("Failed to decode %#v with error: %s", p.src, err)
		}
		if diff := cmp.Diff(p.src, string(sc)); diff != "" {
			t.Fatalf("Failed to roundtrip %#v\n%s", p.src, diff)
		}
	}
}

func TestDecodeRejectsImpossibleTrailingLength(t *testing.T) {
	for _, encoded := range []string{"0", "000000"} {
		decoded, err := DecodeString(encoded)
		if len(decoded) != 0 {
			t.Fatalf("Expected no decoded bytes for impossible length %#v, got %#v", encoded, decoded)
		}
		corrupt, ok := err.(CorruptInputError)
		if !ok {
			t.Fatalf("Expected CorruptInputError for %#v, got %T: %v", encoded, err, err)
		}
		if int64(corrupt) != int64(len(encoded)) {
			t.Fatalf("Expected trailing byte index %d for %#v, got %d", len(encoded), encoded, corrupt)
		}
	}

	encoded := []byte("000000")
	dst := make([]byte, DecodedLen(len(encoded)))
	for i := range dst {
		dst[i] = 0xAA
	}
	n, err := Decode(dst, encoded)
	if n != 0 {
		t.Fatalf("Expected impossible input length to be rejected before decoding, got %d bytes", n)
	}
	for i, b := range dst {
		if b != 0xAA {
			t.Fatalf("Decode changed destination byte %d before rejecting the input length", i)
		}
	}
	if corrupt, ok := err.(CorruptInputError); !ok || int64(corrupt) != int64(len(encoded)) {
		t.Fatalf("Expected CorruptInputError at byte %d, got %T: %v", len(encoded), err, err)
	}

	_, err = DecodeString("0.0000")
	if corrupt, ok := err.(CorruptInputError); !ok || int64(corrupt) != 2 {
		t.Fatalf("Expected invalid-character error at byte 2, got %T: %v", err, err)
	}
}

func TestBase85RejectsOneCharacterTrailingGroup(t *testing.T) {
	if _, err := Decode(make([]byte, DecodedLen(1)), []byte("x")); err == nil {
		t.Fatal("Decode accepted an impossible one-character trailing group")
	}
	if _, err := DecodeString("x"); err == nil {
		t.Fatal("DecodeString accepted an impossible one-character trailing group")
	}
}
