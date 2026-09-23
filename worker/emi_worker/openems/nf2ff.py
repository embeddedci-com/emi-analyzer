"""The board's far field: near-field to far-field transform, with the ground reflection (§16.2).

A solve produces fields on copper. A radiated scan measures volts per metre at three metres,
from an antenna that sees the whole board at once. Surface equivalence is what connects them:
the tangential E and H on any closed surface around the structure determine the field
everywhere outside it, exactly. openEMS writes those six faces as frequency-domain dumps, and
the `nf2ff` CLI shipped in the image does the transform.

M0 measured that this works — a half-wave dipole's directivity comes back 2.13 dBi against a
textbook 2.15, and a PEC `Mirror` reproduces image theory to 0.08 dB median — and it also met
three ways to get a confident wrong answer out of it. Each has a guard here rather than a
comment:

* **`nf2ff` takes radians and metres.** Degrees are accepted silently and produce a plausible
  pattern; so is a mirror position in millimetres, which puts the image plane a thousand
  wavelengths away. It echoes the angles it was given straight back into `/Mesh/theta`, so the
  output file agrees with the bad input and confirms nothing.
* **A box placed close to the structure reads the reactive near field,** where the six faces
  very nearly cancel. M0's first attempt put them 0.064 λ out and got a pattern oscillating on
  a 7° scale, which no box that size can produce. The box is placed a stated clearance from
  the copper (``model.far_field_clearance_mm``) and the grid is grown to hold it, with the
  absorbing layer still clear of every face.
* **A dump with no frequencies writes every timestep.** `DumpBox` already refuses that; this
  never builds one without them.

**The job's `Radius` is where the field is evaluated, and `E` really does scale with it.**
Measured on M0's dipole dumps, transformed three times: peak |E| came back 1.037e-11, 3.456e-12
and 1.037e-12 V/m at 1, 3 and 10 m — exactly 1/r, to five figures. So the transform is asked for
the standard's own distance and the number in the artifact is directly comparable with a limit,
rather than a 1 m value with a correction applied somewhere else that can be forgotten.
"""

from __future__ import annotations

import logging
import os
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import csx

log = logging.getLogger(__name__)

NF2FF_BIN = os.environ.get("NF2FF_BIN", "/usr/bin/nf2ff")

#: The six faces, as (name, axis, sign). Axis 0/1/2 is x/y/z.
FACES: tuple[tuple[str, int, int], ...] = (
    ("xn", 0, -1), ("xp", 0, +1),
    ("yn", 1, -1), ("yp", 1, +1),
    ("zn", 2, -1), ("zp", 2, +1),
)

#: Grid lines that must lie between a face and the edge of the grid. openEMS's PML_8 absorbs
#: over the outermost eight, and a face among them samples the absorber, not the field.
MIN_LINES_OUTSIDE = 10

#: Record every Nth grid line on each face.
#:
#: The transform needs the surface sampled against the wavelength, not against the copper: at
#: 1 GHz a wavelength is 300 mm in air, and a quarter of even a 50 µm board mesh is 200 µm.
#: M0 measured what it saves — six faces of E and H at 60 frequencies are 1.1–4.0 GB at full
#: resolution and 0.07–0.25 GB at a quarter, which is the difference between an artifact that
#: can be served over HTTP and one that cannot.
FACE_SUB_SAMPLING = 4

#: Elevation angles of the scan, in degrees. A radiated scan sweeps the receiving antenna in
#: height, which for a fixed 3 m distance is a sweep in elevation: 1–4 m at 3 m is roughly
#: 18–53° above the horizon, and the horizon itself is the table plane.
THETA_DEG = np.arange(30.0, 90.1, 2.5)

#: Azimuths, in degrees. A scan turns the product on a turntable, so every azimuth is sampled
#: and the reported level is the maximum. 15° steps resolve a board-sized radiator's pattern:
#: the first sidelobe of a 100 mm aperture at 1 GHz is tens of degrees wide.
PHI_DEG = np.arange(0.0, 360.0, 15.0)

#: Where the table top is, in metres, relative to the board. The measurement standards put the
#: product on a 0.8 m table over a ground plane, and the plane is what the mirror models.
TABLE_HEIGHT_M = 0.8


class NF2FFError(RuntimeError):
    """The transform failed, or returned something other than what it was asked for."""


@dataclass(frozen=True)
class Faces:
    """Where the six dump faces sit, in the model's millimetres."""

    x0: float
    y0: float
    z0: float
    x1: float
    y1: float
    z1: float

    def box(self, axis: int, sign: int) -> csx.Box:
        lo = [self.x0, self.y0, self.z0]
        hi = [self.x1, self.y1, self.z1]
        p1, p2 = lo[:], hi[:]
        p1[axis] = p2[axis] = hi[axis] if sign > 0 else lo[axis]
        return csx.Box(p1=(p1[0], p1[1], p1[2]), p2=(p2[0], p2[1], p2[2]))

    def centre(self) -> tuple[float, float, float]:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2, (self.z0 + self.z1) / 2)


def plan_faces(mesh, copper: tuple[float, float, float, float, float, float],
               clearance_mm: float) -> Faces:
    """Put the box ``clearance_mm`` outside the copper on every side.

    ``copper`` is ``(x0, y0, x1, y1, z0, z1)`` in model mm: everything that carries current,
    including a cable stub that extends the domain on one side.

    The box used to be placed "0.8 of the way from the region to the boundary". In x and y the
    grid ended *at* the region unless a cable stub extended it, so the side faces landed on the
    outermost grid lines, inside the absorbing layer; and vertically 0.8 of the default 5 mm
    air put them 4 mm from the copper. Placing the box from the copper and checking the grid
    has room outside it turns both into refusals rather than quiet errors.
    """
    x0, y0, x1, y1, z0, z1 = copper
    faces = Faces(x0=x0 - clearance_mm, y0=y0 - clearance_mm, z0=z0 - clearance_mm,
                  x1=x1 + clearance_mm, y1=y1 + clearance_mm, z1=z1 + clearance_mm)

    for axis, lines, lo, hi in ((0, mesh.x, faces.x0, faces.x1), (1, mesh.y, faces.y0, faces.y1),
                                (2, mesh.z, faces.z0, faces.z1)):
        lines = np.asarray(lines)
        below = int(np.sum(lines < lo))
        above = int(np.sum(lines > hi))
        if min(below, above) < MIN_LINES_OUTSIDE:
            raise NF2FFError(
                f"the far-field box does not fit inside the grid along {'xyz'[axis]}: it needs "
                f"{MIN_LINES_OUTSIDE} grid lines outside each face so the absorbing boundary "
                f"stays clear of it, and has {min(below, above)}"
            )
    return faces


def add_dumps(doc: csx.CSXDocument, faces: Faces, frequencies: list[float]) -> list[str]:
    """Add the twelve frequency-domain dumps the transform reads. Returns their names."""
    if not frequencies:
        raise NF2FFError("the far field needs at least one frequency")
    names = []
    for name, axis, sign in FACES:
        box = faces.box(axis, sign)
        for kind, dump_type in (("E", csx.DUMP_E_FREQ), ("H", csx.DUMP_H_FREQ)):
            dump = f"nf2ff_{kind}_{name}"
            doc.add(csx.DumpBox(name=dump, dump_type=dump_type, dump_mode=1,
                                frequencies=frequencies, sub_sampling=FACE_SUB_SAMPLING,
                                primitives=[box]))
            names.append(dump)
    return names


def write_job(
    workdir: str,
    frequencies: list[float],
    out_h5: str,
    *,
    centre_mm: tuple[float, float, float] = (0.0, 0.0, 0.0),
    radius_m: float = 3.0,
    mirror_z_m: float | None = None,
    lower_face_z_m: float | None = None,
    theta_deg: np.ndarray = THETA_DEG,
    phi_deg: np.ndarray = PHI_DEG,
) -> str:
    """Write the nf2ff job XML.

    ``radius_m`` is where the field is evaluated — the standard's measuring distance, so the
    result is directly comparable with a limit rather than needing a 1/r correction applied by
    hand somewhere else.

    ``mirror_z_m`` puts a PEC image plane there, which is the ground plane every radiated
    standard specifies.

    **The lower face is kept unless the mirror lies on it.** The job used to drop it whenever a
    mirror was set, on the reasoning that the image replaces it. That is true only for the case
    M0 measured, where the mirror sat exactly on the lower face (openEMS's own symmetry-plane
    use). The solve puts the plane 0.8 m below a board whose box ends a few centimetres down,
    so dropping the face left the surface open: surface equivalence no longer held, and the
    far field was computed from five sixths of the currents that carry it. The image of a
    closed box is a second closed box, and both are needed.

    ``lower_face_z_m`` is where that face is, in metres. With it, a mirror on the face drops
    the face (the M0 case), and a mirror *above* it is refused: the box would then reach
    through the ground plane, which is not a configuration images describe.
    """
    drop_lower = False
    if mirror_z_m is not None and lower_face_z_m is not None:
        # A micrometre: the mesh writes positions in millimetres to a handful of places.
        if abs(mirror_z_m - lower_face_z_m) <= 1e-6:
            drop_lower = True
        elif mirror_z_m > lower_face_z_m:
            raise NF2FFError(
                f"the ground plane at {mirror_z_m:g} m is above the bottom of the far-field box "
                f"at {lower_face_z_m:g} m, so the box reaches through it"
            )

    wd = Path(workdir)
    root = ET.Element("nf2ff", {
        "Eps_r": "1", "Mue_r": "1", "Verbose": "0",
        "freq": ",".join(repr(float(f)) for f in frequencies),
        "Outfile": str(out_h5),
        # Metres. The dump meshes are in metres whatever the model's drawing unit is.
        "Center": ",".join(repr(v / 1000.0) for v in centre_mm),
        "Radius": repr(float(radius_m)),
    })
    for name, _axis, _sign in FACES:
        if drop_lower and name == "zn":
            continue
        e_path = wd / f"nf2ff_E_{name}.h5"
        h_path = wd / f"nf2ff_H_{name}.h5"
        for p in (e_path, h_path):
            if not p.exists():
                raise NF2FFError(
                    f"the solve wrote no {p.name}: the far field cannot be computed from a "
                    f"partial box, because surface equivalence needs the surface closed"
                )
        ET.SubElement(root, "Planes", {"E_Field": str(e_path), "H_Field": str(h_path)})

    if mirror_z_m is not None:
        ET.SubElement(root, "Mirror",
                      {"Type": "PEC", "Dir": "2", "Pos": repr(float(mirror_z_m))})

    # Radians, always. Degrees are accepted and produce a pattern that looks fine.
    ET.SubElement(root, "theta").text = ",".join(
        repr(float(v)) for v in np.deg2rad(theta_deg))
    ET.SubElement(root, "phi").text = ",".join(repr(float(v)) for v in np.deg2rad(phi_deg))

    path = wd / "nf2ff_job.xml"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return str(path)


def run_job(job_path: str, out_h5: str, *, timeout_s: float = 900.0) -> None:
    try:
        p = subprocess.run([NF2FF_BIN, job_path], capture_output=True, text=True,
                           timeout=timeout_s)
    except FileNotFoundError as exc:
        raise NF2FFError(f"the {NF2FF_BIN} binary is not installed on this worker") from exc
    except subprocess.TimeoutExpired as exc:
        raise NF2FFError(f"nf2ff did not finish within {timeout_s:.0f} s") from exc
    if p.returncode != 0:
        raise NF2FFError(f"nf2ff exited with status {p.returncode}: {p.stdout[-800:]}")
    if not Path(out_h5).exists():
        # It can exit zero having written nothing, which is the family of failure M0 met
        # three times. Exit status is not evidence.
        raise NF2FFError("nf2ff exited cleanly but wrote no output file")


def read_field(
    out_h5: str,
    frequencies: list[float],
    *,
    theta_deg: np.ndarray = THETA_DEG,
    phi_deg: np.ndarray = PHI_DEG,
) -> dict:
    """Read the transform back, and check it answered the question that was asked.

    Returns the **maximum** |E| over every observation angle and both polarisations at each
    frequency, which is what a scan reports: the turntable and the height sweep exist to find
    the worst direction, and the number written on a test report is that worst one.
    """
    import h5py

    with h5py.File(out_h5, "r") as f:
        theta = np.asarray(f["/Mesh/theta"][:], dtype=float)
        phi = np.asarray(f["/Mesh/phi"][:], dtype=float)
        if theta.size != theta_deg.size or phi.size != phi_deg.size:
            raise NF2FFError(
                f"nf2ff returned {theta.size} theta by {phi.size} phi angles, not "
                f"{theta_deg.size} by {phi_deg.size}: the job's angles were not read"
            )
        # It echoes what it was given, so this catches a units mistake on the way in rather
        # than a transform error on the way out -- which is exactly the bug M0 hit.
        if not np.allclose(np.sort(theta), np.sort(np.deg2rad(theta_deg)), atol=1e-9):
            raise NF2FFError(
                "nf2ff echoed back angles that are not the ones written; the job was built "
                "in the wrong units"
            )

        peaks: list[float] = []
        per_angle: list[list[float]] = []
        for k in range(len(frequencies)):
            try:
                e_theta = np.asarray(f[f"/nf2ff/E_theta/FD/f{k}_real"][:]) \
                    + 1j * np.asarray(f[f"/nf2ff/E_theta/FD/f{k}_imag"][:])
                e_phi = np.asarray(f[f"/nf2ff/E_phi/FD/f{k}_real"][:]) \
                    + 1j * np.asarray(f[f"/nf2ff/E_phi/FD/f{k}_imag"][:])
            except KeyError as exc:
                raise NF2FFError(
                    f"nf2ff wrote no field for frequency {k} ({frequencies[k] / 1e6:g} MHz)"
                ) from exc
            # Per polarisation, because a scan measures one at a time with a linearly
            # polarised antenna and reports the better of the two -- not their vector sum,
            # which no single antenna can read at once.
            mag = np.maximum(np.abs(e_theta), np.abs(e_phi))
            peaks.append(float(mag.max()))
            per_angle.append([float(v) for v in mag.max(axis=0).ravel()])

    return {
        "frequencies_hz": [float(v) for v in frequencies],
        "e_max_v_per_m": peaks,
        "theta_deg": [float(v) for v in theta_deg],
        "e_by_theta_v_per_m": per_angle,
    }
