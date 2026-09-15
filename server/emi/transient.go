package emi

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

// TransientModel is a vendor SPICE model a user uploaded for one part on the board.
//
// The file itself goes through the ordinary upload endpoint, so it lands content-addressed under
// the organisation's uploads prefix. The server never reads it: deciding whether it is really a
// SPICE model -- and not a netlist that runs shell commands -- is the worker's job, and the
// worker does it before ngspice sees a byte. What the server enforces is where the key points.
type TransientModel struct {
	Part     string            `json:"part"`
	Key      string            `json:"key"`
	Filename string            `json:"filename"`
	Subckt   string            `json:"subckt,omitempty"`
	Pins     map[string]string `json:"pins,omitempty"`
}

// TransientParams is what a transient run accepts.
type TransientParams struct {
	Standard string           `json:"standard"`
	Level    int              `json:"level"`
	Polarity string           `json:"polarity"`
	Lines    []string         `json:"lines,omitempty"`
	Models   []TransientModel `json:"models,omitempty"`
}

const (
	transientStandard  = "61000-4-2"
	maxTransientModels = 20
	maxTransientLines  = 200
	maxModelBytes      = 2 << 20
	maxModelPins       = 64
)

// modelSuffixes are the names SPICE libraries are shipped under. A suffix proves nothing about
// the contents; it only stops the obvious mistake of attaching a board file or a PDF here.
var modelSuffixes = []string{
	".lib", ".cir", ".sub", ".subckt", ".mod", ".model", ".sp", ".spi", ".spice", ".ckt", ".txt", ".inc",
}

// modelKeyPrefix is the only place a model a run uses may live: this organisation's uploads.
// Without this a run could name any object in the bucket and have a worker download it.
func modelKeyPrefix(orgID string) string {
	return "uploads/" + orgID + "/"
}

func modelKeyAllowed(key, orgID string) bool {
	return orgID != "" && strings.HasPrefix(key, modelKeyPrefix(orgID)) && !strings.Contains(key, "..")
}

type statFunc func(ctx context.Context, key string) (int64, string, error)

// validateTransientParams checks a transient run's parameters before it is queued, and returns
// them normalised: defaults filled in, so the worker and the UI read one shape.
func validateTransientParams(ctx context.Context, raw json.RawMessage, orgID string, stat statFunc) (TransientParams, error) {
	var p TransientParams
	if len(raw) > 0 && string(raw) != "null" {
		if err := json.Unmarshal(raw, &p); err != nil {
			return p, fmt.Errorf("params are not valid: %v", err)
		}
	}
	if p.Standard == "" {
		p.Standard = transientStandard
	}
	if p.Standard != transientStandard {
		return p, fmt.Errorf("standard must be %q", transientStandard)
	}
	if p.Level == 0 {
		p.Level = 4
	}
	if p.Level < 1 || p.Level > 4 {
		return p, errors.New("level must be 1, 2, 3 or 4 (2, 4, 6 or 8 kV contact discharge)")
	}
	switch p.Polarity {
	case "":
		p.Polarity = "both"
	case "both", "positive", "negative":
	default:
		return p, errors.New(`polarity must be "both", "positive" or "negative"`)
	}
	if len(p.Lines) > maxTransientLines {
		return p, fmt.Errorf("at most %d lines can be named", maxTransientLines)
	}
	for _, l := range p.Lines {
		if l == "" || len(l) > 200 {
			return p, errors.New("line names must be net names of up to 200 characters")
		}
	}
	if len(p.Models) > maxTransientModels {
		return p, fmt.Errorf("at most %d models can be attached to a run", maxTransientModels)
	}

	seen := map[string]bool{}
	for i, m := range p.Models {
		where := fmt.Sprintf("model %d", i+1)
		m.Part = strings.TrimSpace(m.Part)
		if m.Part == "" || len(m.Part) > 100 {
			return p, fmt.Errorf("%s: part must be the part value, up to 100 characters", where)
		}
		if seen[strings.ToUpper(m.Part)] {
			return p, fmt.Errorf("%s: %s already has a model in this run", where, m.Part)
		}
		seen[strings.ToUpper(m.Part)] = true
		if !modelKeyAllowed(m.Key, orgID) {
			return p, fmt.Errorf("%s: the file must be one uploaded to this project", where)
		}
		name := strings.ToLower(m.Filename)
		okSuffix := false
		for _, s := range modelSuffixes {
			if strings.HasSuffix(name, s) {
				okSuffix = true
				break
			}
		}
		if !okSuffix {
			return p, fmt.Errorf("%s: %q does not look like a SPICE library (.lib, .cir, .sub, .mod, .sp, …)", where, m.Filename)
		}
		if len(m.Subckt) > 100 {
			return p, fmt.Errorf("%s: subcircuit name is too long", where)
		}
		if len(m.Pins) > maxModelPins {
			return p, fmt.Errorf("%s: at most %d pins can be mapped", where, maxModelPins)
		}
		for pad, pin := range m.Pins {
			if pad == "" || pin == "" || len(pad) > 50 || len(pin) > 50 {
				return p, fmt.Errorf("%s: pin mappings must be short pad numbers and pin names", where)
			}
		}
		size, _, err := stat(ctx, m.Key)
		if err != nil {
			return p, fmt.Errorf("%s: the uploaded file was not found", where)
		}
		if size <= 0 || size > maxModelBytes {
			return p, fmt.Errorf("%s: a model file must be between 1 byte and %d MB", where, maxModelBytes>>20)
		}
		p.Models[i] = m
	}
	return p, nil
}
