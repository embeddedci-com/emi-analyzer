package emi

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type deleteStore struct {
	Store
	deleted  []string
	renamed  string
	sharedBy int
}

func (s *deleteStore) GetProject(_ context.Context, id string) (*Project, error) {
	if id == "other-org" {
		return &Project{ID: id, OrganizationID: "someone-else"}, nil
	}
	return &Project{ID: id, OrganizationID: "org", Name: "board"}, nil
}
func (s *deleteStore) ListRuns(context.Context, string, int) ([]*Run, error) {
	return []*Run{{ID: "run-1"}, {ID: "run-2"}}, nil
}
func (s *deleteStore) ListBoards(context.Context, string) ([]*Board, error) {
	return []*Board{{ID: "b1", InputKey: "uploads/org/sha256/aa/board.kicad_pcb", BoardKey: "runs/run-1/board.json"}}, nil
}
func (s *deleteStore) CountBoardsSharingInput(_ context.Context, key, except string) (int, error) {
	if strings.HasSuffix(key, "board.kicad_pcb") {
		return s.sharedBy, nil
	}
	return 0, nil
}
func (s *deleteStore) DeleteProject(_ context.Context, id string) error {
	s.deleted = append(s.deleted, id)
	return nil
}
func (s *deleteStore) RenameProject(_ context.Context, _, name string) error {
	s.renamed = name
	return nil
}

// A Blob that can delete, so the handler's cleanup is observable.
type recordingBlob struct {
	stubBlob
	deleted  []string
	prefixes []string
}

func (b *recordingBlob) Delete(_ context.Context, key string) error {
	b.deleted = append(b.deleted, key)
	return nil
}
func (b *recordingBlob) DeletePrefix(_ context.Context, prefix string) error {
	b.prefixes = append(b.prefixes, prefix)
	return nil
}

func deleteService(t *testing.T, store Store, blob Blob) *http.ServeMux {
	t.Helper()
	svc, err := New(Deps{Store: store, Keys: stubKeys{}, Blob: blob, TokenSecret: bytes.Repeat([]byte("k"), 32)})
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	svc.Mount(mux, "/api", func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			next(w, r.WithContext(WithUser(r.Context(), UserIdentity{UserID: "u", OrganizationID: "org"})))
		}
	})
	return mux
}

// Deleting a project has to take the board file with it: it is somebody's unreleased layout,
// and "deleted" that leaves it in storage is not deleted.
func TestDeleteProjectRemovesRowsAndObjects(t *testing.T) {
	store := &deleteStore{}
	blob := &recordingBlob{}
	mux := deleteService(t, store, blob)

	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest(http.MethodDelete, "/api/emi/projects/p1", nil))
	if w.Code != http.StatusNoContent {
		t.Fatalf("got %d %s, want 204", w.Code, w.Body.String())
	}
	if len(store.deleted) != 1 || store.deleted[0] != "p1" {
		t.Fatalf("store deletions = %v", store.deleted)
	}
	if len(blob.prefixes) != 2 || blob.prefixes[0] != "runs/run-1/" || blob.prefixes[1] != "runs/run-2/" {
		t.Fatalf("run artifacts not removed: %v", blob.prefixes)
	}
	if len(blob.deleted) != 2 {
		t.Fatalf("board objects removed = %v, want the upload and the parsed board", blob.deleted)
	}
}

// The same upload can belong to two projects, because an upload is content-addressed. Deleting
// one project must not take the other project's board away.
func TestDeleteProjectKeepsAnUploadAnotherProjectShares(t *testing.T) {
	store := &deleteStore{sharedBy: 1}
	blob := &recordingBlob{}
	mux := deleteService(t, store, blob)

	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest(http.MethodDelete, "/api/emi/projects/p1", nil))
	if w.Code != http.StatusNoContent {
		t.Fatalf("got %d, want 204", w.Code)
	}
	for _, key := range blob.deleted {
		if strings.HasSuffix(key, "board.kicad_pcb") {
			t.Fatalf("a shared upload was deleted: %v", blob.deleted)
		}
	}
}

func TestDeleteAndRenameRespectTheOrganisation(t *testing.T) {
	store := &deleteStore{}
	mux := deleteService(t, store, &recordingBlob{})

	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest(http.MethodDelete, "/api/emi/projects/other-org", nil))
	if w.Code != http.StatusNotFound || len(store.deleted) != 0 {
		t.Fatalf("another organisation's project: got %d, deletions %v", w.Code, store.deleted)
	}

	w = httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodPatch, "/api/emi/projects/other-org", strings.NewReader(`{"name":"mine now"}`))
	mux.ServeHTTP(w, r)
	if w.Code != http.StatusNotFound || store.renamed != "" {
		t.Fatalf("rename across organisations: got %d, renamed %q", w.Code, store.renamed)
	}
}

func TestRenameProject(t *testing.T) {
	store := &deleteStore{}
	mux := deleteService(t, store, &recordingBlob{})

	for _, body := range []string{`{"name":"  "}`, `{"name":"` + strings.Repeat("x", 201) + `"}`} {
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest(http.MethodPatch, "/api/emi/projects/p1", strings.NewReader(body)))
		if w.Code != http.StatusBadRequest {
			t.Errorf("body %.20s: got %d, want 400", body, w.Code)
		}
	}

	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest(http.MethodPatch, "/api/emi/projects/p1", strings.NewReader(`{"name":"  Rev B  "}`)))
	if w.Code != http.StatusOK || store.renamed != "Rev B" {
		t.Fatalf("got %d, renamed %q", w.Code, store.renamed)
	}
}

// A Blob with no delete support must not stop a project being deleted; the rows are what the
// user asked about.
func TestDeleteProjectWorksWithStorageThatCannotDelete(t *testing.T) {
	store := &deleteStore{}
	mux := deleteService(t, store, stubBlob{})
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest(http.MethodDelete, "/api/emi/projects/p1", nil))
	if w.Code != http.StatusNoContent || len(store.deleted) != 1 {
		t.Fatalf("got %d, deletions %v", w.Code, store.deleted)
	}
}
