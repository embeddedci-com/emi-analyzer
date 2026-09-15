package main

import (
	"io"
	"io/fs"
	"net"
	"net/http"
	"os"
	"path"
	"strings"
	"time"
)

// spaHandler serves the built EMI webapp under a base path.
//
// The tool is reached at a sub-path of the site (/tools/emi) rather than at a host of its
// own, so the SPA is built with a matching Vite `base` and every asset it requests already
// carries that prefix. This handler therefore strips the prefix and looks the rest up in
// dir, with the usual SPA fallback: a path that names no file is a client-side route and
// gets index.html, so a deep link like /tools/emi/<project-id> survives a page reload.
//
// The fallback deliberately does *not* apply to the asset directory. Without that
// exception a mistyped or stale bundle URL returns index.html with a 200 and an HTML
// content type, which the browser then tries to execute as JavaScript -- an error that
// reads as a syntax error in a file that looks fine.
func spaHandler(dir, base string) (http.Handler, error) {
	index, err := os.ReadFile(path.Join(dir, "index.html"))
	if err != nil {
		return nil, err
	}
	root := os.DirFS(dir)
	files := http.FileServerFS(root)

	base = "/" + strings.Trim(base, "/")

	serveIndex := func(w http.ResponseWriter, r *http.Request) {
		// The index names the hashed bundles, so it is the one file that must never be
		// cached: a stale copy points at assets that no longer exist.
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write(index)
	}

	return http.StripPrefix(base, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rel := strings.TrimPrefix(path.Clean("/"+r.URL.Path), "/")
		if rel == "" || rel == "." {
			serveIndex(w, r)
			return
		}
		if st, err := fs.Stat(root, rel); err != nil || st.IsDir() {
			if strings.HasPrefix(rel, "assets/") {
				http.NotFound(w, r)
				return
			}
			serveIndex(w, r)
			return
		}
		// Vite fingerprints asset filenames, so their contents can never change under a
		// given name and they are safe to cache for as long as the browser will.
		if strings.HasPrefix(rel, "assets/") {
			w.Header().Set("Cache-Control", "public, max-age=31536000, immutable")
		}
		files.ServeHTTP(w, r)
	})), nil
}

// probeHealth is the container healthcheck, run as `emi-server -health-check`.
//
// addr is the listen address, so it is usually just ":8090" -- the host half is empty and
// has to be filled in before it can be dialled.
func probeHealth(addr string) int {
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return 2
	}
	if host == "" || host == "0.0.0.0" || host == "::" {
		host = "127.0.0.1"
	}
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get("http://" + net.JoinHostPort(host, port) + "/api/health")
	if err != nil {
		return 1
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode != http.StatusOK {
		return 1
	}
	return 0
}
