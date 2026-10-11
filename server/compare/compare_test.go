package compare

import (
	"encoding/json"
	"os"
	"reflect"
	"sort"
	"testing"
)

type fixtures struct {
	Findings []struct {
		Name     string    `json:"name"`
		Before   []Finding `json:"before"`
		After    []Finding `json:"after"`
		Expected struct {
			Unchanged []string                  `json:"unchanged"`
			Fixed     []string                  `json:"fixed"`
			New       []string                  `json:"new"`
			Headline  string                    `json:"headline"`
			Counts    map[string]SeverityCounts `json:"counts"`
			Groups    []struct {
				Rule    string   `json:"rule"`
				Changes []string `json:"changes"`
			} `json:"groups"`
		} `json:"expected"`
	} `json:"findings"`
	Nets []struct {
		Name     string      `json:"name"`
		Before   []NetRow    `json:"before"`
		After    []NetRow    `json:"after"`
		Expected []NetChange `json:"expected"`
	} `json:"nets"`
}

func load(t *testing.T) fixtures {
	t.Helper()
	raw, err := os.ReadFile("testdata/compare_fixtures.json")
	if err != nil {
		t.Fatal(err)
	}
	var f fixtures
	if err := json.Unmarshal(raw, &f); err != nil {
		t.Fatal(err)
	}
	return f
}

func sorted(s []string) []string {
	out := append([]string{}, s...)
	sort.Strings(out)
	return out
}

func TestFindingsFixtures(t *testing.T) {
	for _, c := range load(t).Findings {
		t.Run(c.Name, func(t *testing.T) {
			d := DiffFindings(c.Before, c.After, MatchToleranceMM)
			unchanged, fixed, added := []string{}, []string{}, []string{}
			for _, ch := range d.Changes {
				switch ch.Status {
				case Unchanged:
					unchanged = append(unchanged, ch.Before.ID+">"+ch.After.ID)
				case Fixed:
					fixed = append(fixed, ch.Before.ID)
				case New:
					added = append(added, ch.After.ID)
				}
			}
			e := c.Expected
			for _, got := range []struct {
				what      string
				got, want []string
			}{{"unchanged", unchanged, e.Unchanged}, {"fixed", fixed, e.Fixed}, {"new", added, e.New}} {
				if !reflect.DeepEqual(sorted(got.got), sorted(got.want)) {
					t.Errorf("%s = %v, want %v", got.what, sorted(got.got), got.want)
				}
			}
			if h := Headline(d.Counts); h != e.Headline {
				t.Errorf("headline = %q, want %q", h, e.Headline)
			}
			if e.Counts != nil && !reflect.DeepEqual(d.Counts, e.Counts) {
				t.Errorf("counts = %+v, want %+v", d.Counts, e.Counts)
			}
			if e.Groups != nil {
				groups := GroupByRule(d.Changes)
				if len(groups) != len(e.Groups) {
					t.Fatalf("%d groups, want %d", len(groups), len(e.Groups))
				}
				for i, g := range groups {
					var got []string
					for _, ch := range g.Changes {
						id := ""
						if ch.After != nil {
							id = ch.After.ID
						} else {
							id = ch.Before.ID
						}
						got = append(got, ch.Status+":"+id)
					}
					if g.Rule != e.Groups[i].Rule || !reflect.DeepEqual(got, e.Groups[i].Changes) {
						t.Errorf("group %d = %s %v, want %s %v", i, g.Rule, got, e.Groups[i].Rule, e.Groups[i].Changes)
					}
				}
			}
		})
	}
}

func TestNetsFixtures(t *testing.T) {
	for _, c := range load(t).Nets {
		t.Run(c.Name, func(t *testing.T) {
			got := DiffNets(NetReport{Rows: c.Before}, NetReport{Rows: c.After})
			gj, _ := json.Marshal(got)
			wj, _ := json.Marshal(c.Expected)
			if string(gj) != string(wj) {
				t.Errorf("got  %s\nwant %s", gj, wj)
			}
		})
	}
}
