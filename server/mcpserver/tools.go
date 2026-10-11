package mcpserver

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
	"github.com/embeddedci-com/emi-analyzer/server/emiclient"
	"github.com/embeddedci-com/emi-analyzer/server/localapp"
)

// maxWait caps one wait, so a call returns well inside any client's tool timeout.
const maxWait = 600

// ---- status ----

type statusIn struct{}

type statusOut struct {
	URL     string          `json:"url" jsonschema:"where the app is listening (loopback only)"`
	Version string          `json:"version"`
	DataDir string          `json:"data_dir" jsonschema:"where its database, boards and results are kept"`
	Worker  localapp.Worker `json:"worker" jsonschema:"the Docker worker that runs analyzes; state 'running' means it can take work"`
	Advice  string          `json:"advice,omitempty" jsonschema:"what to tell the user when the worker cannot run"`
}

func (s *Server) status(ctx context.Context, _ *mcp.CallToolRequest, _ statusIn) (*mcp.CallToolResult, statusOut, error) {
	_, e, err := s.client(ctx)
	if err != nil {
		return nil, statusOut{}, err
	}
	return nil, statusOut{URL: e.URL, Version: e.Version, DataDir: e.DataDir, Worker: e.Worker, Advice: workerAdvice(e.Worker)}, nil
}

// workerAdvice turns the worker's state into what the user can do about it.
func workerAdvice(w localapp.Worker) string {
	switch w.State {
	case "docker_missing":
		return "Docker is not installed. EMI Analyzer runs its analysis in Docker: install Docker Desktop (or Docker Engine on Linux), then restart the app."
	case "docker_not_running":
		return "Docker is installed but not running. Start Docker Desktop (or the Docker engine) and the app picks it up."
	case "pulling":
		return "The worker image is downloading, once per app version. Analyzes queue until it is ready."
	case "error":
		return "The worker could not start: " + w.Message + " Open the app for details and its worker log."
	}
	return ""
}

// ---- analyze_board ----

type analyzeIn struct {
	Path    string `json:"path" jsonschema:"absolute path to a .kicad_pcb, .kicad_pro, project folder, Gerber folder or .zip"`
	Project string `json:"project_id,omitempty" jsonschema:"file the board as a new version of this project instead of the one remembered for this path"`
	Force   bool   `json:"force,omitempty" jsonschema:"analyze again even when the app already has these exact files analyzed"`
	WaitS   int    `json:"wait_s,omitempty" jsonschema:"seconds to wait for the analysis to finish (0 returns at once, max 600)"`
}

type analyzeOut struct {
	ProjectID  string   `json:"project_id"`
	BoardID    string   `json:"board_id" jsonschema:"this version of the board"`
	NewProject bool     `json:"new_project"`
	Uploaded   bool     `json:"uploaded" jsonschema:"false when the app already had these files and nothing was sent"`
	Kind       string   `json:"source_kind" jsonschema:"kicad or gerber"`
	Files      []string `json:"files" jsonschema:"what was sent"`
	Run        *runOut  `json:"run,omitempty" jsonschema:"the analysis; absent when the app already had this board analyzed"`
	Note       string   `json:"note,omitempty"`
}

func (s *Server) analyzeBoard(ctx context.Context, req *mcp.CallToolRequest, in analyzeIn) (*mcp.CallToolResult, analyzeOut, error) {
	if strings.TrimSpace(in.Path) == "" {
		return nil, analyzeOut{}, fmt.Errorf("path is required: a .kicad_pcb, .kicad_pro, folder or .zip")
	}
	p, err := emiclient.Pack(in.Path)
	if err != nil {
		return nil, analyzeOut{}, err
	}
	c, _, err := s.client(ctx)
	if err != nil {
		return nil, analyzeOut{}, err
	}
	o, err := emiclient.Open(ctx, c, s.store, p, in.Project, in.Force)
	if err != nil {
		return nil, analyzeOut{}, err
	}
	out := analyzeOut{ProjectID: o.ProjectID, BoardID: o.BoardID, NewProject: o.NewProject, Uploaded: o.Uploaded,
		Kind: string(p.Kind), Files: p.Files}
	if o.RunID == "" {
		out.Note = "The app already has these exact files analyzed; nothing was sent. Pass force=true to analyze them again."
		return nil, out, nil
	}
	if !o.Uploaded {
		out.Note = "These exact files were already sent and their analysis is still running; this is that run."
	}
	r, err := s.wait(ctx, req, c, o.RunID, in.WaitS)
	if err != nil {
		return nil, analyzeOut{}, err
	}
	out.Run = r
	return nil, out, nil
}

// ---- run_status ----

type runIn struct {
	RunID string `json:"run_id"`
	WaitS int    `json:"wait_s,omitempty" jsonschema:"seconds to wait for the run to finish (0 returns at once, max 600)"`
}

type runOut struct {
	ID        string         `json:"run_id"`
	Kind      string         `json:"kind"`
	Status    string         `json:"status" jsonschema:"new, retry_pending, in_progress, stopping, done, failed or timed_out"`
	Finished  bool           `json:"finished"`
	ProjectID string         `json:"project_id"`
	BoardID   string         `json:"board_id,omitempty"`
	Stage     string         `json:"stage,omitempty"`
	Pct       float64        `json:"pct,omitempty"`
	Message   string         `json:"message,omitempty"`
	Error     string         `json:"error,omitempty"`
	Summary   map[string]any `json:"summary,omitempty" jsonschema:"what the run reported when it finished"`
	Waited    string         `json:"waited,omitempty"`
	Hint      string         `json:"hint,omitempty"`
}

func (s *Server) runStatus(ctx context.Context, req *mcp.CallToolRequest, in runIn) (*mcp.CallToolResult, runOut, error) {
	if in.RunID == "" {
		return nil, runOut{}, fmt.Errorf("run_id is required")
	}
	c, _, err := s.client(ctx)
	if err != nil {
		return nil, runOut{}, err
	}
	r, err := s.wait(ctx, req, c, in.RunID, in.WaitS)
	if err != nil {
		return nil, runOut{}, err
	}
	return nil, *r, nil
}

// wait polls a run until it finishes or waitS passes, reporting progress to a client that
// asked for it.
func (s *Server) wait(ctx context.Context, req *mcp.CallToolRequest, c *emiclient.Client, runID string, waitS int) (*runOut, error) {
	if waitS < 0 {
		waitS = 0
	}
	if waitS > maxWait {
		waitS = maxWait
	}
	began := time.Now()
	deadline := began.Add(time.Duration(waitS) * time.Second)
	var token any
	if req != nil && req.Params != nil {
		token = req.Params.GetProgressToken()
	}
	lastPct := -1.0
	for {
		r, err := c.GetRun(ctx, runID)
		if err != nil {
			return nil, err
		}
		out := shapeRun(r)
		if waitS > 0 {
			out.Waited = time.Since(began).Round(time.Second).String()
		}
		if out.Finished || !time.Now().Before(deadline) {
			if !out.Finished && waitS > 0 && out.Hint == "" {
				out.Hint = "Still running. Call run_status again with wait_s, or tell the user it is in progress."
			}
			return &out, nil
		}
		if token != nil && req.Session != nil && out.Pct != lastPct {
			lastPct = out.Pct
			msg := out.Stage
			if out.Message != "" {
				msg = out.Message
			}
			_ = req.Session.NotifyProgress(ctx, &mcp.ProgressNotificationParams{
				ProgressToken: token, Progress: out.Pct, Total: 100, Message: msg,
			})
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(s.poll):
		}
	}
}

func shapeRun(r emi.Run) runOut {
	out := runOut{ID: r.ID, Kind: string(r.Kind), Status: string(r.Status), Finished: !emiclient.Pending(r.Status),
		ProjectID: r.ProjectID, BoardID: r.BoardID, Error: r.Error}
	if r.Progress != nil {
		out.Stage, out.Pct, out.Message = r.Progress.Stage, r.Progress.Pct, r.Progress.Message
	}
	if out.Finished && len(r.Summary) > 0 {
		_ = json.Unmarshal(r.Summary, &out.Summary) // a summary that is not an object is left out
	}
	switch r.Status {
	case emi.StatusNew, emi.StatusRetryPending:
		out.Hint = "Queued for the worker. If it stays queued, call status: Docker may not be running."
	case emi.StatusTimedOut:
		out.Hint = "The worker stopped reporting on this run. Call status to see whether the worker is running."
	}
	return out
}

// ---- list_projects ----

type listProjectsIn struct{}

type projectOut struct {
	ID         string `json:"project_id"`
	Name       string `json:"name"`
	SourceKind string `json:"source_kind"`
	CreatedAt  string `json:"created_at"`
}

type listProjectsOut struct {
	Projects []projectOut `json:"projects"`
}

func (s *Server) listProjects(ctx context.Context, _ *mcp.CallToolRequest, _ listProjectsIn) (*mcp.CallToolResult, listProjectsOut, error) {
	c, _, err := s.client(ctx)
	if err != nil {
		return nil, listProjectsOut{}, err
	}
	ps, err := c.ListProjects(ctx)
	if err != nil {
		return nil, listProjectsOut{}, err
	}
	sort.SliceStable(ps, func(i, j int) bool { return ps[i].CreatedAt.After(ps[j].CreatedAt) })
	out := listProjectsOut{Projects: []projectOut{}}
	for _, p := range ps {
		out.Projects = append(out.Projects, projectOut{ID: p.ID, Name: p.Name, SourceKind: string(p.SourceKind),
			CreatedAt: p.CreatedAt.UTC().Format(time.RFC3339)})
	}
	return nil, out, nil
}

// ---- list_versions ----

type listVersionsIn struct {
	ProjectID string `json:"project_id"`
}

type runBrief struct {
	ID        string `json:"run_id"`
	Kind      string `json:"kind"`
	Status    string `json:"status"`
	CreatedAt string `json:"created_at"`
	Error     string `json:"error,omitempty"`
}

type versionOut struct {
	Number    int        `json:"version"`
	BoardID   string     `json:"board_id"`
	Filename  string     `json:"filename"`
	CreatedAt string     `json:"created_at"`
	Layers    int        `json:"layers,omitempty"`
	Nets      int        `json:"nets,omitempty"`
	Analysis  *runBrief  `json:"analysis,omitempty" jsonschema:"the newest finished ingest, whose findings are this version's"`
	Runs      []runBrief `json:"runs" jsonschema:"every run on this version, newest first"`
}

type listVersionsOut struct {
	ProjectID string       `json:"project_id"`
	Name      string       `json:"name"`
	Versions  []versionOut `json:"versions"`
}

func (s *Server) listVersions(ctx context.Context, _ *mcp.CallToolRequest, in listVersionsIn) (*mcp.CallToolResult, listVersionsOut, error) {
	if in.ProjectID == "" {
		return nil, listVersionsOut{}, fmt.Errorf("project_id is required (list_projects lists them)")
	}
	c, _, err := s.client(ctx)
	if err != nil {
		return nil, listVersionsOut{}, err
	}
	p, err := c.GetProject(ctx, in.ProjectID)
	if err != nil {
		return nil, listVersionsOut{}, err
	}
	boards, err := c.ListBoards(ctx, in.ProjectID)
	if err != nil {
		return nil, listVersionsOut{}, err
	}
	runs, err := c.ListRuns(ctx, in.ProjectID)
	if err != nil {
		return nil, listVersionsOut{}, err
	}
	return nil, versions(p, boards, runs), nil
}

// versions numbers a project's boards oldest first, as the compare page does (versionsOf in
// webapp/src/lib/compare.ts), and files each run under its board.
func versions(p emi.Project, boards []emi.Board, runs []emi.Run) listVersionsOut {
	sort.SliceStable(boards, func(i, j int) bool {
		if !boards[i].CreatedAt.Equal(boards[j].CreatedAt) {
			return boards[i].CreatedAt.Before(boards[j].CreatedAt)
		}
		return boards[i].ID < boards[j].ID
	})
	sort.SliceStable(runs, func(i, j int) bool { return runs[i].CreatedAt.After(runs[j].CreatedAt) })
	out := listVersionsOut{ProjectID: p.ID, Name: p.Name, Versions: []versionOut{}}
	for i, b := range boards {
		v := versionOut{Number: i + 1, BoardID: b.ID, Filename: b.InputKey[strings.LastIndex(b.InputKey, "/")+1:],
			CreatedAt: b.CreatedAt.UTC().Format(time.RFC3339), Layers: b.LayerCount, Nets: b.NetCount, Runs: []runBrief{}}
		for _, r := range runs {
			if r.BoardID != b.ID {
				continue
			}
			rb := runBrief{ID: r.ID, Kind: string(r.Kind), Status: string(r.Status),
				CreatedAt: r.CreatedAt.UTC().Format(time.RFC3339), Error: r.Error}
			v.Runs = append(v.Runs, rb)
			if v.Analysis == nil && r.Kind == emi.RunKindIngest && r.Status == emi.StatusDone {
				a := rb
				v.Analysis = &a
			}
		}
		out.Versions = append(out.Versions, v)
	}
	return out
}
