# Documentation

Start with the [README](../README.md), which covers installing and using the app.

## Using the app

| | |
|---|---|
| [known-issues.md](known-issues.md) | What is verified, what is experimental (and switched off), and what is not built yet. Read before relying on a number. |
| [rules-file.md](rules-file.md) | `emi.rules.yaml`: tune the checks for a board by putting a rules file next to it. |
| [running-a-worker.md](running-a-worker.md) | Run the worker yourself — on a bigger machine, or from source. |
| [emi-driver-format.md](emi-driver-format.md) | The driver file format: what a net carries. Used by full-wave simulation, which is experimental. |
| [component-library.md](component-library.md) | The capacitor library: which named parts it has (JLCPCB Basic MLCCs), where their numbers came from, how parts are matched by part number, and how to add one. |

## How it works

| | |
|---|---|
| [implementation.md](implementation.md) | The model as built: runs, meshing, drivers, components, cables, compliance. |
| [limits-and-decisions.md](limits-and-decisions.md) | The limits library (FCC Part 15; CISPR 32 is not included yet), where each number came from, and the decisions behind the model. |
| [esd-transient-simulation.md](esd-transient-simulation.md) | Design and as-built notes for the ESD discharge simulation. |
| [length-matching-and-impedance.md](length-matching-and-impedance.md) | Design note for length matching, impedance and rule settings. |
| [csx-xml-notes.md](csx-xml-notes.md) | openEMS CSX XML schema notes, verified against the solver. |

## Verification

What each feature was checked against, the numbers, and what is left.

| | |
|---|---|
| [verification/decoupling.md](verification/decoupling.md) | The decoupling view: closed forms and a published mounting-inductance table. |
| [verification/cables-and-drivers.md](verification/cables-and-drivers.md) | Cable budgets against openEMS; cable emissions (experimental); drivers. |
| [verification/conducted-emissions.md](verification/conducted-emissions.md) | Conducted emissions (experimental): LISN, ripple and filter closed forms. |
| [verification/small-part-solve.md](verification/small-part-solve.md) | Small-part solves: lines, vias, convergence on real parts, the sample board. |
| [verification/solver-and-components.md](verification/solver-and-components.md) | The openEMS model: microstrip, lumped elements, capacitor models, cost estimate. |
| [verification/far-field.md](verification/far-field.md) | The far field (experimental): dipoles over ground against nec2c. |

Building from source, running the tests and cutting a release are in
[CONTRIBUTING.md](../CONTRIBUTING.md).
