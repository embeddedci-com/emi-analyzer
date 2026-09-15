package main

import (
	"net"
	"net/http"
	"net/url"
	"strings"

	"github.com/embeddedci-com/emi-analyzer/server/local"
)

// guard is what makes a server with no sign-in safe to run.
//
// Listening on loopback keeps the network out, but not the web: any page open in the user's
// browser can send requests to 127.0.0.1. Two checks close that.
//
//   - Host must be a name this server is actually reached by. A site that points its own
//     domain at 127.0.0.1 (DNS rebinding) sends its own name as Host and is refused, so it
//     can never read a response.
//   - A request that changes something must not carry a foreign Origin. That stops a page on
//     another site from creating projects or runs with a blind cross-site POST.
//
// It also records the origin a request arrived on, which is what the blob URLs are minted
// against (see local.FileBlob).
func guard(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !allowedHost(r.Host) {
			http.Error(w, "this server only answers requests addressed to localhost", http.StatusForbidden)
			return
		}
		if !safeMethod(r.Method) {
			if o := r.Header.Get("Origin"); o != "" {
				u, err := url.Parse(o)
				if err != nil || !allowedHost(u.Host) {
					http.Error(w, "cross-origin request refused", http.StatusForbidden)
					return
				}
			}
		}
		next.ServeHTTP(w, r.WithContext(local.WithOrigin(r.Context(), "http://"+r.Host)))
	})
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
