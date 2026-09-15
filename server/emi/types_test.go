package emi

import (
	"os"
	"strings"
	"testing"
)

// A compliance run is arithmetic on artifacts other runs already produced, so it has to be a
// kind the server will accept and any worker can claim -- not something bolted onto a solve.
func TestComplianceIsAValidKind(t *testing.T) {
	if !RunKindCompliance.Valid() {
		t.Fatal("compliance is not a valid run kind")
	}
	for _, k := range []RunKind{RunKindIngest, RunKindSolve, RunKindTransient, RunKindCable} {
		if !k.Valid() {
			t.Fatalf("%s stopped being valid", k)
		}
	}
	if RunKind("nonsense").Valid() {
		t.Fatal("an unknown kind was accepted")
	}
}

// Every RunKind the Go vocabulary accepts has to be in the database's CHECK constraint, and in
// the migration that grows it on a database created before the kind existed.
//
// This test exists because both halves were missed twice. 'cable' and 'compliance' were added to
// RunKind, to the worker's dispatch table and to the table definition, but not to the DO block
// that repairs an existing database -- so on any database older than the kind, creating a run
// answered 500 with nothing in the log, and the Cables tab had never once worked end to end.
func TestEveryRunKindIsInTheSchemaConstraint(t *testing.T) {
	raw, err := os.ReadFile("schema.sql")
	if err != nil {
		t.Fatalf("reading schema.sql: %v", err)
	}
	schema := string(raw)

	// The CHECK in CREATE TABLE, and the ALTER that repairs an existing database. Both matter:
	// the first reaches new databases only, the second reaches every other one.
	if strings.Count(schema, "emi_runs_kind_check") < 2 {
		t.Fatal("schema.sql no longer has both the CREATE TABLE constraint and its migration")
	}
	for _, k := range []RunKind{
		RunKindIngest, RunKindSolve, RunKindTransient, RunKindCable, RunKindCompliance,
	} {
		if strings.Count(schema, "'"+string(k)+"'::text") < 2 {
			t.Errorf("run kind %q is missing from the CHECK constraint or from the migration "+
				"that grows it; a database created before this kind will reject the INSERT "+
				"and the API will answer 500", k)
		}
	}

	// The migration only fires when the constraint lacks the value it probes for, so that probe
	// has to name the newest kind. Naming an older one makes the whole block a no-op.
	newest := string(RunKindCompliance)
	if !strings.Contains(schema, "LIKE '%"+newest+"%'") {
		t.Errorf("the migration probes for something other than %q, so it will not fire on a "+
			"database that predates it", newest)
	}
}
