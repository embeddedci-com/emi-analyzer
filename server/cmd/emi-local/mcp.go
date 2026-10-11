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

	"github.com/embeddedci-com/emi-analyzer/server/localapp"
	"github.com/embeddedci-com/emi-analyzer/server/mcpserver"
)

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
			"  "+localapp.AppEnv+"  the application to start, when it is not where it usually is")
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
	srv := mcpserver.New(version, mcpserver.Options{Finder: localapp.New(localapp.Options{Self: self})})
	if err := srv.Run(ctx, &mcp.StdioTransport{}); err != nil && ctx.Err() == nil {
		logger.Error("mcp server stopped", "err", err)
		return 1
	}
	return 0
}
