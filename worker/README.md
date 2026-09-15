# EMI worker

The part of the EMI Analyzer that does the work: parses the board, runs the geometric checks,
simulates ESD discharges and cable budgets, meshes a region and calls openEMS, and uploads the
results.

It **dials out**: it opens a WebSocket to the app, says what it can do, and is handed runs over
that connection. It **keeps nothing**: its identity is a key in its environment, and its disk is
one scratch directory per run, deleted when the run ends.

The app starts this worker in Docker by itself. To run one yourself — on another machine, or from
source — see [docs/running-a-worker.md](../docs/running-a-worker.md).

## Develop on it

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

```bash
.venv/bin/pip install --no-deps "gerbonara>=1.5"
```

```bash
.venv/bin/python -m pytest
```

Tests that need `ngspice`, `nec2c` or openEMS skip where those are missing; run the whole suite in
the image to cover them (see `.github/workflows/test.yml`). A few tests also run against real
boards when `EMI_TEST_BOARDS` names a directory of `<board>/<board>.kicad_pcb` files.

A worker without openEMS says so at registration and is only offered the run kinds it can do, so
it is a useful thing to run rather than a broken one.

## Layout

```
emi_worker/
  runner.py      the run loop: dial out, claim, execute, report
  client.py      HTTP + WebSocket to the app
  config.py      environment, and capability detection (cores, RAM, container limits, tools)
  kicad/         .kicad_pcb parsing -- s-expressions to a normalised board
  gerber/        RS-274X, Excellon and IPC-D-356, and net reconstruction from copper
  rules/         the geometric EMI/EMC checks
  transient/     ESD discharge simulation with ngspice
  cables/        cable budget and antenna model (nec2c)
  drivers/       driver spectra, for re-weighting solve results
  components/    component models and footprint matching
  compliance/    limits tables and the compliance estimate
  openems/       mesh generation, CSX XML, running the solver, reading its output
  stages/        one module per run kind: ingest, solve, transient, cable, compliance
```
