"""What a small-part solve reports at its ports: input impedance and S-parameters.

One excited port and any number of 50 ohm loads, so one run gives one column of the scattering
matrix: S11 at the driven port and Sk1 into every other. The waves follow openEMS's own lumped
port convention, with each port's current probe measuring current *into* the structure from
the port:

    a = (V + Z0 I) / 2      b = (V - Z0 I) / 2      (power waves over sqrt(Z0), which cancels)

so S11 = b1 / a1 and Sk1 = bk / a1. At a passive 50 ohm port V = -Z0 I, which makes ak zero and
bk = V: the loads are matched terminations by construction and need no correction.

Each quantity is a ratio of two transforms of the same record, so ``post._dft``'s convention
(no factor of two) cancels and nothing here depends on it.
"""

from __future__ import annotations

import math
import os

import numpy as np

from . import post

FORMAT_VERSION = 1

#: Points on the log grid across the band. The same density ``post.dense_grid`` uses, which is
#: enough that a quarter-wave resonance of a 60 mm coupon falls between no two points unseen.
POINTS = 60

#: How negative a port's resistance may read, as a fraction of |Z|, before that frequency is
#: taken as unresolved. The far field's rule (``stages/solve.PASSIVE_TOLERANCE``), for the
#: same reason: a passive structure cannot have one, so a negative resistance is the record
#: stopping while the structure was still ringing.
PASSIVE_TOLERANCE = 0.05

#: The noise floor for a transmission, dB. Below this an S-parameter is the residue of the
#: transform, not coupling, and is reported as "below the floor" rather than as a number.
FLOOR_DB = -80.0


def log_grid(f_lo: float, f_hi: float, points: int = POINTS) -> list[float]:
    if not 0 < f_lo < f_hi:
        raise ValueError("a band needs 0 < f_lo < f_hi")
    step = (f_hi / f_lo) ** (1.0 / (points - 1))
    return [f_lo * step ** k for k in range(points)]


def _db(x: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore"):
        return 20.0 * np.log10(np.maximum(np.abs(x), 1e-30))


def network(
    workdir: str,
    ports: list[dict],
    frequencies_hz: list[float],
    *,
    unusable_reason: str | None = None,
) -> dict:
    """``network.json`` for one run.

    ``ports`` is ``[{"name", "resistance", "excited", ...}]`` in the run's order; extra keys
    (``net``, ``pad``) are carried into the document so the result names what it measured.
    """
    driven = [p for p in ports if p.get("excited")]
    if len(driven) != 1:
        raise ValueError("a port network needs exactly one excited port")
    src = driven[0]
    z0 = float(src.get("resistance", 50.0))
    f = np.asarray(frequencies_hz, dtype=np.float64)

    def spectra(name: str):
        u = post.read_probe(os.path.join(workdir, f"{name}_ut"))
        i = post.read_probe(os.path.join(workdir, f"{name}_it"))
        return post._dft(u, f), post._dft(i, f)

    v1, i1 = spectra(src["name"])
    a1 = (v1 + z0 * i1) / 2.0
    b1 = (v1 - z0 * i1) / 2.0
    with np.errstate(divide="ignore", invalid="ignore"):
        z_in = np.where(np.abs(i1) > 0, v1 / i1, np.nan + 0j)
        s11 = np.where(np.abs(a1) > 0, b1 / a1, np.nan + 0j)

    finite = np.isfinite(z_in) & np.isfinite(s11)
    passive = np.array([bool(not ok or z.real >= -PASSIVE_TOLERANCE * abs(z))
                        for z, ok in zip(z_in, finite)])
    usable = finite & passive
    if unusable_reason:
        usable = np.zeros_like(usable)

    transmission = []
    for p in ports:
        if p is src:
            continue
        vk, ik = spectra(p["name"])
        zk = float(p.get("resistance", 50.0))
        bk = (vk - zk * ik) / 2.0
        with np.errstate(divide="ignore", invalid="ignore"):
            sk1 = np.where(np.abs(a1) > 0, bk / a1, np.nan + 0j)
        # Power waves: the reference impedances differ only if a port was given another one.
        sk1 = sk1 * math.sqrt(z0 / zk)
        db = _db(sk1)
        transmission.append({
            "to": p["name"],
            **{k: p[k] for k in ("net", "pad") if p.get(k)},
            "s_db": [round(float(v), 3) if ok and v > FLOOR_DB else None
                     for v, ok in zip(db, usable)],
            "s_phase_deg": [round(float(np.degrees(np.angle(s))), 2) if ok else None
                            for s, ok in zip(sk1, usable)],
        })

    return {
        "format": "emi-port-network",
        "format_version": FORMAT_VERSION,
        "reference_impedance_ohm": z0,
        "frequencies_hz": [float(v) for v in f],
        "driven": {"name": src["name"], **{k: src[k] for k in ("net", "pad") if src.get(k)}},
        "z_in_real": [round(float(z.real), 4) if ok else None for z, ok in zip(z_in, usable)],
        "z_in_imag": [round(float(z.imag), 4) if ok else None for z, ok in zip(z_in, usable)],
        "s11_db": [round(float(v), 3) if ok else None for v, ok in zip(_db(s11), usable)],
        "transmission": transmission,
        "usable": [bool(v) for v in usable],
        # The run stopped before the structure did at these frequencies; a longer run (a lower
        # end criterion) would recover them.
        "truncated_hz": [float(v) for v, ok in zip(f, passive) if not ok],
        "unusable_reason": unusable_reason,
    }
