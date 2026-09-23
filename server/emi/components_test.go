package emi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// The verification gate: "A visitor POST returns 403 with the UI bypassed. Share, edit and
// delete by a non-owner return 403."
//
// The UI hides the save button for a visitor, which is the friendly half. These tests are
// about the other half: what happens when someone posts anyway, which is the only version
// that actually protects anything.

func visitor() UserIdentity {
	return UserIdentity{
		UserID: VisitorOrgPrefix + "abc123", OrganizationID: VisitorOrgPrefix + "abc123",
		Anonymous: true,
	}
}

func member(user, org string) UserIdentity {
	return UserIdentity{UserID: user, OrganizationID: org}
}

func requestAs(u UserIdentity, method, target, body string) *http.Request {
	var r *http.Request
	if body == "" {
		r = httptest.NewRequest(method, target, nil)
	} else {
		r = httptest.NewRequest(method, target, strings.NewReader(body))
	}
	return r.WithContext(WithUser(context.Background(), u))
}

func TestRequireAccountRefusesAVisitor(t *testing.T) {
	w := httptest.NewRecorder()
	if _, ok := requireAccount(w, requestAs(visitor(), "POST", "/emi/components", "{}")); ok {
		t.Fatal("a visitor was allowed to save a component")
	}
	if w.Code != http.StatusForbidden {
		t.Errorf("status = %d, want %d", w.Code, http.StatusForbidden)
	}
	// A 401 would invite the client to retry with the same cookie forever. The message has
	// to say what would actually help.
	var body map[string]string
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	if !strings.Contains(body["error"], "needs an account") {
		t.Errorf("error = %q, want it to explain that an account is needed", body["error"])
	}
}

func TestRequireAccountRefusesAVisitorIdEvenWithoutTheAnonymousFlag(t *testing.T) {
	// Belt and braces, matching the table's CHECK. If a host ever hands us a visitor id with
	// Anonymous unset, the prefix still gives it away -- and a component owned by an id that
	// cannot sign in again is a row nobody can reach.
	u := UserIdentity{UserID: VisitorOrgPrefix + "x", OrganizationID: VisitorOrgPrefix + "x"}
	w := httptest.NewRecorder()
	if _, ok := requireAccount(w, requestAs(u, "POST", "/emi/components", "{}")); ok {
		t.Fatal("a visitor id was allowed through because the flag was not set")
	}
	if w.Code != http.StatusForbidden {
		t.Errorf("status = %d, want %d", w.Code, http.StatusForbidden)
	}
}

func TestRequireAccountAllowsASignedInUser(t *testing.T) {
	w := httptest.NewRecorder()
	u, ok := requireAccount(w, requestAs(member("u-1", "org-1"), "POST", "/emi/components", "{}"))
	if !ok {
		t.Fatalf("a signed-in user was refused: %s", w.Body.String())
	}
	if u.UserID != "u-1" {
		t.Errorf("user = %q, want u-1", u.UserID)
	}
}

func TestRequireAccountRefusesAnUnauthenticatedRequest(t *testing.T) {
	w := httptest.NewRecorder()
	r := httptest.NewRequest("POST", "/emi/components", nil)
	if _, ok := requireAccount(w, r); ok {
		t.Fatal("a request with no identity at all was allowed")
	}
	if w.Code != http.StatusUnauthorized {
		t.Errorf("status = %d, want %d", w.Code, http.StatusUnauthorized)
	}
}

// ---- the body ----

func TestComponentBodyRequiresNameAndKind(t *testing.T) {
	for _, tc := range []struct{ name, body, want string }{
		{"no name", `{"kind":"capacitor","name":"  "}`, "needs a name"},
		{"no kind", `{"kind":"","name":"100n 0402"}`, "needs a kind"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var b componentBody
			if err := json.Unmarshal([]byte(tc.body), &b); err != nil {
				t.Fatal(err)
			}
			w := httptest.NewRecorder()
			if b.validate(w) {
				t.Fatal("accepted; want a refusal")
			}
			if !strings.Contains(w.Body.String(), tc.want) {
				t.Errorf("error = %q, want it to mention %q", w.Body.String(), tc.want)
			}
		})
	}
}

func TestComponentBodyTrimsWhitespace(t *testing.T) {
	b := componentBody{Kind: " capacitor ", Name: "  100n 0402  "}
	if !b.validate(httptest.NewRecorder()) {
		t.Fatal("refused a valid body")
	}
	if b.Name != "100n 0402" || b.Kind != "capacitor" {
		t.Errorf("got %q / %q, want trimmed values", b.Kind, b.Name)
	}
}

// ---- ownership ----

type componentStore struct {
	Store
	component *Component
	err       error
}

func (s componentStore) GetComponent(context.Context, string) (*Component, error) {
	if s.err != nil {
		return nil, s.err
	}
	return s.component, nil
}

func ownerTestService(c *Component) *Service {
	return &Service{deps: Deps{Store: componentStore{component: c}}}
}

func TestComponentOwnerRefusesANonOwnerInTheSameOrganisation(t *testing.T) {
	// A colleague can see a shared component and use it. Editing, sharing and deleting stay
	// with whoever made it, because an edit silently changes what every future run of every
	// colleague's board is modelled with.
	c := &Component{ID: "c-1", OwnerUserID: "u-1", OrganizationID: "org-1", Shared: true}
	w := httptest.NewRecorder()
	r := requestAs(member("u-2", "org-1"), "PUT", "/emi/components/c-1", "{}")
	r.SetPathValue("component_id", "c-1")
	if _, ok := ownerTestService(c).componentOwner(w, r); ok {
		t.Fatal("a non-owner was allowed to change a component")
	}
	if w.Code != http.StatusForbidden {
		t.Errorf("status = %d, want %d", w.Code, http.StatusForbidden)
	}
}

func TestComponentOwnerHidesAnotherOrganisationsComponent(t *testing.T) {
	// 404 rather than 403: outside the organisation it does not exist as far as this caller
	// is concerned, and a 403 would confirm the id for them.
	c := &Component{ID: "c-1", OwnerUserID: "u-1", OrganizationID: "org-1"}
	w := httptest.NewRecorder()
	r := requestAs(member("u-9", "org-2"), "DELETE", "/emi/components/c-1", "")
	r.SetPathValue("component_id", "c-1")
	if _, ok := ownerTestService(c).componentOwner(w, r); ok {
		t.Fatal("a component from another organisation was reachable")
	}
	if w.Code != http.StatusNotFound {
		t.Errorf("status = %d, want %d", w.Code, http.StatusNotFound)
	}
}

func TestComponentOwnerAllowsTheOwner(t *testing.T) {
	c := &Component{ID: "c-1", OwnerUserID: "u-1", OrganizationID: "org-1"}
	w := httptest.NewRecorder()
	r := requestAs(member("u-1", "org-1"), "PUT", "/emi/components/c-1", "{}")
	r.SetPathValue("component_id", "c-1")
	got, ok := ownerTestService(c).componentOwner(w, r)
	if !ok {
		t.Fatalf("the owner was refused: %s", w.Body.String())
	}
	if got.ID != "c-1" {
		t.Errorf("id = %q, want c-1", got.ID)
	}
}

func TestComponentOwnerRefusesAVisitorReachingForSomebodyElsesComponent(t *testing.T) {
	c := &Component{ID: "c-1", OwnerUserID: "u-1", OrganizationID: "org-1"}
	w := httptest.NewRecorder()
	r := requestAs(visitor(), "DELETE", "/emi/components/c-1", "")
	r.SetPathValue("component_id", "c-1")
	if _, ok := ownerTestService(c).componentOwner(w, r); ok {
		t.Fatal("a visitor reached another organisation's component")
	}
	if w.Code != http.StatusNotFound {
		t.Errorf("status = %d, want %d", w.Code, http.StatusNotFound)
	}
}

// ---- attaching the library to a run ----

func TestComponentBodyRejectsGenericProvenance(t *testing.T) {
	// "generic" means a class average from the built-in library. A user's entry describes a
	// part they chose, so claiming it is generic would be a claim about somebody else's data.
	b := componentBody{Kind: "capacitor", Name: "x", Provenance: "generic"}
	w := httptest.NewRecorder()
	if b.validate(w) {
		t.Fatal("accepted generic provenance from a user")
	}
	if !strings.Contains(w.Body.String(), "reserved for the") {
		t.Errorf("error = %q, want it to say why", w.Body.String())
	}
}

func TestComponentBodyDefaultsProvenanceToUser(t *testing.T) {
	b := componentBody{Kind: "capacitor", Name: "x"}
	if !b.validate(httptest.NewRecorder()) {
		t.Fatal("refused a valid body")
	}
	if b.Provenance != "user" {
		t.Errorf("provenance = %q, want user", b.Provenance)
	}
}

func TestComponentDocumentCarriesProvenanceThrough(t *testing.T) {
	// Without this the form's "from the part datasheet" becomes "entered by hand" in every
	// finding built from the run.
	doc, err := componentDocument(&Component{
		ID: "c-1", Kind: "capacitor", Name: "100n", Provenance: "vendor",
		Model: json.RawMessage(`{"type":"series_rlc","c_f":1e-7}`),
	})
	if err != nil {
		t.Fatal(err)
	}
	var parsed map[string]any
	if err := json.Unmarshal(doc, &parsed); err != nil {
		t.Fatal(err)
	}
	if parsed["provenance"] != "vendor" {
		t.Errorf("provenance = %v, want vendor", parsed["provenance"])
	}
	if parsed["format"] != "emi-component" {
		t.Errorf("format = %v", parsed["format"])
	}
}

func TestComponentDocumentDefaultsARowWrittenBeforeTheColumn(t *testing.T) {
	doc, err := componentDocument(&Component{ID: "c-1", Kind: "capacitor", Name: "x"})
	if err != nil {
		t.Fatal(err)
	}
	var parsed map[string]any
	_ = json.Unmarshal(doc, &parsed)
	if parsed["provenance"] != "user" {
		t.Errorf("provenance = %v, want user", parsed["provenance"])
	}
}

type libraryStore struct {
	Store
	components []*Component
}

func (s libraryStore) ListComponents(context.Context, string, string) ([]*Component, error) {
	return s.components, nil
}

func attachTestService(components ...*Component) *Service {
	return &Service{deps: Deps{Store: libraryStore{components: components}}}
}

func attach(t *testing.T, svc *Service, u UserIdentity, params string) map[string]any {
	t.Helper()
	r := requestAs(u, "POST", "/emi/projects/p/runs", "")
	out, err := svc.attachComponents(r, json.RawMessage(params))
	if err != nil {
		t.Fatalf("attachComponents: %v", err)
	}
	var parsed map[string]any
	if len(out) > 0 {
		if err := json.Unmarshal(out, &parsed); err != nil {
			t.Fatalf("params is not an object: %v", err)
		}
	}
	return parsed
}

func TestAttachComponentsDoesNothingUnlessAsked(t *testing.T) {
	// A client that knows nothing about components sends nothing and gets exactly the solve
	// it got before they existed.
	svc := attachTestService(&Component{ID: "c-1", OwnerUserID: "u-1", Kind: "capacitor"})
	got := attach(t, svc, member("u-1", "org-1"), `{"roi":[0,0,1,1]}`)
	if _, ok := got["components"]; ok {
		t.Error("components were attached to a run that did not ask for them")
	}
}

func TestAttachComponentsPutsMineBeforeShared(t *testing.T) {
	// A component I made outranks one a colleague shared: I am the one who knows which parts
	// this board actually uses.
	svc := attachTestService(
		&Component{ID: "theirs", OwnerUserID: "u-2", Kind: "capacitor", Name: "theirs",
			Shared: true, Model: json.RawMessage(`{"type":"series_rlc","c_f":1e-7}`)},
		&Component{ID: "mine", OwnerUserID: "u-1", Kind: "capacitor", Name: "mine",
			Model: json.RawMessage(`{"type":"series_rlc","c_f":1e-7}`)},
	)
	got := attach(t, svc, member("u-1", "org-1"), `{"model_components":true}`)
	list, _ := got["components"].([]any)
	if len(list) != 2 {
		t.Fatalf("got %d components, want 2", len(list))
	}
	first, _ := list[0].(map[string]any)
	if first["id"] != "mine" {
		t.Errorf("first component is %v, want mine", first["id"])
	}
}

func TestAttachComponentsKeepsInlineOnesFirst(t *testing.T) {
	// What the browser just sent is what the person just typed, and it outranks the library.
	svc := attachTestService(&Component{
		ID: "saved", OwnerUserID: "u-1", Kind: "capacitor", Name: "saved",
		Model: json.RawMessage(`{"type":"series_rlc","c_f":1e-7}`)})
	params := `{"model_components":true,"components":[{"format":"emi-component","id":"inline"}]}`
	got := attach(t, svc, member("u-1", "org-1"), params)
	list, _ := got["components"].([]any)
	first, _ := list[0].(map[string]any)
	if first["id"] != "inline" {
		t.Errorf("first component is %v, want inline", first["id"])
	}
}

func TestAttachComponentsGivesAVisitorTheirInlineOnesOnly(t *testing.T) {
	// A visitor has no library. Their inline components still apply, which is what makes the
	// signed-out path useful rather than decorative.
	svc := attachTestService(&Component{ID: "saved", OwnerUserID: "u-1", Kind: "capacitor"})
	params := `{"model_components":true,"components":[{"format":"emi-component","id":"inline"}]}`
	got := attach(t, svc, visitor(), params)
	list, _ := got["components"].([]any)
	if len(list) != 1 {
		t.Fatalf("got %d components, want only the inline one", len(list))
	}
}

func TestAttachComponentsRefusesAMalformedFlag(t *testing.T) {
	svc := attachTestService()
	r := requestAs(member("u-1", "org-1"), "POST", "/emi/projects/p/runs", "")
	if _, err := svc.attachComponents(r, json.RawMessage(`{"model_components":"yes"}`)); err == nil {
		t.Fatal("accepted a non-boolean model_components")
	}
}
