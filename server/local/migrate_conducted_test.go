package local

import (
	"context"
	"database/sql"
	"net/url"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

// Step 2 rebuilds emi_runs to add the "conducted" kind. With foreign keys on, dropping the old
// runs table is a DELETE that cascades into emi_artifacts, so the obvious rebuild would have
// emptied every user's artifacts. A database at step 1 with a run and an artifact has to come
// through with both, and with the cascade still pointing at the new table.
func TestConductedStepKeepsRunsAndArtifacts(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "emi.db")
	q := url.Values{}
	q.Add("_pragma", "foreign_keys(1)")
	db, err := sql.Open("sqlite", path+"?"+q.Encode())
	if err != nil {
		t.Fatal(err)
	}
	ms, err := migrations()
	if err != nil {
		t.Fatal(err)
	}
	if err := apply(ctx, db, ms[0]); err != nil {
		t.Fatal(err)
	}
	for _, stmt := range []string{
		`INSERT INTO emi_projects (id, organization_id, name, source_kind, created_at)
			VALUES ('p1', 'local', 'board', 'kicad', '2026-09-01T00:00:00Z')`,
		`INSERT INTO emi_runs (id, project_id, kind, status, created_at, updated_at)
			VALUES ('r1', 'p1', 'transient', 'done', '2026-09-01T00:00:00Z', '2026-09-01T00:00:00Z')`,
		`INSERT INTO emi_artifacts (id, run_id, name, s3_key, created_at)
			VALUES ('a1', 'r1', 'transient.json', 'k', '2026-09-01T00:00:00Z')`,
	} {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := db.Exec(`INSERT INTO emi_runs (id, project_id, kind, status, created_at, updated_at)
			VALUES ('r0', 'p1', 'conducted', 'new', 'x', 'x')`); err == nil {
		t.Fatal("step 1 accepted a conducted run; this test no longer tests the rebuild")
	}
	db.Close()

	s, err := OpenSQLite(ctx, path)
	if err != nil {
		t.Fatalf("migrating: %v", err)
	}
	defer s.Close()
	arts, err := s.ListArtifacts(ctx, "r1")
	if err != nil || len(arts) != 1 || arts[0].Name != "transient.json" {
		t.Fatalf("artifacts after the rebuild = %+v, %v", arts, err)
	}
	var ddl string
	if err := s.DB().QueryRow(`SELECT sql FROM sqlite_master WHERE name = 'emi_artifacts'`).Scan(&ddl); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(ddl, `"emi_runs"`) && !strings.Contains(ddl, "emi_runs (") {
		t.Fatalf("emi_artifacts no longer references emi_runs: %s", ddl)
	}

	now := time.Now()
	r := &emi.Run{ID: "r2", ProjectID: "p1", Kind: emi.RunKindConducted, Status: emi.StatusNew,
		CreatedAt: now, UpdatedAt: now}
	if err := s.CreateRun(ctx, r); err != nil {
		t.Fatalf("a conducted run after the step: %v", err)
	}
	if err := s.DeleteProject(ctx, "p1"); err != nil {
		t.Fatal(err)
	}
	var n int
	if err := s.DB().QueryRow(`SELECT count(*) FROM emi_artifacts`).Scan(&n); err != nil || n != 0 {
		t.Fatalf("deleting the project left %d artifacts (%v): the cascade is broken", n, err)
	}
	var idx int
	if err := s.DB().QueryRow(`SELECT count(*) FROM sqlite_master WHERE type = 'index' AND tbl_name = 'emi_runs' AND name LIKE 'emi_runs_%_idx'`).Scan(&idx); err != nil || idx != 3 {
		t.Fatalf("emi_runs has %d of its 3 indexes (%v)", idx, err)
	}
}
