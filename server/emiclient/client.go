// Package emiclient is the bit of the EMI Analyzer API a program on this computer uses: the
// MCP server today. It is the Go twin of kicad-plugin/emi_analyzer/client.py and project.py.
//
// The local app serves one fixed identity with no credentials and refuses anything that did
// not arrive on the loopback interface (cmd/emi-local/guard.go), so there is nothing to sign
// in to: a plain HTTP client is the whole of it. Find the app with package localapp first.
package emiclient

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

const (
	// MaxUploadBytes is the app's own default limit. A board above it is refused here,
	// before any bytes move.
	MaxUploadBytes = 200 << 20

	defaultTimeout = 30 * time.Second
	// transferTimeout covers sending a board or fetching a big artifact.
	transferTimeout = 5 * time.Minute
)

// APIError is an error status from the app, with the sentence it gave.
type APIError struct {
	Status  int
	Message string
	Path    string
}

func (e *APIError) Error() string {
	if e.Status == 0 {
		return e.Message
	}
	return fmt.Sprintf("%s (HTTP %d)", e.Message, e.Status)
}

// Client talks to one running app.
type Client struct {
	base string
	http *http.Client
}

// New returns a client for the app at base, e.g. "http://127.0.0.1:7465".
func New(base string) *Client {
	return &Client{base: strings.TrimRight(base, "/"), http: &http.Client{}}
}

func (c *Client) do(ctx context.Context, method, rawURL string, body []byte, contentType string, timeout time.Duration) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var rd io.Reader
	if body != nil {
		rd = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, rawURL, rd)
	if err != nil {
		return nil, err
	}
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	req.Header.Set("Accept", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, &APIError{Message: fmt.Sprintf("the app could not be reached (%v)", err), Path: rawURL}
	}
	defer resp.Body.Close()
	out, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, &APIError{Message: fmt.Sprintf("reading the app's answer: %v", err), Path: rawURL}
	}
	if resp.StatusCode >= 300 {
		var e struct {
			Error   string `json:"error"`
			Message string `json:"message"`
		}
		_ = json.Unmarshal(out, &e)
		msg := e.Error
		if msg == "" {
			msg = e.Message
		}
		if msg == "" {
			msg = fmt.Sprintf("the app answered %d", resp.StatusCode)
		}
		return nil, &APIError{Status: resp.StatusCode, Message: msg, Path: rawURL}
	}
	return out, nil
}

func (c *Client) api(path string) string { return c.base + "/api" + path }

func (c *Client) get(ctx context.Context, path string, into any) error {
	raw, err := c.do(ctx, http.MethodGet, c.api(path), nil, "", defaultTimeout)
	if err != nil {
		return err
	}
	return decode(raw, into)
}

func (c *Client) post(ctx context.Context, path string, payload, into any, timeout time.Duration) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	raw, err := c.do(ctx, http.MethodPost, c.api(path), body, "application/json", timeout)
	if err != nil {
		return err
	}
	return decode(raw, into)
}

func decode(raw []byte, into any) error {
	if into == nil || len(raw) == 0 {
		return nil
	}
	if err := json.Unmarshal(raw, into); err != nil {
		return fmt.Errorf("the app's answer did not parse: %w", err)
	}
	return nil
}

func esc(s string) string { return url.PathEscape(s) }

// ---- projects, boards, runs ----

// Lookup is the app's answer to "have you seen exactly these bytes before?".
type Lookup struct {
	Found   bool         `json:"found"`
	Parsed  bool         `json:"parsed"`
	Board   *emi.Board   `json:"board"`
	Project *emi.Project `json:"project"`
}

// LookupBoard asks whether the app already holds a board with this sha256.
func (c *Client) LookupBoard(ctx context.Context, sha256 string) (Lookup, error) {
	var l Lookup
	err := c.get(ctx, "/emi/boards/lookup?sha256="+url.QueryEscape(sha256), &l)
	return l, err
}

// ListProjects is every project in the app.
func (c *Client) ListProjects(ctx context.Context) ([]emi.Project, error) {
	var got struct {
		Projects []emi.Project `json:"projects"`
	}
	err := c.get(ctx, "/emi/projects", &got)
	return got.Projects, err
}

// GetProject is one project; an *APIError with Status 404 when there is none.
func (c *Client) GetProject(ctx context.Context, id string) (emi.Project, error) {
	var p emi.Project
	err := c.get(ctx, "/emi/projects/"+esc(id), &p)
	return p, err
}

// CreateProject makes an empty project.
func (c *Client) CreateProject(ctx context.Context, name string, kind emi.SourceKind) (emi.Project, error) {
	var p emi.Project
	err := c.post(ctx, "/emi/projects", map[string]any{"name": name, "source_kind": kind}, &p, defaultTimeout)
	return p, err
}

// ListBoards is a project's boards: every upload into it is a version.
func (c *Client) ListBoards(ctx context.Context, projectID string) ([]emi.Board, error) {
	var got struct {
		Boards []emi.Board `json:"boards"`
	}
	err := c.get(ctx, "/emi/projects/"+esc(projectID)+"/boards", &got)
	return got.Boards, err
}

// ListRuns is a project's runs.
func (c *Client) ListRuns(ctx context.Context, projectID string) ([]emi.Run, error) {
	var got struct {
		Runs []emi.Run `json:"runs"`
	}
	err := c.get(ctx, "/emi/projects/"+esc(projectID)+"/runs", &got)
	return got.Runs, err
}

// CreateRun starts a run of a kind on a version of a project: an ESD simulation
// ("transient") or a cable budget ("cable"), with the params that kind takes.
func (c *Client) CreateRun(ctx context.Context, projectID, boardID string, kind emi.RunKind, params any) (emi.Run, error) {
	var r emi.Run
	err := c.post(ctx, "/emi/projects/"+esc(projectID)+"/runs",
		map[string]any{"board_id": boardID, "kind": kind, "params": params}, &r, defaultTimeout)
	return r, err
}

// GetRun is one run, with its progress while it works.
func (c *Client) GetRun(ctx context.Context, id string) (emi.Run, error) {
	var r emi.Run
	err := c.get(ctx, "/emi/runs/"+esc(id), &r)
	return r, err
}

// Artifact fetches a run's artifact through the URL the app mints for it.
func (c *Client) Artifact(ctx context.Context, runID, name string) ([]byte, error) {
	var ref struct {
		URL string `json:"url"`
	}
	// The name is a relative path ("nearfield/1.bin"): its slashes are path segments.
	if err := c.get(ctx, "/emi/runs/"+esc(runID)+"/artifacts/"+name, &ref); err != nil {
		return nil, err
	}
	if ref.URL == "" {
		return nil, &APIError{Message: "the app gave no URL for " + name}
	}
	return c.do(ctx, http.MethodGet, c.resolve(ref.URL), nil, "", transferTimeout)
}

// ArtifactJSON fetches a run's JSON artifact into v.
func (c *Client) ArtifactJSON(ctx context.Context, runID, name string, v any) error {
	raw, err := c.Artifact(ctx, runID, name)
	if err != nil {
		return err
	}
	return decode(raw, v)
}

// resolve makes a URL the app handed out absolute. The local app's blob URLs are absolute
// already; a relative one is taken against the app.
func (c *Client) resolve(u string) string {
	if strings.HasPrefix(u, "/") {
		return c.base + u
	}
	return u
}

// UploadResult is the board an upload became and the ingest run analysing it.
type UploadResult struct {
	Board emi.Board `json:"board"`
	Run   emi.Run   `json:"run"`
}

// UploadBoard sends a board and queues its analysis.
//
// Two steps, as in the browser: the app says where to put the bytes, the bytes go there, and
// only then does it hear about them. The first step can answer "I already have those", and
// then nothing is sent at all.
func (c *Client) UploadBoard(ctx context.Context, projectID, filename string, data []byte, sha256 string) (UploadResult, error) {
	var res UploadResult
	if len(data) > MaxUploadBytes {
		return res, &APIError{Message: fmt.Sprintf("the board is %.0f MB, larger than the app accepts", float64(len(data))/1e6)}
	}
	var init struct {
		Key             string `json:"key"`
		UploadURL       string `json:"upload_url"`
		AlreadyUploaded bool   `json:"already_uploaded"`
		ContentType     string `json:"content_type"`
	}
	err := c.post(ctx, "/emi/projects/"+esc(projectID)+"/uploads", map[string]any{
		"filename": filename, "content_type": "application/zip", "sha256": sha256, "size_bytes": len(data),
	}, &init, defaultTimeout)
	if err != nil {
		return res, err
	}
	if !init.AlreadyUploaded {
		if init.UploadURL == "" {
			return res, &APIError{Message: "the app offered neither an upload URL nor an existing copy"}
		}
		ct := init.ContentType
		if ct == "" {
			ct = "application/zip"
		}
		if _, err := c.do(ctx, http.MethodPut, c.resolve(init.UploadURL), data, ct, transferTimeout); err != nil {
			return res, err
		}
	}
	err = c.post(ctx, "/emi/projects/"+esc(projectID)+"/boards",
		map[string]any{"input_key": init.Key, "sha256": sha256}, &res, transferTimeout)
	return res, err
}
