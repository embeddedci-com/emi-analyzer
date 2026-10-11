package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/embeddedci-com/emi-analyzer/server/emiclient"
	"github.com/embeddedci-com/emi-analyzer/server/localapp"
	"github.com/embeddedci-com/emi-analyzer/server/mcpserver"
)

// stateEnv moves the MCP server's own state (which project each board file became) out of the
// user's config folder: for tests, and for a second, separate setup.
const stateEnv = "EMI_ANALYZER_MCP_STATE"

// runMCP serves the MCP server on stdin and stdout: `emi-local mcp`, which an agent's plugin
// starts. It is a client of the app, never the app itself: it finds the running one (the
// desktop app, or a server you started) and starts it when it is not up, headless from this
// very binary only when no app is installed.
//
// stdout is the MCP stream, so everything else -- logs included -- goes to stderr.
func runMCP(args []string) int {
	fs := flag.NewFlagSet("mcp", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	fs.Usage = func() {
		fmt.Fprintln(os.Stderr, "usage: emi-local mcp\n\nServes the EMI Analyzer MCP server on stdio for coding agents (Claude Code, Codex).\n"+
			"It talks to the running app, starting it when it is not up. Environment:\n"+
			"  "+localapp.URLEnv+"  use the app at this address (http://127.0.0.1:port) and never start one\n"+
			"  "+localapp.AppEnv+"  the application to start, when it is not where it usually is\n"+
			"  "+stateEnv+"  where to remember which project each board file became (default: "+emiclient.DefaultStoreDir()+")")
	}
	if err := fs.Parse(args); err != nil {
		return 2
	}
	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelWarn}))
	slog.SetDefault(logger)

	self, err := os.Executable()
	if err != nil {
		self = ""
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	opts := mcpserver.Options{Finder: localapp.New(localapp.Options{Self: self})}
	if dir := os.Getenv(stateEnv); dir != "" {
		opts.Store = emiclient.NewProjectStore(dir)
	}
	srv := mcpserver.New(version, opts)
	if err := srv.Run(ctx, &mcp.StdioTransport{}); err != nil && ctx.Err() == nil {
		logger.Error("mcp server stopped", "err", err)
		return 1
	}
	return 0
}
