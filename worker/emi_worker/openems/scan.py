"""The field a radiated scan reads: 3 m from the product, 1-4 m up, over the ground plane.

The NF2FF box's tangential E and H are equivalent surface currents, J = n x H and M = -n x E,
and they radiate exactly the field of whatever is inside the box, everywhere outside it. The
`nf2ff` tool evaluates that radiation in the far-field limit only: a direction and a 1/r. This
module evaluates it with the full free-space Green's function at the points a scan actually
visits, plus the currents' image in the ground plane.

It replaced the far-field transform for the level because the two answer different questions,
and the transform's answer was measured wrong:

* **A scan is not a sphere.** The receiving antenna stands 3 m from the product's boundary and
  rises 1-4 m above the plane; the board sits 0.8 m up. The transform was asked for the field on
  a 3 m sphere around the board, 30-90 degrees from the zenith. The highest antenna position is
  4.4 m from the board, not 3, and the image ray is longer still.
* **Nothing below 100 MHz is in the far field at 3 m.** kr is 1.9 at 30 MHz, where the terms a
  far-field transform drops are a third of the one it keeps.
* **An image 1.6 m below a source seen from 3 m is not a far-field array**, and `nf2ff`'s own PEC
  mirror images horizontal currents wrongly on top of that.

Against nec2c on dipoles 0.8 m over a perfect plane, the old sphere read up to 7.7 dB high on a
vertical dipole and 19 dB high on a horizontal one at 30 MHz. This module reads within 0.1 dB of
nec2c on short dipoles, and within 1 dB on half-wave ones up to their resonance.
docs/verification/far-field.md has the numbers.

The ground plane is modelled with image currents: a PEC plane reverses the horizontal parts of
J and the vertical part of M. The solve itself has no plane in it -- the board is simulated in
free space and only its radiation is imaged -- so the plane's effect back on the board's own
currents is left out. nec2c, which does include it, puts it at under 1 % of an electrically short
dipole's input impedance at 0.8 m and up to 11 % of a half-wave one's.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np

C0 = 299_792_458.0
ETA0 = 376.730313668

#: Metres from the product's boundary to the antenna. FCC Part 15 Class B is measured at 3 m,
#: from the periphery of the equipment (ANSI C63.4), which is how the cable path measures too.
SCAN_DISTANCE_M = 3.0

#: Antenna heights above the ground plane. The standard sweeps 1-4 m continuously. The direct
#: and reflected rays interfere, and at 1 GHz from a board 0.8 m up the lobes are about half a
#: metre apart in height, so a step of 0.1 m keeps the reading within a few tenths of a dB of
#: the true peak where 1 m steps can miss it by several dB.
SCAN_HEIGHTS_M = tuple(float(h) for h in np.round(np.arange(1.0, 4.0001, 0.1), 3))

#: Azimuths of the turntable. 15 degrees resolves a board-sized radiator's pattern below 1 GHz.
SCAN_AZIMUTHS = 24

#: Faces, as (name, axis, sign); must match ``nf2ff.FACES``.
_FACES = (("xn", 0, -1), ("xp", 0, 1), ("yn", 1, -1), ("yp", 1, 1), ("zn", 2, -1), ("zp", 2, 1))


class ScanError(RuntimeError):
    """The box cannot be read, or does not enclose the structure."""


@dataclass
class Surface:
    """The equivalent currents on the box, already multiplied by their area."""

    #: (N, 3) metres.
    pos: np.ndarray
    #: (F, N, 3) complex: J dA in A*m and M dA in V*m, in openEMS's spectral density units.
    j: np.ndarray
    m: np.ndarray
    #: The box, (x0, y0, z0, x1, y1, z1) in metres, as the dumps actually sampled it.
    box: tuple[float, float, float, float, float, float]


def _dual(lines: np.ndarray) -> np.ndarray:
    """Length each sample stands for along one axis: half the distance to each neighbour."""
    w = np.empty_like(lines)
    w[1:-1] = (lines[2:] - lines[:-2]) / 2
    w[0] = (lines[1] - lines[0]) / 2
    w[-1] = (lines[-1] - lines[-2]) / 2
    return np.abs(w)


def _read(path: str, frequencies: list[float]):
    import h5py

    with h5py.File(path, "r") as f:
        recorded = np.asarray(f["FieldData/FD"].attrs["frequency"], dtype=float).ravel()
        mesh = [np.asarray(f[f"Mesh/{a}"][:], dtype=float) for a in "xyz"]
        fields = []
        for freq in frequencies:
            # The dump records 9 significant figures; the job asks by value.
            hit = np.flatnonzero(np.abs(recorded - freq) <= 1e-6 * freq)
            if hit.size == 0:
                raise ScanError(f"{os.path.basename(path)} has no field at {freq / 1e6:g} MHz")
            k = int(hit[0])
            re = np.asarray(f[f"FieldData/FD/f{k}_real"][:], dtype=float)
            im = np.asarray(f[f"FieldData/FD/f{k}_imag"][:], dtype=float)
            # (component, z, y, x) -> (z, y, x, component)
            fields.append(np.moveaxis(re + 1j * im, 0, -1))
    return mesh, np.stack(fields)


def read_surface(workdir: str, frequencies: list[float]) -> Surface:
    """Read the twelve face dumps and turn them into area-weighted equivalent currents.

    **The box must be closed**, and the check is on what the dumps actually sampled, not on
    what was asked for. openEMS sub-samples a dump by striding from its first grid line, so a
    face whose line count does not fit the stride stops short of the edge. That is how the
    production box used to be open: on the fixture board every face stopped 14 mm short of its
    neighbours, and a surface with a slot in it does not satisfy surface equivalence.
    """
    faces = {}
    for name, axis, sign in _FACES:
        e_mesh, e = _read(os.path.join(workdir, f"nf2ff_E_{name}.h5"), frequencies)
        h_mesh, h = _read(os.path.join(workdir, f"nf2ff_H_{name}.h5"), frequencies)
        if any(len(a) != len(b) or not np.allclose(a, b) for a, b in zip(e_mesh, h_mesh)):
            raise ScanError(f"the E and H dumps of face {name} sampled different points")
        if len(e_mesh[axis]) != 1:
            raise ScanError(f"face {name} is not a plane: it spans {len(e_mesh[axis])} lines "
                            f"across its own normal")
        faces[name] = (axis, sign, e_mesh, e, h)

    box = (faces["xn"][2][0][0], faces["yn"][2][1][0], faces["zn"][2][2][0],
           faces["xp"][2][0][0], faces["yp"][2][1][0], faces["zp"][2][2][0])
    size = max(box[3] - box[0], box[4] - box[1], box[5] - box[2])
    tol = 1e-4 * size
    for name, (axis, _sign, mesh, _e, _h) in faces.items():
        for a in range(3):
            if a == axis:
                continue
            lo, hi = mesh[a][0], mesh[a][-1]
            want_lo, want_hi = box[a], box[a + 3]
            if lo - want_lo > tol or want_hi - hi > tol:
                gap = max(lo - want_lo, want_hi - hi) * 1000
                raise ScanError(
                    f"the far-field box is open: face {name} stops {gap:.1f} mm short of the "
                    f"box along {'xyz'[a]}, so its surface currents do not enclose the board"
                )

    pos, js, ms = [], [], []
    for name, (axis, sign, mesh, e, h) in faces.items():
        zz, yy, xx = np.meshgrid(mesh[2], mesh[1], mesh[0], indexing="ij")
        dz, dy, dx = np.meshgrid(*(_dual(m) if len(m) > 1 else np.ones(1) for m in
                                   (mesh[2], mesh[1], mesh[0])), indexing="ij")
        area = (dx * dy * dz).ravel()
        normal = np.zeros(3)
        normal[axis] = sign
        p = np.stack([xx.ravel(), yy.ravel(), zz.ravel()], axis=-1)
        ef = e.reshape(e.shape[0], -1, 3)
        hf = h.reshape(h.shape[0], -1, 3)
        # J = n x H, M = -n x E, each times the area it stands for.
        js.append(np.cross(normal, hf) * area[None, :, None])
        ms.append(-np.cross(normal, ef) * area[None, :, None])
        pos.append(p)
    return Surface(pos=np.concatenate(pos), j=np.concatenate(js, axis=1),
                   m=np.concatenate(ms, axis=1), box=tuple(float(v) for v in box))


def _with_image(s: Surface, ground_z_m: float | None):
    if ground_z_m is None:
        return s.pos, s.j, s.m
    if ground_z_m > s.box[2] + 1e-9:
        raise ScanError(f"the ground plane at {ground_z_m:g} m is above the bottom of the box "
                        f"at {s.box[2]:g} m")
    ipos = s.pos.copy()
    ipos[:, 2] = 2 * ground_z_m - ipos[:, 2]
    # A PEC plane images an electric current with its horizontal part reversed and a magnetic
    # current with its vertical part reversed.
    ij = s.j * np.array([-1.0, -1.0, 1.0])
    im = s.m * np.array([1.0, 1.0, -1.0])
    return (np.concatenate([s.pos, ipos]), np.concatenate([s.j, ij], axis=1),
            np.concatenate([s.m, im], axis=1))


def field(s: Surface, frequencies: list[float], points: np.ndarray, *,
          ground_z_m: float | None = None, chunk: int = 2048) -> np.ndarray:
    """E at ``points`` (P, 3 metres) for each frequency: (F, P, 3) complex.

    The exact field of electric and magnetic current elements, time convention e^{jwt} as
    openEMS's dumps use (outgoing waves are e^{-jkr}):

        E_J = -j eta k G [ J a - n (n.J) b ],   a = 1 - j/kr - 1/(kr)^2,  b = 1 - 3j/kr - 3/(kr)^2
        E_M = (jk + 1/r) G (n x M),             G = e^{-jkr} / (4 pi r)

    with n the unit vector from the element to the point. At large r these reduce to exactly
    what `nf2ff` computes, which the verification checks against it.
    """
    pos, j, m = _with_image(s, ground_z_m)
    pts = np.asarray(points, dtype=float)
    ks = 2 * np.pi * np.asarray(frequencies, dtype=float) / C0
    out = np.zeros((len(ks), len(pts), 3), dtype=complex)
    for c0 in range(0, len(pos), chunk):
        d = pts[:, None, :] - pos[None, c0:c0 + chunk, :]          # (P, C, 3)
        r = np.sqrt((d ** 2).sum(-1))
        n = d / r[..., None]
        jc, mc = j[:, c0:c0 + chunk], m[:, c0:c0 + chunk]
        for fi, k in enumerate(ks):
            kr = k * r
            g = np.exp(-1j * kr) / (4 * np.pi * r)
            inv = 1.0 / kr
            a = 1 - 1j * inv - inv ** 2
            b = 1 - 3j * inv - 3 * inv ** 2
            ja = -1j * ETA0 * k * g
            jf, mf = jc[fi], mc[fi]                                # (C, 3)
            ndj = np.einsum("pci,ci->pc", n, jf)
            e = np.einsum("pc,ci->pi", ja * a, jf)
            e -= np.einsum("pc,pci->pi", ja * b * ndj, n)
            cm = (1j * k + 1.0 / r) * g
            e += np.einsum("pc,pci->pi", cm, np.cross(n, mf[None, :, :]))
            out[fi] += e
    return out


def ring(centre_m: tuple[float, float], radius_m: float, ground_z_m: float,
         heights_m=SCAN_HEIGHTS_M, azimuths: int = SCAN_AZIMUTHS) -> np.ndarray:
    """The scan's antenna positions: (len(heights) * azimuths, 3), heights outermost."""
    a = 2 * np.pi * np.arange(azimuths) / azimuths
    return np.array([[centre_m[0] + radius_m * np.cos(t), centre_m[1] + radius_m * np.sin(t),
                      ground_z_m + h] for h in heights_m for t in a])


def reading(e: np.ndarray) -> np.ndarray:
    """What a linearly polarised antenna reports at each point: the larger of vertical and
    horizontal, as the cable path reads nec2c. (F, P, 3) -> (F, P)."""
    return np.maximum(np.abs(e[..., 2]), np.sqrt(np.abs(e[..., 0]) ** 2 + np.abs(e[..., 1]) ** 2))
