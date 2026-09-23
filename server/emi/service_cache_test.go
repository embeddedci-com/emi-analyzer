package emi

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// No EMI response may be kept by a cache in front of the server.
//
// Regression test for a production failure. The origin sent no Cache-Control, so Cloudflare
// fell back to deciding by extension and cached GET .../artifacts/geometry.bin — then kept
// handing browsers a presigned URL that had expired. Storage rejects an expired signature
// without CORS headers, so it showed up as a CORS error on the bucket, and only for .bin
// artifacts; board.json, with an extension Cloudflare does not cache, kept working.
func TestEveryRouteForbidsCaching(t *testing.T) {
	svc, err := New(Deps{
		Store: &registerStore{}, Keys: stubKeys{}, Blob: stubBlob{},
		TokenSecret: bytes.Repeat([]byte("k"), 32),
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	// Stands in for the host's auth middleware and answers from inside it. If the header
	// still arrives, it was set outside the middleware, so it holds for a success and for a
	// rejection alike.
	allow := true
	userAuth := func(http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, _ *http.Request) {
			if allow {
				writeJSON(w, http.StatusOK, map[string]string{"url": "https://storage.example/presigned"})
				return
			}
			writeErr(w, http.StatusUnauthorized, "unauthorized")
		}
	}
	mux := http.NewServeMux()
	svc.Mount(mux, "/api", userAuth)

	cases := []struct {
		name  string
		allow bool
		path  string
	}{
		{"artifact url, signed in", true, "/api/emi/runs/r1/artifacts/geometry.bin"},
		{"artifact url, rejected", false, "/api/emi/runs/r1/artifacts/geometry.bin"},
		{"nested artifact url", true, "/api/emi/runs/r1/artifacts/nearfield/F_Cu/1000000000.bin"},
		// The real worker-key middleware, with no key: a 401 from inside the package.
		{"worker route without a key", true, "/api/emi-agent/runs"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			allow = c.allow
			w := httptest.NewRecorder()
			mux.ServeHTTP(w, httptest.NewRequest(http.MethodGet, c.path, nil))

			if w.Code == http.StatusNotFound {
				t.Fatalf("%s did not match a route", c.path)
			}
			if cc := w.Header().Get("Cache-Control"); !strings.Contains(cc, "no-store") {
				t.Fatalf("%s answered %d with Cache-Control %q; a shared cache may keep it",
					c.path, w.Code, cc)
			}
		})
	}
}
