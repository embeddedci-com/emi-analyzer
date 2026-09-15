"""Compliance: the margin, what drives it, and how much weight it can bear (§16, §17).

Seconds of arithmetic on things other runs already produced — a solve's far field, a solve's
cable transfer functions, the antenna terms beside them, the rule findings, and whichever driver
the user attached. No solver runs here, which is why this is a run kind of its own rather than
a stage bolted onto a solve: a user changing a cable length or swapping a driver wants the
answer back immediately, and none of that needs the fields recomputed.

**The gate comes first.** §17.3 says an incomplete estimate carries no margin and no confidence
at all, so `complete` is decided before anything is combined, and an incomplete document simply
has no `margin_db` key. That is deliberate: a warning is something a reader can skip, and a
missing field is not — and an export or an API client will happily print a number beside a
caveat it never read.
"""

from __future__ import annotations

import json
import logging

from ..compliance import completeness, predict, recommend
from ..compliance.limits import limit_at, standard
from . import StageContext, StageError, StageResult

log = logging.getLogger(__name__)

FORMAT = "emi-compliance"
FORMAT_VERSION = 1

#: Where the uncertainty budget's terms come from, so the document can show its own working.
#: §17.2 calls these engineering placeholders, and the document says so in the same breath as
#: it reports them — a σ a reader cannot interrogate is worse than no σ.
PLACEHOLDER_NOTE = (
    "These are engineering placeholders, chosen conservatively. They are replaced by residuals "
    "as the verification tests and recorded lab results produce them, not tuned."
)


def run_compliance(ctx: StageContext) -> StageResult:
    p = ctx.params
    standard_id = str(p.get("standard_id") or "fcc-15b-radiated-3m")
    try:
        std = standard(standard_id)
    except Exception as exc:
        raise StageError(f"unknown standard {standard_id!r}: {exc}") from exc

    ctx.progress("compliance", 10, "checking inputs")
    gate = completeness.check(
        drivers=list(p.get("drivers") or []),
        source_nets=list(p.get("source_nets") or []),
        connectors=list(p.get("connectors") or []),
        cable_assignments=dict(p.get("cable_assignments") or {}),
        driven_ports=list(p.get("driven_ports") or []),
        far_field_refs=list(p.get("far_field_refs") or []),
        solved_f_max_hz=float(p.get("solved_f_max_hz") or 0.0),
        required_f_max_hz=float(p.get("required_f_max_hz") or 0.0),
        undriven_harmonics={float(k): v for k, v in (p.get("undriven_harmonics") or {}).items()},
        enclosure=str(p.get("enclosure") or "none"),
        power=str(p.get("power") or ""),
        power_entry_found=bool(p.get("power_entry_found", True)),
    )

    paths = _paths(p)
    frequencies = sorted({float(f) for path in paths for f in
                          (pt.frequency_hz for pt in path.points)})

    document: dict = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "standard_id": standard_id,
        "standard": std.name,
        "distance_m": std.distance_m,
        "scan": std.scan,
        "complete": gate.complete,
        "gaps": [{"key": g.key, "message": g.message, "fixed_on": g.fixed_on}
                 for g in gate.gaps],
        "paths": [{"kind": q.kind, "label": q.label, "driver_id": q.driver_id,
                   "sigma_terms": q.sigma_terms} for q in paths],
        "uncertainty_note": PLACEHOLDER_NOTE,
    }

    # The spectrum is always drawn, complete or not: §17.3 says a partial one is shown greyed,
    # because seeing which frequencies are close is useful long before the estimate is whole.
    if paths and frequencies:
        ctx.progress("compliance", 40, "combining paths")
        out = predict.outlook(paths, frequencies, standard_id,
                              shared_terms=dict(p.get("shared_sigma_terms") or {}))
        document["spectrum"] = [
            {
                "frequency_hz": pt.frequency_hz,
                "field_dbuv_per_m": pt.field_dbuv_per_m,
                "limit_dbuv_per_m": pt.limit_dbuv_per_m,
            }
            for pt in out.points
        ]
        if gate.complete and out.worst is not None:
            ctx.progress("compliance", 70, "ranking contributions")
            document.update(_scored(out, p))
    elif not paths:
        document["spectrum"] = []
        # Not a gap in the §17.3 sense -- it is what an empty project looks like -- but the
        # view still needs to say something other than showing an empty chart.
        document["no_paths"] = (
            "Nothing radiating has been modelled yet: this needs a solve with a far-field box, "
            "or a cable with a gap port, and a driver to give either an absolute level."
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
    }
    if "margin_db" in document:
        summary["margin_db"] = document["margin_db"]
        summary["confidence"] = document["confidence"]
    return StageResult(summary=summary, artifacts=[artifact])


def _paths(p: dict) -> list[predict.Path]:
    """Build the radiating paths from what the caller assembled.

    The caller — the server — is the only thing that knows which runs belong to this project
    and which driver the user attached, so it does the gathering and this does the arithmetic.
    Levels arrive in V/m already composed, because both producers (§7's cable composition and
    §16.2's far field) write V/m and neither should be turned into dB and back on the way here.
    """
    out: list[predict.Path] = []
    for raw in p.get("paths") or []:
        points = [
            predict.PathPoint(frequency_hz=float(f), field_v_per_m=float(e))
            for f, e in zip(raw.get("frequencies_hz") or [],
                            raw.get("field_v_per_m") or [])
        ]
        out.append(predict.Path(
            kind=str(raw.get("kind") or "board"),
            label=str(raw.get("label") or "unnamed path"),
            driver_id=str(raw.get("driver_id") or "unknown"),
            points=points,
            sigma_terms={str(k): float(v) for k, v in (raw.get("sigma_terms") or {}).items()},
        ))
    return out


def _scored(out: predict.Outlook, p: dict) -> dict:
    """The half of the document that only exists when the inputs are complete."""
    worst = out.worst
    assert worst is not None
    lo, hi = out.range_80_db or (0.0, 0.0)

    dominant = worst.contributions[0] if worst.contributions else None
    findings = list(p.get("findings") or [])
    recs = None
    if dominant is not None:
        path_meta = next(
            (q for q in (p.get("paths") or []) if q.get("label") == dominant.label), {})
        near = path_meta.get("near_mm")
        recs = recommend.gather(
            findings, dominant.kind, dominant.label,
            nets=tuple(path_meta.get("nets") or ()),
            near=(float(near[0]), float(near[1])) if near else None,
        )

    doc: dict = {
        "worst": {
            "frequency_hz": worst.frequency_hz,
            "field_dbuv_per_m": worst.field_dbuv_per_m,
            "limit_dbuv_per_m": worst.limit_dbuv_per_m,
        },
        "margin_db": worst.margin_db,
        "sigma_db": out.sigma_db,
        "sigma_terms": out.sigma_terms,
        # Uncalibrated until §17.4's recorded results exist, and named that way in the payload
        # so no client can present it as a pass probability by accident.
        "confidence_uncalibrated": out.confidence,
        "confidence": out.confidence,
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
