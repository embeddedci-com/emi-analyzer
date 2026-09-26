"""Conducted: what the board's switching regulators put back onto its power input (experimental).

On demand only, and behind its own experimental feature on the server. The regulators'
switching frequency, input current and edges are not on the board; until the user gives them
every result is built on assumed defaults, and the result says which.

Reads the original upload, as the ESD simulation does, for tracks, pads, vias and the stackup at
full precision. Produces ``conducted.json``.
"""

from __future__ import annotations

import json
import logging

from .. import stackup, topology
from ..conducted import lisn, rail as crail, scan, sources
from ..kicad.normalize import board_extent, normalize
from ..rules import settings
from ..rules.model import RuleContext
from ..transient import ngspice
from . import StageContext, StageError, StageResult
from .ingest import DEFAULT_MAX_FREQUENCY_HZ, load_board, load_sidecars

log = logging.getLogger(__name__)

FORMAT = 2
MAX_REGULATORS = 20

ASSUMPTIONS = [
    "Two CISPR 16-1-2 50 Ω/50 µH networks, one in the supply line and one in the return, as a "
    "test house connects them. The level is the voltage at the network's board terminal, which is "
    "what a receiver reads once the network's division factor is added back.",
    "Differential mode only: the regulator's input current flows out on the supply line and back "
    "on the return.",
    "Each harmonic is a steady sine, which reads the same on the peak, quasi-peak and average "
    "detectors, so one level is compared with both limits and the average limit decides.",
    "A buck's input current is a trapezoid: I_in / D high for D of each period, with the given "
    "edges and a 30 % ripple on its top. An inverting converter draws the same shape, and a "
    "four-switch buck-boost is modelled in buck mode, which draws it too.",
    "A boost's input current is its inductor current: I_in with a triangle on top, V_in·D/(f·L) "
    "peak to peak from the inductor's value, or 30 % of I_in when that cannot be worked out.",
    "Sources that share a clock (the outputs of one PMIC) add as if in phase until their phases "
    "are given; sources with a given phase add as phasors.",
    "Capacitors use the component library's ESR and ESL, plus one via and the track to it; parts "
    "the library does not know get an assumed ESR and ESL, and say so. Capacitance is nominal: "
    "no DC-bias derating, and a ceramic near its rated voltage has well below its nominal value.",
    "Traces are their loop inductance over the plane, from the stackup's impedance and delay; a "
    "pour is treated as a trace as wide as the pour, at most 20 mm. The ground return is ideal.",
    "FCC 15.107 is measured at the AC plug of whatever powers the board. The networks here sit on "
    "the board's own input, so an external power supply's filter is not included.",
]

NOT_MODELLED = [
    "Common-mode emissions: they flow through the capacitance from the switch node and the board "
    "to the test bench's reference plane, which depends on the setup and the enclosure.",
    "Spread-spectrum clocking, burst or pulse-skipping modes, which lower quasi-peak and average "
    "readings.",
    "Flyback, SEPIC and charge-pump input currents; regulators fed from another regulator; a "
    "buck-boost in boost mode; discontinuous conduction.",
    "Radiated coupling from the switch node or the inductor into the input wiring.",
    "Self-resonance of an inductor or ferrite in the input path, and a bead's resistive peak.",
]


def _flag(given: dict, name: str) -> bool:
    raw = given.get(name)
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise StageError(f"{name} must be true or false")
    return raw


def _regulator_params(ctx: StageContext) -> dict[str, dict]:
    raw = ctx.params.get("regulators") or {}
    if isinstance(raw, list):  # also accept [{"ref": ...}, ...]
        raw = {str(r.get("ref")): r for r in raw if isinstance(r, dict) and r.get("ref")}
    if not isinstance(raw, dict):
        raise StageError("regulators must be an object keyed by reference designator")
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def _regulator(r, s: sources.RegulatorSource, given: dict, at) -> dict:
    """One regulator in the result: what it is, how it was found, and the settings used."""
    out = {
        "id": r.id, "ref": r.ref, "topology": s.topology,
        # "user" when the user changed it; otherwise what the discovery went by.
        "topology_from": "user" if given.get("topology") not in (None, r.topology) else r.topology_from,
        "found_as": r.topology, "found_by": r.how, "confidence": r.confidence,
        "confirmed": bool(given.get("confirmed")),
        "switch_net": r.switch_net, "input_net": r.input_net, "output_net": r.output_net,
        "inductor": r.inductor, "group": r.group, **at(r.input_pad),
        "params": s.as_dict(), "assumed": s.assumed,
    }
    if not s.pulsed:
        ripple, source = s.ripple_pp()
        out["input_ripple_a"] = {"value": round(ripple, 6), "source": source}
    return out


def run_conducted(ctx: StageContext) -> StageResult:
    if not ngspice.available():
        raise StageError("this worker has no ngspice, so it cannot run a conducted-emissions scan")
    std_class = str(ctx.params.get("class") or "B").upper()
    if std_class not in scan.STANDARDS:
        raise StageError("class must be A or B")
    given = _regulator_params(ctx)
    choice = ctx.params.get("entry") or None

    ctx.progress("fetch", 5, "fetching the board")
    info = ctx.client.run_input(ctx.token)
    url = info.get("input_url")
    if not url:
        raise StageError("this run has no uploaded board file")
    data = ctx.client.download(url)

    ctx.progress("parse", 15, "reading board file")
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

    ctx.progress("topology", 30, "tracing the power input")
    rctx = RuleContext(
        model=model, transform=board_extent(model),
        max_frequency_hz=float(cfg.value("max_frequency_hz") or DEFAULT_MAX_FREQUENCY_HZ),
        settings=cfg, electrics=electrics, topology=topology.build(model),
    )
    edge = cfg.param("input-filter", "edge_mm")
    found = crail.discover(rctx, choice, 5.0 if edge is None else float(edge))
    notes = list(found.notes) + list(electrics.notes)

    removed = [r for r in found.regulators if _flag(given.get(r.id, {}), "removed")]
    kept = [r for r in found.regulators if r.id not in {x.id for x in removed}]
    for r in found.regulators:
        _flag(given.get(r.id, {}), "confirmed")
    regs = kept[:MAX_REGULATORS]
    # A removed regulator is not a source, and a what-if does not put a capacitor at its pin.
    found.network.sources = {k: v for k, v in found.network.sources.items() if k in {r.id for r in regs}}
    if len(kept) > MAX_REGULATORS:
        notes.append(f"{len(kept)} regulators; the first {MAX_REGULATORS} were scanned")
    unknown = sorted(set(given) - {r.id for r in found.regulators})
    if unknown:
        notes.append(f"no regulator on the input rail is called {', '.join(unknown)}; "
                     f"{'its' if len(unknown) == 1 else 'their'} settings were not used")
    groups: dict[str, int] = {}
    for r in regs:
        groups[r.group] = groups.get(r.group, 0) + 1
    srcs = []
    for r in regs:
        try:
            srcs.append(sources.from_params(r.id, given.get(r.id), r.duty_from_rails, r.topology,
                                            r.inductance_h, r.v_in, phased=groups[r.group] > 1))
        except ValueError as exc:
            raise StageError(str(exc)) from exc
    for s in srcs:
        if not s.pulsed:
            s.ripple_pp()
        if s.capped:
            notes.append(f"{s.ref}: the inductor ripple worked out from its inductance is more than twice "
                         f"the input current, so it runs in discontinuous mode, which is not modelled; the "
                         f"ripple was capped at twice the input current")

    def progress(i: int, n: int, ref: str) -> None:
        ctx.progress("simulate", 40 + 50 * i / max(n, 1), f"{ref} ({i + 1} of {n})")

    try:
        core = scan.run(found.network, srcs, std_class, check_stop=ctx.check_stop, progress=progress)
    except ngspice.SimulationError as exc:
        raise StageError(f"the circuit simulation failed: {exc}") from exc
    except ValueError as exc:
        raise StageError(str(exc)) from exc
    notes += core.pop("notes")

    def at(pad) -> dict:
        if pad is None:
            return {}
        x, y = rctx.pt(pad.x, pad.y)
        return {"x": round(x, 3), "y": round(y, 3)}

    pads = {p.ref: p for p in model.pads if p.ref}
    net = found.network
    result = {
        "format": FORMAT,
        "entry": ({"id": found.entry.id, "connector": found.entry.connector, "net": found.entry.net,
                   "ground_net": found.entry.ground_pad.net if found.entry.ground_pad else "",
                   **at(found.entry.pad)} if found.entry else None),
        "entries": [e.id for e in found.entries],
        "rail_nets": found.rail_nets,
        "regulators": [_regulator(r, s, given.get(r.id) or {}, at) for r, s in zip(regs, srcs)],
        "removed": [{"id": r.id, "ref": r.ref, "topology": r.topology, "found_by": r.how,
                     "confidence": r.confidence, **at(r.input_pad)} for r in removed],
        "skipped": [{"ref": ref, "why": why} for ref, why in found.skipped],
        "network": {
            "caps": [
                {"ref": c.ref, "value": c.value, "c_f": c.c_f, "esr_ohm": round(c.esr_ohm, 5),
                 "esl_nh": round(c.esl_h * 1e9, 3), "mount_nh": round(c.mount_h * 1e9, 3),
                 "distance_mm": c.distance_mm, "model": c.model, "assumed": c.assumed,
                 **at(pads.get(c.ref))}
                for c in net.shunts
            ],
            "series": [
                {"ref": s.ref, "kind": s.kind, "value": s.value, "l_nh": round(s.l_h * 1e9, 3),
                 "r_ohm": round(s.r_ohm, 4), "assumed": s.assumed}
                for s in net.series if s.kind != "trace"
            ],
            "trace_nh": round(sum(s.l_h for s in net.series if s.kind == "trace") * 1e9, 2),
            "routed": found.routed,
        },
        "lisn": {"l_uh": lisn.L_H * 1e6, "r_ohm": lisn.R_RECEIVER_OHM, "standard": "CISPR 16-1-2 4.3"},
        **core,
        "assumptions": ASSUMPTIONS,
        "not_modelled": NOT_MODELLED,
        "notes": notes,
    }
    if found.entry and not regs:
        result["notes"].append(
            f"no switching regulator draws from {found.entry.net}, so there is nothing to scan")

    ctx.progress("upload", 97, "uploading results")
    artifact = ctx.client.upload_artifact(
        ctx.token, "conducted.json", json.dumps(result, separators=(",", ":")).encode(), "application/json",
    )
    worst = core.get("worst")
    summary = {
        "stage": "conducted",
        "class": std_class,
        "entry": found.entry.id if found.entry else "",
        "regulators": len(regs),
        "assumed": any(s.assumed for s in srcs),
        "worst_margin_db": worst["margin_db"] if worst else None,
        "worst_f_hz": worst["f_hz"] if worst else None,
    }
    log.info("conducted %s: %d regulators, worst %s", filename, len(regs), summary["worst_margin_db"])
    return StageResult(summary=summary, artifacts=[artifact])
