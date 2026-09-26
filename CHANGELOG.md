# Changelog

All notable changes to EMI Analyzer are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The KiCad plugin is versioned and released separately; see
[kicad-plugin/README.md](kicad-plugin/README.md).

## [Unreleased]

### Added

- Report export from the board menu: a self-contained HTML report (prints to PDF) or JSON, with
  the findings on a board image, notes, cable budgets, ESD results and changes since an earlier
  version. Missing runs can be started from the dialog. Runs now record the worker version.
- A KiCad plugin: a front end that hands the board open in the PCB Editor to the desktop app
  and shows the app's pages beside pcbnew.
- The desktop app can keep running in the tray with its window closed, so the plugin can reach
  it.
- Deleting and renaming boards; cable settings are kept per board.
- The worker reads `emi.rules.yaml` (PyYAML is now in the image; YAML also reads JSON). A file
  whose top level is not a mapping is an error rather than a silent fallback to the defaults.
- A licence notice in the worker image (`/usr/share/doc/emi-worker/NOTICE`) with the Debian
  and Python package lists it refers to.
- SHA256SUMS on every release, and a security policy, code of conduct and issue templates.
- Rule settings in the app: a Checks view under Findings switches checks on and off and sets
  severities and thresholds, shows where each value came from, re-analyses with the change,
  and exports an equivalent `emi.rules.yaml`.
- Notes about the analysis above the findings: which rules file was applied, and what the
  worker skipped or assumed (unfilled zones, an unreadable rules file, a missing stackup).
- "Try the sample board" on the home page, and a one-time hint on what to do after the
  findings.

### Changed

- Cable fields are measured 3 m from the smallest circle around board and cable, at 0.8 m
  table height, as CISPR 16-2-3 and ANSI C63.4 define the distance. The first radiation peak
  of a 1 m USB cable moves from 39 to 45 MHz. Cables longer than 10 m are refused.
- Builds from source start their own worker tag (`:dev`), and a release always builds its own
  worker image.
- The worker image pins its base image, openEMS source and Python packages. A pre-release tag
  no longer moves `:latest`.
- The worker image runs openEMS compiled from a pinned upstream commit instead of Debian's
  0.0.35, which skips a lumped inductor, so capacitor models in a full-wave solve are no longer
  left out. openEMS is built once, in its own image (`ghcr.io/embeddedci-com/emi-openems`), and
  the worker image starts from it, so worker builds compile nothing. The image grows from about
  270 MB to about 1.4 GB. `--build-arg OPENEMS_SOURCE=apt` still builds on 0.0.35.
- The README, the limits page and the docs no longer claim a CISPR 32 table (only FCC Part 15
  limits exist) or a second solver for the antenna model.

### Fixed

- Ground and power nets are recognized by whole words: `+3.3V` and `+24V` were signals, and
  `+10V` and `VBUS_20V` were grounds, which produced false return-via findings.
- The divergence check refused every long solve; mesh grading is now enforced.
- Release builds come from the tag's commit, not from the branch a manual run started on.
- The worker image health check now fails when the worker or a promised tool is missing.
- Solves: a via is its drilled barrel and a round ring, not a box as wide as the ring, whose
  corner shorted a diagonal trace beside it; and a diagonal trace has grid lines all along it,
  where it used to be cut in two between its ends.
- Small-part solves read each map 0.1 mm above the copper on every preset, hold the absorbing
  layer to the band's cell size, start on the coarse mesh when normal is over budget, and say
  when other copper sits within one cell of the net.

### Security

- Board text can no longer reach the ngspice deck: a net name containing `.control` could run
  shell commands on the worker. Decks with control blocks or includes are refused.
- The local app accepts changes only from its own exact origin and only as JSON, and cannot be
  framed by other sites. Before, a page on another localhost port or a private IP could create
  projects and runs.
- The KiCad plugin trusts `endpoint.json` only for `http://127.0.0.1`, parsed rather than
  prefix-matched.
- gerbv, installed and never used, is gone from the worker image.

## [0.1.0] - 2026-09-15

First public release: the desktop app for macOS, Windows and Linux, the standalone `emi-local`
binary and the worker image.

[Unreleased]: https://github.com/embeddedci-com/emi-analyzer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/embeddedci-com/emi-analyzer/releases/tag/v0.1.0
