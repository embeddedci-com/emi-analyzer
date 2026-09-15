package emi

import (
	"net/http"
	"strings"
	"testing"
)

func visitorRequest() *http.Request {
	r, _ := http.NewRequest("GET", "/api/emi/whoami", nil)
	return r
}

// Two signed-out visitors must not be able to read each other's boards. This is the whole
// point of the cookie: without it they shared one organisation, and a board is somebody's
// unreleased layout.
func TestSignedOutVisitorsAreSeparatedFromOneAnother(t *testing.T) {
	v := Visitors{Secret: []byte("cookie-signing-key")}

	first, cookieA := v.Identify(visitorRequest())
	second, cookieB := v.Identify(visitorRequest())

	if cookieA == nil || cookieB == nil {
		t.Fatal("a first-time visitor was not given a cookie")
	}
	if first.OrganizationID == second.OrganizationID {
		t.Fatal("two visitors landed in the same organisation")
	}
	for _, id := range []string{first.OrganizationID, second.OrganizationID} {
		if !strings.HasPrefix(id, VisitorOrgPrefix) || len(id) < len(VisitorOrgPrefix)+32 {
			t.Errorf("visitor organisation %q is not a random id", id)
		}
	}
}

// A returning visitor keeps their boards, or the space is useless.
func TestAVisitorCookieIsRemembered(t *testing.T) {
	v := Visitors{Secret: []byte("cookie-signing-key")}
	first, cookie := v.Identify(visitorRequest())

	r := visitorRequest()
	r.AddCookie(&http.Cookie{Name: cookie.Name, Value: cookie.Value})
	again, setAgain := v.Identify(r)

	if again.OrganizationID != first.OrganizationID {
		t.Errorf("a returning visitor got a different space: %q then %q",
			first.OrganizationID, again.OrganizationID)
	}
	if setAgain != nil {
		t.Error("a valid cookie was replaced rather than reused")
	}
}

// The cookie is signed: otherwise a visitor could type in somebody else's id and read their
// boards.
func TestATamperedVisitorCookieIsRejected(t *testing.T) {
	v := Visitors{Secret: []byte("cookie-signing-key")}
	victim, cookie := v.Identify(visitorRequest())

	for name, value := range map[string]string{
		"no signature":  strings.Split(cookie.Value, ".")[0],
		"bad signature": strings.Split(cookie.Value, ".")[0] + ".00ff",
		"forged id":     "deadbeefdeadbeefdeadbeefdeadbeef." + strings.Split(cookie.Value, ".")[1],
		"not hex":       strings.Split(cookie.Value, ".")[0] + ".zzzz",
		"empty":         "",
	} {
		r := visitorRequest()
		r.AddCookie(&http.Cookie{Name: VisitorCookieName, Value: value})
		got, setCookie := v.Identify(r)
		if got.OrganizationID == victim.OrganizationID {
			t.Errorf("%s: reached another visitor's space", name)
		}
		if setCookie == nil {
			t.Errorf("%s: an unusable cookie was accepted rather than replaced", name)
		}
	}
}

// A header is not an identity: nothing a client can simply set may name an organisation.
func TestHeadersAreNotAVisitorIdentity(t *testing.T) {
	v := Visitors{Secret: []byte("cookie-signing-key")}
	r := visitorRequest()
	r.Header.Set("X-Dev-Org", "someone-elses-org")
	r.Header.Set("Authorization", "Bearer anything")

	id, _ := v.Identify(r)
	if !id.Anonymous || !strings.HasPrefix(id.OrganizationID, VisitorOrgPrefix) {
		t.Errorf("a header named an organisation: %+v", id)
	}
}
