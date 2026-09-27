package emi

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

const conductedBody = `{"board_id":"b1","kind":"conducted","params":{"regulators":{"U3":{"frequency_hz":2.2e6}}}}`

// The conducted scan has its own switch, refused everywhere a run can start or be handed out
// when it is off, and not implied by full-wave: it is a different model with its own checks.
func TestConductedIsItsOwnFeature(t *testing.T) {
	for _, f := range []Features{{}, {FullWave: true, SmallPartSolve: true}} {
		mux, store := newFeatureService(t, f)
		store.runs["solve"] = &Run{ID: "cond", ProjectID: "p1", Kind: RunKindConducted, Status: StatusFailed}
		store.runs["cond"] = store.runs["solve"]

		w := serve(mux, "POST", "/api/emi/projects/p1/runs", conductedBody)
		if w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), "EMI_EXPERIMENTAL="+FeatureConducted) {
			t.Errorf("%+v: create got %d %s, want 403 naming the feature", f, w.Code, w.Body.String())
		}
		if len(store.created) != 0 {
			t.Errorf("%+v: a refused run was stored", f)
		}
		if w := serve(mux, "POST", "/api/emi/runs/cond/retry", ""); w.Code != http.StatusForbidden {
			t.Errorf("%+v: retry got %d, want 403", f, w.Code)
		}
		if w := serve(mux, "GET", "/api/emi-agent/runs", ""); strings.Contains(w.Body.String(), `"cond"`) {
			t.Errorf("%+v: worker list = %s, want the conducted run left out", f, w.Body.String())
		}
		if w := serve(mux, "POST", "/api/emi-agent/runs/cond/token", ""); w.Code != http.StatusForbidden {
			t.Errorf("%+v: mint got %d, want 403", f, w.Code)
		}
	}

	mux, store := newFeatureService(t, Features{Conducted: true})
	store.runs["solve"] = &Run{ID: "cond", ProjectID: "p1", Kind: RunKindConducted, Status: StatusNew}
	if w := serve(mux, "POST", "/api/emi/projects/p1/runs", conductedBody); w.Code == http.StatusForbidden || w.Code >= 400 {
		t.Fatalf("conducted refused with its feature on: %d %s", w.Code, w.Body.String())
	}
	if len(store.created) != 1 {
		t.Fatalf("created %d runs, want 1", len(store.created))
	}
	var p ConductedParams
	if err := json.Unmarshal(store.created[0].Params, &p); err != nil || p.Class != "B" ||
		p.Regulators["U3"].FrequencyHz == nil || *p.Regulators["U3"].FrequencyHz != 2.2e6 {
		t.Fatalf("stored params = %s, %v", store.created[0].Params, err)
	}
	if w := serve(mux, "GET", "/api/emi-agent/runs", ""); !strings.Contains(w.Body.String(), `"cond"`) {
		t.Errorf("worker list = %s, want the conducted run", w.Body.String())
	}
	// Full-wave stays off: turning one experiment on does not turn on another.
	if w := serve(mux, "POST", "/api/emi/projects/p1/runs", `{"board_id":"b1","kind":"solve"}`); w.Code != http.StatusForbidden {
		t.Errorf("a solve with only conducted on: got %d, want 403", w.Code)
	}
	if w := serve(mux, "GET", "/api/emi/features", ""); !strings.Contains(w.Body.String(), `"conducted":true`) {
		t.Errorf("features = %s", w.Body.String())
	}
}

// The socket's queue is gated like the REST list: a run queued while the feature was on is not
// pushed after it is switched off.
func TestConductedRunsAreNotPushedWhenOff(t *testing.T) {
	store := &featureStore{runs: map[string]*Run{
		"solve":  {ID: "cond", ProjectID: "p1", Kind: RunKindConducted, Status: StatusNew},
		"ingest": {ID: "ingest", ProjectID: "p1", Kind: RunKindIngest, Status: StatusNew},
	}}
	caps := Capabilities{Kinds: []RunKind{RunKindIngest, RunKindConducted}}
	for _, on := range []bool{false, true} {
		svc, err := New(Deps{Store: store, Keys: allowKeys{}, Blob: stubBlob{},
			TokenSecret: bytes.Repeat([]byte("k"), 32), Features: Features{Conducted: on}})
		if err != nil {
			t.Fatal(err)
		}
		runs, err := svc.pendingFor(context.Background(), "org", caps)
		if err != nil {
			t.Fatal(err)
		}
		var got []string
		for _, r := range runs {
			got = append(got, r.ID)
		}
		want := "ingest"
		if on {
			want = "cond,ingest"
		}
		if strings.Join(got, ",") != want {
			t.Errorf("feature %v: pushed %v, want %s", on, got, want)
		}
	}
}

func TestConductedParamsAreChecked(t *testing.T) {
	for _, bad := range []string{
		`{"class":"C"}`,
		`{"regulators":{"U1":{"frequency_hz":1}}}`,
		`{"regulators":{"U1":{"duty":1.2}}}`,
		`{"regulators":{"U1":{"rise_s":0}}}`,
		`{"regulators":{"U1":{"phase_deg":400}}}`,
		`{"regulators":{"U1":{"inductance_h":1}}}`,
		`{"regulators":{"U1":{"topology":"flyback"}}}`,
		`{"regulators":{"U1":{"removed":"yes"}}}`,
		`{"regulators":{"":{}}}`,
		`{"regulators":{"U1\n.control":{}}}`,
		`{"entry":"J1\u0000"}`,
		`{"surprise":1}`,
	} {
		if _, err := validateConductedParams(json.RawMessage(bad)); err == nil {
			t.Errorf("%s was accepted", bad)
		}
	}
	p, err := validateConductedParams(json.RawMessage(`{"class":"a","entry":"J1:+12V"}`))
	if err != nil || p.Class != "A" || p.Entry != "J1:+12V" {
		t.Fatalf("got %+v, %v", p, err)
	}
	p, err = validateConductedParams(json.RawMessage(
		`{"regulators":{"U1/VLX1":{"topology":"boost","phase_deg":180,"inductance_h":4.7e-6,"confirmed":true},"U2":{"removed":true}}}`))
	if err != nil || p.Regulators["U1/VLX1"].Topology != "boost" || !p.Regulators["U1/VLX1"].Confirmed ||
		*p.Regulators["U1/VLX1"].PhaseDeg != 180 || !p.Regulators["U2"].Removed {
		t.Fatalf("got %+v, %v", p, err)
	}
	if p, err := validateConductedParams(nil); err != nil || p.Class != "B" {
		t.Fatalf("empty params: %+v, %v", p, err)
	}
}

func TestParseExperimentalKnowsConducted(t *testing.T) {
	f, unknown := ParseExperimental("conducted")
	if !f.Conducted || f.FullWave || len(unknown) != 0 {
		t.Fatalf("got %+v %v", f, unknown)
	}
	if f, _ := ParseExperimental("full-wave"); f.Conducted {
		t.Fatal("full-wave turned conducted on")
	}
}
