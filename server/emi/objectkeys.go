package emi

import (
	"path"
	"strings"
)

// Object keys that arrive from outside -- a browser naming its upload, a worker naming what it
// wrote -- are checked here before the server stores, presigns or deletes anything by them.
//
// A prefix check alone is not enough. Both blob backends clean a key before they use it, so
// "uploads/orgA/../orgB/board.kicad_pcb" passes a HasPrefix("uploads/orgA/") test and then
// reads organisation B's file. Deleting the project that recorded it deleted B's file too,
// because the sharing check compares keys as raw strings. So a key must already be in its
// clean form: what is checked is then exactly what storage will touch.

// cleanKey reports whether key is a relative object key in canonical form: no leading slash,
// no backslashes, no "." or ".." segments, no empty segments.
func cleanKey(key string) bool {
	if key == "" || strings.HasPrefix(key, "/") || strings.Contains(key, "\\") {
		return false
	}
	if path.Clean(key) != key {
		return false
	}
	for _, seg := range strings.Split(key, "/") {
		if seg == "" || seg == "." || seg == ".." {
			return false
		}
	}
	return true
}

// uploadKeyPrefix is where everything a user of an organisation uploads lives.
func uploadKeyPrefix(orgID string) string {
	return "uploads/" + orgID + "/"
}

// uploadKeyAllowed reports whether key names an object in this organisation's uploads.
func uploadKeyAllowed(key, orgID string) bool {
	return orgID != "" && cleanKey(key) && strings.HasPrefix(key, uploadKeyPrefix(orgID))
}

// runKeyAllowed reports whether key names an object under one run's own prefix. A worker
// holds a token for exactly one run, so that is the only place it may say it wrote to.
func runKeyAllowed(key, runID string) bool {
	return runID != "" && cleanKey(key) && strings.HasPrefix(key, artifactKey(runID, ""))
}
