#!/usr/bin/env python3
"""Verification · Tier A against a second solver (docs/verification/cables-and-drivers.md).

Tier A's number is nec2c's ``e_per_amp`` for the product deck: a cable fed against a 0.1 m
board arm, 0.8 m over a perfect ground, read on a ring 3 m outside the arrangement. Before
this, the solver had been checked against transmission-line theory on a *different* deck
(fed against the plane), and nothing had checked the deck the product actually runs.

This builds the same structure in openEMS and compares the two, for three lengths and the
three far ends the library uses:

    openEMS  a square PEC column one cell across, a PEC wall at z = 0 for the ground, a
             lumped 50 ohm port in a one-cell gap between the board arm and the cable, and a
             current probe on every cell along the wire. The ground is a boundary condition,
             not meshed copper, so it is exact.
    nec2c    ``nec.Deck`` exactly as the product writes it, with the radius set to the
             column's equivalent radius (0.59 a) so both solve the same wire; and again at
             the product's own 0.5 mm, to say how much the radius itself moves the answer.

**The field is not taken from openEMS's grid.** The ring is 3 m and more from the wire and up
to 4 m high, and a grid that held it at λ/20 of 1.2 GHz would be ~200 M cells. Instead the
current openEMS puts on the wire is integrated to the ring with the exact field of a current
element over a perfect ground (every 1/r, 1/r² and 1/r³ term, plus its image). The same
integrator applied to nec2c's own currents reproduces nec2c's own near field, which is the
check that the integrator is not where a disagreement comes from. What is compared is then
what a solver is actually responsible for: the input impedance and the current distribution.

    docker run --rm --cpus 3 -m 6g -v "$PWD/worker:/spike" -v <out>:/out -e OUT=/out \\
        -w /spike -e PYTHONPATH=/spike --entrypoint python3 emi-worker:phase1 \\
        research/verify_cable_tier_a.py

``ONLY_NEC=1`` skips openEMS (seconds instead of hours) and reuses any runs already in OUT.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

from emi_worker.cables import nec
from emi_worker.cables.budget import field_v_per_m
from emi_worker.openems import csx, post, run
from emi_worker.openems.mesh import build_axis
from emi_worker.openems.model import excitation_band

C = 299_792_458.0
EPS0 = 8.8541878128e-12
OUT = Path(os.environ.get("OUT", "/out"))

LENGTHS_M = [float(v) for v in os.environ.get("LENGTHS", "0.3,1,2").split(",")]
FAR_ENDS = os.environ.get("FAR_ENDS", "open,ground,equipment").split(",")
F_MIN, F_MAX = 30e6, 1.2e9
FREQS = np.geomspace(F_MIN, F_MAX, int(os.environ.get("N_FREQ", "121")))

#: The column is one cell across. 5 mm is M0's wire, and its equivalent radius is 2.95 mm.
CELL_MM = float(os.environ.get("CELL_MM", "5"))
#: Air between the wire and the absorbing boundary, on every side but the ground.
PAD_MM = float(os.environ.get("PAD_MM", "600"))
THREADS = int(os.environ.get("THREADS", "3"))
ONLY_NEC = os.environ.get("ONLY_NEC") == "1"

HEIGHT_MM = nec.TABLE_HEIGHT_M * 1e3
BOARD_MM = 100.0
PRODUCT_RADIUS_M = nec.Deck(length_m=1.0, frequency_hz=1e8).radius_m
EQUIVALENT_RADIUS = 0.59


# ---- the field of a current on a wire, over a perfect ground -----------------------------

def _dipole_field(points: np.ndarray, centres: np.ndarray, moments: np.ndarray,
                  k: float, omega: float) -> np.ndarray:
    """E at ``points`` from short current elements (I·dl, A·m) at ``centres``.

    The complete field of a Hertzian dipole, not its far-field part: at 30 MHz and 3 m the
    1/r² and 1/r³ terms are most of it. Summing elements with piecewise-constant current puts
    the charge ΔI/(jω) at every joint, which is continuity, so an open end carries its charge
    without being told to.
    """
    p = moments / (1j * omega)                       # dipole moment, C·m
    d = points[:, None, :] - centres[None, :, :]
    r = np.linalg.norm(d, axis=2)
    n = d / r[..., None]
    n_dot_p = np.einsum("pek,ek->pe", n, p)
    far = (p[None, :, :] - n * n_dot_p[..., None])   # (n × p) × n
    near = 3.0 * n * n_dot_p[..., None] - p[None, :, :]
    g = np.exp(-1j * k * r) / (4 * math.pi * EPS0)
    e = g[..., None] * (far * (k * k / r)[..., None]
                        + near * (1.0 / r ** 3 + 1j * k / r ** 2)[..., None])
    return e.sum(axis=1)


def ring_field(path: np.ndarray, current: np.ndarray, f: float, ring: list, *,
               step_m: float = 0.004) -> float:
    """The scan's reading for a current along ``path`` (N×3, metres): max over the ring of
    the better polarisation, exactly as ``nec.parse_output`` reads nec2c's near field."""
    s = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(path, axis=0), axis=1))])
    fine = np.linspace(0.0, s[-1], max(2, int(math.ceil(s[-1] / step_m)) + 1))
    pts = np.stack([np.interp(fine, s, path[:, j]) for j in range(3)], axis=1)
    cur = np.interp(fine, s, current.real) + 1j * np.interp(fine, s, current.imag)
    centres = (pts[1:] + pts[:-1]) / 2
    dl = pts[1:] - pts[:-1]
    moment = dl * ((cur[1:] + cur[:-1]) / 2)[:, None]
    # A perfect ground: the image of a current element is mirrored in z, with its horizontal
    # components reversed and its vertical one kept.
    img_c = centres * np.array([1.0, 1.0, -1.0])
    img_m = moment * np.array([-1.0, -1.0, 1.0])
    omega = 2 * math.pi * f
    k = omega / C
    pts_obs = np.asarray(ring, dtype=float)
    e = _dipole_field(pts_obs, np.vstack([centres, img_c]), np.vstack([moment, img_m]),
                      k, omega)
    vertical = np.abs(e[:, 2])
    horizontal = np.sqrt(np.abs(e[:, 0]) ** 2 + np.abs(e[:, 1]) ** 2)
    return float(np.max(np.maximum(vertical, horizontal)))


def ring_points(length_m: float) -> list:
    return nec.ObservationRing(distance_m=3.0).points(-BOARD_MM / 1e3, length_m)


# ---- nec2c ---------------------------------------------------------------------------------

_CURRENT_ROW = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+" + r"\s+".join([nec._NUM] * 4) + r"\s+"
    + r"(" + nec._NUM + r")\s+(" + nec._NUM + r")\s+" + nec._NUM + r"\s+" + nec._NUM
    + r"\s*$", re.M)


def _wires(deck_text: str) -> dict[int, tuple[int, np.ndarray, np.ndarray]]:
    wires = {}
    for line in deck_text.splitlines():
        if line.startswith("GW "):
            v = line.split()
            tag, n = int(v[1]), int(v[2])
            a = np.array([float(x) for x in v[3:6]])
            b = np.array([float(x) for x in v[6:9]])
            wires[tag] = (n, a, b)
    return wires


def nec_solve(deck: nec.Deck) -> tuple[nec.NecResult, np.ndarray, np.ndarray]:
    """nec2c's own answer, plus its current as a path the integrator can read."""
    text = deck.to_text()
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "d.nec").write_text(text)
        subprocess.run(["nec2c", f"-i{tmp}/d.nec", f"-o{tmp}/d.out"], check=True,
                       capture_output=True)
        out = (Path(tmp) / "d.out").read_text()
    result = nec.parse_output(out, deck.frequency_hz)
    block = out.split("CURRENTS AND LOCATION", 1)[1].split("NEAR ELECTRIC FIELDS", 1)[0]
    by_tag: dict[int, list[complex]] = {}
    for m in _CURRENT_ROW.finditer(block):
        by_tag.setdefault(int(m.group(2)), []).append(complex(float(m.group(3)),
                                                              float(m.group(4))))
    wires = _wires(text)

    def centres(tag):
        n, a, b = wires[tag]
        return [a + (b - a) * (i + 0.5) / n for i in range(n)]

    # Board tip (open) -> feed -> cable -> far end. Every tag already runs in path order.
    pts = [wires[2][1]] + centres(2) + centres(1)
    cur = [0j] + by_tag[2] + by_tag[1]
    if 3 in wires:
        pts += [wires[1][2]] + centres(3) + [wires[3][2]]
        cur += [(by_tag[1][-1] + by_tag[3][0]) / 2] + by_tag[3] + [by_tag[3][-1]]
    else:
        pts += [wires[1][2]]
        cur += [0j]
    return result, np.array(pts), np.array(cur)


# ---- openEMS -------------------------------------------------------------------------------

def fdtd_model(length_m: float, far_end: str) -> tuple[csx.CSXDocument, dict]:
    a = CELL_MM
    h = a / 2
    H = HEIGHT_MM
    L = length_m * 1e3
    drop = far_end in ("ground", "equipment")
    coarse = C / F_MAX / 20 * 1e3

    x_req = [-BOARD_MM - PAD_MM, -BOARD_MM, 0.0, a, L + PAD_MM]
    x_req += [L - h, L + h] if drop else [L]
    y_req = [-PAD_MM, -h, h, PAD_MM]
    z_req = [0.0, H - h, H + h, H + PAD_MM]
    if far_end == "equipment":
        z_req.append(H - h - a)
    x = build_axis(x_req, a, coarse)
    y = build_axis(y_req, a, coarse)
    z = build_axis(z_req, a, coarse)
    z = z[z >= -1e-9]                    # the ground is a PEC wall at z = 0, not a PML

    f0, fc, _ = excitation_band(F_MIN, F_MAX)
    doc = csx.CSXDocument(
        excitation=csx.Excitation(type=0, f0=f0, fc=fc),
        x_lines=x.tolist(), y_lines=y.tolist(), z_lines=z.tolist(), f_max=f0 + fc,
        boundaries=csx.Boundaries(zmin="PEC"),
        # A grounded far end makes a loop that rings: stopped at -60 dB of energy, the last
        # fifth of a 0.3 m grounded record still moved Z by 0.4 dB. Asking for -70 dB is not
        # the answer either, because the energy then plateaus above it (charge the source's DC
        # content left behind) while the feed current is already 120 dB down; the run would go
        # to its cap. So: a fixed record of 385 ns, 11 periods of 30 MHz, and the 80 % check
        # below says whether that was enough for each case.
        end_criteria=float(os.environ.get("END_CRITERIA", "1e-7")),
        max_timesteps=int(os.environ.get("MAX_STEPS", "40000")),
    )
    cable_end = L + h if drop else L
    prims = [
        csx.Box(p1=(-BOARD_MM, -h, H - h), p2=(0.0, h, H + h), priority=csx.PRIORITY_METAL),
        csx.Box(p1=(a, -h, H - h), p2=(cable_end, h, H + h), priority=csx.PRIORITY_METAL),
    ]
    if far_end == "ground":
        prims.append(csx.Box(p1=(L - h, -h, 0.0), p2=(L + h, h, H + h),
                             priority=csx.PRIORITY_METAL))
    if far_end == "equipment":
        # CISPR 16-1-2's 150 ohm in the drop's top cell, where nec.Deck loads segment 1.
        prims.append(csx.Box(p1=(L - h, -h, 0.0), p2=(L + h, h, H - h - a),
                             priority=csx.PRIORITY_METAL))
        doc.add(csx.LumpedElement(
            name="far_150", direction=2, resistance=150.0, caps=True,
            primitives=[csx.Box(p1=(L - h, -h, H - h - a), p2=(L + h, h, H - h),
                                priority=csx.PRIORITY_PORT)]))
    doc.add(csx.Metal(name="wire", primitives=prims))

    feed = csx.Box(p1=(0.0, -h, H - h), p2=(a, h, H + h), priority=csx.PRIORITY_PORT)
    doc.add(csx.LumpedElement(name="feed_r", direction=0, resistance=50.0, caps=True,
                              primitives=[feed]))
    doc.add(csx.ExcitationProperty(name="feed_e", excite=(1.0, 0.0, 0.0), primitives=[feed]))
    doc.add(csx.ProbeBox(name="feed_ut", type=0, norm_dir=0, weight=-1.0, primitives=[
        csx.Box(p1=(0.0, h, H + h), p2=(a, h, H + h))]))

    def loop(lines, lo, hi):
        """The dual lines just outside the column, which is where openEMS reads H."""
        mids = (np.asarray(lines[1:]) + np.asarray(lines[:-1])) / 2
        return float(mids[mids < lo].max()), float(mids[mids > hi].min())

    y0, y1 = loop(y, -h, h)
    z0, z1 = loop(z, H - h, H + h)
    xmid = (x[1:] + x[:-1]) / 2
    probes: list[tuple[str, tuple[float, float, float], float]] = []

    def along_x(name, xm):
        doc.add(csx.ProbeBox(name=name, type=1, norm_dir=0, weight=1.0, primitives=[
            csx.Box(p1=(xm, y0, z0), p2=(xm, y1, z1))]))

    along_x("feed_it", a / 2)
    for i, xm in enumerate(xmid[(xmid > -BOARD_MM) & (xmid < 0)]):
        along_x(f"i_b{i:03d}", xm)
        probes.append((f"i_b{i:03d}", (xm, 0.0, H), 1.0))
    probes.append(("feed_it", (a / 2, 0.0, H), 1.0))
    for i, xm in enumerate(xmid[(xmid > a) & (xmid < (L - h if drop else L))]):
        along_x(f"i_c{i:03d}", xm)
        probes.append((f"i_c{i:03d}", (xm, 0.0, H), 1.0))
    if drop:
        dx0, dx1 = loop(x, L - h, L + h)
        zmid = (z[1:] + z[:-1]) / 2
        # Top to bottom, the direction the path runs, so the z-directed probe is negated.
        for i, zm in enumerate(sorted(zmid[(zmid > 0) & (zmid < H - h)], reverse=True)):
            doc.add(csx.ProbeBox(name=f"i_d{i:03d}", type=1, norm_dir=2, weight=1.0,
                                 primitives=[csx.Box(p1=(dx0, y0, zm), p2=(dx1, y1, zm))]))
            probes.append((f"i_d{i:03d}", (L, 0.0, zm), -1.0))

    return doc, {"probes": probes, "cells": doc.cell_count(), "drop": drop,
                 "length_mm": L, "lines": [len(x), len(y), len(z)]}


def fdtd_solve(length_m: float, far_end: str) -> dict:
    tag = f"{length_m:g}m_{far_end}_{CELL_MM:g}mm_pad{PAD_MM:g}"
    wd = OUT / f"tier_a_{tag}"
    doc, info = fdtd_model(length_m, far_end)
    done = wd / "feed_it"
    if not done.exists():
        if ONLY_NEC:
            return {}
        problems = doc.validate()
        if problems:
            raise SystemExit("; ".join(problems))
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "model.xml").write_text(doc.to_string())
        print(f"  openEMS {tag}: {info['cells']/1e6:.2f} M cells {info['lines']}", flush=True)
        t0 = time.time()
        r = run.run_openems(str(wd / "model.xml"), str(wd), threads=THREADS)
        print(f"    {r.final_timestep:,} steps in {time.time() - t0:.0f} s, energy "
              f"{r.final_energy_db:.1f} dB, warnings {r.warnings}", flush=True)
        (wd / "run.json").write_text(json.dumps({
            "steps": r.final_timestep, "energy_db": r.final_energy_db,
            "seconds": time.time() - t0, "warnings": r.warnings, "cells": info["cells"]}))

    u_tr = post.read_probe(str(wd / "feed_ut"))
    i_tr = post.read_probe(str(wd / "feed_it"))
    u, i_feed = post._dft(u_tr, FREQS), post._dft(i_tr, FREQS)
    z = u / i_feed

    # Was the record long enough? The same transform on the first 80 % of it.
    n = int(0.8 * len(i_tr.time_s))
    short = (post._dft(post.ProbeTrace(u_tr.time_s[:n], u_tr.values[:n]), FREQS)
             / post._dft(post.ProbeTrace(i_tr.time_s[:n], i_tr.values[:n]), FREQS))
    record_db = float(np.max(np.abs(20 * np.log10(np.abs(short) / np.abs(z)))))

    L = length_m
    H = nec.TABLE_HEIGHT_M
    currents = {}
    for name, _, sign in info["probes"]:
        currents[name] = sign * post._dft(post.read_probe(str(wd / name)), FREQS)
    pts = [(-BOARD_MM / 1e3, 0.0, H)] + [(p[0] / 1e3, p[1] / 1e3, p[2] / 1e3)
                                          for _, p, _ in info["probes"]]
    ring = ring_points(L)
    e_per_amp = []
    for k, f in enumerate(FREQS):
        cur = [0j] + [currents[name][k] for name, _, _ in info["probes"]]
        path = list(pts)
        if info["drop"]:
            # The corner, then the foot, where the current runs on into its image.
            cable_last = max(i for i, (nm, _, _) in enumerate(info["probes"])
                             if nm.startswith("i_c") or nm == "feed_it") + 1
            path.insert(cable_last + 1, (L, 0.0, H))
            cur.insert(cable_last + 1, (cur[cable_last] + cur[cable_last + 1]) / 2)
            path.append((L, 0.0, 0.0))
            cur.append(cur[-1])
        else:
            path.append((L, 0.0, H))
            cur.append(0j)
        e = ring_field(np.array(path), np.array(cur), float(f), ring)
        e_per_amp.append(e / abs(currents["feed_it"][k]))
    run_info = json.loads((wd / "run.json").read_text()) if (wd / "run.json").exists() else {}
    return {"z": z, "e_per_amp": np.array(e_per_amp), "record_db": record_db, **run_info}


# ---- comparison ----------------------------------------------------------------------------

def first_resonance(z: np.ndarray) -> float:
    """Lowest series resonance: reactance crossing zero going inductive (as cable test 4)."""
    x = z.imag
    for i in range(len(x) - 1):
        if x[i] < 0 <= x[i + 1]:
            t = -x[i] / (x[i + 1] - x[i])
            return float(math.exp(math.log(FREQS[i])
                                  + t * (math.log(FREQS[i + 1]) - math.log(FREQS[i]))))
    return float(FREQS[-1])


def gate_resonance(z: np.ndarray, e: np.ndarray) -> float:
    """Where "below the first resonance" ends: the lower of the first reactance zero and the
    first radiation peak.

    Either alone misleads on this structure. A cable fed against a floating board over a
    plane can radiate best well below its first series resonance (0.3 m grounded: a peak at
    69 MHz, the first reactance zero at 192 MHz with a 3 mm wire), and whether a weak
    resonance's reactance crosses zero at all depends on the wire radius.
    """
    first_peak = peaks(e)[0][0] if peaks(e) else float(FREQS[-1])
    return min(first_resonance(z), first_peak)


def peaks(e: np.ndarray) -> list[tuple[float, float]]:
    """Local maxima of e_per_amp that stand 1 dB above the band ±15 % around them."""
    out = []
    db = 20 * np.log10(e)
    for i in range(1, len(e) - 1):
        band = (FREQS >= FREQS[i] * 0.85) & (FREQS <= FREQS[i] * 1.15)
        if db[i] >= db[band].max() - 1e-9 and db[i] - db[band].min() >= 1.0 \
                and 0 < i < len(e) - 1 and db[i] > db[i - 1] and db[i] >= db[i + 1]:
            out.append((float(FREQS[i]), float(db[i])))
    return out


def db(a, b):
    return 20 * np.log10(np.abs(a) / np.abs(b))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"frequencies_hz": FREQS.tolist(), "cell_mm": CELL_MM, "pad_mm": PAD_MM,
              "equivalent_radius_m": EQUIVALENT_RADIUS * CELL_MM / 1e3, "cases": []}
    r_eq = EQUIVALENT_RADIUS * CELL_MM / 1e3
    for length_m in LENGTHS_M:
        for far_end in FAR_ENDS:
            ring = ring_points(length_m)
            nec_eq, nec_prod, integ = [], [], []
            for f in FREQS:
                deck = nec.Deck(length_m=length_m, frequency_hz=float(f), far_end=far_end,
                                radius_m=r_eq)
                res, path, cur = nec_solve(deck)
                nec_eq.append(res)
                # The integrator on nec2c's own current, normalised the way e_per_amp is.
                integ.append(ring_field(path, cur, float(f), ring) * abs(res.z_in))
                nec_prod.append(nec.run(nec.Deck(length_m=length_m, frequency_hz=float(f),
                                                 far_end=far_end)))
            z_nec = np.array([r.z_in for r in nec_eq])
            e_nec = np.array([r.e_per_amp() for r in nec_eq])
            e_prod = np.array([r.e_per_amp() for r in nec_prod])
            z_prod = np.array([r.z_in for r in nec_prod])
            f_res = gate_resonance(z_nec, e_nec)
            # Short of the resonance by 5 %, so a peak's own shoulder is not held to 1 dB.
            below = FREQS < 0.95 * f_res

            case = {
                "length_m": length_m, "far_end": far_end,
                "first_resonance_hz": f_res,
                "first_resonance_product_hz": gate_resonance(z_prod, e_prod),
                "nec_z_real": z_nec.real.tolist(), "nec_z_imag": z_nec.imag.tolist(),
                "nec_e_per_amp": e_nec.tolist(),
                "product_e_per_amp": e_prod.tolist(),
                "integrator_vs_nec_db_max": float(np.max(np.abs(db(integ, e_nec)))),
                "radius_db_below_max": float(np.max(np.abs(db(e_prod[below], e_nec[below]))))
                if below.any() else None,
                "nec_peaks": peaks(e_nec),
            }

            # The closed form, only where it claims to hold: the cable under λ/10 and the
            # ring in the far field (above c/(2π·3 m) with a factor of two of margin).
            window = (FREQS <= 0.1 * C / length_m) & (FREQS >= 2 * C / (2 * math.pi * 3.0))
            if window.any():
                closed = np.array([field_v_per_m(float(f), length_m, 1.0, 3.0)
                                   for f in FREQS[window]])
                case["closed_form_window_hz"] = [float(FREQS[window][0]),
                                                 float(FREQS[window][-1])]
                case["closed_minus_product_db"] = db(closed, e_prod[window]).tolist()

            fd = fdtd_solve(length_m, far_end)
            if fd:
                d = db(fd["e_per_amp"], e_nec)
                case.update({
                    "fdtd_z_real": fd["z"].real.tolist(), "fdtd_z_imag": fd["z"].imag.tolist(),
                    "fdtd_e_per_amp": fd["e_per_amp"].tolist(),
                    "fdtd_first_resonance_hz": gate_resonance(fd["z"], fd["e_per_amp"]),
                    "fdtd_peaks": peaks(fd["e_per_amp"]),
                    "record_db": fd["record_db"],
                    "steps": fd.get("steps"), "seconds": fd.get("seconds"),
                    "cells": fd.get("cells"),
                    "e_db_below_max": float(np.max(np.abs(d[below]))) if below.any() else None,
                    "e_db_below_median": float(np.median(np.abs(d[below])))
                    if below.any() else None,
                    "e_db_all_median": float(np.median(np.abs(d))),
                    "e_db_all_p90": float(np.percentile(np.abs(d), 90)),
                    "e_db_all_max": float(np.max(np.abs(d))),
                    "z_db_below_max": float(np.max(np.abs(db(fd["z"][below], z_nec[below]))))
                    if below.any() else None,
                })
            report["cases"].append(case)
            summary(case)
            (OUT / "tier_a_verification.json").write_text(json.dumps(report, indent=1))
    print(f"wrote {OUT}/tier_a_verification.json")


def summary(c: dict) -> None:
    print(f"\n{c['length_m']:g} m, far end {c['far_end']}: first resonance "
          f"{c['first_resonance_hz']/1e6:.0f} MHz (r_eq) / "
          f"{c['first_resonance_product_hz']/1e6:.0f} MHz (0.5 mm)")
    print(f"  integrator on nec2c's currents vs nec2c's field: "
          f"{c['integrator_vs_nec_db_max']:.2f} dB worst")
    if c.get("radius_db_below_max") is not None:
        print(f"  0.5 mm vs equivalent radius, below resonance: "
              f"{c['radius_db_below_max']:.2f} dB worst")
    if "closed_minus_product_db" in c:
        v = c["closed_minus_product_db"]
        print(f"  closed form minus product deck in its window "
              f"{c['closed_form_window_hz'][0]/1e6:.0f}-{c['closed_form_window_hz'][1]/1e6:.0f}"
              f" MHz: {min(v):+.1f} to {max(v):+.1f} dB")
    if "fdtd_e_per_amp" in c:
        print(f"  openEMS: {c['cells']/1e6 if c.get('cells') else 0:.2f} M cells, "
              f"{c.get('steps')} steps, record check {c['record_db']:.3f} dB")
        print(f"  openEMS first resonance {c['fdtd_first_resonance_hz']/1e6:.0f} MHz")
        if c.get("e_db_below_max") is not None:
            print(f"  e_per_amp below resonance: median {c['e_db_below_median']:.2f}, "
                  f"worst {c['e_db_below_max']:.2f} dB; |Z| worst {c['z_db_below_max']:.2f} dB")
        print(f"  e_per_amp whole band: median {c['e_db_all_median']:.2f}, "
              f"90th {c['e_db_all_p90']:.2f}, worst {c['e_db_all_max']:.2f} dB")
        print(f"  peaks nec2c  {[(round(f/1e6), round(v, 1)) for f, v in c['nec_peaks']]}")
        print(f"  peaks openEMS {[(round(f/1e6), round(v, 1)) for f, v in c['fdtd_peaks']]}")
    print(flush=True)


if __name__ == "__main__":
    main()
