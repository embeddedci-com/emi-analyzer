"""The near-field to far-field transform, and the three ways M0 got a confident wrong answer.

Every test here checks that the job asks the question it means to, separately from checking any
number — because `nf2ff` echoes its input angles into its output and will happily transform a
job written in the wrong units into a plausible-looking pattern.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from emi_worker.openems import csx
from emi_worker.openems.nf2ff import (
    FACE_FRACTION,
    FACES,
    PHI_DEG,
    THETA_DEG,
    Faces,
    NF2FFError,
    add_dumps,
    plan_faces,
    write_job,
)


class FakeMesh:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = np.asarray(x), np.asarray(y), np.asarray(z)


def _mesh(x0=-10.0, x1=30.0, y0=-10.0, y1=30.0, z0=-8.0, z1=8.0):
    return FakeMesh(np.linspace(x0, x1, 80), np.linspace(y0, y1, 80),
                    np.linspace(z0, z1, 40))


def test_faces_sit_between_the_region_and_the_boundary():
    roi = (0.0, 0.0, 20.0, 20.0)
    f = plan_faces(_mesh(), roi)
    # Outside the region -- a face cutting through the board would enclose nothing.
    assert f.x0 < roi[0] and f.x1 > roi[2]
    assert f.y0 < roi[1] and f.y1 > roi[3]
    # Inside the grid, with room to spare, so no face sits in the absorbing boundary.
    m = _mesh()
    assert f.x0 > m.x.min() and f.x1 < m.x.max()
    assert f.z0 > m.z.min() and f.z1 < m.z.max()


def test_faces_follow_an_asymmetric_grid():
    """A cable port extends the domain on one side only (§7).

    A box placed symmetrically around the region would then sit outside the grid on the
    extended side -- or, worse, well inside the structure on the other, which is the reactive
    near field M0 measured as giving a pattern no box that size can produce.
    """
    roi = (0.0, 0.0, 20.0, 20.0)
    m = FakeMesh(np.linspace(-500.0, 30.0, 200), np.linspace(-10.0, 30.0, 80),
                 np.linspace(-8.0, 8.0, 40))
    f = plan_faces(m, roi)
    assert f.x0 < -300.0            # it followed the extension west
    assert 20.0 < f.x1 < 30.0       # and did not follow it east
    assert f.x0 > m.x.min()


def test_every_face_is_a_plane_and_the_box_is_closed():
    f = plan_faces(_mesh(), (0.0, 0.0, 20.0, 20.0))
    seen = set()
    for name, axis, sign in FACES:
        box = f.box(axis, sign)
        assert box.p1[axis] == box.p2[axis], f"{name} is not flat"
        for other in range(3):
            if other != axis:
                assert box.p1[other] != box.p2[other], f"{name} has no extent in {other}"
        seen.add((axis, sign))
    assert len(seen) == 6


def test_dumps_are_frequency_domain_and_paired():
    doc = csx.CSXDocument(excitation=csx.Excitation(type=0, f0=5e8, fc=4e8),
                          x_lines=[0, 1], y_lines=[0, 1], z_lines=[0, 1], f_max=9e8)
    freqs = [30e6, 100e6, 1e9]
    names = add_dumps(doc, plan_faces(_mesh(), (0.0, 0.0, 20.0, 20.0)), freqs)

    assert len(names) == 12
    assert sum(n.startswith("nf2ff_E_") for n in names) == 6
    assert sum(n.startswith("nf2ff_H_") for n in names) == 6

    xml = ET.fromstring(doc.to_string())
    dumps = [d for d in xml.iter("DumpBox") if d.get("Name", "").startswith("nf2ff_")]
    assert len(dumps) == 12
    for d in dumps:
        # A time-domain dump writes every timestep and reaches hundreds of gigabytes.
        assert d.get("DumpType") in {"10", "11"}
        assert d.find("FD_Samples") is not None


def test_no_frequencies_is_refused():
    doc = csx.CSXDocument(excitation=csx.Excitation(type=0, f0=5e8, fc=4e8),
                          x_lines=[0, 1], y_lines=[0, 1], z_lines=[0, 1], f_max=9e8)
    with pytest.raises(NF2FFError, match="at least one frequency"):
        add_dumps(doc, plan_faces(_mesh(), (0.0, 0.0, 20.0, 20.0)), [])


def _stub_dumps(wd: Path, drop: str | None = None) -> None:
    for name, _a, _s in FACES:
        if name == drop:
            continue
        for kind in ("E", "H"):
            (wd / f"nf2ff_{kind}_{name}.h5").write_bytes(b"")


def test_the_job_is_written_in_radians_and_metres(tmp_path):
    _stub_dumps(tmp_path)
    job = write_job(str(tmp_path), [100e6], str(tmp_path / "ff.h5"),
                    centre_mm=(10.0, 20.0, 0.0), radius_m=3.0, mirror_z_m=-0.8)
    root = ET.parse(job).getroot()

    theta = [float(v) for v in root.find("theta").text.split(",")]
    assert max(theta) <= np.pi + 1e-9, "angles were written in degrees"
    assert np.allclose(np.rad2deg(theta), THETA_DEG)
    phi = [float(v) for v in root.find("phi").text.split(",")]
    assert np.allclose(np.rad2deg(phi), PHI_DEG)

    # Metres, not the model's millimetres. mm moves the image plane a thousand wavelengths.
    assert root.get("Center") == "0.01,0.02,0.0"
    assert root.find("Mirror").get("Pos") == "-0.8"
    assert root.find("Mirror").get("Type") == "PEC"
    assert root.get("Radius") == "3.0"


def test_a_mirror_replaces_the_lower_face():
    """Keeping both counts the structure twice: the image *is* the lower hemisphere."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        wd = Path(d)
        _stub_dumps(wd)
        plain = ET.parse(write_job(str(wd), [100e6], str(wd / "a.h5"))).getroot()
        assert len(plain.findall("Planes")) == 6

        mirrored = ET.parse(
            write_job(str(wd), [100e6], str(wd / "b.h5"), mirror_z_m=-0.8)).getroot()
        assert len(mirrored.findall("Planes")) == 5
        assert not any("nf2ff_E_zn" in p.get("E_Field") for p in mirrored.findall("Planes"))


def test_a_missing_face_is_refused_rather_than_transformed(tmp_path):
    """Surface equivalence needs the surface closed. Five faces is not a smaller answer."""
    _stub_dumps(tmp_path, drop="yp")
    with pytest.raises(NF2FFError, match="surface closed"):
        write_job(str(tmp_path), [100e6], str(tmp_path / "ff.h5"))


def test_the_scan_angles_cover_a_height_sweep_and_a_full_turntable():
    # 1-4 m of antenna height at 3 m distance is roughly 18-53 degrees above the horizon,
    # and the horizon (90 deg) is the table plane.
    assert THETA_DEG.min() <= 30.0 and THETA_DEG.max() == 90.0
    assert PHI_DEG.min() == 0.0 and PHI_DEG.max() >= 345.0
    assert len(PHI_DEG) >= 24


def test_faces_box_corners_are_ordered_for_every_face():
    f = Faces(x0=-1.0, y0=-2.0, z0=-3.0, x1=4.0, y1=5.0, z1=6.0)
    for _name, axis, sign in FACES:
        box = f.box(axis, sign)
        for k in range(3):
            assert box.p1[k] <= box.p2[k]


def test_faces_are_sub_sampled():
    """Full face resolution is gigabytes; a quarter is tens of megabytes and costs nothing.

    The NF2FF surface has to resolve the *wavelength*, not the copper. M0 measured both halves
    of that: 1.1-4.0 GB full against 0.07-0.25 GB at a quarter, and a quarter of even a 50 um
    board mesh is 200 um against the 300 mm a wavelength is at 1 GHz.
    """
    from emi_worker.openems.nf2ff import FACE_SUB_SAMPLING

    doc = csx.CSXDocument(excitation=csx.Excitation(type=0, f0=5e8, fc=4e8),
                          x_lines=[0, 1], y_lines=[0, 1], z_lines=[0, 1], f_max=9e8)
    add_dumps(doc, plan_faces(_mesh(), (0.0, 0.0, 20.0, 20.0)), [100e6])
    xml = ET.fromstring(doc.to_string())
    dumps = [d for d in xml.iter("DumpBox") if d.get("Name", "").startswith("nf2ff_")]
    assert dumps
    for d in dumps:
        assert d.get("SubSampling") == f"{FACE_SUB_SAMPLING},{FACE_SUB_SAMPLING},{FACE_SUB_SAMPLING}"


def test_a_stride_below_one_is_refused():
    with pytest.raises(ValueError, match="stride in grid lines"):
        csx.DumpBox(name="bad", dump_type=csx.DUMP_E_FREQ, frequencies=[1e9],
                    sub_sampling=0, primitives=[csx.Box(p1=(0, 0, 0), p2=(1, 1, 0))]).to_xml()


def test_an_ordinary_dump_still_writes_no_sub_sampling_attribute():
    """Every existing dump -- the J maps the tool has always produced -- is untouched."""
    el = csx.DumpBox(name="Jf_F_Cu", dump_type=csx.DUMP_J_FREQ, frequencies=[1e9],
                     primitives=[csx.Box(p1=(0, 0, 0), p2=(1, 1, 0))]).to_xml()
    assert "SubSampling" not in el.attrib


# ---- the grid the far field is evaluated on -------------------------------------------

def test_the_far_field_grid_spans_the_radiated_band():
    from emi_worker.openems.model import FAR_FIELD_POINTS, RADIATED_MIN_HZ, far_field_grid

    g = far_field_grid(1e9)
    assert len(g) == FAR_FIELD_POINTS
    assert g[0] == pytest.approx(RADIATED_MIN_HZ)
    assert g[-1] == pytest.approx(1e9)
    # Log-spaced: the limits, the cable resonances and the board's modes all are, and a linear
    # grid would spend half its points above 500 MHz where nothing changes quickly.
    ratios = [g[k + 1] / g[k] for k in range(len(g) - 1)]
    assert max(ratios) - min(ratios) < 1e-9


def test_the_grid_never_asks_for_a_frequency_the_solve_did_not_contain():
    """The transform returns a number for any frequency it is given, and above the excitation
    band that number is noise."""
    from emi_worker.openems.model import far_field_grid

    assert max(far_field_grid(200e6)) == pytest.approx(200e6)


def test_a_solve_below_the_radiated_band_still_produces_a_usable_grid():
    from emi_worker.openems.model import RADIATED_MIN_HZ, far_field_grid

    g = far_field_grid(1e6)
    assert g[0] == pytest.approx(RADIATED_MIN_HZ)
    assert g[-1] > g[0]


def test_the_job_asks_for_the_standard_distance_directly(tmp_path):
    """`Radius` is where E is evaluated, and E scales exactly as 1/r.

    Measured on M0's dipole dumps: peak |E| of 1.037e-11, 3.456e-12 and 1.037e-12 V/m at 1, 3
    and 10 m. So the distance belongs in the job rather than as a correction applied later --
    a 1 m field compared against a 3 m limit is 9.5 dB optimistic, and nothing in the output
    file would say so.
    """
    _stub_dumps(tmp_path)
    for distance in (3.0, 10.0):
        job = write_job(str(tmp_path), [100e6], str(tmp_path / "ff.h5"), radius_m=distance)
        assert float(ET.parse(job).getroot().get("Radius")) == pytest.approx(distance)
