package local

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

func newTestBlob(t *testing.T) (*FileBlob, *httptest.Server) {
	t.Helper()
	b, err := NewFileBlob(t.TempDir(), bytes.Repeat([]byte("s"), 32), "/blob", "http://fallback")
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	mux.Handle("/blob/", b.Handler())
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return b, srv
}

func do(t *testing.T, method, u string, body []byte, ct string) (*http.Response, []byte) {
	t.Helper()
	req, _ := http.NewRequest(method, u, bytes.NewReader(body))
	if ct != "" {
		req.Header.Set("Content-Type", ct)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	out, _ := io.ReadAll(resp.Body)
	return resp, out
}

func TestPresignedPutThenGet(t *testing.T) {
	b, srv := newTestBlob(t)
	ctx := WithOrigin(context.Background(), srv.URL)

	put, _ := b.PresignPut(ctx, "runs/r1/nearfield/100000000.bin", "application/octet-stream", time.Minute)
	if !strings.HasPrefix(put, srv.URL+"/blob/runs/r1/nearfield/") {
		t.Fatalf("url %q does not use the request's origin", put)
	}
	if resp, body := do(t, http.MethodPut, put, []byte("field"), "application/octet-stream"); resp.StatusCode != 200 {
		t.Fatalf("put = %d %s", resp.StatusCode, body)
	}

	size, ct, err := b.Stat(ctx, "runs/r1/nearfield/100000000.bin")
	if err != nil || size != 5 || ct != "application/octet-stream" {
		t.Fatalf("stat = %d %q %v", size, ct, err)
	}

	get, _ := b.PresignGet(ctx, "runs/r1/nearfield/100000000.bin", time.Minute)
	resp, body := do(t, http.MethodGet, get, nil, "")
	if resp.StatusCode != 200 || string(body) != "field" {
		t.Fatalf("get = %d %q", resp.StatusCode, body)
	}

	// A GET URL is not a PUT URL.
	if resp, _ := do(t, http.MethodPut, get, []byte("x"), ""); resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("put on a get url = %d", resp.StatusCode)
	}
}

func TestSignatureBindsKeyAndExpiry(t *testing.T) {
	b, srv := newTestBlob(t)
	ctx := WithOrigin(context.Background(), srv.URL)
	get, _ := b.PresignGet(ctx, "uploads/p/board.kicad_pcb", time.Minute)

	// Same signature, another object.
	other := strings.Replace(get, "board.kicad_pcb", "secret.kicad_pcb", 1)
	if resp, _ := do(t, http.MethodGet, other, nil, ""); resp.StatusCode != http.StatusForbidden {
		t.Fatalf("signature reused for another key = %d", resp.StatusCode)
	}

	// Same signature, a later expiry.
	u, _ := url.Parse(get)
	q := u.Query()
	q.Set("exp", "99999999999")
	u.RawQuery = q.Encode()
	if resp, _ := do(t, http.MethodGet, u.String(), nil, ""); resp.StatusCode != http.StatusForbidden {
		t.Fatalf("extended expiry = %d", resp.StatusCode)
	}

	expired, _ := b.PresignGet(ctx, "uploads/p/board.kicad_pcb", -time.Minute)
	if resp, _ := do(t, http.MethodGet, expired, nil, ""); resp.StatusCode != http.StatusForbidden {
		t.Fatalf("expired url = %d", resp.StatusCode)
	}
}

// Keys come from the worker. None of them may name a file outside the store.
func TestKeysCannotLeaveTheStore(t *testing.T) {
	root := t.TempDir()
	b, err := NewFileBlob(filepath.Join(root, "store"), bytes.Repeat([]byte("s"), 32), "/blob", "http://x")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "outside"), []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"../../outside", "/../../outside", "a/../../../outside"} {
		p := b.objectPath(key)
		if !strings.HasPrefix(p, filepath.Join(root, "store", "objects")+string(filepath.Separator)) {
			t.Errorf("key %q maps to %q, outside the store", key, p)
		}
		if _, _, err := b.Stat(context.Background(), key); !errors.Is(err, emi.ErrNotFound) {
			t.Errorf("Stat(%q) = %v, want ErrNotFound", key, err)
		}
	}
}

func TestURLWithoutARequestUsesTheFallbackOrigin(t *testing.T) {
	b, _ := newTestBlob(t)
	u, _ := b.PresignGet(context.Background(), "k", time.Minute)
	if !strings.HasPrefix(u, "http://fallback/blob/k?") {
		t.Fatalf("url = %q", u)
	}
}
