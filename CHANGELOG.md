# Changelog

All notable changes to EMI Analyzer are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The KiCad plugin is versioned and released separately; see
[kicad-plugin/README.md](kicad-plugin/README.md).

## [Unreleased]

### Fixed

- USB pairs named `USB_DP`/`USB_DM` are now found as pairs, so their skew is checked. Only
  `_P/_N`, `P/N` and `+/-` were recognized before.

### Added

- Named capacitors: the 59 MLCCs on JLCPCB's Basic Parts list that have a manufacturer model
  (58 Samsung, 1 Murata), each fitted to that model and cited. A capacitor whose footprint
  carries an MPN or LCSC field (`MPN`, `LCSC`, `JLCPCB Part #` and similar) now gets that
  part, even on a custom footprint; others keep the generic model. The decoupling table, the
  modelled-parts list and the report say "datasheet (Samsung CL05B104KO5NNNC)" or
  "generic 0402 X7R". FH and Yageo parts are not included: their makers publish no model
  ([docs/component-library.md](docs/component-library.md)).

- Small-part solves are on by default, on the Part solve tab: one net (or a pair) cut out over
  its planes and solved in openEMS in minutes, with its current map, loudest spots, port
  impedance and S-parameters. Lines and vias match their closed forms and three real boards
  converge over the cut and the mesh
  ([docs/verification/small-part-solve.md](docs/verification/small-part-solve.md)). No far
  field, no compliance estimate, no coupling into neighboring nets and no component models.

- A Decoupling tab: per supply rail and IC, a lumped estimate of the supply impedance from the
  layout (each capacitor as a series R-L-C with its mounting loop, plus the plane pair), against
  a target from the rail voltage, ripple and current step. It shades where the impedance is
  above the target, marks anti-resonances and clock harmonics that are not filtered, lists the
  capacitors, and ranks computed fixes (move a part, add a via, add or remove a part) by how
  much of the worst gap they close. The target is editable in the tab and in the decoupling
  check's settings (`ripple_pct`, `step_current_a`, `board_max_hz`, `clock_hz`, `switching_hz`).
- An experimental conducted-emissions scan (`EMI_EXPERIMENTAL=conducted`), on a Conducted tab:
  the board's power input through two CISPR 16-1-2 LISNs, the input filter as laid out and each
  buck regulator as a trapezoidal current source, simulated with ngspice against the FCC
  15.107 limits. Regulator settings not entered are assumed and marked so. It shows the worst
  margin, which capacitor carries the ripple, what adding a capacitor or an LC filter would
  change, and what each input capacitor is worth. Differential mode only; see
  [docs/verification/conducted-emissions.md](docs/verification/conducted-emissions.md).
- The conducted scan finds more regulators: boosts (modelled by their inductor ripple, not as
  pulses), inverting stages, controllers with external FETs (drawing through the high-side
  drain), buck modules by part number, and each output of a PMIC as its own source with its own
  frequency and phase. The rail also follows a charger or LDO a regulator draws from, and a
  supply connector away from the board edge can be the power input. Each regulator says how it
  was found and with what confidence, and can be confirmed, given another type or removed in
  the tab.

- Report export from the board menu: a self-contained HTML report (prints to PDF) or JSON, with
  the findings on a board image, notes, decoupling, cable budgets, ESD results, the conducted
  scan (when that feature is on, labeled experimental) and changes since an earlier version.
  Decoupling lists each IC; one with gaps gets its |Z| chart, target, top fixes and capacitor
  table. Missing runs can be started from the dialog. Runs now record the worker version.
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
- Small-part solves list every spot within 3 dB of each map's
  loudest point away from the ports, with the nearest net and part, and mark them on the board.
  When there are several, the result says to treat them together. A part that runs on the
  coarse mesh with vias says its via inductance can read up to about 10% high.

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

- Modelled capacitors (full-wave, experimental): upstream openEMS keeps its series lumped
  element's state in float and lost the capacitor to rounding at a board's timestep, so a
  100 pF 0402 resonated 17 % low. The worker image's openEMS is now patched for it
  (`worker/openems-image/lumped-rlc-double.patch`; NOTICE says so), and a worker whose openEMS
  fails a check of that element places no capacitor. A 100 pF and a 1 nF 0402 now match their
  series R-L-C within 0.01 % on resonance, with a -70 dB record; the solve's own -40 dB is still
  too short for them, so the option stays experimental
  ([docs/verification/solver-and-components.md](docs/verification/solver-and-components.md) §4).
- Small-part port impedance and S-parameters no longer depend on exactly when the run stopped.
  The ports ring faintly when it stops, and cutting that off moved a via's 100 MHz inductance
  by tens of percent, so the same solve read +1.8 % on one machine and +10.5 % on another. The
  last nanosecond of the record is now tapered before it is transformed
  ([docs/verification/small-part-solve.md](docs/verification/small-part-solve.md) check 4).
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
- Small-part solves: a hotspot's level is averaged over a 0.25 mm disc, about a probe tip; a run
  stops only once its energy has stayed under the criterion for a nanosecond and is not
  climbing; a net with one pad gets a 50 ohm load at its far end; less of the pulse falls below
  the band; and the cost estimate counts the mesh, diagonal traces included, instead of pricing
  the area.

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
