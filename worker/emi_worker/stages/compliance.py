"""Compliance: the margin, what drives it, and how much weight it can bear.

The model is in docs/implementation.md §7.

Seconds of arithmetic on things other runs already produced -- a solve's far field, its cable
transfer functions and the antenna terms beside them -- and the driver the user attached. No
solver runs here, which is why this is a run kind of its own rather than a stage bolted onto a
solve: a user swapping a driver wants the answer back immediately.

**Everything that decides the answer is read here, not sent.** The server hands this run the
original upload, presigned URLs for the chosen solve's artifacts (same project, same board, a
finished solve -- it checks), and the attached driver's document. The paths are assembled from
those (``compliance/assemble.py``) and so is every input to the gate. The request carries only
what the user alone knows: which solve and driver, which connectors carry no cable, which nets
are sources, the enclosure and the power. The first version took the paths and the gate's
inputs from the request, which the browser sent empty and an API client could fill with
anything.

**The gate comes first.** An incomplete estimate carries no margin and no confidence at all
(docs/implementation.md §7.5), so an incomplete document simply has no ``margin_db`` key. A
warning is something a reader can skip, and a missing field is not.
"""

from __future__ import annotations

import json
import logging

from ..compliance import completeness, predict, recommend
from ..compliance.assemble import AttachedDriver, SolveArtifacts, assemble
from ..compliance.limits import detector_at, scan_to_hz, standard
from ..drivers.document import parse as parse_driver
from ..drivers.spectrum import DriverError
from . import StageContext, StageError, StageResult

log = logging.getLogger(__name__)

FORMAT = "emi-compliance"
#: Version 2: paths assembled in the worker, ``confidence`` renamed ``confidence_uncalibrated``,
#: and the ``inputs`` block that says what the gate was given.
FORMAT_VERSION = 2

#: Where every field this estimate reads was computed: the solve's far field and the antenna
#: terms are both evaluated at 3 m (``stages/solve.py``, ``cables/emission.py``).
COMPUTED_DISTANCE_M = 3.0

#: Where the uncertainty budget's terms come from, so the document can show its own working.
PLACEHOLDER_NOTE = (
    "These are engineering placeholders, chosen conservatively. They are replaced by residuals "
    "as the verification tests and recorded lab results produce them, not tuned."
)

#: The solve artifacts this run reads. The server presigns exactly these.
SOLVE_ARTIFACTS = ("manifest.json", "ports.json", "farfield.json", "cable_ports.json",
                   "cable_antenna.json")


def _refuse_unsupported(std) -> None:
    """Standards whose measurement this estimate cannot reproduce are refused, not scaled.

    The fields are computed at 3 m, for a 1-4 m antenna height scan over a ground plane. A
    10 m standard sweeps a different range of elevation angles, so 1/r scaling of the 3 m
    maximum is not the 10 m maximum; and a conducted scan is a different quantity altogether.
    Both used to be accepted and compared against 3 m radiated numbers.
    """
    if std.scan != "radiated":
        raise StageError(
            f"{std.name} is a conducted scan. The compliance estimate computes radiated fields "
            f"only, so it cannot give a margin against it."
        )
    if std.distance_m is None or abs(float(std.distance_m) - COMPUTED_DISTANCE_M) > 1e-9:
        raise StageError(
            f"{std.name} is measured at {std.distance_m:g} m. This estimate computes fields at "
            f"{COMPUTED_DISTANCE_M:g} m, and scaling across a different antenna height scan is "
            f"not modelled. Use a {COMPUTED_DISTANCE_M:g} m standard."
        )


def _fetch_json(ctx: StageContext, url: str | None) -> dict | None:
    if not url:
        return None
    try:
        return json.loads(ctx.client.download(url))
    except (ValueError, OSError) as exc:
        raise StageError(f"a solve artifact could not be read: {exc}") from exc


def _board_facts(ctx: StageContext, info: dict) -> dict:
    """Connectors, where they are, and whether power enters through one -- from the upload.

    Read from the board rather than taken from the request, so leaving a connector out of the
    request cannot remove its gap.
    """
    from ..cables import suggest_all
    from ..rules import settings as settings_mod
    from ..rules.emc import _power_entry
    from .ingest import load_board, load_sidecars

    url = info.get("input_url")
    if not url:
        raise StageError("this run has no board to read its connectors from")
    data = ctx.client.download(url)
    try:
        board, _filename, _ = load_board(data)
    except StageError:
        raise
    except ValueError as exc:
        raise StageError(f"the board file could not be read: {exc}") from exc
    cfg = settings_mod.load(("file", load_sidecars(data).settings))

    refs = [s.ref for s in suggest_all(board)]
    pads = {ref: [p for p in board.pads if p.ref == ref] for ref in refs}
    return {
        "connectors": refs,
        "settings_cables": dict(cfg.cables),
        "nets": {ref: sorted({p.net for p in ps if p.net}) for ref, ps in pads.items()},
        "centre": {
            ref: (sum(p.x for p in ps) / len(ps), sum(p.y for p in ps) / len(ps))
            for ref, ps in pads.items() if ps
        },
        "power_entry_found": any(_power_entry(p.net) for ps in pads.values() for p in ps
                                 if p.net),
    }


def _solve(ctx: StageContext, info: dict) -> tuple[SolveArtifacts | None, str | None]:
    solve = info.get("solve") or {}
    urls = solve.get("artifacts") or {}
    if not urls.get("manifest.json"):
        return None, solve.get("error")
    return SolveArtifacts(
        manifest=_fetch_json(ctx, urls.get("manifest.json")) or {},
        ports=_fetch_json(ctx, urls.get("ports.json")),
        farfield=_fetch_json(ctx, urls.get("farfield.json")),
        cable_ports=_fetch_json(ctx, urls.get("cable_ports.json")),
        cable_antenna=_fetch_json(ctx, urls.get("cable_antenna.json")),
    ), None


def _driver(info: dict) -> tuple[AttachedDriver | None, list[tuple[str, str]]]:
    raw = info.get("drivers") or []
    if not raw:
        return None, []
    if len(raw) > 1:
        return None, [(
            "drivers",
            "More than one driver was attached. A solve has one source, so attach the one "
            "that drives it.",
        )]
    entry = raw[0]
    try:
        return AttachedDriver(id=str(entry.get("id") or "driver"),
                              driver=parse_driver(entry.get("document"))), []
    except DriverError as exc:
        raise StageError(f"the attached driver could not be read: {exc}") from exc


def run_compliance(ctx: StageContext) -> StageResult:
    p = ctx.params
    standard_id = str(p.get("standard_id") or "fcc-15b-radiated-3m")
    try:
        std = standard(standard_id)
    except Exception as exc:
        raise StageError(f"unknown standard {standard_id!r}: {exc}") from exc
    _refuse_unsupported(std)

    ctx.progress("compliance", 5, "reading the board and the solve")
    info = ctx.client.run_input(ctx.token) or {}
    board = _board_facts(ctx, info)
    art, solve_error = _solve(ctx, info)
    attached, driver_problems = _driver(info)

    ctx.progress("compliance", 30, "assembling the radiating paths")
    problems = list(driver_problems)
    if solve_error:
        problems.append(("solve", solve_error))
    try:
        asm = assemble(art, attached, standard_id) if art is not None else None
    except DriverError as exc:
        raise StageError(str(exc)) from exc
    if asm is not None:
        problems.extend(asm.problems)
        for d in sorted(asm.distances_m):
            if abs(d - COMPUTED_DISTANCE_M) > 1e-9:
                problems.append(("distance", f"The solve computed a field at {d:g} m, not "
                                             f"{COMPUTED_DISTANCE_M:g} m. Run it again."))

    # The scan's top edge (§15.33(b)) from the highest frequency in the product: the clock
    # driver's fundamental, or a higher one the user states. A stated one can only raise it.
    declared_hz = float(p.get("highest_frequency_hz") or 0.0)
    highest = max([h for h in ((asm.highest_hz if asm else None), declared_hz) if h] or [0.0])
    std_lo, std_hi = std.range_hz()
    required = (std_lo, min(scan_to_hz(highest), std_hi)) if highest > 0 else None

    # The user's declarations: the run's own, over the board's committed settings file.
    assignments: dict = dict(board["settings_cables"])
    for ref, spec in (p.get("cable_assignments") or {}).items():
        assignments[str(ref)] = spec if isinstance(spec, dict) else {"type": spec}

    gate = completeness.check(
        drivers=[{"net": attached.driver.net}] if attached else [],
        source_nets=list(p.get("source_nets") or []),
        connectors=board["connectors"],
        cable_assignments=assignments,
        modelled_cables=asm.modelled_cables if asm else {},
        excited_ports=asm.excited_ports if asm else [],
        far_field_ports=asm.far_field_ports if asm else [],
        covered=asm.covered if asm else {},
        required_band=required,
        undriven_harmonics=asm.undriven if asm else {},
        enclosure=str(p.get("enclosure") or "none"),
        power=str(p.get("power") or ""),
        power_entry_found=board["power_entry_found"],
        problems=problems,
        has_solve=asm is not None,
    )

    paths = asm.paths if asm else []
    document: dict = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "standard_id": standard_id,
        "standard": std.name,
        "distance_m": std.distance_m,
        "scan": std.scan,
        "experimental": True,
        "complete": gate.complete,
        "gaps": [{"key": g.key, "message": g.message, "fixed_on": g.fixed_on}
                 for g in gate.gaps],
        "paths": [{"kind": q.kind, "label": q.label, "driver_id": q.driver_id,
                   "sigma_terms": q.sigma_terms, "points": len(q.points),
                   "line_spectrum": q.line} for q in paths],
        "inputs": {
            # What the gate was given, so a reader can check it rather than trust it.
            "solve_run_id": p.get("solve_run_id"),
            "driver": None if attached is None else {
                "id": attached.id, "name": attached.driver.name,
                "kind": attached.driver.kind, "net": attached.driver.net},
            "connectors": board["connectors"],
            "modelled_cables": asm.modelled_cables if asm else {},
            "excited_ports": asm.excited_ports if asm else [],
            "far_field_ports": asm.far_field_ports if asm else [],
            "covered_hz": {k: list(v) for k, v in (asm.covered if asm else {}).items()},
            "required_hz": list(required) if required else None,
            "declared": {
                "cables": {r: s for r, s in assignments.items()},
                "source_nets": list(p.get("source_nets") or []),
                "enclosure": str(p.get("enclosure") or "none"),
                "power": str(p.get("power") or ""),
                "highest_frequency_hz": declared_hz or None,
            },
        },
        "notes": _notes(art),
        "uncertainty_note": PLACEHOLDER_NOTE,
    }

    # The spectrum is always drawn, complete or not: docs/implementation.md §7.5 says a partial one
    # is shown greyed.
    if paths:
        ctx.progress("compliance", 50, "combining paths")
        out = predict.outlook(paths, None, standard_id, shared_terms=asm.shared_sigma)
        document["spectrum"] = [_spectrum_point(pt, standard_id) for pt in out.points]
        if gate.complete and out.worst is not None:
            ctx.progress("compliance", 70, "ranking contributions")
            document.update(_scored(out, p, asm, board))
    else:
        document["spectrum"] = []
        document["no_paths"] = (
            "Nothing radiating could be modelled: this needs a finished solve with the far "
            "field or a cable gap port, and a driver attached to give it a level."
        )

    ctx.progress("compliance", 95, "writing the result")
    artifact = ctx.client.upload_artifact(
        ctx.token, "compliance.json",
        json.dumps(document, separators=(",", ":")).encode(), "application/json",
    )
    ctx.progress("done", 100, "compliance complete")

    summary = {
        "stage": "compliance",
        "standard_id": standard_id,
        "complete": gate.complete,
        "gaps": len(gate.gaps),
        "paths": len(paths),
    }
    if "margin_db" in document:
        summary["margin_db"] = document["margin_db"]
        summary["confidence_uncalibrated"] = document["confidence_uncalibrated"]
    return StageResult(summary=summary, artifacts=[artifact])


def _spectrum_point(pt: predict.Point, standard_id: str) -> dict:
    out = {
        "frequency_hz": pt.frequency_hz,
        "field_dbuv_per_m": pt.field_dbuv_per_m if pt.field_v_per_m > 0 else None,
        "limit_dbuv_per_m": pt.limit_dbuv_per_m,
        "detector": detector_at(standard_id, pt.frequency_hz),
    }
    if pt.uncovered:
        out["uncovered"] = pt.uncovered
    return out


def _notes(art: SolveArtifacts | None) -> list[str]:
    """What the result should say about itself that is not a gap."""
    notes = [
        "Levels are RMS, which is what an analyzer reads for a steady harmonic on every "
        "detector, so they compare directly with the quasi-peak and average limits.",
    ]
    ff = (art.farfield if art else None) or {}
    tenth = ff.get("tenth_wavelength_above_hz")
    freqs = ff.get("frequencies_hz") or []
    if tenth and freqs and freqs[0] < tenth:
        notes.append(
            f"Below {tenth / 1e6:.0f} MHz the far-field box is closer to the board than a "
            f"tenth of a wavelength. That was checked on test antennas down to 30 MHz, but "
            f"not on a board."
        )
    return notes


def _scored(out: predict.Outlook, p: dict, asm, board: dict) -> dict:
    """The half of the document that only exists when the inputs are complete."""
    worst = out.worst
    assert worst is not None
    lo, hi = out.range_80_db or (0.0, 0.0)

    dominant = worst.contributions[0] if worst.contributions else None
    findings = list(p.get("findings") or [])
    recs = None
    if dominant is not None:
        meta = asm.path_meta.get(dominant.label, {})
        ref = meta.get("ref")
        nets = tuple(meta.get("nets") or (board["nets"].get(ref, []) if ref else ()))
        near = board["centre"].get(ref) if ref else None
        recs = recommend.gather(findings, dominant.kind, dominant.label, nets=nets, near=near)

    doc: dict = {
        "worst": {
            "frequency_hz": worst.frequency_hz,
            "field_dbuv_per_m": worst.field_dbuv_per_m,
            "limit_dbuv_per_m": worst.limit_dbuv_per_m,
        },
        "margin_db": worst.margin_db,
        "sigma_db": out.sigma_db,
        "sigma_terms": out.sigma_terms,
        # Uncalibrated until recorded lab results exist (docs/implementation.md §7.4), and named
        # that way in the payload -- and only that way -- so no client can present it as a pass
        # probability.
        "confidence_uncalibrated": out.confidence_uncalibrated,
        "range_80_db": [lo, hi],
        "contributions": [
            {"label": c.label, "kind": c.kind, "driver_id": c.driver_id,
             "field_dbuv_per_m": predict.to_dbuv(c.field_v_per_m), "share": c.share}
            for c in worst.contributions
        ],
        "shares_indicative": worst.shares_indicative,
        "near_misses": [
            {"frequency_hz": q.frequency_hz, "margin_db": q.margin_db,
             "field_dbuv_per_m": q.field_dbuv_per_m}
            for q in out.near_misses
        ],
    }
    if recs is not None:
        doc["recommendations"] = {
            "path_kind": recs.path_kind,
            "path_label": recs.path_label,
            "general_only": recs.general_only,
            "items": [
                {"finding_id": r.finding_id, "rule": r.rule, "severity": r.severity,
                 "title": r.title, "detail": r.detail, "net": r.net,
                 "distance_mm": r.distance_mm}
                for r in recs.items
            ],
            "general": recs.general,
        }
    return doc
