package emi

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
)

// Component endpoints. The model is in docs/implementation.md §3.
//
// These are the only user-facing objects in this package that a signed-out visitor cannot
// create. Boards, projects, drivers and runs all work without an account, because they live
// or die with the browser that made them and that is a reasonable bargain for trying a tool.
// A component is not like that: it is a library entry meant to be reused across boards and
// shared with colleagues, and a visitor has no identity to come back to. Letting one be
// saved would write a row nobody could ever reach again.
//
// So the refusal is explicit and says what to do about it, rather than being a 401 the UI has
// to guess the meaning of. The browser keeps unsaved components in memory and offers to save
// them after signing in, which is what makes the gate tolerable rather than a wall.

// componentOwner loads a component and checks the caller may act on it as its owner.
//
// Ownership, not organisation. A colleague in the same organisation can *see* a shared
// component and use it; editing, sharing and deleting stay with whoever made it, because an
// edit silently changes what every future run of every colleague's board is modelled with.
func (s *Service) componentOwner(w http.ResponseWriter, r *http.Request) (*Component, bool) {
	u, ok := userFrom(r.Context())
	if !ok {
		writeErr(w, http.StatusUnauthorized, "not authenticated")
		return nil, false
	}
	c, err := s.deps.Store.GetComponent(r.Context(), r.PathValue("component_id"))
	if err != nil {
		writeStoreErr(w, err)
		return nil, false
	}
	if c.OrganizationID != u.OrganizationID {
		// Not "forbidden": outside the organisation the component does not exist as far as
		// this caller is concerned, and saying otherwise confirms an id for them.
		writeErr(w, http.StatusNotFound, "not found")
		return nil, false
	}
	if c.OwnerUserID != u.UserID {
		writeErr(w, http.StatusForbidden, "only the owner of a component can change it")
		return nil, false
	}
	return c, true
}

// requireAccount is the sign-in gate. It is a 403 with an explanation rather than a 401,
// because the caller is perfectly well authenticated -- as a visitor -- and retrying the same
// request with the same cookie will never work.
func requireAccount(w http.ResponseWriter, r *http.Request) (UserIdentity, bool) {
	u, ok := userFrom(r.Context())
	if !ok {
		writeErr(w, http.StatusUnauthorized, "not authenticated")
		return UserIdentity{}, false
	}
	if u.Anonymous || strings.HasPrefix(u.UserID, VisitorOrgPrefix) {
		writeErr(w, http.StatusForbidden,
			"saving a component needs an account: it is a library entry meant to outlive "+
				"this session, and a signed-out visitor has no identity to file it under. "+
				"Sign in and save it again.")
		return UserIdentity{}, false
	}
	return u, true
}

type componentBody struct {
	Kind       string          `json:"kind"`
	Name       string          `json:"name"`
	Match      json.RawMessage `json:"match"`
	Model      json.RawMessage `json:"model"`
	Sources    json.RawMessage `json:"sources"`
	Provenance string          `json:"provenance"`
}

// : What a caller may claim about their own component. "generic" is reserved for the built-in
// : library: a user's entry describes a part they chose, so calling it a class average would
// : be a claim about somebody else's data.
var userProvenance = map[string]bool{"vendor": true, "measured": true, "user": true}

func (b *componentBody) validate(w http.ResponseWriter) bool {
	b.Name = strings.TrimSpace(b.Name)
	b.Kind = strings.TrimSpace(b.Kind)
	if b.Name == "" {
		writeErr(w, http.StatusBadRequest, "a component needs a name")
		return false
	}
	if b.Kind == "" {
		writeErr(w, http.StatusBadRequest, "a component needs a kind")
		return false
	}
	if b.Provenance == "" {
		b.Provenance = "user"
	}
	if !userProvenance[b.Provenance] {
		writeErr(w, http.StatusBadRequest,
			"provenance must be vendor, measured or user; generic is reserved for the "+
				"built-in library")
		return false
	}
	return true
}

func (s *Service) handleListComponents(w http.ResponseWriter, r *http.Request) {
	u, ok := userFrom(r.Context())
	if !ok {
		writeErr(w, http.StatusUnauthorized, "not authenticated")
		return
	}
	// A visitor has no saved components and never will, but the list still answers: the
	// built-in library is the useful part and the UI renders the same view either way.
	if u.Anonymous {
		writeJSON(w, http.StatusOK, map[string]any{"components": []*Component{}})
		return
	}
	cs, err := s.deps.Store.ListComponents(r.Context(), u.OrganizationID, u.UserID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"components": cs})
}

func (s *Service) handleCreateComponent(w http.ResponseWriter, r *http.Request) {
	u, ok := requireAccount(w, r)
	if !ok {
		return
	}
	var body componentBody
	if !decodeJSON(w, r, &body) || !body.validate(w) {
		return
	}
	now := s.deps.now()
	c := &Component{
		ID: newID(), OwnerUserID: u.UserID, OrganizationID: u.OrganizationID,
		Kind: body.Kind, Name: body.Name, Match: body.Match, Model: body.Model,
		Sources: body.Sources, Provenance: body.Provenance, Version: 1,
		CreatedAt: now, UpdatedAt: now,
	}
	if err := s.deps.Store.CreateComponent(r.Context(), c); err != nil {
		writeStoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, c)
}

func (s *Service) handleUpdateComponent(w http.ResponseWriter, r *http.Request) {
	c, ok := s.componentOwner(w, r)
	if !ok {
		return
	}
	var body componentBody
	if !decodeJSON(w, r, &body) || !body.validate(w) {
		return
	}
	c.Kind, c.Name = body.Kind, body.Name
	c.Match, c.Model, c.Sources = body.Match, body.Model, body.Sources
	c.Provenance = body.Provenance
	if err := s.deps.Store.UpdateComponent(r.Context(), c); err != nil {
		writeStoreErr(w, err)
		return
	}
	updated, err := s.deps.Store.GetComponent(r.Context(), c.ID)
	if err != nil {
		writeStoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, updated)
}

func (s *Service) handleShareComponent(w http.ResponseWriter, r *http.Request) {
	c, ok := s.componentOwner(w, r)
	if !ok {
		return
	}
	var body struct {
		Shared bool `json:"shared"`
	}
	if !decodeJSON(w, r, &body) {
		return
	}
	if err := s.deps.Store.SetComponentShared(r.Context(), c.ID, body.Shared); err != nil {
		writeStoreErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"id": c.ID, "shared": body.Shared})
}

func (s *Service) handleDeleteComponent(w http.ResponseWriter, r *http.Request) {
	c, ok := s.componentOwner(w, r)
	if !ok {
		return
	}
	if err := s.deps.Store.SoftDeleteComponent(r.Context(), c.ID); err != nil {
		writeStoreErr(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// attachComponents resolves the caller's component library into the run's params (§12).
//
// Two things happen here that cannot happen anywhere else.
//
// **The copy is resolved now, not referenced.** A run carries the components it was built
// with, so editing or deleting one later never changes a result that has already been
// reported. That is why the worker is handed documents rather than ids.
//
// **Precedence is decided here**, because only the server knows who owns what: the caller's
// own components first, then anything their organisation has shared. Anything the browser
// sent inline — an unsaved component from a signed-out visitor — is already at the front of
// the list and stays there, because the person who just typed it means it.
func (s *Service) attachComponents(r *http.Request, raw json.RawMessage) (json.RawMessage, error) {
	var params map[string]json.RawMessage
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &params); err != nil {
			return raw, fmt.Errorf("params is not an object: %w", err)
		}
	}
	if params == nil {
		params = map[string]json.RawMessage{}
	}

	// Off unless asked for. A client that knows nothing about components sends nothing and
	// gets exactly the solve it got before they existed.
	var wants bool
	if v, ok := params["model_components"]; ok {
		if err := json.Unmarshal(v, &wants); err != nil {
			return raw, fmt.Errorf("model_components must be true or false")
		}
	}
	if !wants {
		return raw, nil
	}

	u, ok := userFrom(r.Context())
	if !ok {
		return raw, fmt.Errorf("not authenticated")
	}

	// Whatever the browser sent inline keeps its place at the front.
	inline := []json.RawMessage{}
	if v, ok := params["components"]; ok && len(v) > 0 {
		if err := json.Unmarshal(v, &inline); err != nil {
			return raw, fmt.Errorf("components must be a list of component documents")
		}
	}

	// A visitor has no library, and saying so costs nothing: their inline components still
	// apply, which is what makes the signed-out path useful rather than decorative.
	if !u.Anonymous {
		saved, err := s.deps.Store.ListComponents(r.Context(), u.OrganizationID, u.UserID)
		if err != nil {
			return raw, fmt.Errorf("could not read the component library: %w", err)
		}
		// Mine before shared: a component I made outranks one a colleague shared, because I
		// am the one who knows which parts this board actually uses.
		for _, pass := range []bool{true, false} {
			for _, c := range saved {
				if (c.OwnerUserID == u.UserID) != pass {
					continue
				}
				doc, err := componentDocument(c)
				if err != nil {
					return raw, fmt.Errorf("component %s: %w", c.ID, err)
				}
				inline = append(inline, doc)
			}
		}
	}

	encoded, err := json.Marshal(inline)
	if err != nil {
		return raw, fmt.Errorf("failed to encode components: %w", err)
	}
	params["components"] = encoded
	out, err := json.Marshal(params)
	if err != nil {
		return raw, fmt.Errorf("failed to encode params: %w", err)
	}
	return out, nil
}

// provenanceOr defends against a row written before the column existed.
func provenanceOr(v string) string {
	if v == "" {
		return "user"
	}
	return v
}

// componentDocument renders a stored row as the emi-component document the worker parses.
func componentDocument(c *Component) (json.RawMessage, error) {
	doc := map[string]any{
		"format":     "emi-component",
		"version":    1,
		"id":         c.ID,
		"kind":       c.Kind,
		"name":       c.Name,
		"provenance": provenanceOr(c.Provenance),
	}
	for key, value := range map[string]json.RawMessage{
		"match": c.Match, "model": c.Model, "sources": c.Sources,
	} {
		if len(value) > 0 {
			doc[key] = value
		}
	}
	return json.Marshal(doc)
}
