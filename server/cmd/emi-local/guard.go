package main

import (
	"mime"
	"net"
	"net/http"
	"strings"

	"github.com/embeddedci-com/emi-analyzer/server/local"
)

// guard is what makes a server with no sign-in safe to run.
//
// Listening on loopback keeps the network out, but not the web: any page open in the user's
// browser can send requests to 127.0.0.1. These checks close that.
//
//   - Host must be a name this server is actually reached by. A site that points its own
//     domain at 127.0.0.1 (DNS rebinding) sends its own name as Host and is refused, so it
//     can never read a response.
//   - A request that changes something must come from this origin exactly, scheme, host and
//     port, or carry no Origin at all (the worker, the KiCad plugin, curl). Comparing the host
//     alone once let a page on any other localhost port, or on any private IP, create projects
//     and runs with a blind cross-site POST.
//   - A change with a body under /api must say it is JSON. A browser sends a cross-site
//     text/plain POST without asking first; application/json always needs a preflight, which
//     this server never grants.
//   - No other site may frame the app, so it cannot be clickjacked.
//
// It also records the origin a request arrived on, which is what the blob URLs are minted
// against (see local.FileBlob).
func guard(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		h.Set("Content-Security-Policy", "frame-ancestors 'self'")
		h.Set("X-Frame-Options", "SAMEORIGIN")
		h.Set("X-Content-Type-Options", "nosniff")
		if !allowedHost(r.Host) {
			http.Error(w, "this server only answers requests addressed to localhost", http.StatusForbidden)
			return
		}
		self := "http://" + r.Host
		if !safeMethod(r.Method) {
			if o := r.Header.Get("Origin"); o != "" && !strings.EqualFold(o, self) {
				http.Error(w, "cross-origin request refused", http.StatusForbidden)
				return
			}
			if strings.HasPrefix(r.URL.Path, "/api/") && hasBody(r) && !isJSON(r.Header.Get("Content-Type")) {
				http.Error(w, "send the request body as application/json", http.StatusUnsupportedMediaType)
				return
			}
		}
		next.ServeHTTP(w, r.WithContext(local.WithOrigin(r.Context(), self)))
	})
}

func hasBody(r *http.Request) bool {
	return r.ContentLength > 0 || (r.ContentLength < 0 && r.Body != nil && r.Body != http.NoBody)
}

func isJSON(contentType string) bool {
	mt, _, err := mime.ParseMediaType(contentType)
	return err == nil && mt == "application/json"
}

func safeMethod(m string) bool {
	return m == http.MethodGet || m == http.MethodHead || m == http.MethodOptions
}

// allowedHost accepts the loopback names, and the names a container uses to reach its host.
func allowedHost(hostport string) bool {
	host := hostport
	if h, _, err := net.SplitHostPort(hostport); err == nil {
		host = h
	}
	host = strings.Trim(strings.ToLower(host), "[]")
	switch host {
	case "localhost", "host.docker.internal", "host.containers.internal", "host.lima.internal":
		return true
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return false
	}
	if ip.IsLoopback() {
		return true
	}
	// A worker on a bridge network reaches the host by the bridge gateway's address when the
	// engine has no host.docker.internal. Private addresses only: a public IP as Host is never
	// how this server is reached.
	return ip.IsPrivate()
}
