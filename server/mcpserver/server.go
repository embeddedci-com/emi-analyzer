// Package mcpserver is the EMI Analyzer's MCP server: what lets a coding agent (Claude Code,
// Codex) analyze a board, read what the checks found and compare versions, against the app
// running on this computer. `emi-local mcp` serves it over stdio.
//
// It is a client of the app, not a second app: every tool goes through the same HTTP API the
// webapp and the KiCad plugin use (package emiclient), so boards analyzed here show up in the
// app's window, and the other way round. Nothing leaves the computer.
package mcpserver

import (
	"context"
	"sync"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/embeddedci-com/emi-analyzer/server/emiclient"
	"github.com/embeddedci-com/emi-analyzer/server/localapp"
)

// Instructions are sent to the client once, when it connects.
const Instructions = `Analyze PCB layouts for EMI and EMC problems with the EMI Analyzer app on this computer. Boards and results stay on this computer.

Start with analyze_board on a .kicad_pcb, a .kicad_pro, a project folder, a folder of Gerber files (with the drill file and an IPC-D-356 netlist) or a .zip of either. Unsaved edits in an open KiCad are not on disk: ask the user to save first. The first analysis of a board makes a project; every later one adds a version to it, and an unchanged board is not analyzed again (force=true does).

Analysis runs in the background: analyze_board returns a run_id. Pass wait_s to wait for it, or call run_status with wait_s until its status is done (or failed, timed_out). A small board takes seconds, a large one a few minutes. If the app is not running it is started; its worker runs in Docker, and status says when Docker is the problem.

Results compare versions of the user's own board. They are not a pre-compliance test and do not predict a pass or a fail at a test lab: say so when the user asks whether a board will pass.`

// Server holds what the tools share: how to reach the app, and which project each board
// file became.
type Server struct {
	finder  Finder
	store   *emiclient.ProjectStore
	timeout time.Duration
	// poll is how often a wait asks the app about a run.
	poll time.Duration

	mu    sync.Mutex
	app   *localapp.Endpoint
	cache artifactCache
}

// The tool handlers' request and result types, shortened.
type (
	mcpRequest = mcp.CallToolRequest
	mcpResult  = mcp.CallToolResult
)

// Finder finds the app, starting it when it is not up. *localapp.Finder is one.
type Finder interface {
	Running(ctx context.Context) (localapp.Endpoint, bool)
	AttachOrStart(ctx context.Context, timeout time.Duration) (localapp.Endpoint, error)
}

// Options configure New. The zero value is the real computer.
type Options struct {
	Finder Finder
	Store  *emiclient.ProjectStore
	// StartTimeout bounds starting the app; localapp.StartTimeout by default.
	StartTimeout time.Duration
	Poll         time.Duration
}

// New builds the MCP server with every tool registered.
func New(version string, o Options) *mcp.Server {
	if o.Finder == nil {
		o.Finder = localapp.New(localapp.Options{})
	}
	if o.Store == nil {
		o.Store = emiclient.NewProjectStore(emiclient.DefaultStoreDir())
	}
	if o.StartTimeout == 0 {
		o.StartTimeout = localapp.StartTimeout
	}
	if o.Poll == 0 {
		o.Poll = 2 * time.Second
	}
	s := &Server{finder: o.Finder, store: o.Store, timeout: o.StartTimeout, poll: o.Poll}
	srv := mcp.NewServer(&mcp.Implementation{Name: "emi-analyzer", Title: "EMI Analyzer", Version: version},
		&mcp.ServerOptions{Instructions: Instructions})
	s.register(srv)
	return srv
}

// client is a client for the app, starting the app first if it is not up. The endpoint is
// kept between calls and asked again only when a call cannot reach it.
func (s *Server) client(ctx context.Context) (*emiclient.Client, localapp.Endpoint, error) {
	s.mu.Lock()
	app := s.app
	s.mu.Unlock()
	if app != nil {
		if e, ok := s.finder.Running(ctx); ok && e.URL == app.URL {
			return emiclient.New(e.URL), e, nil
		}
	}
	e, err := s.finder.AttachOrStart(ctx, s.timeout)
	if err != nil {
		return nil, localapp.Endpoint{}, err
	}
	s.mu.Lock()
	s.app = &e
	s.mu.Unlock()
	return emiclient.New(e.URL), e, nil
}

var no = false

// readOnly marks a tool that only reads.
func readOnly(title string) *mcp.ToolAnnotations {
	return &mcp.ToolAnnotations{Title: title, ReadOnlyHint: true, OpenWorldHint: &no}
}

func (s *Server) register(srv *mcp.Server) {
	mcp.AddTool(srv, &mcp.Tool{
		Name: "status",
		Description: "Whether the EMI Analyzer app is running (it is started if not), its version, data folder and " +
			"how its Docker worker is doing. Call it when an analysis does not start or never finishes.",
		Annotations: readOnly("App status"),
	}, s.status)

	mcp.AddTool(srv, &mcp.Tool{
		Name: "analyze_board",
		Description: "Analyze a board from disk: a .kicad_pcb (sent with its .kicad_pro and any emi.rules.yaml beside it), " +
			"a .kicad_pro, a folder with one board or a Gerber set, or a .zip of either. Returns the project, the version " +
			"(board_id) and the run analyzing it. An unchanged board is not analyzed again unless force is true. " +
			"wait_s waits up to that many seconds for the run to finish.",
		Annotations: &mcp.ToolAnnotations{Title: "Analyze a board", OpenWorldHint: &no, DestructiveHint: &no, IdempotentHint: true},
	}, s.analyzeBoard)

	mcp.AddTool(srv, &mcp.Tool{
		Name: "run_status",
		Description: "A run's status, stage and progress, and its summary once done. wait_s (up to 600) waits for it to " +
			"finish instead of returning at once; use it rather than calling in a tight loop.",
		Annotations: readOnly("Run status"),
	}, s.runStatus)

	mcp.AddTool(srv, &mcp.Tool{
		Name:        "list_projects",
		Description: "Every project in the app, newest first. One board file is one project; its uploads are its versions.",
		Annotations: readOnly("List projects"),
	}, s.listProjects)

	mcp.AddTool(srv, &mcp.Tool{
		Name: "list_versions",
		Description: "A project's versions, oldest first, numbered from 1, each with its newest analysis (ingest run) " +
			"and every other run made on it.",
		Annotations: readOnly("List versions"),
	}, s.listVersions)

	mcp.AddTool(srv, &mcp.Tool{
		Name: "findings",
		Description: "What the checks found in an analysis, worst first, paginated: counts by severity and by check, then " +
			"one page of findings (id, check, severity, title, net, layer, board x/y in mm, suggested action). Filter by " +
			"severity, check, net, layer or a point on the board. Name the analysis with run_id, or project_id for its " +
			"newest analyzed version.",
		Annotations: readOnly("Findings"),
	}, s.findings)

	mcp.AddTool(srv, &mcp.Tool{
		Name:        "finding",
		Description: "One finding in full: its explanation, what the check looks for, and how to suppress it on purpose in emi.rules.yaml.",
		Annotations: readOnly("Finding detail"),
	}, s.finding)

	mcp.AddTool(srv, &mcp.Tool{
		Name: "decoupling",
		Description: "Supply decoupling per rail and IC: the impedance the capacitors give against a target, where it falls " +
			"short, and the worker's recommended changes. Without rail or ic: one line per rail. With rail or ic: each IC's " +
			"capacitors (distance, ground vias, useful frequency), gaps and recommendations.",
		Annotations: readOnly("Decoupling"),
	}, s.decoupling)

	mcp.AddTool(srv, &mcp.Tool{
		Name: "nets",
		Description: "Per-net routing from the analysis: copper and pad-to-pad length, vias, delay, widths, differential " +
			"partner, and skew against its length-matching group's reference and tolerance. Filter by name, group, or out of tolerance.",
		Annotations: readOnly("Nets"),
	}, s.nets)

	mcp.AddTool(srv, &mcp.Tool{
		Name: "compare",
		Description: "What changed between two analyzed versions of a project: findings fixed, new and unchanged, grouped " +
			"by check (a finding that moved a little is the same finding), and the nets whose length or vias changed. " +
			"Defaults to the two newest analyzed versions.",
		Annotations: readOnly("Compare versions"),
	}, s.compareVersions)

	mcp.AddTool(srv, &mcp.Tool{
		Name:        "checks",
		Description: "The checks the analyzer runs, with their settings and defaults, and how to change them in emi.rules.yaml.",
		Annotations: readOnly("Checks"),
	}, s.checks)

	mcp.AddTool(srv, &mcp.Tool{
		Name: "open_in_app",
		Description: "A link to a project in the EMI Analyzer app (a version, or the comparison of two), where the user sees " +
			"the findings marked on the board. open=true also opens it in their browser.",
		Annotations: &mcp.ToolAnnotations{Title: "Open in the app", OpenWorldHint: &no, DestructiveHint: &no, IdempotentHint: true},
	}, s.openInApp)

	s.registerESDCables(srv)
}
