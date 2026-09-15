package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestGuard(t *testing.T) {
	ok := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusTeapot) })
	h := guard(ok)

	cases := []struct {
		name, method, host, origin string
		want                       int
	}{
		{"the app itself", "GET", "127.0.0.1:7465", "", http.StatusTeapot},
		{"localhost", "POST", "localhost:7465", "http://localhost:7465", http.StatusTeapot},
		{"the worker container", "POST", "host.docker.internal:7465", "", http.StatusTeapot},
		{"ipv6 loopback", "GET", "[::1]:7465", "", http.StatusTeapot},
		// DNS rebinding: a site pointing its own name at 127.0.0.1 sends that name as Host.
		{"rebinding", "GET", "evil.example:7465", "", http.StatusForbidden},
		// A blind cross-site POST from a page on another origin.
		{"cross-site post", "POST", "127.0.0.1:7465", "https://evil.example", http.StatusForbidden},
		{"cross-site get is harmless", "GET", "127.0.0.1:7465", "https://evil.example", http.StatusTeapot},
		{"public ip as host", "GET", "8.8.8.8:7465", "", http.StatusForbidden},
	}
	for _, c := range cases {
		req := httptest.NewRequest(c.method, "/api/emi/projects", nil)
		req.Host = c.host
		if c.origin != "" {
			req.Header.Set("Origin", c.origin)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != c.want {
			t.Errorf("%s: got %d, want %d", c.name, rec.Code, c.want)
		}
	}
}

func TestListenRefusesNonLoopback(t *testing.T) {
	if _, err := listen("0.0.0.0:0", nil); err == nil {
		t.Fatal("listening on all interfaces was allowed")
	}
	ln, err := listen("127.0.0.1:0", nil)
	if err != nil {
		t.Fatal(err)
	}
	ln.Close()
}
