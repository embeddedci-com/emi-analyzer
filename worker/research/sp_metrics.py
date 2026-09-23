"""What the small-part studies compare between two solves of the same part.

A near-field map is drawn relative to its own loudest point, so a map on its own cannot say
whether cutting the part out changed the level. These read the dumps and the port directly:

* the hotspot: the loudest point of the signal layer's map, **away from the ports** (the port
  is where current is injected and is always loud; a hotspot that is the port says nothing),
  as a location and as a level in dB per volt of the solve's own source, which is the same
  pulse in every run of the same band;
* the port: |Z_in| and S21 on the run's own grid, from ``network.json``.
"""

from __future__ import annotations

import math
import os

import numpy as np

from emi_worker.openems import post

#: Around each port, mm: points this close are the injection, not a hotspot.
PORT_EXCLUSION_MM = 1.5


def hotspot(solved, layer: str, freqs: list[float], ports: list[tuple[float, float]]) -> list[dict]:
    """The loudest point of ``layer``'s map at each frequency, away from the ports."""
    dump = f"Hf_{layer.replace('.', '_')}"
    grids = post.read_fd_dump(os.path.join(str(solved.workdir), f"{dump}.h5"))
    src = post.source_spectrum(str(solved.workdir), "p1", 50.0, [g.frequency_hz for g in grids])
    v = np.abs(src["v_src"]) / post.OPENEMS_FD_SCALE
    out = []
    for g, vk in zip(grids, v):
        if freqs and not any(abs(g.frequency_hz - f) < 1 for f in freqs):
            continue
        X, Y = np.meshgrid(g.x_mm, g.y_mm)
        mask = np.ones_like(g.magnitude, dtype=bool)
        for px, py in ports:
            mask &= np.hypot(X - px, Y - py) > PORT_EXCLUSION_MM
        mag = np.where(mask, g.magnitude, 0.0)
        k = int(np.argmax(mag))
        iy, ix = np.unravel_index(k, mag.shape)
        # The local cell, to judge a moved peak against: the larger of the two cells at it.
        cx = float(np.diff(g.x_mm)[min(ix, len(g.x_mm) - 2)])
        cy = float(np.diff(g.y_mm)[min(iy, len(g.y_mm) - 2)])
        out.append({
            "frequency_hz": g.frequency_hz,
            "x_mm": float(g.x_mm[ix]), "y_mm": float(g.y_mm[iy]),
            "cell_mm": max(cx, cy),
            "db_per_volt": float(20 * math.log10(max(mag[iy, ix], 1e-30) / max(vk, 1e-30))),
            # Kept for comparing two runs, never written out (see ``strip``).
            "_map": (g.x_mm, g.y_mm, mag / max(vk, 1e-30)),
        })
    return out


def _at(entry: dict, x: float, y: float) -> float:
    """A map's value at a point, dB per volt: the nearest grid point."""
    xs, ys, m = entry["_map"]
    ix = int(np.argmin(np.abs(xs - x)))
    iy = int(np.argmin(np.abs(ys - y)))
    return float(20 * math.log10(max(m[iy, ix], 1e-30)))


def strip(obj):
    """The report without the maps."""
    if isinstance(obj, dict):
        return {k: strip(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [strip(v) for v in obj]
    return obj


def port(solved) -> dict:
    net = solved.json("network.json")
    f = np.asarray(net["frequencies_hz"])
    zr = np.array([np.nan if v is None else v for v in net["z_in_real"]])
    zi = np.array([np.nan if v is None else v for v in net["z_in_imag"]])
    s21 = None
    if net["transmission"]:
        s21 = [np.nan if v is None else v for v in net["transmission"][0]["s_db"]]
    return {"frequencies_hz": f.tolist(), "z_mag": np.hypot(zr, zi).tolist(),
            "s21_db": s21, "truncated_hz": net["truncated_hz"],
            "unusable_reason": net.get("unusable_reason")}


def compare(a: dict, b: dict) -> dict:
    """How far ``b`` is from ``a``: hotspot move in cells, level in dB, |Z| in %, S21 in dB."""
    moves, levels, still = [], [], []
    for ha, hb in zip(a["hotspot"], b["hotspot"]):
        d = math.hypot(ha["x_mm"] - hb["x_mm"], ha["y_mm"] - hb["y_mm"])
        moves.append(d / max(ha["cell_mm"], hb["cell_mm"]))
        levels.append(hb["db_per_volt"] - ha["db_per_volt"])
        # Along a matched line the field is nearly flat, and its maximum can sit anywhere on
        # it: the peak moving says nothing then. What matters is that the reference's hotspot
        # is still a hotspot here -- within 1 dB of this map's own peak.
        if "_map" in ha and "_map" in hb:
            still.append(hb["db_per_volt"] - _at(hb, ha["x_mm"], ha["y_mm"]))
    za, zb = np.asarray(a["port"]["z_mag"]), np.asarray(b["port"]["z_mag"])
    z_pct = np.abs(zb / za - 1) * 100
    s21 = None
    if a["port"]["s21_db"] and b["port"]["s21_db"]:
        s21 = float(np.nanmax(np.abs(np.asarray(b["port"]["s21_db"], dtype=float)
                                     - np.asarray(a["port"]["s21_db"], dtype=float))))
    return {
        "hotspot_move_cells": [round(m, 2) for m in moves],
        "hotspot_level_db": [round(x, 2) for x in levels],
        "worst_move_cells": float(max(moves)) if moves else None,
        "reference_hotspot_below_peak_db": [round(x, 2) for x in still],
        "worst_reference_below_peak_db": float(max(still)) if still else None,
        "worst_level_db": float(max(abs(x) for x in levels)) if levels else None,
        "worst_z_percent": float(np.nanmax(z_pct)),
        "median_z_percent": float(np.nanmedian(z_pct)),
        "worst_s21_db": s21,
    }
