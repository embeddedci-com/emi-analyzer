package emi

import "testing"

// The prefix is what lets this service share a bucket with the rest of the app, so what
// matters is that nothing can address an object outside it -- including keys that arrive
// from a worker.
func TestObjectKeyStaysInsideThePrefix(t *testing.T) {
	b := &S3Blob{prefix: DefaultKeyPrefix}

	for _, tc := range []struct{ in, want string }{
		{"runs/abc/nearfield/1e9.bin", "emi/runs/abc/nearfield/1e9.bin"},
		{"uploads/p1/u1/board.kicad_pcb", "emi/uploads/p1/u1/board.kicad_pcb"},

		// A leading slash is a spelling of the same key, not a different one.
		{"/runs/abc/x.bin", "emi/runs/abc/x.bin"},

		// Traversal. Each of these would reach outside emi/ if the key were used as given.
		{"../secrets/key", "emi/secrets/key"},
		{"runs/../../app/users.json", "emi/app/users.json"},
		{"../../../../etc/passwd", "emi/etc/passwd"},
		{"..", "emi/"},
	} {
		if got := b.objectKey(tc.in); got != tc.want {
			t.Errorf("objectKey(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestObjectKeyPrefixSpellings(t *testing.T) {
	// "emi", "emi/" and "/emi/" all name the same folder; normalisation happens once, at
	// construction, so the hot path is a string concatenation.
	for _, prefix := range []string{"emi", "emi/", "/emi/"} {
		cfg := S3Config{KeyPrefix: prefix}
		if got := normaliseKeyPrefix(cfg.KeyPrefix); got != "emi/" {
			t.Errorf("prefix %q normalised to %q, want %q", prefix, got, "emi/")
		}
	}
	if got := normaliseKeyPrefix(""); got != DefaultKeyPrefix {
		t.Errorf("empty prefix = %q, want the default %q", got, DefaultKeyPrefix)
	}
}
