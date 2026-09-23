package main

import (
	"crypto/hkdf"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"strings"
)

// secrets are the per-purpose keys derived from the one installation secret in secret.key.
//
// One file is easy to keep and back up, but one key for three jobs means a weakness in any
// one of them (a blob URL signature, a run token, the worker key hash) is a weakness in all
// of them. HKDF gives each job its own key, and none can be worked back from another.
type secrets struct {
	token  []byte // signs run tokens
	blob   []byte // signs blob URLs
	pepper []byte // hashes worker keys
}

const secretLen = 32

func deriveSecrets(master []byte) (secrets, error) {
	derive := func(purpose string) ([]byte, error) {
		return hkdf.Key(sha256.New, master, nil, "emi-local "+purpose, secretLen)
	}
	var s secrets
	var err error
	if s.token, err = derive("run tokens"); err != nil {
		return s, err
	}
	if s.blob, err = derive("blob urls"); err != nil {
		return s, err
	}
	if s.pepper, err = derive("worker keys"); err != nil {
		return s, err
	}
	return s, nil
}

// loadSecret returns the installation secret, creating it on first start.
//
// A file that is there but unreadable as a secret is an error, never a reason to make a new
// one. Replacing it would silently invalidate every worker key issued so far, and a file that
// changed under us is worth a person looking at.
func loadSecret(path string) ([]byte, error) {
	b, err := os.ReadFile(path)
	switch {
	case err == nil:
		s, dErr := hex.DecodeString(strings.TrimSpace(string(b)))
		if dErr != nil || len(s) < secretLen {
			return nil, fmt.Errorf("%s is damaged (it should hold %d hex-encoded bytes). "+
				"Restore it from a backup, or delete it to start over: worker keys you issued "+
				"with -issue-key then stop working", path, secretLen)
		}
		return s, nil
	case !errors.Is(err, fs.ErrNotExist):
		return nil, fmt.Errorf("read %s: %w", path, err)
	}

	s := make([]byte, secretLen)
	if _, err := rand.Read(s); err != nil {
		return nil, err
	}
	// O_EXCL: two copies starting at once must not each write a different secret.
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if errors.Is(err, fs.ErrExist) {
		return loadSecret(path)
	}
	if err != nil {
		return nil, fmt.Errorf("write %s: %w", path, err)
	}
	if _, err := f.WriteString(hex.EncodeToString(s) + "\n"); err != nil {
		f.Close()
		os.Remove(path)
		return nil, fmt.Errorf("write %s: %w", path, err)
	}
	if err := f.Close(); err != nil {
		os.Remove(path)
		return nil, fmt.Errorf("write %s: %w", path, err)
	}
	return s, nil
}

// loadSecrets reads (or creates) the installation secret and derives the per-purpose keys.
func loadSecrets(path string) (secrets, error) {
	master, err := loadSecret(path)
	if err != nil {
		return secrets{}, err
	}
	return deriveSecrets(master)
}
