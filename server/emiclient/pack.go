package emiclient

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/embeddedci-com/emi-analyzer/server/emi"
)

// SettingsNames are the rules documents committed beside a board, in the order the worker
// prefers them (worker/emi_worker/stages/ingest.py). The first one found travels with the
// board, so the checks a team agreed on are the checks that run.
var SettingsNames = []string{"emi.rules.yaml", "emi.rules.yml", "emi.rules.json", ".emi.yaml"}

// gerberMarkers make a folder a Gerber set, and gerberSet is every file of one the worker
// reads. Both mirror worker/emi_worker/stages/ingest.py (GERBER_SUFFIXES, _GERBER_SET).
var (
	gerberMarkers = []string{".gbr", ".gbrjob", ".drl", ".gtl", ".gbl", ".gm1", ".d356"}
	gerberSet     = regexp.MustCompile(`(?i)\.(gbr|gbrjob|drl|xln|exc|txt|d356|ipc|net|gtl|gbl|gko|gm\d*|g\d+)$`)
)

// zipTime is the timestamp inside every archive. Uploads are content-addressed, so the same
// board has to give the same bytes: with a real timestamp every call would look like a new
// board and re-analyze a design that had not changed.
var zipTime = time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)

// Packed is a board ready to upload.
type Packed struct {
	// Data is the zip the app receives, and SHA256 its hash.
	Data   []byte
	SHA256 string
	// Filename is what the upload is called: "<stem>.zip".
	Filename string
	// Name is what a new project is called: the board's stem, or the folder's name.
	Name string
	Kind emi.SourceKind
	// Key identifies the board between calls: its absolute path.
	Key string
	// Files lists what went into the archive, for telling the user.
	Files []string
}

// Pack reads a board from disk the way the analyzer reads a project:
//
//   - a .kicad_pcb, with its .kicad_pro (netclasses and differential pairs live there) and a
//     committed rules document from the same folder;
//   - a .kicad_pro, meaning the .kicad_pcb beside it;
//   - a folder holding one .kicad_pcb, read as above, or a Gerber set (with its drill file and
//     IPC-D-356 netlist);
//   - a .zip, sent as it is: a zipped KiCad project or Gerber output.
//
// Nothing is written: the analyzer reports on a layout, it does not edit one. Unsaved edits
// in an open editor are not on disk and so not analyzed; save first.
func Pack(path string) (*Packed, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	st, err := os.Stat(abs)
	if err != nil {
		return nil, fmt.Errorf("no board at %s: %w", path, err)
	}
	if st.IsDir() {
		return packDir(abs)
	}
	switch ext := strings.ToLower(filepath.Ext(abs)); ext {
	case ".kicad_pcb":
		return packKiCad(abs)
	case ".kicad_pro":
		pcb := strings.TrimSuffix(abs, filepath.Ext(abs)) + ".kicad_pcb"
		if _, err := os.Stat(pcb); err != nil {
			return nil, fmt.Errorf("%s has no %s beside it", filepath.Base(abs), filepath.Base(pcb))
		}
		return packKiCad(pcb)
	case ".zip":
		return packZip(abs)
	default:
		return nil, fmt.Errorf("%s is not a board: give a .kicad_pcb, a .kicad_pro, a folder with a KiCad board or Gerber files, or a .zip of either", filepath.Base(abs))
	}
}

func packKiCad(pcb string) (*Packed, error) {
	dir := filepath.Dir(pcb)
	name := filepath.Base(pcb)
	stem := strings.TrimSuffix(name, filepath.Ext(name))
	files := map[string]string{name: pcb}
	if pro := filepath.Join(dir, stem+".kicad_pro"); isRegular(pro) {
		files[stem+".kicad_pro"] = pro
	}
	addSettings(dir, files)
	p, err := pack(files, stem, emi.SourceKiCad, pcb)
	if err == nil {
		p.Name = projectName(dir, stem)
	}
	return p, err
}

// projectName is what a new project is called: the board's name, with its folder in front
// when the two differ. Boards are often named after the product and kept in folders named
// after the variant ("benchpod-digital/vbench-pod.kicad_pcb"), and two projects both called
// "vbench-pod" cannot be told apart in a list.
func projectName(dir, stem string) string {
	folder := filepath.Base(dir)
	if folder == "" || folder == "." || folder == string(filepath.Separator) || strings.EqualFold(folder, stem) {
		return stem
	}
	return folder + "/" + stem
}

func packDir(dir string) (*Packed, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var boards, gerbers []string
	marked := false
	for _, e := range entries {
		if !e.Type().IsRegular() {
			continue
		}
		lower := strings.ToLower(e.Name())
		if strings.HasSuffix(lower, ".kicad_pcb") {
			boards = append(boards, e.Name())
		}
		for _, s := range gerberMarkers {
			if strings.HasSuffix(lower, s) {
				marked = true
			}
		}
		if gerberSet.MatchString(e.Name()) {
			gerbers = append(gerbers, e.Name())
		}
	}
	switch {
	case len(boards) == 1:
		return packKiCad(filepath.Join(dir, boards[0]))
	case len(boards) > 1:
		// A project's own board is the one named after its .kicad_pro.
		for _, b := range boards {
			if isRegular(filepath.Join(dir, strings.TrimSuffix(b, filepath.Ext(b))+".kicad_pro")) {
				return packKiCad(filepath.Join(dir, b))
			}
		}
		sort.Strings(boards)
		return nil, fmt.Errorf("%s holds %d boards (%s): give the one to analyze", dir, len(boards), strings.Join(boards, ", "))
	case marked:
		files := map[string]string{}
		for _, g := range gerbers {
			files[g] = filepath.Join(dir, g)
		}
		addSettings(dir, files)
		return pack(files, filepath.Base(dir), emi.SourceGerber, dir)
	default:
		return nil, fmt.Errorf("%s holds neither a .kicad_pcb nor Gerber files", dir)
	}
}

func packZip(path string) (*Packed, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	zr, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return nil, fmt.Errorf("%s is not a zip archive: %w", filepath.Base(path), err)
	}
	// Labelled by what is inside, as the worker decides it; the label is only the project's.
	kind := emi.SourceGerber
	var names []string
	for _, f := range zr.File {
		names = append(names, f.Name)
		if strings.HasSuffix(strings.ToLower(f.Name), ".kicad_pcb") {
			kind = emi.SourceKiCad
		}
	}
	stem := strings.TrimSuffix(filepath.Base(path), filepath.Ext(path))
	return finish(data, stem, kind, path, names)
}

// addSettings adds the first rules document the folder has.
func addSettings(dir string, files map[string]string) {
	for _, n := range SettingsNames {
		if p := filepath.Join(dir, n); isRegular(p) {
			files[n] = p
			return
		}
	}
}

// pack zips files (archive name → path on disk), sorted by name with a fixed timestamp, so
// that the same files always give the same bytes.
func pack(files map[string]string, stem string, kind emi.SourceKind, key string) (*Packed, error) {
	names := make([]string, 0, len(files))
	for n := range files {
		names = append(names, n)
	}
	sort.Strings(names)
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	total := 0
	for _, n := range names {
		data, err := os.ReadFile(files[n])
		if err != nil {
			return nil, err
		}
		if total += len(data); total > MaxUploadBytes {
			return nil, fmt.Errorf("the board's files come to more than %d MB, larger than the app accepts", MaxUploadBytes>>20)
		}
		h := &zip.FileHeader{Name: n, Method: zip.Deflate, Modified: zipTime}
		h.SetMode(0o644)
		w, err := zw.CreateHeader(h)
		if err != nil {
			return nil, err
		}
		if _, err := w.Write(data); err != nil {
			return nil, err
		}
	}
	if err := zw.Close(); err != nil {
		return nil, err
	}
	return finish(buf.Bytes(), stem, kind, key, names)
}

func finish(data []byte, stem string, kind emi.SourceKind, key string, names []string) (*Packed, error) {
	if len(data) > MaxUploadBytes {
		return nil, fmt.Errorf("the board is %.0f MB, larger than the app accepts", float64(len(data))/1e6)
	}
	sum := sha256.Sum256(data)
	if stem == "" {
		stem = "board"
	}
	return &Packed{
		Data: data, SHA256: hex.EncodeToString(sum[:]), Filename: stem + ".zip",
		Name: stem, Kind: kind, Key: key, Files: names,
	}, nil
}

func isRegular(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.Mode().IsRegular()
}
