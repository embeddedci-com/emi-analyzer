package local

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func userVersion(t *testing.T, db *sql.DB) int {
	t.Helper()
	var v int
	if err := db.QueryRow(`PRAGMA user_version`).Scan(&v); err != nil {
		t.Fatal(err)
	}
	return v
}

// A database from v0.1.0, created by applying schema.sql as it was then and never given a
// user_version, has to come up on the migration ladder with its data intact.
func TestMigrateUpgradesADatabaseFromBeforeTheLadder(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "emi.db")

	old, err := os.ReadFile("testdata/schema_v0.1.0.sql")
	if err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(string(old)); err != nil {
		t.Fatalf("applying the v0.1.0 schema: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO emi_projects (id, organization_id, name, source_kind, created_at)
		VALUES ('p-old', 'local', 'from v0.1.0', 'kicad', '2026-09-01T00:00:00.000000000Z')`); err != nil {
		t.Fatal(err)
	}
	if v := userVersion(t, db); v != 0 {
		t.Fatalf("a v0.1.0 database starts at user_version %d, want 0", v)
	}
	db.Close()

	s, err := OpenSQLite(ctx, path)
	if err != nil {
		t.Fatalf("opening a v0.1.0 database: %v", err)
	}
	defer s.Close()
	if v := userVersion(t, s.DB()); v != SchemaVersion() || v < 1 {
		t.Fatalf("user_version = %d, want %d", v, SchemaVersion())
	}
	p, err := s.GetProject(ctx, "p-old")
	if err != nil || p.Name != "from v0.1.0" {
		t.Fatalf("the old project = %+v, %v", p, err)
	}

	// And the store works on it end to end.
	_, _, r := seed(t, s, "local", time.Now())
	if _, err := s.ClaimRun(ctx, r.ID, "kid", "jti", time.Now()); err != nil {
		t.Fatalf("claim on a migrated database: %v", err)
	}
}

func TestMigrateIsIdempotentAndRefusesANewerDatabase(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "emi.db")
	for i := 0; i < 2; i++ {
		s, err := OpenSQLite(ctx, path)
		if err != nil {
			t.Fatalf("open %d: %v", i, err)
		}
		if v := userVersion(t, s.DB()); v != SchemaVersion() {
			t.Fatalf("open %d: user_version = %d, want %d", i, v, SchemaVersion())
		}
		s.Close()
	}

	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`PRAGMA user_version = 999`); err != nil {
		t.Fatal(err)
	}
	db.Close()
	if _, err := OpenSQLite(ctx, path); err == nil || !strings.Contains(err.Error(), "newer") {
		t.Fatalf("opening a database from a newer app: %v, want a refusal", err)
	}
}

// A step that fails must leave the database at the version before it, not half-applied.
func TestFailedMigrationStepRollsBack(t *testing.T) {
	ctx := context.Background()
	s := openTestStore(t)
	before := userVersion(t, s.DB())

	bad := migration{version: before + 1, name: "bad.sql", sql: `
		CREATE TABLE emi_half_done (id TEXT);
		INSERT INTO no_such_table VALUES (1);`}
	if err := apply(ctx, s.DB(), bad); err == nil {
		t.Fatal("a failing step reported success")
	}
	if v := userVersion(t, s.DB()); v != before {
		t.Fatalf("user_version after a failed step = %d, want %d", v, before)
	}
	var n int
	if err := s.DB().QueryRow(`SELECT count(*) FROM sqlite_master WHERE name = 'emi_half_done'`).Scan(&n); err != nil || n != 0 {
		t.Fatalf("the failed step left a table behind: %d, %v", n, err)
	}
}

func TestMigrationsAreNumberedInSequence(t *testing.T) {
	ms, err := migrations()
	if err != nil {
		t.Fatal(err)
	}
	if len(ms) == 0 || ms[0].version != 1 {
		t.Fatalf("migrations = %+v", ms)
	}
}
