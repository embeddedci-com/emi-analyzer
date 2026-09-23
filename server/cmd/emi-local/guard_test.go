package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
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
		// Another page on this machine or this network is still another origin.
		{"another localhost port", "POST", "127.0.0.1:7465", "http://localhost:3000", http.StatusForbidden},
		{"same host, other port", "POST", "127.0.0.1:7465", "http://127.0.0.1:8080", http.StatusForbidden},
		{"a router's page", "POST", "127.0.0.1:7465", "http://192.168.1.1", http.StatusForbidden},
		{"localhost by another name", "POST", "127.0.0.1:7465", "http://localhost:7465", http.StatusForbidden},
		{"sandboxed or file page", "POST", "127.0.0.1:7465", "null", http.StatusForbidden},
		{"the vite dev server's proxy", "POST", "localhost:5175", "http://localhost:5175", http.StatusTeapot},
		{"a delete from this origin", "DELETE", "127.0.0.1:7465", "http://127.0.0.1:7465", http.StatusTeapot},
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

func TestGuardWantsJSONBodies(t *testing.T) {
	ok := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusTeapot) })
	h := guard(ok)

	cases := []struct {
		name, path, contentType, body string
		want                          int
	}{
		// What a cross-site form or a text/plain fetch sends without a preflight.
		{"text/plain", "/api/emi/projects", "text/plain", `{"name":"x"}`, http.StatusUnsupportedMediaType},
		{"form", "/api/emi/projects", "application/x-www-form-urlencoded", "name=x", http.StatusUnsupportedMediaType},
		{"no content type", "/api/emi/projects", "", `{"name":"x"}`, http.StatusUnsupportedMediaType},
		{"json", "/api/emi/projects", "application/json", `{"name":"x"}`, http.StatusTeapot},
		{"json with a charset", "/api/emi/projects", "application/json; charset=utf-8", `{}`, http.StatusTeapot},
		{"no body", "/api/local/worker/restart", "", "", http.StatusTeapot},
		// A signed blob upload carries the file's own type.
		{"blob upload", "/blob/uploads/x.zip", "application/zip", "PK", http.StatusTeapot},
	}
	for _, c := range cases {
		req := httptest.NewRequest("POST", c.path, strings.NewReader(c.body))
		req.Host = "127.0.0.1:7465"
		if c.contentType != "" {
			req.Header.Set("Content-Type", c.contentType)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != c.want {
			t.Errorf("%s: got %d, want %d", c.name, rec.Code, c.want)
		}
	}
}

func TestGuardRefusesFraming(t *testing.T) {
	h := guard(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	req := httptest.NewRequest("GET", "/", nil)
	req.Host = "127.0.0.1:7465"
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if got := rec.Header().Get("Content-Security-Policy"); got != "frame-ancestors 'self'" {
		t.Errorf("CSP %q", got)
	}
	if got := rec.Header().Get("X-Frame-Options"); got != "SAMEORIGIN" {
		t.Errorf("X-Frame-Options %q", got)
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
