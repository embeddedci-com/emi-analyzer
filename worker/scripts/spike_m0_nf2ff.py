"""M0 · C7 — does the shipped nf2ff give a dipole's far field, and does Mirror do the ground?

§19's C7 is the M4 gate: a half-wave dipole fixture's NF2FF directivity within 0.5 dB of
theory, and a dipole over ground whose height scan is within 1 dB of image theory. The
earlier M0 note read the XML schema out of `libnf2ff.so` strings; reading strings is not
running the tool, so this runs it.

The same dipole `spike_m0_wire_fdtd.py` already agrees with nec2c on, plus six
frequency-domain E and H face dumps, then the `nf2ff` CLI on the result. It asserts nothing
about the numbers it cannot see: every step checks that what came back is what was asked
for, because M0 has already met two tools that complete successfully and compute nothing.

    docker run --rm -v "$PWD/worker:/spike" -v <out>:/spike/spike_out -w /spike \\
        -e PYTHONPATH=/spike --entrypoint python3 embeddedci/emi-worker:dev \\
        scripts/spike_m0_nf2ff.py
"""

from __future__ import annotations

import json
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import h5py
import numpy as np

from emi_worker.openems import csx, run
from emi_worker.openems.mesh import build_axis

OUT = Path("/spike/spike_out")
NF2FF_BIN = "/usr/bin/nf2ff"

LENGTH_MM = 500.0
CELL_MM = 5.0
MAX_RES_MM = 12.5          # lambda/90 at the resonance; M0 showed lambda/20 is not enough
SPAN_XY_MM = float(os.environ.get("SPAN_XY_MM", "400.0"))
SPAN_Z_MM = float(os.environ.get("SPAN_Z_MM", "600.0"))
#: The NF2FF box. Surface equivalence is exact in principle, so the first version of this
#: spike put the faces close in to keep the dumps small — 70 mm from the wire tips, 0.064 λ,
#: on 12.5 mm cells. That is the reactive near field of a wire end, and the six faces came
#: back nearly cancelling: the pattern oscillated on a 7° scale, which no 0.22 λ box can do.
#: The box belongs just inside the PML, as far from the structure as the domain allows.
FACE_XY_MM = float(os.environ.get("FACE_XY_MM", "350.0"))
FACE_Z_MM = float(os.environ.get("FACE_Z_MM", "550.0"))

F0, FC = 350e6, 320e6
FREQS = [200e6, 275.1e6, 400e6]   # the middle one is the measured resonance

FACES = [("xn", 0, -1), ("xp", 0, +1), ("yn", 1, -1), ("yp", 1, +1),
         ("zn", 2, -1), ("zp", 2, +1)]

#: Observation angles, in DEGREES here for readability. The job is written in radians:
#: nf2ff takes radians, echoes whatever it is given back into /Mesh/theta unchanged, and
#: produces a perfectly plausible-looking pattern from degrees. Feeding it 0..180 "radians"
#: wraps the sphere 28 times and puts a null every 7*pi ~ 22 units — which is how this was
#: caught, since no physical pattern is periodic in theta rather than cos(theta).
THETA = np.arange(0.0, 180.1, 2.0)
PHI = np.array([0.0, 90.0])


def face_box(axis: int, sign: int) -> csx.Box:
    lo = [-FACE_XY_MM, -FACE_XY_MM, -FACE_Z_MM]
    hi = [FACE_XY_MM, FACE_XY_MM, FACE_Z_MM]
    p1, p2 = lo[:], hi[:]
    p1[axis] = p2[axis] = hi[axis] if sign > 0 else lo[axis]
    return csx.Box(p1=tuple(p1), p2=tuple(p2))


def model() -> csx.CSXDocument:
    half = LENGTH_MM / 2.0
    h = CELL_MM / 2.0
    gap = CELL_MM

    x = build_axis([-SPAN_XY_MM, -FACE_XY_MM, -h, 0.0, h, FACE_XY_MM, SPAN_XY_MM],
                   CELL_MM, MAX_RES_MM)
    y = build_axis([-SPAN_XY_MM, -FACE_XY_MM, -h, 0.0, h, FACE_XY_MM, SPAN_XY_MM],
                   CELL_MM, MAX_RES_MM)
    z = build_axis([-SPAN_Z_MM, -FACE_Z_MM, -half, -gap / 2, 0.0, gap / 2, half,
                    FACE_Z_MM, SPAN_Z_MM], CELL_MM, MAX_RES_MM)

    doc = csx.CSXDocument(
        excitation=csx.Excitation(type=0, f0=F0, fc=FC),
        x_lines=x.tolist(), y_lines=y.tolist(), z_lines=z.tolist(),
        f_max=F0 + FC, end_criteria=1e-6, max_timesteps=60000,
    )
    doc.add(csx.Metal(name="wire", primitives=[
        csx.Box(p1=(-h, -h, -half), p2=(h, h, -gap / 2), priority=csx.PRIORITY_METAL),
        csx.Box(p1=(-h, -h, gap / 2), p2=(h, h, half), priority=csx.PRIORITY_METAL),
    ]))
    feed = csx.Box(p1=(-h, -h, -gap / 2), p2=(h, h, gap / 2), priority=csx.PRIORITY_PORT)
    doc.add(csx.LumpedElement(name="feed_res", direction=2, resistance=50.0, caps=True,
                              primitives=[feed]))
    doc.add(csx.ExcitationProperty(name="feed_exc", number=0, excite=(0.0, 0.0, 1.0),
                                   primitives=[feed]))

    for name, axis, sign in FACES:
        box = face_box(axis, sign)
        doc.add(csx.DumpBox(name=f"nf2ff_E_{name}", dump_type=csx.DUMP_E_FREQ,
                            dump_mode=1, frequencies=FREQS, primitives=[box]))
        doc.add(csx.DumpBox(name=f"nf2ff_H_{name}", dump_type=csx.DUMP_H_FREQ,
                            dump_mode=1, frequencies=FREQS, primitives=[box]))
    return doc


def write_job(wd: Path, out_h5: Path, mirror: tuple[int, float] | None) -> Path:
    """The nf2ff job. Attribute spelling comes from libnf2ff's own strings; the run checks."""
    root = ET.Element("nf2ff", {
        "Eps_r": "1", "Mue_r": "1", "Verbose": "1",
        "freq": ",".join(repr(f) for f in FREQS),
        "Outfile": str(out_h5),
        "Center": "0,0,0",
        "Radius": "1",
    })
    for name, _axis, _sign in FACES:
        if mirror is not None and name == "zn":
            continue      # the mirror replaces the lower faces
        ET.SubElement(root, "Planes", {
            "E_Field": str(wd / f"nf2ff_E_{name}.h5"),
            "H_Field": str(wd / f"nf2ff_H_{name}.h5"),
        })
    if mirror is not None:
        axis, pos = mirror
        # Metres, like everything else nf2ff reads: the dump meshes are in metres, not in
        # the model's drawing unit. mm here is silently accepted and moves the image plane
        # a thousand wavelengths away.
        ET.SubElement(root, "Mirror",
                      {"Type": "PEC", "Dir": str(axis), "Pos": repr(pos / 1000.0)})
    ET.SubElement(root, "theta").text = ",".join(repr(float(t)) for t in np.deg2rad(THETA))
    ET.SubElement(root, "phi").text = ",".join(repr(float(p)) for p in np.deg2rad(PHI))

    path = wd / ("nf2ff_job_mirror.xml" if mirror else "nf2ff_job.xml")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def pattern(h5: Path, k: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """theta (rad), phi (rad) and the radiated power density for frequency index ``k``."""
    with h5py.File(h5, "r") as f:
        theta = f["/Mesh/theta"][:].astype(float)      # radians, as sent
        phi = f["/Mesh/phi"][:].astype(float)
        prad = f[f"/nf2ff/P_rad/FD/f{k}"][:]        # (phi, theta), W/sr
    if theta.size != THETA.size or phi.size != PHI.size:
        raise SystemExit(
            f"nf2ff returned {theta.size} theta x {phi.size} phi, not "
            f"{THETA.size} x {PHI.size}: the job's angles were not read")
    return theta, phi, prad


def directivity(h5: Path, k: int, hemisphere: bool = False) -> tuple[float, np.ndarray]:
    """Peak directivity in dBi, and D(theta) in the first phi cut.

    The pattern is rotationally symmetric about a z-directed dipole, so the total radiated
    power is 2*pi times the theta integral of one cut. ``hemisphere`` integrates only
    theta <= 90 deg, which is what a PEC mirror leaves.
    """
    theta, _phi, prad = pattern(h5, k)
    cut = prad[0]
    sel = theta <= np.pi / 2 + 1e-9 if hemisphere else np.ones_like(theta, dtype=bool)
    total = 2.0 * np.pi * np.trapezoid(cut[sel] * np.sin(theta[sel]), theta[sel])
    d = 4.0 * np.pi * cut / total
    return float(10 * np.log10(d.max())), d


def show(h5: Path) -> None:
    """Print the file's layout once, so the parser is written against what is there."""
    with h5py.File(h5, "r") as f:
        print("  attrs:", {k: f.attrs[k] for k in f.attrs})

        def walk(name, obj):
            kind = "group" if isinstance(obj, h5py.Group) else f"{obj.shape} {obj.dtype}"
            print(f"    /{name}  {kind}")
        f.visititems(walk)


def main() -> None:
    doc = model()
    problems = doc.validate()
    if problems:
        raise SystemExit("; ".join(problems))
    wd = OUT / "nf2ff_dipole"
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "model.xml").write_text(doc.to_string())
    print(f"{doc.cell_count():,} cells, faces at +-{FACE_XY_MM:.0f}/{FACE_Z_MM:.0f} mm",
          flush=True)

    if not (wd / "nf2ff_E_zp.h5").exists() or os.environ.get("RESOLVE"):
        r = run.run_openems(str(wd / "model.xml"), str(wd),
                            threads=int(os.environ.get("THREADS", "8")))
        print(f"{r.final_timestep:,} steps, {r.elapsed_s:.0f}s, "
              f"energy {r.final_energy_db:.1f} dB", flush=True)
        for w in r.warnings:
            print(f"  warning: {w}")
    else:
        print("reusing the existing dumps", flush=True)

    for name, _a, _s in FACES:
        for kind in ("E", "H"):
            p = wd / f"nf2ff_{kind}_{name}.h5"
            if not p.exists():
                raise SystemExit(f"missing dump {p}")
        print(f"  {name}: {(wd / f'nf2ff_E_{name}.h5').stat().st_size / 1e6:.1f} MB")

    out_h5 = wd / "ff.h5"
    out_h5.unlink(missing_ok=True)
    job = write_job(wd, out_h5, None)
    print(f"\nrunning nf2ff on {job.name}", flush=True)
    p = subprocess.run([NF2FF_BIN, str(job)], capture_output=True, text=True)
    print(p.stdout[-3000:])
    if p.stderr.strip():
        print("stderr:", p.stderr[-2000:])
    if p.returncode != 0 or not out_h5.exists():
        raise SystemExit(f"nf2ff returned {p.returncode} and "
                         f"{'wrote' if out_h5.exists() else 'wrote no'} output")
    show(out_h5)

    print("\nFree-space directivity against theory (a thin half-wave dipole is 2.15 dBi)")
    print("\n  f (MHz)    D_max dBi   vs 2.15   peak at theta")
    result: dict = {"free_space": {}}
    for k, f in enumerate(FREQS):
        dmax, d = directivity(out_h5, k)
        peak = float(THETA[int(np.argmax(d))])
        print(f"  {f/1e6:7.1f}   {dmax:9.2f}   {dmax - 2.15:+7.2f}   {peak:9.0f} deg")
        result["free_space"][f"{f:.0f}"] = {"d_max_dbi": dmax, "peak_theta_deg": peak}

    # The ground plane. A PEC mirror under the box images a z-directed element in phase, so
    # the pattern is the dipole times a two-element array factor with the image spacing.
    out_m = wd / "ff_mirror.h5"
    out_m.unlink(missing_ok=True)
    job_m = write_job(wd, out_m, (2, -FACE_Z_MM))
    print(f"\nrunning nf2ff on {job_m.name} (PEC mirror at z = {-FACE_Z_MM:.0f} mm)",
          flush=True)
    pm = subprocess.run([NF2FF_BIN, str(job_m)], capture_output=True, text=True)
    if pm.returncode != 0 or not out_m.exists():
        print(pm.stdout[-2000:], pm.stderr[-2000:])
        raise SystemExit(f"nf2ff mirror job returned {pm.returncode}")

    k = 1                                   # the resonance
    lam = 299792458.0 / FREQS[k] * 1e3      # mm
    spacing = 2 * FACE_Z_MM                 # dipole at 0, image at -2*FACE_Z, in phase
    theta, _phi, prad = pattern(out_m, k)
    upper = theta <= np.pi / 2 - 1e-9

    # Image theory: the half-wave dipole's own element pattern times a two-element array
    # factor. Comparing against the array factor alone is not a test of anything.
    with np.errstate(divide="ignore", invalid="ignore"):
        elem = np.where(np.sin(theta) > 1e-9,
                        (np.cos(np.pi / 2 * np.cos(theta)) / np.sin(theta)) ** 2, 0.0)
    af = np.cos(np.pi * spacing / lam * np.cos(theta)) ** 2
    pred = elem * af

    cut = prad[0] / prad[0].max()
    pred = pred / pred[upper].max()
    with np.errstate(divide="ignore"):
        got_db = 10 * np.log10(np.maximum(cut, 1e-12))
        pred_db = 10 * np.log10(np.maximum(pred, 1e-12))
    # Deep nulls move by a fraction of a degree and swing tens of dB; compare where there is
    # something to compare.
    live = upper & (pred_db > -20.0)
    err = got_db[live] - pred_db[live]

    print(f"\n  image spacing {spacing:.0f} mm = {spacing / lam:.2f} lambda at "
          f"{FREQS[k] / 1e6:.1f} MHz")
    print("\n  theta   pattern dB   image theory dB    error")
    for j in range(0, int(upper.sum()), 4):
        mark = "" if pred_db[j] > -20 else "   (null)"
        print(f"  {np.rad2deg(theta[j]):5.0f}   {got_db[j]:10.1f}   {pred_db[j]:15.1f}"
              f"   {got_db[j] - pred_db[j]:+7.1f}{mark}")
    print(f"\n  over {live.sum()} angles within 20 dB of the peak: "
          f"median |error| {np.median(np.abs(err)):.2f} dB, worst {np.abs(err).max():.2f} dB")
    result["mirror"] = {"spacing_mm": spacing, "median_err_db": float(np.median(np.abs(err))),
                        "worst_err_db": float(np.abs(err).max())}

    (OUT / "m0_nf2ff.json").write_text(json.dumps(result, indent=2))
    print(f"\nwrote {OUT}/m0_nf2ff.json")


if __name__ == "__main__":
    main()
