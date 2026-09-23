package local

import (
	"context"
	"database/sql"
	"embed"
	"fmt"
	"io/fs"
	"sort"
	"strconv"
	"strings"
)

// The local database is on the user's disk and outlives the binary that created it, so its
// schema changes go through a ladder of numbered steps, one file each in migrations/:
//
//	migrations/0001_initial.sql
//	migrations/0002_<what it does>.sql
//	...
//
// PRAGMA user_version records the last step applied. Opening a database runs every step above
// it, each in its own transaction together with the version bump, so a step that fails leaves
// the database at the version before it rather than half-changed.
//
// Rules for a new step:
//   - append a file with the next number; never edit or renumber one that has shipped
//   - SQLite cannot ALTER a CHECK constraint. Growing a vocabulary (a new run kind, say) means
//     rebuilding the table: CREATE the new one, INSERT ... SELECT, DROP, RENAME, recreate its
//     indexes. Foreign keys cannot be switched off inside the transaction, so the rebuild must
//     not leave a dangling reference at COMMIT.
//
// Before the ladder, schema.sql was applied on every open as CREATE ... IF NOT EXISTS and
// user_version stayed 0. Step 1 is that same schema, still IF NOT EXISTS, so such a database
// passes through it unchanged and comes out at version 1.

//go:embed migrations/*.sql
var migrationFiles embed.FS

type migration struct {
	version int
	name    string
	sql     string
}

// migrations returns the steps in order, and refuses a gap or a duplicate number: either would
// make a user's database skip a step or apply one twice.
func migrations() ([]migration, error) {
	entries, err := fs.ReadDir(migrationFiles, "migrations")
	if err != nil {
		return nil, err
	}
	var out []migration
	for _, e := range entries {
		num, _, ok := strings.Cut(e.Name(), "_")
		v, err := strconv.Atoi(num)
		if !ok || err != nil || !strings.HasSuffix(e.Name(), ".sql") {
			return nil, fmt.Errorf("local: migration %q is not named NNNN_name.sql", e.Name())
		}
		body, err := migrationFiles.ReadFile("migrations/" + e.Name())
		if err != nil {
			return nil, err
		}
		out = append(out, migration{version: v, name: e.Name(), sql: string(body)})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].version < out[j].version })
	for i, m := range out {
		if m.version != i+1 {
			return nil, fmt.Errorf("local: migration %s is out of sequence, want number %d", m.name, i+1)
		}
	}
	return out, nil
}

// SchemaVersion is the version a database is at once OpenSQLite has migrated it.
func SchemaVersion() int {
	ms, err := migrations()
	if err != nil {
		return 0
	}
	return len(ms)
}

// migrate brings db up to the newest schema this binary knows.
func migrate(ctx context.Context, db *sql.DB) error {
	ms, err := migrations()
	if err != nil {
		return err
	}
	var current int
	if err := db.QueryRowContext(ctx, `PRAGMA user_version`).Scan(&current); err != nil {
		return fmt.Errorf("local: read schema version: %w", err)
	}
	// Opening a newer database with an older app would run it against tables it does not
	// understand. Better to say so than to corrupt the user's projects.
	if current > len(ms) {
		return fmt.Errorf("local: the database is at schema version %d, newer than this app "+
			"understands (%d); update the app", current, len(ms))
	}
	for _, m := range ms[current:] {
		if err := apply(ctx, db, m); err != nil {
			return err
		}
	}
	return nil
}

func apply(ctx context.Context, db *sql.DB, m migration) error {
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback() //nolint:errcheck // a no-op after Commit
	if _, err := tx.ExecContext(ctx, m.sql); err != nil {
		return fmt.Errorf("local: migration %s: %w", m.name, err)
	}
	// user_version is part of the database header and changes with the transaction, so the
	// step and the record of it commit or roll back together.
	if _, err := tx.ExecContext(ctx, fmt.Sprintf(`PRAGMA user_version = %d`, m.version)); err != nil {
		return fmt.Errorf("local: migration %s: set version: %w", m.name, err)
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("local: migration %s: %w", m.name, err)
	}
	return nil
}
