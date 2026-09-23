"""The scan's field from the NF2FF box: exact at 3 m, with the ground plane as an image.

The box here is written the way openEMS writes it -- twelve HDF5 dumps, (component, z, y, x),
metres -- holding the exact fields of a Hertzian dipole, so the answer is known in closed form.
The same checks on a real openEMS run are in research/ff_verify_dipole.py.
"""

from __future__ import annotations

import numpy as np
import pytest

h5py = pytest.importorskip("h5py")

from emi_worker.openems import scan  # noqa: E402

C0 = scan.C0
ETA = scan.ETA0


def dipole_eh(p, src, m, k):
    """Exact E and H of an electric current element ``m`` (A*m) at ``src``, at points ``p``."""
    d = p - src
    r = np.linalg.norm(d, axis=-1)[..., None]
    n = d / r
    g = np.exp(-1j * k * r) / (4 * np.pi * r)
    a = 1 - 1j / (k * r) - 1 / (k * r) ** 2
    b = 1 - 3j / (k * r) - 3 / (k * r) ** 2
    ndm = (n * m).sum(-1)[..., None]
    e = -1j * ETA * k * g * (m * a - n * ndm * b)
    h = (1j * k + 1 / r) * g * np.cross(m, n)
    return e, h


FREQS = [30e6, 300e6, 1e9]
HALF = 0.05          # a 100 mm box
N = 41               # 2.5 mm samples


def write_box(wd, fields, freqs=FREQS, lines=None, drop_last=None):
    """Write the twelve dumps. ``fields(points, k)`` returns (E, H) at (..., 3) points."""
    lines = lines if lines is not None else np.linspace(-HALF, HALF, N)
    for name, axis, sign in scan._FACES:
        mesh = [lines, lines, lines]
        mesh[axis] = np.array([sign * HALF])
        if drop_last is not None and name == drop_last[0]:
            mesh[drop_last[1]] = mesh[drop_last[1]][:-3]
        zz, yy, xx = np.meshgrid(mesh[2], mesh[1], mesh[0], indexing="ij")
        pts = np.stack([xx, yy, zz], axis=-1)
        for kind in ("E", "H"):
            with h5py.File(wd / f"nf2ff_{kind}_{name}.h5", "w") as f:
                for a, v in zip("xyz", mesh):
                    f[f"Mesh/{a}"] = v.astype(np.float32)
                fd = f.create_group("FieldData/FD")
                fd.attrs["frequency"] = np.asarray(freqs)
                for i, freq in enumerate(freqs):
                    e, h = fields(pts, 2 * np.pi * freq / C0)
                    v = np.moveaxis(e if kind == "E" else h, -1, 0)
                    fd[f"f{i}_real"] = v.real.astype(np.float32)
                    fd[f"f{i}_imag"] = v.imag.astype(np.float32)


def db(a, b):
    return 20 * np.log10(np.abs(a) / np.abs(b))


@pytest.mark.parametrize("m", [np.array([0.0, 0.0, 1e-3]), np.array([1e-3, 0.0, 0.0])])
def test_the_box_radiates_its_contents_exactly_at_3_m(tmp_path, m):
    """Surface equivalence with the full Green's function: the 1/r^2 and 1/r^3 terms that a
    far-field transform drops are a third of the field at 30 MHz and 3 m, and are here."""
    src = np.zeros(3)
    write_box(tmp_path, lambda p, k: dipole_eh(p, src, m, k))
    s = scan.read_surface(str(tmp_path), FREQS)
    pts = np.array([[3.0, 0.0, 0.0], [0.0, 3.0, 1.0], [2.0, -1.0, 2.5], [0.3, 0.2, 3.0]])
    e = scan.field(s, FREQS, pts)
    for fi, f in enumerate(FREQS):
        want, _ = dipole_eh(pts, src, m, 2 * np.pi * f / C0)
        for p in range(len(pts)):
            if np.linalg.norm(want[p]) < 1e-3 * np.abs(want).max():
                continue
            assert db(np.linalg.norm(e[fi, p]), np.linalg.norm(want[p])) == \
                pytest.approx(0.0, abs=0.05), (f, pts[p])


@pytest.mark.parametrize("m,image", [
    (np.array([0.0, 0.0, 1e-3]), np.array([0.0, 0.0, 1e-3])),     # vertical: in phase
    (np.array([1e-3, 0.0, 0.0]), np.array([-1e-3, 0.0, 0.0])),    # horizontal: reversed
])
def test_the_ground_plane_is_the_image_of_the_currents(tmp_path, m, image):
    """A PEC plane reverses the horizontal part of an electric current. nf2ff's own mirror got
    the horizontal case wrong -- 19 dB high at 30 MHz on a horizontal dipole -- so this is the
    case that matters."""
    src = np.zeros(3)
    ground = -0.8
    write_box(tmp_path, lambda p, k: dipole_eh(p, src, m, k))
    s = scan.read_surface(str(tmp_path), FREQS)
    pts = scan.ring((0.0, 0.0), 3.0, ground, heights_m=(1.0, 2.5, 4.0), azimuths=4)
    e = scan.field(s, FREQS, pts, ground_z_m=ground)
    for fi, f in enumerate(FREQS):
        k = 2 * np.pi * f / C0
        want = dipole_eh(pts, src, m, k)[0] + dipole_eh(pts, np.array([0, 0, 2 * ground]),
                                                        image, k)[0]
        got, ref = scan.reading(e[fi][None])[0], scan.reading(want[None])[0]
        live = ref > 1e-3 * ref.max()
        assert np.abs(db(got[live], ref[live])).max() < 0.05, f


def test_an_open_box_is_refused(tmp_path):
    """openEMS's stride sub-sampling dropped the last lines of a face, which left a slot around
    the rim of the production box. A surface with a slot in it encloses nothing."""
    write_box(tmp_path, lambda p, k: dipole_eh(p, np.zeros(3), np.array([0, 0, 1e-3]), k),
              drop_last=("xn", 2))
    with pytest.raises(scan.ScanError, match="box is open: face xn"):
        scan.read_surface(str(tmp_path), FREQS)


def test_a_frequency_the_dumps_did_not_record_is_refused(tmp_path):
    write_box(tmp_path, lambda p, k: dipole_eh(p, np.zeros(3), np.array([0, 0, 1e-3]), k))
    with pytest.raises(scan.ScanError, match="no field at 50 MHz"):
        scan.read_surface(str(tmp_path), [50e6])


def test_a_ground_plane_inside_the_box_is_refused(tmp_path):
    write_box(tmp_path, lambda p, k: dipole_eh(p, np.zeros(3), np.array([0, 0, 1e-3]), k))
    s = scan.read_surface(str(tmp_path), FREQS)
    with pytest.raises(scan.ScanError, match="above the bottom of the box"):
        scan.field(s, FREQS, np.array([[3.0, 0, 0]]), ground_z_m=0.0)


def test_the_scan_is_1_to_4_m_up_all_the_way_round():
    pts = scan.ring((0.1, 0.2), 3.05, -0.8)
    assert len(pts) == len(scan.SCAN_HEIGHTS_M) * scan.SCAN_AZIMUTHS
    heights = pts[:, 2] + 0.8
    assert heights.min() == pytest.approx(1.0) and heights.max() == pytest.approx(4.0)
    # At 1 GHz from a board 0.8 m up the height lobes are about 0.56 m apart; a 1 m step can
    # land between two of them.
    assert np.diff(scan.SCAN_HEIGHTS_M).max() <= 0.1 + 1e-9
    r = np.hypot(pts[:, 0] - 0.1, pts[:, 1] - 0.2)
    assert r == pytest.approx(3.05)


def test_the_reading_is_the_better_polarisation_not_the_total():
    e = np.array([[[3.0, 4.0, 1.0], [0.0, 0.0, 2.0]]])
    assert scan.reading(e)[0] == pytest.approx([5.0, 2.0])
