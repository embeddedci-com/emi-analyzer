package emi

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// A host whose Store cannot delete one board keeps compiling, and says so rather than
// pretending: the route answers 501 and nothing is removed.
func TestDeleteBoardWithoutABoardDeleter(t *testing.T) {
	store := &deleteStore{}
	blob := &recordingBlob{}
	mux := deleteService(t, store, blob)

	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodDelete, "/api/emi/projects/p1/boards/b1", nil))
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("got %d, want 501", rec.Code)
	}
	if len(blob.deleted)+len(blob.prefixes) != 0 {
		t.Errorf("objects removed: %v %v", blob.deleted, blob.prefixes)
	}
}
