package emiclient

import (
	"context"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

// ProjectStore remembers which project a board file became, between calls.
//
// One board file is one project. A board is analyzed over and over while it is being worked
// on, and each pass is a new board (a version) in the same project, so the history of a
// design stays in one place instead of scattering into a project per call. Same file format
// as the KiCad plugin's ProjectStore (project.py), one JSON file per board path.
type ProjectStore struct{ dir string }

// NewProjectStore keeps its files under dir/boards.
func NewProjectStore(dir string) *ProjectStore {
	return &ProjectStore{dir: filepath.Join(dir, "boards")}
}

// DefaultStoreDir is the MCP server's own folder beside the app's endpoint file.
func DefaultStoreDir() string {
	dir, err := os.UserConfigDir()
	if err != nil {
		dir = os.TempDir()
	}
	return filepath.Join(dir, "emi-analyzer", "mcp")
}

func (s *ProjectStore) file(board string) string {
	sum := sha1.Sum([]byte(board))
	return filepath.Join(s.dir, hex.EncodeToString(sum[:])[:20]+".json")
}

type storedBoard struct {
	Board   string `json:"board"`
	SavedAt int64  `json:"saved_at"`
	Project string `json:"project"`
	// The last upload from here: its hash and the board and ingest run it became. The app
	// records a board's hash only once its ingest has read the bytes, so until then this is
	// the only way to know the same bytes are already queued.
	LastSHA256 string `json:"last_sha256,omitempty"`
	LastBoard  string `json:"last_board,omitempty"`
	LastRun    string `json:"last_run,omitempty"`
}

func (s *ProjectStore) load(board string) storedBoard {
	b, err := os.ReadFile(s.file(board))
	if err != nil {
		return storedBoard{}
	}
	var d storedBoard
	if json.Unmarshal(b, &d) != nil || d.Board != board {
		return storedBoard{}
	}
	return d
}

// Load is the project this board became, or "".
func (s *ProjectStore) Load(board string) string { return s.load(board).Project }

// Save records the project a board became, keeping what is known about its last upload
// when the project is the same.
func (s *ProjectStore) Save(board, project string) error {
	d := s.load(board)
	if d.Project != project {
		d = storedBoard{}
	}
	d.Board, d.Project = board, project
	return s.write(d)
}

// saveUpload records an upload: the project, and the hash, board and run it became.
func (s *ProjectStore) saveUpload(board, project, sha, boardID, runID string) error {
	return s.write(storedBoard{Board: board, Project: project, LastSHA256: sha, LastBoard: boardID, LastRun: runID})
}

// write stores a record atomically: a crash mid-write keeps the old file.
func (s *ProjectStore) write(d storedBoard) error {
	if err := os.MkdirAll(s.dir, 0o700); err != nil {
		return err
	}
	d.SavedAt = time.Now().Unix()
	b, _ := json.MarshalIndent(d, "", "  ")
	path := s.file(d.Board)
	tmp := fmt.Sprintf("%s.%d.tmp", path, os.Getpid())
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// Forget drops what was remembered about a board.
func (s *ProjectStore) Forget(board string) { _ = os.Remove(s.file(board)) }

// Pending is true for a run that has not finished: it may still be waiting for a worker.
func Pending(s emi.RunStatus) bool {
	switch s {
	case emi.StatusNew, emi.StatusRetryPending, emi.StatusInProgress, emi.StatusStopping:
		return true
	}
	return false
}

// Opened is what became of a board handed to the app.
type Opened struct {
	ProjectID string `json:"project_id"`
	BoardID   string `json:"board_id,omitempty"`
	// RunID is the ingest analyzing it: a new one, or the one still pending for these exact
	// bytes sent from here. Empty when the app already had this board analyzed and nothing was sent.
	RunID    string `json:"run_id,omitempty"`
	Uploaded bool   `json:"uploaded"`
	// NewProject is true when this call made the project.
	NewProject bool `json:"new_project"`
}

// Open hands a packed board to the app and says which project and run to look at.
//
// Nothing is sent when the app already holds exactly these bytes, analyzed: the common case of
// asking again about a board that was not touched. force sends it anyway, for a fresh run.
// store may be nil; project, when set, files the board there instead of the remembered one.
func Open(ctx context.Context, c *Client, store *ProjectStore, p *Packed, project string, force bool) (Opened, error) {
	remembered := project
	if remembered == "" && store != nil {
		remembered = store.Load(p.Key)
	}
	// A project deleted in the app is not a project. Checked before it is preferred over what
	// the app already holds, or the board would be analyzed again for nothing.
	if remembered != "" {
		if _, err := c.GetProject(ctx, remembered); err != nil {
			var ae *APIError
			if project != "" || !errors.As(err, &ae) || ae.Status != 404 {
				return Opened{}, err
			}
			remembered = ""
		}
	}

	if !force && store != nil && remembered != "" {
		// These exact bytes went from here last time and their ingest is still on its way:
		// that run is the answer. Asking twice must not queue the same board twice.
		if d := store.load(p.Key); d.Project == remembered && d.LastSHA256 == p.SHA256 && d.LastRun != "" {
			if r, err := c.GetRun(ctx, d.LastRun); err == nil && Pending(r.Status) {
				return Opened{ProjectID: remembered, BoardID: d.LastBoard, RunID: r.ID}, nil
			}
		}
	}
	if !force {
		// A failed lookup costs an upload, not the run.
		if l, err := c.LookupBoard(ctx, p.SHA256); err == nil && l.Found && l.Parsed && l.Project != nil &&
			(remembered == "" || l.Project.ID == remembered) {
			if store != nil {
				_ = store.Save(p.Key, l.Project.ID)
			}
			o := Opened{ProjectID: l.Project.ID}
			if l.Board != nil {
				o.BoardID = l.Board.ID
			}
			return o, nil
		}
	}

	o := Opened{ProjectID: remembered, Uploaded: true}
	if o.ProjectID == "" {
		pr, err := c.CreateProject(ctx, p.Name, p.Kind)
		if err != nil {
			return Opened{}, err
		}
		o.ProjectID, o.NewProject = pr.ID, true
	}
	got, err := c.UploadBoard(ctx, o.ProjectID, p.Filename, p.Data, p.SHA256)
	if err != nil {
		return Opened{}, err
	}
	o.BoardID, o.RunID = got.Board.ID, got.Run.ID
	if store != nil {
		_ = store.saveUpload(p.Key, o.ProjectID, p.SHA256, o.BoardID, o.RunID)
	}
	return o, nil
}
