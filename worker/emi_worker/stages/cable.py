"""Cable: the Tier A current budget for every cable assigned to a connector (§4).

Seconds, on any worker, with no solve at all. A cable's resonances depend on the cable rather
than the layout, so this needs nothing from openEMS — which is exactly why it is worth having
as a run kind of its own rather than something that rides on a solve.

Reads the original upload the way ingest and transient do, so a cable run works on a board
whether or not it has ever been solved. Produces ``cables.json``.

**Assumptions, which the result carries** — see ASSUMPTIONS below and §6.2 in the design doc.
The one that matters most is the height: a cable is modelled 0.8 m above the ground plane,
because that is the table height the measurement standards specify, and the height sets both
the resonance and the radiation.
"""

from __future__ import annotations

import json
import logging

from ..cables import CableError, get, suggest_all
from ..cables.budget import solver_budget
from ..cables.nec import available as nec_available
from ..kicad.normalize import _board_extent
from ..rules import settings as settings_mod
from . import StageContext, StageError, StageResult
from .ingest import load_board, load_sidecars

log = logging.getLogger(__name__)

#: Frequencies the budget is evaluated at: a log grid over the radiated range. 48 points
#: resolves the peaks of a 2 m cable without the list of "frequencies to avoid" becoming a
#: list of every frequency.
GRID_POINTS = 48
GRID_MIN_HZ = 30e6
GRID_MAX_HZ = 1.2e9

ASSUMPTIONS = [
    "The cable is modelled 0.8 m above a perfect ground plane, which is the table height the "
    "measurement standards specify. It lies straight, and the field is read 3 m outside the "
    "circle around the board and cable, as a turntable scan does. Height sets both where the cable resonates and how well "
    "it radiates, so a cable that will be run along a metal chassis behaves differently.",
    "The board is the other arm of the antenna, modelled as a 0.1 m wire rather than as its "
    "real outline. Below about 300 MHz the cable dominates and this matters little; above it, "
    "the board's own dimensions start to count and this is optimistic.",
    "The far end is whatever the cable library says — open, grounded, or 150 Ω to ground for "
    "equipment (CISPR 16-1-2). It moves the first resonance by about a factor of two, so a "
    "cable declared with the wrong far end is the largest single error available here.",
    "The budget is a current limit, not a prediction. It says how much common-mode current "
    "this cable may carry before it reaches the limit; how much it actually carries needs a "
    "solve with a driver attached (Tier B).",
    "Shielding is not modelled. Without an enclosure a shielded and an unshielded cable are "
    "the same antenna: the common-mode current flows on the outside of the shield either way.",
]


def _grid() -> list[float]:
    step = (GRID_MAX_HZ / GRID_MIN_HZ) ** (1.0 / (GRID_POINTS - 1))
    return [GRID_MIN_HZ * step ** k for k in range(GRID_POINTS)]


def run_cable(ctx: StageContext) -> StageResult:
    if not nec_available():
        raise StageError(
            "this worker has no nec2c, so it cannot run the antenna solver. The worker "
            "advertises the cable run kind only when the binary resolves"
        )

    params = ctx.params or {}
    standard_id = str(params.get("standard_id") or "fcc-15b-radiated-3m")

    ctx.progress("fetch", 5, "fetching the board")
    info = ctx.client.run_input(ctx.token)
    url = info.get("input_url")
    if not url:
        raise StageError("the run has no input to read")
    data = ctx.client.download(url)
    try:
        board, filename, _ = load_board(data)
    except StageError:
        raise
    except ValueError as exc:
        raise StageError(f"the board file could not be read: {exc}") from exc
    transform = _board_extent(board)
    # Sidecars read the same upload, not a separate fetch: a settings document travelling
    # inside the archive is where a project's committed cable assignments live, and it is what
    # makes a cable run reproducible in CI.
    sidecars = load_sidecars(data)
    cfg = settings_mod.load(
        ("project", ctx.params.get("project_settings")),
        ("file", sidecars.settings),
        ("run", ctx.params.get("settings")),
    )

    # Assignments come from the run's params first, then the board's settings file. The run
    # wins because it is what the user just chose in the Cables tab; the settings file is what
    # travels with the board for CI.
    assignments: dict = dict(cfg.cables)
    for ref, spec in (params.get("connectors") or {}).items():
        assignments[str(ref)] = {"type": spec} if isinstance(spec, str) else dict(spec)

    suggestions = suggest_all(board)
    ctx.progress("cables", 15, f"{len(assignments)} assigned, {len(suggestions)} connectors found")

    results = []
    unassigned = []
    notes: list[str] = []
    grid = _grid()
    for i, suggestion in enumerate(suggestions):
        spec = assignments.get(suggestion.ref)
        if not spec:
            unassigned.append({
                "ref": suggestion.ref, "footprint": suggestion.footprint,
                "suggested": suggestion.cable_id, "reason": suggestion.reason,
            })
            continue
        cable_id = spec.get("type")
        if not cable_id or cable_id == "none":
            # A real answer: this connector is never cabled in the product.
            results.append({"ref": suggestion.ref, "cable_id": None, "declared_none": True})
            continue
        try:
            cable = get(cable_id)
            if spec.get("length_m"):
                cable = cable.with_length(float(spec["length_m"]))
        except CableError as exc:
            raise StageError(f"{suggestion.ref}: {exc}") from exc

        ctx.progress("solve", 20 + 70 * i / max(len(suggestions), 1),
                     f"{suggestion.ref}: {cable.name} at {cable.length_m:g} m")
        budget = solver_budget(cable, grid, standard_id)
        tightest = budget.tightest()
        if cable.cm_choke and not cable.cm_choke.from_curve:
            notes.append(
                f"{suggestion.ref}: the choke on {cable.name} is described by one number, so it "
                f"is modelled as a resistance of {cable.cm_choke.z_ohm_at_100mhz:g} ohm at "
                f"100 MHz rising with frequency. Above the ferrite's peak that overstates it. "
                f"Give its datasheet impedance curve for R and X."
            )
        results.append({
            "ref": suggestion.ref,
            "cable_id": cable.id,
            "cable_name": cable.name,
            "length_m": cable.length_m,
            "far_end": cable.far_end,
            "shield": cable.describe_shield(),
            "declared_none": False,
            "standard_id": budget.standard_id,
            "distance_m": budget.distance_m,
            "radiation_peaks_hz": budget.radiation_peaks(),
            # Without this an empty peak list reads as "this cable has no resonance" when it
            # may only mean "nobody looked closely enough".
            "grid_too_coarse": budget.grid_too_coarse(),
            "tightest": None if tightest is None else {
                "frequency_hz": tightest.frequency_hz,
                "max_current_dbua": tightest.max_current_dbua,
                "limit_dbuv_per_m": tightest.limit_dbuv_per_m,
            },
            "points": [
                {
                    "frequency_hz": p.frequency_hz,
                    "limit_dbuv_per_m": p.limit_dbuv_per_m,
                    "max_current_dbua": p.max_current_dbua,
                    "e_per_amp": p.e_per_amp,
                    "radiation_peak": p.radiation_peak,
                }
                for p in budget.points
            ],
        })

    document = {
        "format": "emi-cables",
        "format_version": 1,
        "solver": cfg.cable_solver,
        "standard_id": standard_id,
        "assumptions": ASSUMPTIONS,
        "cables": results,
        "unassigned": unassigned,
        "notes": notes,
    }
    if unassigned:
        document["notes"].append(
            f"{len(unassigned)} connector{'s' if len(unassigned) != 1 else ''} "
            f"{'have' if len(unassigned) != 1 else 'has'} no cable assigned, so "
            f"{'they are' if len(unassigned) != 1 else 'it is'} not modelled at all. "
            f"Compliance treats that as incomplete rather than as zero."
        )

    ctx.progress("upload", 96, "uploading results")
    artifact = ctx.client.upload_artifact(
        ctx.token, "cables.json",
        json.dumps(document, separators=(",", ":")).encode(), "application/json",
    )
    modelled = [r for r in results if r.get("cable_id")]
    summary = {
        "stage": "cable",
        "solver": cfg.cable_solver,
        "connectors": len(suggestions),
        "modelled": len(modelled),
        "declared_none": sum(1 for r in results if r.get("declared_none")),
        "unassigned": len(unassigned),
        "tightest_dbua": min(
            (r["tightest"]["max_current_dbua"] for r in modelled if r.get("tightest")),
            default=None,
        ),
    }
    log.info("cable %s: %d modelled, %d unassigned", filename, len(modelled), len(unassigned))
    return StageResult(summary=summary, artifacts=[artifact])
