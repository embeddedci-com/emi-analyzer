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
    FACES,
    MIN_LINES_OUTSIDE,
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


def _mesh(x0=-60.0, x1=80.0, y0=-60.0, y1=80.0, z0=-60.0, z1=62.0):
    return FakeMesh(np.linspace(x0, x1, 141), np.linspace(y0, y1, 141),
                    np.linspace(z0, z1, 123))


#: A 20 x 20 mm board, 1.6 mm thick, as the copper extent plan_faces takes.
COPPER = (0.0, 0.0, 20.0, 20.0, 0.0, 1.6)


def test_faces_sit_the_clearance_outside_the_copper():
    f = plan_faces(_mesh(), COPPER, 25.0)
    assert (f.x0, f.y0, f.z0) == pytest.approx((-25.0, -25.0, -25.0))
    # 26.6 is between grid lines; the face goes out to the next one, never in.
    assert (f.x1, f.y1, f.z1) == pytest.approx((45.0, 45.0, 27.0))


def test_faces_lie_on_grid_lines():
    """openEMS snaps a dump to the nearest line anyway, possibly inward. Snapping here, outward,
    means the clearance is never less than asked for and the result records where the faces
    really were."""
    m = FakeMesh(np.linspace(-60.0, 80.0, 71), np.linspace(-60.0, 80.0, 57),
                 np.linspace(-60.0, 62.0, 45))
    f = plan_faces(m, COPPER, 25.0)
    for value, lines in ((f.x0, m.x), (f.x1, m.x), (f.y0, m.y), (f.y1, m.y), (f.z0, m.z),
                         (f.z1, m.z)):
        assert np.min(np.abs(lines - value)) < 1e-9
    assert f.x0 <= -25.0 and f.x1 >= 45.0 and f.z1 >= 26.6


def test_a_face_in_the_absorbing_boundary_is_refused():
    """The grid used to end at the region in x and y, so the side faces sat on its outermost
    lines -- inside the PML, sampling the absorber. That is now a refusal."""
    tight = FakeMesh(np.linspace(0.0, 20.0, 80), np.linspace(-60.0, 80.0, 141),
                     np.linspace(-60.0, 62.0, 123))
    with pytest.raises(NF2FFError, match="along x"):
        plan_faces(tight, COPPER, 25.0)
    # Just enough lines outside passes.
    ok = FakeMesh(np.concatenate([np.linspace(-40.0, -26.0, MIN_LINES_OUTSIDE),
                                  np.linspace(-25.0, 45.0, 50),
                                  np.linspace(46.0, 60.0, MIN_LINES_OUTSIDE)]),
                  np.linspace(-60.0, 80.0, 141), np.linspace(-60.0, 62.0, 123))
    plan_faces(ok, COPPER, 25.0)


def test_faces_follow_an_asymmetric_copper_extent():
    """A cable stub extends the copper on one side only (§7), and the box encloses it."""
    m = FakeMesh(np.linspace(-600.0, 80.0, 700), np.linspace(-60.0, 80.0, 141),
                 np.linspace(-60.0, 62.0, 123))
    f = plan_faces(m, (-500.0, 0.0, 20.0, 20.0, 0.0, 1.6), 25.0)
    cell = 680.0 / 699
    assert -525.0 - cell < f.x0 <= -525.0
    assert 45.0 <= f.x1 < 45.0 + cell


def test_every_face_is_a_plane_and_the_box_is_closed():
    f = plan_faces(_mesh(), COPPER, 25.0)
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
    names = add_dumps(doc, plan_faces(_mesh(), COPPER, 25.0), freqs)

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
        add_dumps(doc, plan_faces(_mesh(), COPPER, 25.0), [])


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


def test_a_mirror_below_the_box_keeps_every_face():
    """The ground plane sits 0.8 m under a box that ends millimetres below the board.

    Dropping the lower face there left the surface open, so the transform integrated five of
    the six faces the field crosses. Only a mirror lying *on* the lower face replaces it.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        wd = Path(d)
        _stub_dumps(wd)
        plain = ET.parse(write_job(str(wd), [100e6], str(wd / "a.h5"))).getroot()
        assert len(plain.findall("Planes")) == 6

        below = ET.parse(write_job(str(wd), [100e6], str(wd / "b.h5"), mirror_z_m=-0.8,
                                   lower_face_z_m=-0.03)).getroot()
        assert len(below.findall("Planes")) == 6
        assert below.find("Mirror").get("Pos") == "-0.8"

        # And without saying where the face is, the face is never assumed away.
        unknown = ET.parse(write_job(str(wd), [100e6], str(wd / "c.h5"),
                                     mirror_z_m=-0.8)).getroot()
        assert len(unknown.findall("Planes")) == 6


def test_a_mirror_on_the_lower_face_replaces_it():
    """M0's configuration: the symmetry plane is the face, so the image is the lower half."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        wd = Path(d)
        _stub_dumps(wd, drop="zn")
        on = ET.parse(write_job(str(wd), [100e6], str(wd / "b.h5"), mirror_z_m=-0.03,
                                lower_face_z_m=-0.03)).getroot()
        assert len(on.findall("Planes")) == 5
        assert not any("nf2ff_E_zn" in p.get("E_Field") for p in on.findall("Planes"))


def test_a_box_reaching_through_the_ground_plane_is_refused(tmp_path):
    _stub_dumps(tmp_path)
    with pytest.raises(NF2FFError, match="reaches through"):
        write_job(str(tmp_path), [100e6], str(tmp_path / "a.h5"), mirror_z_m=-0.8,
                  lower_face_z_m=-1.0)


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


def test_faces_are_thinned_to_a_spacing_not_strided():
    """Full face resolution is gigabytes, so the faces are thinned -- by spacing, not by stride.

    A stride of every 4th line counts from the box's first line and drops whatever does not fit
    at the far end, which left the production box with a 14 mm slot around its rim on the
    fixture board. ``OptResolution`` keeps both ends (``scan.read_surface`` checks that it did).
    """
    from emi_worker.openems.nf2ff import FACE_RESOLUTION_MM, face_resolution_mm

    doc = csx.CSXDocument(excitation=csx.Excitation(type=0, f0=5e8, fc=4e8),
                          x_lines=[0, 1], y_lines=[0, 1], z_lines=[0, 1], f_max=9e8)
    add_dumps(doc, plan_faces(_mesh(), COPPER, 25.0), [100e6])
    xml = ET.fromstring(doc.to_string())
    dumps = [d for d in xml.iter("DumpBox") if d.get("Name", "").startswith("nf2ff_")]
    assert dumps
    for d in dumps:
        assert "SubSampling" not in d.attrib
        assert d.get("OptResolution") == "5,5,5"
    assert FACE_RESOLUTION_MM == 5.0
    assert face_resolution_mm(1e9) == 5.0
    # A twentieth of the wavelength once that is finer.
    assert face_resolution_mm(6e9) == pytest.approx(2.498, abs=1e-3)


def test_a_resolution_that_is_not_positive_is_refused():
    with pytest.raises(ValueError, match="resolution"):
        csx.DumpBox(name="bad", dump_type=csx.DUMP_E_FREQ, frequencies=[1e9],
                    opt_resolution_mm=0.0,
                    primitives=[csx.Box(p1=(0, 0, 0), p2=(1, 1, 0))]).to_xml()


def test_a_stride_below_one_is_refused():
    with pytest.raises(ValueError, match="stride in grid lines"):
        csx.DumpBox(name="bad", dump_type=csx.DUMP_E_FREQ, frequencies=[1e9],
                    sub_sampling=0, primitives=[csx.Box(p1=(0, 0, 0), p2=(1, 1, 0))]).to_xml()


def test_an_ordinary_dump_still_writes_no_sub_sampling_attribute():
    """Every existing dump -- the J maps the tool has always produced -- is untouched."""
    el = csx.DumpBox(name="Jf_F_Cu", dump_type=csx.DUMP_J_FREQ, frequencies=[1e9],
                     primitives=[csx.Box(p1=(0, 0, 0), p2=(1, 1, 0))]).to_xml()
    assert "SubSampling" not in el.attrib
    assert "OptResolution" not in el.attrib


# ---- the grid the far field is evaluated on -------------------------------------------

def test_the_far_field_grid_spans_the_solved_radiated_band():
    from emi_worker.openems.model import FAR_FIELD_POINTS, RADIATED_MIN_HZ, far_field_grid

    g = far_field_grid(10e6, 1e9)
    assert len(g) == FAR_FIELD_POINTS
    assert g[0] == pytest.approx(RADIATED_MIN_HZ)
    assert g[-1] == pytest.approx(1e9)
    # Log-spaced: the limits, the cable resonances and the board's modes all are, and a linear
    # grid would spend half its points above 500 MHz where nothing changes quickly.
    ratios = [g[k + 1] / g[k] for k in range(len(g) - 1)]
    assert max(ratios) - min(ratios) < 1e-9


def test_the_grid_never_asks_for_a_frequency_the_solve_did_not_contain():
    """The transform returns a number for any frequency it is given, and outside the excited
    band -- above it, or below the lowest frequency the record was sized for -- that number
    is noise. The grid used to start at 30 MHz whatever the solve covered."""
    from emi_worker.openems.model import far_field_grid

    g = far_field_grid(100e6, 500e6)
    assert min(g) == pytest.approx(100e6)
    assert max(g) == pytest.approx(500e6)
    # And it used to reach 60 MHz even for a solve that stopped at 40.
    assert max(far_field_grid(30e6, 40e6)) == pytest.approx(40e6)


def test_a_solve_below_the_radiated_band_records_no_far_field():
    from emi_worker.openems.model import far_field_grid

    assert far_field_grid(1e6, 20e6) == []


def test_the_box_clearance_is_a_tenth_of_a_wavelength_at_the_top_but_never_below_25_mm():
    from emi_worker.openems.model import far_field_clearance_mm

    assert far_field_clearance_mm(1e9) == pytest.approx(29.98, abs=0.01)
    assert far_field_clearance_mm(300e6) == pytest.approx(99.93, abs=0.01)
    assert far_field_clearance_mm(6e9) == pytest.approx(25.0)


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


# ---- per volt of source ----------------------------------------------------------------

def _probe(path: Path, t, v) -> None:
    path.write_text("% time value\n" + "".join(f"{a:.9e} {b:.9e}\n" for a, b in zip(t, v)))


def test_the_source_is_in_openems_single_sided_convention(tmp_path):
    """A far field dump is 2·Σx·e^(-jωt)·Δt; the port's transform must match before dividing,
    or every far field is 6 dB high. Checked on a sine whose amplitude is known exactly."""
    from emi_worker.openems.post import OPENEMS_FD_SCALE, source_spectrum

    f0 = 100e6
    t = np.arange(0, 400) * 1.25e-10   # exactly 5 periods
    v = 1.0 * np.cos(2 * np.pi * f0 * t)
    i = 0.01 * np.cos(2 * np.pi * f0 * t)
    _probe(tmp_path / "p1_ut", t, v)
    _probe(tmp_path / "p1_it", t, i)
    out = source_spectrum(str(tmp_path), "p1", 50.0, [f0])
    # V_src = V + I·50 = 1.5 V amplitude. The single-sided transform 2·Σ·Δt of a cosine of
    # amplitude A over a record T is A·T: 1.5 V · 5e-8 s.
    assert OPENEMS_FD_SCALE == 2.0
    assert abs(out["v_src"][0]) == pytest.approx(1.5 * 400 * 1.25e-10, rel=1e-3)
    assert out["z_in"][0].real == pytest.approx(100.0, rel=1e-6)


def test_the_far_field_document_is_per_volt_and_refuses_an_empty_source():
    from emi_worker.openems.model import Port
    from emi_worker.stages.solve import FAR_FIELD_FORMAT_VERSION, far_field_document

    field = {"frequencies_hz": [100e6, 200e6], "e_max_v_per_m": [2e-3, 1e-3],
             "e_by_height_v_per_m": [[2e-3, 1e-3], [1e-3, 1e-3]]}
    source = {"v_src": np.array([4.0 + 0j, 0.0 + 0j]),
              "z_in": np.array([30 + 40j, np.nan + 0j])}
    meta = {"faces_mm": [0, 0, 0, 1, 1, 1], "face_resolution_mm": 5.0, "clearance_mm": 30.0,
            "tenth_wavelength_above_hz": 1e9}
    doc = far_field_document(field, source, [Port("p1", 0, 0, "F.Cu", resistance=50.0)], meta)
    assert doc["format_version"] == FAR_FIELD_FORMAT_VERSION == 3
    assert doc["e_per_volt"] == pytest.approx([5e-4, 0.0])
    assert doc["e_by_height_per_volt"][0] == pytest.approx([5e-4, 2.5e-4])
    assert doc["usable"] == [True, False]
    assert doc["truncated_hz"] == []
    assert doc["z_in_real"][0] == 30.0 and doc["z_in_imag"][0] == 40.0
    assert doc["source_impedance_ohm"] == 50.0
    assert doc["driven_by"] == "p1"
    assert "e_max_v_per_m" not in doc, "the raw pulse units must not look like V/m"


def test_a_port_reading_negative_resistance_is_truncation_not_a_result():
    """A passive port cannot have a negative resistance. On a real board stopped at -40 dB the
    port read -25 kOhm against |Z| = 25.5 kOhm at 30 MHz: the record ended before the board
    stopped ringing, and the far field there is the truncation."""
    from emi_worker.openems.model import Port
    from emi_worker.stages.solve import far_field_document

    field = {"frequencies_hz": [30e6, 100e6, 300e6], "e_max_v_per_m": [1e-3, 1e-3, 1e-3]}
    source = {"v_src": np.array([1.0 + 0j] * 3),
              "z_in": np.array([-25485 - 1493j, -100 - 20000j, 0.5 - 2000j])}
    doc = far_field_document(field, source, [Port("p1", 0, 0, "F.Cu")],
                             {"faces_mm": [0, 0, 0, 1, 1, 1]})
    # -0.5 % of |Z| is a complete record's numerical noise (a short dipole at 30 MHz); -99.8 %
    # is not.
    assert doc["usable"] == [False, True, True]
    assert doc["truncated_hz"] == [30e6]


def test_the_job_names_frequencies_exactly_as_the_dumps_recorded_them(tmp_path):
    """nf2ff looks each frequency up in the dump by value. The dumps record FD_Samples at nine
    significant figures, and a job written with repr() asked for 31837045.556580506 where the
    dump held 31837045.6 -- "Error, analysing Plane" on the first real far field."""
    from emi_worker.openems.model import far_field_grid

    _stub_dumps(tmp_path)
    freqs = far_field_grid(30e6, 1e9)
    doc = csx.CSXDocument(excitation=csx.Excitation(type=0, f0=5e8, fc=4e8),
                          x_lines=[0, 1], y_lines=[0, 1], z_lines=[0, 1], f_max=1e9)
    add_dumps(doc, plan_faces(_mesh(), COPPER, 25.0), freqs)
    dumped = next(d for d in ET.fromstring(doc.to_string()).iter("DumpBox")
                  if d.get("Name") == "nf2ff_E_xn").find("FD_Samples").text
    job = ET.parse(write_job(str(tmp_path), freqs, str(tmp_path / "ff.h5"))).getroot()
    assert job.get("freq") == dumped
