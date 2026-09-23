package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLoadSecretCreatesThenKeeps(t *testing.T) {
	path := filepath.Join(t.TempDir(), "secret.key")
	first, err := loadSecret(path)
	if err != nil || len(first) != secretLen {
		t.Fatalf("first load: %d bytes, %v", len(first), err)
	}
	if st, err := os.Stat(path); err != nil || st.Mode().Perm() != 0o600 {
		t.Fatalf("secret file mode: %v, %v", st.Mode(), err)
	}
	again, err := loadSecret(path)
	if err != nil || !bytes.Equal(first, again) {
		t.Fatalf("second load returned a different secret (%v)", err)
	}
}

// A damaged file used to be replaced without a word, which quietly invalidated every worker
// key issued with it. It is now an error, and the file is left as it was.
func TestLoadSecretRefusesADamagedFile(t *testing.T) {
	for name, content := range map[string]string{
		"not hex":   "this is not a secret\n",
		"too short": "abcd\n",
		"empty":     "",
	} {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "secret.key")
			if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
				t.Fatal(err)
			}
			_, err := loadSecret(path)
			if err == nil || !strings.Contains(err.Error(), "damaged") {
				t.Fatalf("err = %v, want a damaged-file error", err)
			}
			if got, _ := os.ReadFile(path); string(got) != content {
				t.Fatalf("the damaged file was overwritten: %q", got)
			}
		})
	}
}

// Each purpose gets its own key, stable for one secret and different between purposes.
func TestDerivedSecretsAreSeparate(t *testing.T) {
	master := bytes.Repeat([]byte{7}, secretLen)
	a, err := deriveSecrets(master)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := deriveSecrets(master)
	for _, k := range [][]byte{a.token, a.blob, a.pepper} {
		if len(k) != secretLen || bytes.Equal(k, master) {
			t.Fatalf("derived key %x is the wrong length or the master itself", k)
		}
	}
	if bytes.Equal(a.token, a.blob) || bytes.Equal(a.token, a.pepper) || bytes.Equal(a.blob, a.pepper) {
		t.Fatal("two purposes share a key")
	}
	if !bytes.Equal(a.token, b.token) || !bytes.Equal(a.pepper, b.pepper) {
		t.Fatal("derivation is not stable")
	}
}
