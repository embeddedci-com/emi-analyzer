package main

import (
	"net/http"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

// Identity for the development stack: every request is a signed-out visitor.
//
// Accounts are not this repository's business. Signed-in users, their sessions and their API
// keys belong to the host that mounts the analyzer -- embeddedci-server checks all three in its
// own middleware before calling emi.WithUser -- and the local app has one fixed user. So the
// harness verifies no credentials at all: each browser gets a private visitor space from
// emi.Visitors, the same policy the mounted analyzer applies to signed-out visitors.
func visitorAuth(visitors emi.Visitors) func(http.HandlerFunc) http.HandlerFunc {
	return func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			identity, setCookie := visitors.Identify(r)
			if setCookie != nil {
				// Before the handler writes anything, or the header is already on the wire.
				http.SetCookie(w, setCookie)
			}
			next(w, r.WithContext(emi.WithUser(r.Context(), identity)))
		}
	}
}
