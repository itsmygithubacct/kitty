package ask

import "testing"

func pressCalculator(s *calculatorState, keys ...string) {
	for _, key := range keys {
		s.press(key)
	}
}

func TestCalculatorArithmeticAndEditing(t *testing.T) {
	s := newCalculatorState()
	pressCalculator(s, "1", "2", "+", "8", "=")
	if s.entry != "20" || s.err != "" {
		t.Fatalf("12 + 8 = got entry %q error %q", s.entry, s.err)
	}
	pressCalculator(s, "C", "9", "BS", "4", ".", "5", "+/-")
	if s.entry != "-4.5" {
		t.Fatalf("editing produced %q", s.entry)
	}
}

func TestCalculatorChainingAndErrors(t *testing.T) {
	s := newCalculatorState()
	pressCalculator(s, "2", "+", "3", "*", "4", "=")
	if s.entry != "20" {
		t.Fatalf("left-to-right chain produced %q", s.entry)
	}
	pressCalculator(s, "C", "1", "/", "0", "=")
	if s.err != calculatorError {
		t.Fatalf("division by zero produced %q", s.err)
	}
	s.press("CE")
	if s.err != "" || s.entry != "0" {
		t.Fatalf("CE failed to clear error: %#v", s)
	}
	pressCalculator(s, "+/-", "sqrt")
	if s.err != "" { // negative zero remains zero
		t.Fatalf("sqrt(0) unexpectedly failed: %q", s.err)
	}
}

func TestCalculatorFunctionsAndMemory(t *testing.T) {
	s := newCalculatorState()
	pressCalculator(s, "9", "sqrt")
	if s.entry != "3" {
		t.Fatalf("sqrt(9) = %q", s.entry)
	}
	pressCalculator(s, "MS", "C", "MR", "+", "2", "=")
	if s.entry != "5" {
		t.Fatalf("memory calculation = %q", s.entry)
	}
	pressCalculator(s, "M+", "C", "MR")
	if s.entry != "8" {
		t.Fatalf("M+ result = %q", s.entry)
	}
	pressCalculator(s, "C", "5", "0", "+", "1", "0", "%", "=")
	if s.entry != "55" {
		t.Fatalf("50 + 10%% = %q", s.entry)
	}
}
