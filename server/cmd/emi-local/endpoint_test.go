package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestEndpointIsPublishedAndTakenBack(t *testing.T) {
	path := filepath.Join(t.TempDir(), "nested", "endpoint.json")
	forget, err := writeEndpoint(path, "http://127.0.0.1:7465", "/data")
	if err != nil {
		t.Fatal(err)
	}

	var got endpoint
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatal(err)
	}
	if got.URL != "http://127.0.0.1:7465" || got.DataDir != "/data" || got.PID != os.Getpid() {
		t.Fatalf("endpoint = %+v", got)
	}

	forget()
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("the file outlived the server: %v", err)
	}
}

// A second app that started after this one owns the file. Exiting must not remove a pointer
// to a server that is still running.
func TestEndpointOfAnotherAppIsLeftAlone(t *testing.T) {
	path := filepath.Join(t.TempDir(), "endpoint.json")
	forget, err := writeEndpoint(path, "http://127.0.0.1:7465", "/data")
	if err != nil {
		t.Fatal(err)
	}
	other, _ := json.Marshal(endpoint{URL: "http://127.0.0.1:9999", PID: os.Getpid() + 1})
	if err := os.WriteFile(path, other, 0o600); err != nil {
		t.Fatal(err)
	}

	forget()

	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("the other app's file was removed: %v", err)
	}
	var got endpoint
	_ = json.Unmarshal(b, &got)
	if got.URL != "http://127.0.0.1:9999" {
		t.Fatalf("endpoint = %+v", got)
	}
}

func TestNoEndpointFileAsked(t *testing.T) {
	forget, err := writeEndpoint("", "http://127.0.0.1:7465", "/data")
	if err != nil {
		t.Fatal(err)
	}
	forget()
}
