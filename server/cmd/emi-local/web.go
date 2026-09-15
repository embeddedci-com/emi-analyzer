package main

import (
	"embed"
	"io/fs"
	"net/http"
	"os"
	"path"
	"strings"
)

// The built webapp (webapp/dist-app), copied in by `make local-webapp` before `go build`.
// Only a .gitkeep is committed, so a build that skipped that step still compiles and says so.
//
//go:embed all:web
var embedded embed.FS

const notBuilt = `<!doctype html><meta charset="utf-8"><title>EMI Analyzer</title>
<body style="font-family:system-ui;max-width:40rem;margin:4rem auto;padding:0 1rem">
<h1>The webapp is not built into this binary</h1>
<p>Build it with <code>make local</code> from the repository root, or run
<code>emi-local -webapp webapp/dist-app</code>.</p>`

// webHandler serves the single-page app: a path naming a file gets the file, any other path
// gets index.html so a deep link survives a reload. /api and /blob never reach here.
func webHandler(dir string) (http.Handler, error) {
	var root fs.FS
	if dir != "" {
		root = os.DirFS(dir)
	} else {
		sub, err := fs.Sub(embedded, "web")
		if err != nil {
			return nil, err
		}
		root = sub
	}
	index, err := fs.ReadFile(root, "index.html")
	if err != nil {
		index = []byte(notBuilt)
	}
	files := http.FileServerFS(root)

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rel := strings.TrimPrefix(path.Clean("/"+r.URL.Path), "/")
		if rel == "" || rel == "index.html" {
			serveIndex(w, index)
			return
		}
		if st, err := fs.Stat(root, rel); err != nil || st.IsDir() {
			// A missing bundle must 404, not return index.html as JavaScript.
			if strings.HasPrefix(rel, "assets/") {
				http.NotFound(w, r)
				return
			}
			serveIndex(w, index)
			return
		}
		if strings.HasPrefix(rel, "assets/") {
			w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
		}
		files.ServeHTTP(w, r)
	}), nil
}

func serveIndex(w http.ResponseWriter, index []byte) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write(index)
}
