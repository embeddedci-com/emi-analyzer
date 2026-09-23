package main

import (
	"strings"
	"testing"
)

func envMap(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

var goodEnv = map[string]string{
	"EMI_PEPPER":           strings.Repeat("p", 32),
	"EMI_TOKEN_SECRET":     strings.Repeat("t", 32),
	"S3_ACCESS_KEY_ID":     "access",
	"S3_SECRET_ACCESS_KEY": "secret",
}

func with(k, v string) map[string]string {
	m := map[string]string{}
	for kk, vv := range goodEnv {
		m[kk] = vv
	}
	m[k] = v
	return m
}

// The development secrets are in this repository. A server that starts with them, or with
// nothing, is a server anyone can forge tokens for.
func TestSecretsRefuseDefaultsOutsideDev(t *testing.T) {
	if _, err := loadSecrets(false, envMap(goodEnv)); err != nil {
		t.Fatalf("real secrets refused: %v", err)
	}
	for name, env := range map[string]map[string]string{
		"nothing set":       {},
		"no pepper":         with("EMI_PEPPER", ""),
		"dev pepper":        with("EMI_PEPPER", devPepper),
		"short pepper":      with("EMI_PEPPER", "short"),
		"dev token secret":  with("EMI_TOKEN_SECRET", devTokenSecret),
		"no token secret":   with("EMI_TOKEN_SECRET", ""),
		"minio access key":  with("S3_ACCESS_KEY_ID", "minioadmin"),
		"minio secret key":  with("S3_SECRET_ACCESS_KEY", "minioadmin"),
		"no s3 credentials": with("S3_ACCESS_KEY_ID", ""),
	} {
		_, err := loadSecrets(false, envMap(env))
		if err == nil || !strings.Contains(err.Error(), "-dev") {
			t.Errorf("%s: got %v, want a refusal that mentions -dev", name, err)
		}
	}
}

func TestSecretsFallBackInDev(t *testing.T) {
	s, err := loadSecrets(true, envMap(nil))
	if err != nil {
		t.Fatal(err)
	}
	if s.pepper != devPepper || s.tokenSecret != devTokenSecret || s.s3Key != devS3Key || s.s3Secret != devS3Key {
		t.Fatalf("dev defaults = %+v", s)
	}
	// A value that is set wins, in dev too.
	s, err = loadSecrets(true, envMap(goodEnv))
	if err != nil || s.pepper != goodEnv["EMI_PEPPER"] || s.s3Key != "access" {
		t.Fatalf("dev with values = %+v, %v", s, err)
	}
}

func TestEnvBool(t *testing.T) {
	for v, want := range map[string]bool{"1": true, "true": true, "Yes": true, "on": true, "": false, "0": false, "no": false} {
		if got := envBool(envMap(map[string]string{"X": v}), "X"); got != want {
			t.Errorf("%q: got %v, want %v", v, got, want)
		}
	}
}
