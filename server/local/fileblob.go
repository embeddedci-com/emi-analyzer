package local

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

// FileBlob implements emi.Blob on the local filesystem, serving the "presigned" URLs itself.
//
// The emi package never carries bulk bytes: it hands out URLs, and the browser and the worker
// PUT and GET against those directly. Hosted, the URLs point at S3. Here they point back at
// this process, at Handler, and are signed with an HMAC over the method, key, expiry and
// content type -- the same properties a presigned S3 URL has, so the handlers and both
// clients cannot tell the difference.
//
// The URL's origin is the one the *requester* used to reach this server, taken from the
// request context (see WithOrigin). That matters because the two clients reach it by
// different names: the browser as 127.0.0.1, the worker container as host.docker.internal.
// A URL minted for one would be unreachable for the other.
type FileBlob struct {
	root     string // objects live under root/objects, content types under root/meta
	secret   []byte
	basePath string // where Handler is mounted, e.g. "/blob"
	fallback string // origin used when the context carries none
}

// NewFileBlob stores objects under dir. basePath is where Handler will be mounted; fallback
// is the origin used for a URL minted outside a request.
func NewFileBlob(dir string, secret []byte, basePath, fallback string) (*FileBlob, error) {
	if len(secret) < 32 {
		return nil, errors.New("local: blob secret must be at least 32 bytes")
	}
	for _, sub := range []string{"objects", "meta"} {
		if err := os.MkdirAll(filepath.Join(dir, sub), 0o700); err != nil {
			return nil, err
		}
	}
	return &FileBlob{
		root:     dir,
		secret:   secret,
		basePath: "/" + strings.Trim(basePath, "/"),
		fallback: strings.TrimRight(fallback, "/"),
	}, nil
}

var _ emi.Blob = (*FileBlob)(nil)

type originKey struct{}

// WithOrigin records the scheme://host a request arrived on, for URLs minted while serving it.
func WithOrigin(ctx context.Context, origin string) context.Context {
	return context.WithValue(ctx, originKey{}, strings.TrimRight(origin, "/"))
}

func (b *FileBlob) origin(ctx context.Context) string {
	if o, ok := ctx.Value(originKey{}).(string); ok && o != "" {
		return o
	}
	return b.fallback
}

// cleanKey maps a key onto a relative slash path that cannot climb out of the store. The same
// guarantee S3Blob.objectKey gives: artifact keys come from the worker, and ".." must not let
// one read or write outside its namespace.
func cleanKey(key string) string {
	return strings.TrimPrefix(path.Clean("/"+key), "/")
}

func (b *FileBlob) objectPath(key string) string {
	return filepath.Join(b.root, "objects", filepath.FromSlash(cleanKey(key)))
}

func (b *FileBlob) metaPath(key string) string {
	return filepath.Join(b.root, "meta", filepath.FromSlash(cleanKey(key)))
}

func (b *FileBlob) sign(op, key, contentType string, exp int64) string {
	m := hmac.New(sha256.New, b.secret)
	fmt.Fprintf(m, "%s\n%s\n%s\n%d", op, cleanKey(key), contentType, exp)
	return hex.EncodeToString(m.Sum(nil))
}

func (b *FileBlob) presign(ctx context.Context, op, key, contentType string, ttl time.Duration) string {
	exp := time.Now().Add(ttl).Unix()
	q := url.Values{}
	q.Set("op", op)
	q.Set("exp", strconv.FormatInt(exp, 10))
	if contentType != "" {
		q.Set("ct", contentType)
	}
	q.Set("sig", b.sign(op, key, contentType, exp))
	u := url.URL{Path: b.basePath + "/" + cleanKey(key)}
	return b.origin(ctx) + u.EscapedPath() + "?" + q.Encode()
}

func (b *FileBlob) PresignGet(ctx context.Context, key string, ttl time.Duration) (string, error) {
	return b.presign(ctx, "get", key, "", ttl), nil
}

func (b *FileBlob) PresignPut(ctx context.Context, key, contentType string, ttl time.Duration) (string, error) {
	return b.presign(ctx, "put", key, contentType, ttl), nil
}

func (b *FileBlob) Stat(_ context.Context, key string) (int64, string, error) {
	st, err := os.Stat(b.objectPath(key))
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return 0, "", emi.ErrNotFound
		}
		return 0, "", err
	}
	if st.IsDir() {
		return 0, "", emi.ErrNotFound
	}
	ct, _ := os.ReadFile(b.metaPath(key))
	return st.Size(), string(ct), nil
}

// Handler serves GET, HEAD and PUT on signed URLs. Mount it at basePath + "/".
func (b *FileBlob) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Blob responses are per-object and short-lived, like every other emi response.
		w.Header().Set("Cache-Control", "private, no-store")

		key, ok := strings.CutPrefix(r.URL.Path, b.basePath+"/")
		if !ok || cleanKey(key) == "" {
			http.NotFound(w, r)
			return
		}
		q := r.URL.Query()
		op := q.Get("op")
		exp, err := strconv.ParseInt(q.Get("exp"), 10, 64)
		if err != nil {
			http.Error(w, "missing expiry", http.StatusForbidden)
			return
		}
		want := b.sign(op, key, q.Get("ct"), exp)
		got, _ := hex.DecodeString(q.Get("sig"))
		wantB, _ := hex.DecodeString(want)
		if len(got) == 0 || !hmac.Equal(got, wantB) {
			http.Error(w, "signature does not match", http.StatusForbidden)
			return
		}
		if time.Now().Unix() > exp {
			http.Error(w, "url has expired", http.StatusForbidden)
			return
		}

		switch {
		case op == "get" && (r.Method == http.MethodGet || r.Method == http.MethodHead):
			b.serveGet(w, r, key)
		case op == "put" && r.Method == http.MethodPut:
			b.servePut(w, r, key, q.Get("ct"))
		default:
			http.Error(w, "method not allowed for this url", http.StatusMethodNotAllowed)
		}
	})
}

func (b *FileBlob) serveGet(w http.ResponseWriter, r *http.Request, key string) {
	f, err := os.Open(b.objectPath(key))
	if err != nil {
		http.NotFound(w, r)
		return
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil || st.IsDir() {
		http.NotFound(w, r)
		return
	}
	if ct, err := os.ReadFile(b.metaPath(key)); err == nil && len(ct) > 0 {
		w.Header().Set("Content-Type", string(ct))
	}
	// ServeContent handles Range, which the viewer uses on large field dumps.
	http.ServeContent(w, r, "", st.ModTime(), f)
}

func (b *FileBlob) servePut(w http.ResponseWriter, r *http.Request, key, signedCT string) {
	dst := b.objectPath(key)
	if err := os.MkdirAll(filepath.Dir(dst), 0o700); err != nil {
		http.Error(w, "cannot create directory", http.StatusInternalServerError)
		return
	}
	// Write beside the destination and rename, so a reader never sees half an object and an
	// interrupted upload leaves the previous one intact.
	tmp, err := os.CreateTemp(filepath.Dir(dst), ".upload-*")
	if err != nil {
		http.Error(w, "cannot create file", http.StatusInternalServerError)
		return
	}
	_, copyErr := io.Copy(tmp, r.Body)
	closeErr := tmp.Close()
	if copyErr != nil || closeErr != nil {
		os.Remove(tmp.Name())
		http.Error(w, "upload interrupted", http.StatusBadRequest)
		return
	}
	if err := os.Rename(tmp.Name(), dst); err != nil {
		os.Remove(tmp.Name())
		http.Error(w, "cannot store file", http.StatusInternalServerError)
		return
	}

	ct := signedCT
	if ct == "" {
		ct = r.Header.Get("Content-Type")
	}
	meta := b.metaPath(key)
	if err := os.MkdirAll(filepath.Dir(meta), 0o700); err == nil {
		_ = os.WriteFile(meta, []byte(ct), 0o600)
	}
	w.WriteHeader(http.StatusOK)
}
