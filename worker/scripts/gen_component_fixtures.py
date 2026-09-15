#!/usr/bin/env python3
"""Regenerate the shared component fixtures.

Asserted against by worker/tests/test_component_document.py and
webapp/src/lib/componentDocument.test.ts. The browser previews a component's impedance while
someone types it; the worker then places that component in a solve. A disagreement is a user
shown a self-resonance the solve does not model.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from emi_worker.components.document import ComponentError, parse  # noqa: E402

OUT = (Path(__file__).resolve().parents[2] / "server" / "emi" / "testdata"
       / "component_fixtures.json")


def base(**over):
    d = {
        "format": "emi-component", "version": 1,
        "id": "mlcc-100n-0402-x7r", "kind": "capacitor", "name": "MLCC 100 nF 0402 X7R",
        "provenance": "vendor",
        "match": {"value": "100n", "package": "0402", "mpn": None},
        "model": {"type": "series_rlc", "c_f": 1.0e-7, "esl_h": 6.0e-10, "esr_ohm": 0.02},
        "esl_includes_mount": False,
        "valid_hz": [1.0e5, 3.0e9],
        "sources": [{"doc": "part datasheet", "rev": "C", "what": "ESL and ESR"}],
    }
    d.update(over)
    return d


VALID = [
    ("vendor-series-rlc", base()),
    ("generic-no-numbers", base(
        id="generic-placeholder", provenance="generic", sources=[],
        model={"type": "series_rlc", "c_f": 1.0e-7, "esl_h": None, "esr_ohm": None})),
    ("generic-family", base(
        id="generic-mlcc-family", provenance="generic", name="MLCC (generic, by package)",
        match={"value": None, "package": None, "mpn": None},
        model={"type": "mlcc_family",
               "esl_h_by_package": {"0402": 4.5e-10, "0603": 6.5e-10},
               "esr_ohm_by_c": [[1.0e-9, 0.25], [1.0e-7, 0.055], [1.0e-5, 0.013]]},
        sources=[{"doc": "typical for the package and value class", "what": "ESL and ESR"}])),
]

INVALID = [
    ("wrong-format", base(format="emi-driver"), "format"),
    ("future-version", base(version=2), "version 2"),
    ("no-name", base(name="  "), "name"),
    ("unknown-kind", base(kind="inductor"), "kind"),
    ("unknown-provenance", base(provenance="vibes"), "provenance"),
    ("uncited-esl", base(sources=[]), "says nothing about where it came from"),
    ("vendor-without-sources", base(
        provenance="vendor", sources=[],
        model={"type": "series_rlc", "c_f": 1e-7, "esl_h": None, "esr_ohm": None}),
     "cites none"),
    ("future-model-type", base(model={"type": "touchstone", "file": "x.s2p"}),
     "does not resolve yet"),
    ("unknown-model-type", base(model={"type": "vibes"}), "unknown model type"),
    ("backwards-valid-hz", base(valid_hz=[3e9, 1e5]), "increasing"),
]

#: Frequencies the impedance is sampled at, spanning both sides of a 100 nF 0402's SRF.
SAMPLE_HZ = [1.0e5, 1.0e6, 1.0e7, 2.0e7, 6.5e7, 1.0e8, 1.0e9]


def main() -> int:
    valid = []
    for name, d in VALID:
        c = parse(d)
        entry = {
            "name": name, "document": d,
            "expected": {
                "id": c.id, "provenance": c.provenance, "generic": c.is_generic,
                "model_type": c.model_type,
                "describe_provenance": c.describe_provenance(),
            },
        }
        if c.model_type == "series_rlc":
            rlc = c.series_rlc()
            entry["expected"]["self_resonance_hz"] = rlc.self_resonance_hz()
            entry["expected"]["complete"] = rlc.complete
            if rlc.complete:
                entry["expected"]["impedance"] = [
                    {"frequency_hz": f,
                     "real": rlc.impedance_at(f).real, "imag": rlc.impedance_at(f).imag}
                    for f in SAMPLE_HZ
                ]
        else:
            fam = c.mlcc_family()
            entry["expected"]["family"] = {
                "esl_by_package": fam.esl_by_package,
                "esr_at": [{"c_f": c_f, "esr_ohm": fam.esr_for(c_f)}
                           for c_f in (1e-10, 1e-9, 1e-8, 1e-7, 1e-6, 1e-5, 1e-3)],
            }
        valid.append(entry)

    invalid = []
    for name, d, mention in INVALID:
        try:
            parse(d)
        except ComponentError as exc:
            message = str(exc)
        else:
            raise SystemExit(f"fixture {name!r} was supposed to be rejected and was not")
        if mention not in message:
            raise SystemExit(f"fixture {name!r}: {message!r} does not mention {mention!r}")
        invalid.append({"name": name, "document": d, "must_mention": mention,
                        "python_message": message})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "_comment": (
            "Shared component fixtures. The Python and TypeScript implementations must agree: "
            "the browser previews a component's impedance while it is typed, and the worker "
            "places that same component in a solve. Regenerate with `make component-fixtures`, "
            "never by hand."
        ),
        "sample_hz": SAMPLE_HZ,
        "valid": valid,
        "invalid": invalid,
    }, indent=2) + "\n")
    print(f"wrote {OUT} ({len(valid)} valid, {len(invalid)} invalid)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
