"""The scan: every regulator's harmonics through the input filter to the LISN, against FCC 15.107.

For each regulator one ngspice AC analysis gives the transfer from a 1 A source at its input pin
to both LISN terminals, at exactly its harmonics, for the layout as it is and for every what-if.
The harmonic's RMS current times that transfer is the level; the network is linear, so this is
the same answer a transient simulation of the switching would give, without its runtime.

Two regulators at the same frequency: if the user gave both a phase (the bucks of one PMIC, or two
regulators on one clock), their lines are added as phasors, so two identical bucks 180° apart
cancel at odd harmonics and add at even ones. Otherwise their relative phase is unknown and
drifts, and the lines are added in magnitude, the worst case.

Detector: a regulator's harmonic is a steady sine, and a steady sine reads the same on the peak,
quasi-peak and average detectors (a receiver is calibrated to its RMS). So one level is compared
with both limits, and the average limit, 10 dB lower, is the one that decides. A regulator that
spreads its spectrum, skips pulses at light load or bursts reads lower on quasi-peak and average
than this; none of that is modelled.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..compliance import limits
from ..components.match import match_part
from ..components.resolve import resolve_part
from ..transient import ngspice
from . import lisn
from .network import Network, Series, Shunt, build, harmonic_sweep
from .sources import RegulatorSource

STANDARDS = {
    "B": ("fcc-15b-conducted-qp", "fcc-15b-conducted-avg"),
    "A": ("fcc-15a-conducted-qp", "fcc-15a-conducted-avg"),
}
#: CISPR 16-1-1 band B resolution bandwidth. Lines of two regulators closer than this are read
#: together by a receiver, so they are added (in phase, the worst case).
RBW_HZ = 9e3

#: What the what-ifs add. Values anyone can buy; the capacitor's ESR and ESL come from the library.
ADDED_CAP_VALUE = "10uF"
ADDED_CAP_LABEL = "10 µF"
ADDED_CAP_FOOTPRINT = "Capacitor_SMD:C_1206_3216Metric"
ADDED_L_H = 4.7e-6
ADDED_L_OHM = 0.03
#: Mounting inductance of an added capacitor when the board gives nothing to copy.
DEFAULT_MOUNT_H = 0.5e-9


@dataclass
class Variant:
    id: str
    label: str
    network: Network
    #: The capacitor a "without" variant removes.
    removes: str = ""


def added_cap(node: str, mount_h: float, label: str) -> Shunt:
    got = resolve_part(match_part("added", ADDED_CAP_VALUE, ADDED_CAP_FOOTPRINT))
    if got is None or not got.placeable:  # the built-in family covers 1206; a guard, not a path
        return Shunt(node, label, ADDED_CAP_VALUE, 10e-6, 0.005, 1e-9, mount_h, "assumed", True, True)
    rlc = got.rlc
    return Shunt(node=node, ref=label, value=ADDED_CAP_VALUE, c_f=rlc.c_f, esr_ohm=rlc.esr_ohm,
                 esl_h=rlc.esl_h, mount_h=mount_h, model="library, generic 1206", added=True)


def variants(net: Network) -> list[Variant]:
    """The layout, three fixes to try, and the layout without each capacitor in turn."""
    mounts = sorted(c.mount_h for c in net.shunts)
    mount = mounts[len(mounts) // 2] if mounts else DEFAULT_MOUNT_H
    out = [Variant("as_laid_out", "As laid out", net)]

    at_conn = net.copy()
    at_conn.shunts.append(added_cap(at_conn.entry, mount, "added at the connector"))
    out.append(Variant("cap_at_connector", f"Add {ADDED_CAP_LABEL} at the power connector", at_conn))

    if net.sources:
        at_reg = net.copy()
        for ref, node in sorted(net.sources.items()):
            at_reg.shunts.append(added_cap(node, mount, f"added at {ref}"))
        out.append(Variant("cap_at_regulator", f"Add {ADDED_CAP_LABEL} at each regulator's input pin", at_reg))

    pi = net.insert_after_entry(Series("", "", ADDED_L_H, ADDED_L_OHM, "added", "added inductor",
                                       f"{ADDED_L_H * 1e6:g}uH"))
    pi.shunts.append(added_cap(pi.entry, mount, "added at the connector"))
    out.append(Variant("lc_filter", f"Add a {ADDED_L_H * 1e6:g} µH inductor and {ADDED_CAP_LABEL} at the connector", pi))

    for c in net.shunts:
        out.append(Variant(f"without:{c.ref}", f"Without {c.ref}", net.without(c.ref), removes=c.ref))
    return out


def _limit_pair(std_class: str, f: float) -> tuple[float, float]:
    qp, avg = STANDARDS[std_class]
    return limits.limit_at(qp, f), limits.limit_at(avg, f)


def _phased(lines: list[dict]) -> list[dict]:
    """Add as phasors the lines at one frequency from sources whose phase the user gave.

    Each line carries its complex voltage at both LISNs ("_vp", "_vn"); the level is the larger
    magnitude. A line that cancels to nothing is dropped.
    """
    out: list[dict] = []
    by_f: dict[float, dict] = {}
    for line in lines:
        if "_vp" not in line:
            out.append(line)
            continue
        if line.get("_phased"):
            key = round(line["f_hz"], 3)
            if key in by_f:
                into = by_f[key]
                into["_vp"] += line["_vp"]
                into["_vn"] += line["_vn"]
                into["sources"] = into["sources"] + line["sources"]
                continue
            line = dict(line)
            by_f[key] = line
        out.append(line)
    kept = []
    for line in out:
        if "_vp" in line:
            volts = max(abs(line.pop("_vp")), abs(line.pop("_vn")))
            line.pop("_phased", None)
            if volts <= 0:
                continue
            line["dbuv"] = lisn.dbuv(volts)
        kept.append(line)
    return kept


def _combine(lines: list[dict]) -> list[dict]:
    """Merge lines a receiver cannot separate, adding their voltages."""
    lines = sorted(_phased(lines), key=lambda x: x["f_hz"])
    out: list[dict] = []
    for line in lines:
        if out and line["f_hz"] - out[-1]["f_hz"] <= RBW_HZ:
            last = out[-1]
            volts = 10 ** (last["dbuv"] / 20) + 10 ** (line["dbuv"] / 20)
            last["dbuv"] = 20 * math.log10(volts)
            last["sources"] = last["sources"] + line["sources"]
            continue
        out.append(dict(line))
    return out


def _score(lines: list[dict], std_class: str) -> dict | None:
    worst = None
    for line in lines:
        qp, avg = _limit_pair(std_class, line["f_hz"])
        line["qp_limit"], line["avg_limit"] = round(qp, 2), round(avg, 2)
        line["margin_qp_db"] = round(qp - line["dbuv"], 2)
        line["margin_avg_db"] = round(avg - line["dbuv"], 2)
        line["dbuv"] = round(line["dbuv"], 2)
        margin = min(line["margin_qp_db"], line["margin_avg_db"])
        if worst is None or margin < worst["margin_db"]:
            worst = {"f_hz": line["f_hz"], "margin_db": margin, "dbuv": line["dbuv"],
                     "detector": "average" if line["margin_avg_db"] <= line["margin_qp_db"] else "quasi-peak",
                     "sources": line["sources"]}
    return worst


def run(net: Network, regulators: list[RegulatorSource], std_class: str = "B",
        check_stop=lambda: None, progress=lambda i, n, ref: None) -> dict:
    """Simulate every regulator on every variant. Returns the result document's core."""
    if std_class not in STANDARDS:
        raise ValueError("class must be A or B")
    vs = variants(net)
    per_variant: dict[str, list[dict]] = {v.id: [] for v in vs}
    shares: dict[tuple[str, int], list[dict]] = {}
    notes: list[str] = []

    for i, reg in enumerate(regulators):
        check_stop()
        progress(i, len(regulators), reg.ref)
        f_sw = reg.frequency_hz.value
        first, last = harmonic_sweep(f_sw)
        if last < first:
            notes.append(f"{reg.ref} switches at {f_sw / 1e3:.4g} kHz and has no harmonic between 150 kHz and 30 MHz")
            continue
        harmonics = reg.phasors(first, last)
        keys = [f"v{j}" for j in range(len(vs))]
        deck, saves = build([(k, v.network, reg.ref) for k, v in zip(keys, vs)], f_sw, first, last,
                            title=f"emi conducted {reg.ref}")
        spectra = ngspice.run_ac(deck)
        if len(spectra.frequency) != len(harmonics):
            raise ngspice.SimulationError(
                f"ngspice returned {len(spectra.frequency)} frequencies for {len(harmonics)} harmonics")
        for k, v in zip(keys, vs):
            vp = abs(spectra[saves[k]["v_p"]])
            vn = abs(spectra[saves[k]["v_n"]])
            cp, cn = spectra[saves[k]["v_p"]], spectra[saves[k]["v_n"]]
            for idx, (n, f, amps) in enumerate(harmonics):
                volts = max(vp[idx], vn[idx]) * abs(amps)
                if volts <= 0:
                    continue
                level = lisn.dbuv(volts)
                per_variant[v.id].append({
                    "f_hz": round(f, 3), "dbuv": level,
                    "sources": [{"ref": reg.ref, "harmonic": n, "dbuv": round(level, 2)}],
                    "_vp": complex(cp[idx]) * amps, "_vn": complex(cn[idx]) * amps,
                    "_phased": reg.phase_deg.source == "user",
                })
            if v.id == "as_laid_out":
                for idx, (n, f, _) in enumerate(harmonics):
                    # Three significant figures, not decimal places: the LISN's share is often a
                    # few parts per million, and rounding it to 0 said nothing reaches it.
                    row = [{"ref": c.ref, "share": float(f"{abs(spectra[saves[k][f'i:{ci}']][idx]):.3g}")}
                           for ci, c in enumerate(v.network.shunts)]
                    z_lisn = abs(lisn.network_impedance(f))
                    into = max(float(vp[idx]), float(vn[idx])) / z_lisn
                    row.append({"ref": "LISN", "share": float(f"{into:.3g}")})
                    shares[(reg.ref, n)] = sorted(row, key=lambda r: -r["share"])

    results = []
    for v in vs:
        combined = _combine(per_variant[v.id])
        worst = _score(combined, std_class)
        results.append({"id": v.id, "label": v.label, "removes": v.removes, "lines": combined, "worst": worst})

    base = results[0]["worst"]
    dominant = None
    if base is not None:
        src = max(base["sources"], key=lambda s: s["dbuv"])
        dominant = {
            "f_hz": base["f_hz"], "regulator": src["ref"], "harmonic": src["harmonic"],
            "shares": shares.get((src["ref"], src["harmonic"]), [])[:6],
        }

    def delta(r: dict) -> float | None:
        if base is None or r["worst"] is None:
            return None
        return round(r["worst"]["margin_db"] - base["margin_db"], 2)

    suggestions = [
        {"id": r["id"], "label": r["label"], "worst": r["worst"], "change_db": delta(r)}
        for r in results[1:] if not r["removes"]
    ]
    suggestions.sort(key=lambda s: -(s["change_db"] or -1e9))
    components = [
        {"ref": r["removes"], "without_change_db": delta(r)}
        for r in results if r["removes"]
    ]
    components.sort(key=lambda c: c["without_change_db"] if c["without_change_db"] is not None else 0)

    return {
        "standard": {"class": std_class, "quasi_peak": STANDARDS[std_class][0], "average": STANDARDS[std_class][1]},
        "variants": [r for r in results if not r["removes"]],
        "worst": base,
        "dominant": dominant,
        "suggestions": suggestions,
        "components": components,
        "notes": notes,
    }
