"""Transient: simulate an ESD discharge on every exposed line of a board.

On demand only. Part models are generic until someone confirms them, and a number produced
automatically on every upload would be read as a prediction.

Reads the original upload, exactly as ingest does, rather than board.json: the simulation needs
tracks, pads, vias and the stackup at full precision, and parsing is a second or two.
Produces ``transient.json``.
"""

from __future__ import annotations

import json
import logging

from .. import stackup, topology
from ..kicad.normalize import board_extent, normalize
from ..rules import emc, settings
from ..rules.model import RuleContext, classify_net
from ..transient import lines as tlines
from ..transient import models as tmodels
from ..transient import ngspice, parts as tparts
from ..transient import simulate as tsim
from ..transient import waveforms
from . import StageContext, StageError, StageResult
from .ingest import DEFAULT_MAX_FREQUENCY_HZ, load_board, load_sidecars

log = logging.getLogger(__name__)

#: A board with more exposed lines than this is simulated in part, and says so.
MAX_LINES = 60

ASSUMPTIONS = [
    f"The discharge is the {waveforms.STANDARD} Table 3 contact-discharge current in parallel "
    f"with the generator's 330 Ω discharge resistor, so a clamped line takes almost all of it and "
    f"a high-impedance one sees about the charge voltage. Air discharge is not simulated.",
    "IC pins are modelled generically — 5 pF, 1 Ω, a diode to each rail, the supply as 100 nF "
    "behind 5 nH with 100 pF on-die — because an IC's internal protection is not published. "
    "Absolute voltages are estimates; compare the variants of a line with each other.",
    "Capacitors to ground on the IC's net are lumped at the IC pin with 1 nH each of mounting "
    "inductance; on a supply that is the decoupling, which is what absorbs a discharge there.",
    "Ground and planes are ideal: no ground bounce across the board, no coupling onto "
    "neighbouring traces, no radiated field from the discharge.",
    "Traces are lossless transmission lines with delay and impedance from the stackup; vias "
    "use L = 0.2·h·(ln(4h/d)+1) nH with h half the board thickness.",
    "A metal connector shell is where a contact discharge is applied in a test; injecting at a "
    "pin, as here, is the worst case.",
]


def _params(ctx: StageContext) -> tuple[int, list[int], set[str] | None, list[dict]]:
    p = ctx.params
    try:
        level = int(p.get("level", 4))
    except (TypeError, ValueError):
        level = 0
    if level not in waveforms.CONTACT_LEVELS:
        raise StageError("level must be 1, 2, 3 or 4 (2, 4, 6 or 8 kV contact discharge)")
    polarity = str(p.get("polarity", "both")).lower()
    polarities = {"positive": [1], "negative": [-1]}.get(polarity, [1, -1])
    only = {str(n) for n in (p.get("lines") or [])} or None
    models = [m for m in (p.get("models") or []) if isinstance(m, dict)]
    return level, polarities, only, models


def run_transient(ctx: StageContext) -> StageResult:
    if not ngspice.available():
        raise StageError("this worker has no ngspice, so it cannot run a transient simulation")
    level, polarities, only, model_entries = _params(ctx)

    ctx.progress("fetch", 3, "fetching the board")
    info = ctx.client.run_input(ctx.token)
    url = info.get("input_url")
    if not url:
        raise StageError("this run has no uploaded board file")
    data = ctx.client.download(url)

    files: dict[str, bytes] = {}
    for item in info.get("model_urls") or []:
        try:
            files[item["key"]] = ctx.client.download(item["url"])
        except Exception as exc:  # noqa: BLE001 -- reported against the model, not the run
            log.warning("could not download model %s: %s", item.get("key"), exc)

    ctx.progress("parse", 10, "reading board file")
    try:
        model, filename, _ = load_board(data)
    except StageError:
        raise
    except ValueError as exc:
        raise StageError(f"the board file could not be read: {exc}") from exc
    sidecars = load_sidecars(data)
    cfg = settings.load(
        ("project", ctx.params.get("project_settings")),
        ("file", sidecars.settings),
        ("run", ctx.params.get("settings")),
    )

    doc, _ = normalize(model, {"filename": filename, "key": info.get("input_key", ""),
                               "size_bytes": len(data), "sha256": ""})
    planes = {layer["name"] for layer in doc["layers"]
              if layer.get("plane_net") and layer.get("plane_coverage", 0) >= 0.3}
    electrics = stackup.analyse(model, planes, epsilon_override=float(cfg.value("epsilon_r") or 0.0),
                                via_ps=float(cfg.value("via_ps") or 2.0))

    ctx.progress("topology", 18, "tracing net connectivity")
    ctx.check_stop()
    rctx = RuleContext(
        model=model, transform=board_extent(model),
        max_frequency_hz=float(cfg.value("max_frequency_hz") or DEFAULT_MAX_FREQUENCY_HZ),
        settings=cfg, electrics=electrics, topology=topology.build(model),
    )
    edge = cfg.param("esd-protection", "edge_mm")
    found, notes = tlines.exposed_lines(rctx, 5.0 if edge is None else float(edge), only)
    notes = list(notes) + list(electrics.notes)

    hidden = [line.net for line in found if cfg.suppressed("esd-protection", line.net)]
    found = [line for line in found if not cfg.suppressed("esd-protection", line.net)]
    if hidden:
        notes.append(f"{len(hidden)} line{'s' if len(hidden) != 1 else ''} not simulated because "
                     f"esd-protection is suppressed for {'them' if len(hidden) != 1 else 'it'}")
    if len(found) > MAX_LINES:
        notes.append(f"{len(found)} exposed lines; the first {MAX_LINES} were simulated")
        found = found[:MAX_LINES]

    ctx.progress("models", 24, f"checking {len(model_entries)} uploaded model{'s' if len(model_entries) != 1 else ''}")
    board_parts: dict[str, list] = {}
    for ref, pads in sorted(emc._parts(rctx).by_ref.items()):
        board_parts.setdefault(tparts._norm(pads[0].value), []).append((ref, pads))
    vendor = tmodels.load(model_entries, files, board_parts, classify_net)

    def progress(i: int, n: int, line) -> None:
        ctx.progress("simulate", 30 + 65 * i / max(n, 1), f"{line.net} at {line.connector} ({i + 1} of {n})")

    result = tsim.simulate(found, level, polarities, vendor, rctx.transform, classify_net,
                           progress=progress, check_stop=ctx.check_stop)
    result["assumptions"] = ASSUMPTIONS
    result["notes"] = notes
    if not found:
        result["notes"].append(
            "no lines leave the board through an edge connector, so there is nothing to discharge into"
        )

    ctx.progress("upload", 97, "uploading results")
    artifact = ctx.client.upload_artifact(
        ctx.token, "transient.json", json.dumps(result, separators=(",", ":")).encode(), "application/json",
    )
    summary = {
        "stage": "transient",
        "standard": waveforms.STANDARD,
        "level": level,
        "kv": waveforms.CONTACT_LEVELS[level].kv,
        "lines": len(result["lines"]),
        "failed_lines": sum(1 for line in result["lines"] if "error" in line),
        "models_accepted": sum(1 for m in result["models"] if m["status"] == "accepted"),
        "models_rejected": sum(1 for m in result["models"] if m["status"] == "rejected"),
    }
    log.info("transient %s: %d lines at level %d", filename, summary["lines"], level)
    return StageResult(summary=summary, artifacts=[artifact])
