package emi

import "testing"

// The digest becomes part of an object key, so anything the parser lets through is text a
// client chose putting itself into the storage namespace.
func TestNormaliseSHA256(t *testing.T) {
	const good = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

	for _, in := range []string{good, "  " + good + "  ", "E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855"} {
		got, err := normaliseSHA256(in)
		if err != nil || got != good {
			t.Errorf("normaliseSHA256(%q) = %q, %v; want %q, nil", in, got, err, good)
		}
	}

	// Absent is allowed: a client that cannot hash still gets the old random-key upload.
	if got, err := normaliseSHA256(""); err != nil || got != "" {
		t.Errorf(`normaliseSHA256("") = %q, %v; want "", nil`, got, err)
	}

	for _, in := range []string{
		"short",
		good + "a",            // too long
		"../../../etc/passwd", // traversal
		good[:63] + "/",       // a separator would make one key into two
		good[:63] + "z",       // not hex
		good[:60] + " abc",    // whitespace inside
		"g3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
	} {
		if _, err := normaliseSHA256(in); err == nil {
			t.Errorf("normaliseSHA256(%q) was accepted; want an error", in)
		}
	}
}
