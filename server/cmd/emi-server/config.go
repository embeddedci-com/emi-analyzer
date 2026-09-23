package main

import (
	"errors"
	"strconv"
	"strings"
)

// The development stack's secrets. They are in this public repository, so a server that runs
// with them has no secrets at all: anyone can forge a run token or a visitor cookie with the
// token secret, and the pepper is half of what protects a worker key. They are only ever
// used with -dev.
const (
	devPepper      = "dev-pepper-not-for-production-use-0123456789"
	devTokenSecret = "dev-token-secret-not-for-production-0123456789"
	devS3Key       = "minioadmin" // MinIO's own default root user and password
)

// minSecretLen matches what emi.New demands of the token secret; the pepper gets the same.
const minSecretLen = 32

type secrets struct {
	pepper      string
	tokenSecret string
	s3Key       string
	s3Secret    string
}

// loadSecrets reads the server's secrets from the environment.
//
// Without dev, every one of them must be set, and none may be a published default: the
// server refuses to start rather than run with credentials anyone can read here. With dev,
// an unset one falls back to the development value, which is what `make up` relies on.
func loadSecrets(dev bool, getenv func(string) string) (secrets, error) {
	var problems []string
	pick := func(name, devValue string, minLen int) string {
		v := getenv(name)
		switch {
		case dev && v == "":
			return devValue
		case dev:
			return v
		case v == "":
			problems = append(problems, name+" is not set")
		case v == devValue:
			problems = append(problems, name+" is the public development value")
		case len(v) < minLen:
			problems = append(problems, name+" is shorter than 32 bytes")
		}
		return v
	}
	s := secrets{
		pepper:      pick("EMI_PEPPER", devPepper, minSecretLen),
		tokenSecret: pick("EMI_TOKEN_SECRET", devTokenSecret, minSecretLen),
		s3Key:       pick("S3_ACCESS_KEY_ID", devS3Key, 0),
		s3Secret:    pick("S3_SECRET_ACCESS_KEY", devS3Key, 0),
	}
	if len(problems) > 0 {
		return secrets{}, errors.New(strings.Join(problems, "; ") +
			" (set them, or pass -dev for the local development stack)")
	}
	return s, nil
}

// envInt64 reads a positive whole number, falling back to def when it is unset or not one.
func envInt64(getenv func(string) string, name string, def int64) int64 {
	if n, err := strconv.ParseInt(strings.TrimSpace(getenv(name)), 10, 64); err == nil && n > 0 {
		return n
	}
	return def
}

// envBool reads a boolean environment variable: 1, true, yes or on.
func envBool(getenv func(string) string, name string) bool {
	switch strings.ToLower(strings.TrimSpace(getenv(name))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}
