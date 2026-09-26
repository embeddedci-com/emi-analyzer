# `emi.rules.yaml`

Rules that should be reviewed alongside the design belong with the design. Put this file
next to your `.kicad_pcb` and upload the project as a zip; the analyzer reads it and every
finding says which setting it used.

JSON works too, because YAML reads JSON. A file that cannot be read is reported on the board,
and the checks then run with their built-in defaults.

```yaml
version: 1

board:
  max_frequency_hz: 1.6e9
  # Force a permittivity when the board file has none, or has a 1 MHz datasheet figure that
  # is optimistic at your real rate. The frequency it applies at is recorded with it.
  epsilon_r: 4.3
  epsilon_r_at_hz: 1.0e9

rules:
  radiator: false                      # off entirely
  plane-gap:
    severity: info                     # still runs, reported as advisory
  ddr-skew:
    params:
      intra_pair_ps: 2
      byte_lane_ps: 10
      address_command_ps: 25
      lane_to_lane_ps: 0               # 0 = do not compare; write levelling absorbs it
  impedance:
    params:
      tolerance_pct: 10

# Overrides for a set of nets. A board has several budgets on it at once. Only settings a
# check reads per net can go here (see below); the last matching group wins.
groups:
  - match: "DDR_DQ*"
    params: { byte_lane_ps: 6 }
  - netclass: "USB"
    params: { differential_ohm: 90 }
  - match: "/PCIE_*"
    params: { differential_ohm: 85, intra_pair_ps: 1 }

# Findings already decided about. A reason is required: one without it is a mystery to
# whoever finds it next year, and the analyzer warns. So does a rule id that names no check.
suppress:
  - rule: edge-proximity
    net: GND
    reason: "guard ring, intentional"
```

## Where settings come from

Most general first; the last one to set a value wins, and every finding can say which:

1. built-in defaults
2. project settings (reserved; nothing sets them yet)
3. this file
4. settings edited in the app, under Findings, Checks

The app shows every value with its source, and saves its edits with the analysis run they
were applied by (the ingest run's `params.settings`, in this file's format). Its **Export
emi.rules.yaml** button writes all of it, including groups and suppressions from this file and
from the app, as one file that gives the same analysis without the app.

Net groups and suppressions can be added in the app too, under Checks, or with **Suppress…** on
a finding. The app lists this file's beside its own, read-only, and shows which of the board's
nets each pattern matches. Groups and suppressions add up across layers rather than replace
each other: the app's come after this file's, so a group set in the app wins where both match.
Suppressed findings are listed, with their reasons, at the end of the Findings list.

## Net patterns

`match` and `net` are glob patterns, case-sensitive: `*` is any run of characters, `?` one,
`[0-3]` one of a set and `[!0-3]` one not in it. `netclass` matches a KiCad netclass exactly
instead, and needs the `.kicad_pro`.

A group can set only these, because they are the only settings a check reads net by net. Any
other setting in a group is ignored with a warning; set it under `rules` instead.

| Check | Setting | Applied to |
| --- | --- | --- |
| `ddr-skew` | `intra_pair_ps`, `byte_lane_ps`, `address_command_ps` | a matched group whose reference net matches |
| `impedance` | `single_ended_ohm`, `differential_ohm` | each net that matches |

## Notes

- **Tolerances are in picoseconds.** An inner-layer millimetre and an outer-layer one differ
  by about 25% in delay, so millimetres would compare the wrong thing. See
  [length-matching-and-impedance.md](length-matching-and-impedance.md).
- **Impedance targets default to 0, meaning "no target"** — only discontinuities along a net
  are reported. A board-wide target is almost never right; scope it with `groups`.
- **`version` is checked.** A document from a newer analyzer is refused rather than
  reinterpreted, because a silently changed threshold is worse than a rejected file.
- **Netclasses need the `.kicad_pro`.** They live in the project file, not the board, so
  upload a zipped project if you want groups identified by netclass rather than by name.
- **A connector is I/O when it sits near the edge.** `esd-protection` and `input-filter` only
  look at connectors within `edge_mm` of the outline, because a debug header in the middle of
  the board is not where a cable plugs in. Set `edge_mm: 0` to check every connector, or
  suppress an edge header you never expose by its nets (`net: "/SWD*"`).
- **EMC checks recognise parts by reference, value and footprint.** A clamp is a part number
  such as USBLC6, PESD, SRV05 or SMAJ on any reference, or a diode from the line to ground. A
  protection part the analyzer does not recognise is reported as missing — suppress the net
  with a reason rather than switching the rule off.

## Rule ids

Settings key off a rule's **id**, not its title — titles get reworded, ids do not. The current
list, with every parameter and its default, is
[`webapp/src/lib/ruleCatalogue.json`](../webapp/src/lib/ruleCatalogue.json). It is generated
from the worker's own catalogue (`python scripts/export_rule_catalogue.py`) and a test fails when
it is stale, so it is always the set of checks that actually runs. The tool's front page shows the
same checks by title.

Every rule can be switched off (`rule-id: false`) or re-severitied (`severity: info`), including
the ones that predate settings.
