# Documentation

Start with the [README](../README.md), which covers installing and using the app.

## Using the app

| | |
|---|---|
| [known-issues.md](known-issues.md) | What is verified, what is experimental (and switched off), and what is not built yet. Read before relying on a number. |
| [rules-file.md](rules-file.md) | `emi.rules.yaml`: tune the checks for a board by putting a rules file next to it. |
| [running-a-worker.md](running-a-worker.md) | Run the worker yourself — on a bigger machine, or from source. |
| [emi-driver-format.md](emi-driver-format.md) | The driver file format: what a net carries. Used by full-wave simulation, which is experimental. |

## How it works

| | |
|---|---|
| [implementation.md](implementation.md) | The model as built: runs, meshing, drivers, components, cables, compliance. |
| [limits-and-decisions.md](limits-and-decisions.md) | The limits library (FCC Part 15, CISPR 32), where each number came from, and the decisions behind the model. |
| [esd-transient-simulation.md](esd-transient-simulation.md) | Design and as-built notes for the ESD discharge simulation. |
| [length-matching-and-impedance.md](length-matching-and-impedance.md) | Design note for length matching, impedance and rule settings. |
| [csx-xml-notes.md](csx-xml-notes.md) | openEMS CSX XML schema notes, verified against the solver. |

Building from source, running the tests and cutting a release are in
[CONTRIBUTING.md](../CONTRIBUTING.md).
