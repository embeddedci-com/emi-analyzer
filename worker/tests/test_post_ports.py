"""Port spectra and manifest v2 — what makes a solve re-weightable (§8, §10).

These are the numbers a driver turns into absolute dBµA/m. A solve that records them
slightly wrong is worse than one that records nothing, because nothing is visible.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from emi_worker.openems import post
from emi_worker.drivers.spectrum import reweight


def _write_probe(path: Path, times: np.ndarray, values: np.ndarray) -> None:
    lines = ["% openEMS probe, written by a test"]
    lines += [f"{t:.12e}\t{v:.12e}" for t, v in zip(times, values)]
    path.write_text("\n".join(lines) + "\n")


def _tone(freq: float, amplitude: float, phase: float = 0.0, n: int = 4000,
          dt: float = 2e-12) -> tuple[np.ndarray, np.ndarray]:
    """A windowed tone, so its transform is concentrated and its phase is meaningful."""
    t = np.arange(n) * dt
    window = np.hanning(n)
    return t, amplitude * window * np.cos(2 * np.pi * freq * t + phase)


def _write_dump(path: Path, frequencies: list[float]) -> None:
    import h5py

    with h5py.File(path, "w") as f:
        mesh = f.create_group("Mesh")
        mesh["x"] = np.linspace(0, 0.01, 5)
        mesh["y"] = np.linspace(0, 0.01, 4)
        mesh["z"] = np.array([0.0015])
        fd = f.create_group("FieldData/FD")
        fd.attrs["frequency"] = np.asarray(frequencies, dtype=np.float64)
        for i in range(len(frequencies)):
            fd[f"f{i}_real"] = np.full((3, 1, 4, 5), 1.0 + i)
            fd[f"f{i}_imag"] = np.zeros((3, 1, 4, 5))


# ---- the dense grid --------------------------------------------------------------------

def test_dense_grid_spans_the_requested_band_and_no_further():
    """Below f_min the record is shorter than the three periods the run was sized for, so
    an answer there would be confidently wrong rather than absent."""
    grid = post.dense_grid([100e6, 300e6, 900e6])
    assert grid[0] == pytest.approx(100e6)
    assert grid[-1] == pytest.approx(900e6)
    assert len(grid) == post.DENSE_GRID_POINTS
    assert all(b > a for a, b in zip(grid, grid[1:]))


def test_dense_grid_is_logarithmic():
    grid = post.dense_grid([10e6, 1e9], points=7)
    ratios = [b / a for a, b in zip(grid, grid[1:])]
    for r in ratios:
        assert r == pytest.approx(ratios[0], rel=1e-12)


def test_dense_grid_of_one_frequency_is_that_frequency():
    assert post.dense_grid([250e6]) == [250e6]


# ---- port spectra ----------------------------------------------------------------------

def test_port_spectra_keep_phase():
    """Re-weighting divides by the port current, so dropping phase would silently discard
    the reactive part of every input impedance."""
    t, v = _tone(200e6, 1.0, phase=0.0)
    _t2, i = _tone(200e6, 0.02, phase=math.pi / 2)
    out = post.port_spectra(post.ProbeTrace(t, v), post.ProbeTrace(t, i), [200e6])
    z = complex(out["v_real"][0], out["v_imag"][0]) / complex(out["i_real"][0], out["i_imag"][0])
    # 90 degrees between voltage and current is a purely reactive impedance. The tolerance
    # is in degrees rather than on the real part: a finite windowed record leaks a little
    # energy across the bin, and a threshold on |Re| would be pinning that leakage rather
    # than the phase this test is about.
    assert abs(math.degrees(math.atan2(z.imag, z.real)) + 90.0) < 2.0


def test_port_spectra_recover_a_known_impedance():
    t, v = _tone(200e6, 2.0)
    _t2, i = _tone(200e6, 0.02)
    out = post.port_spectra(post.ProbeTrace(t, v), post.ProbeTrace(t, i), [200e6])
    z = complex(out["v_real"][0], out["v_imag"][0]) / complex(out["i_real"][0], out["i_imag"][0])
    assert z.real == pytest.approx(100.0, rel=1e-6)


def test_port_spectra_feed_reweight_directly():
    """The contract between post.py and drivers/spectrum.py, in one test."""
    t, v = _tone(200e6, 2.0)
    _t2, i = _tone(200e6, 0.02)
    out = post.port_spectra(post.ProbeTrace(t, v), post.ProbeTrace(t, i), [200e6])
    v_port = complex(out["v_real"][0], out["v_imag"][0])
    i_port = complex(out["i_real"][0], out["i_imag"][0])
    factor = reweight(1.0 + 0j, 50.0, v_port, i_port)
    # Z_in = 100 ohm, so a 1 V source behind 50 ohm pushes 1/150 A against the solve's own.
    assert abs(factor) == pytest.approx(abs((1 / 150) / i_port), rel=1e-6)


# ---- the artifact set ------------------------------------------------------------------

@pytest.fixture()
def solved(tmp_path: Path) -> Path:
    freqs = [200e6, 400e6]
    _write_dump(tmp_path / "Hf_F_Cu.h5", freqs)
    t, v = _tone(200e6, 2.0)
    _t2, i = _tone(200e6, 0.02)
    _write_probe(tmp_path / "p1_ut", t, v)
    _write_probe(tmp_path / "p1_it", t, i)
    return tmp_path


def test_manifest_is_version_2_and_declares_port_spectra(solved):
    art = post.build_artifacts(str(solved), {"F.Cu": "Hf_F_Cu"}, [200e6, 400e6], ["p1"])
    assert art.manifest["format_version"] == 2
    assert art.manifest["has_port_spectra"] is True
    assert "ports.json" in art.files


def test_ports_json_carries_both_grids(solved):
    art = post.build_artifacts(str(solved), {"F.Cu": "Hf_F_Cu"}, [200e6, 400e6], ["p1"])
    ports = json.loads(art.files["ports.json"])["ports"]
    assert [p["port"] for p in ports] == ["p1"]
    at_dumps = ports[0]["at_dump_frequencies"]
    assert at_dumps["frequencies_hz"] == [200e6, 400e6]
    for key in ("v_real", "v_imag", "i_real", "i_imag"):
        assert len(at_dumps[key]) == 2
    dense = ports[0]["dense"]
    assert len(dense["frequencies_hz"]) == post.DENSE_GRID_POINTS
    assert dense["frequencies_hz"][0] == pytest.approx(200e6)
    assert dense["frequencies_hz"][-1] == pytest.approx(400e6)
    assert art.manifest["dense_frequencies_hz"][-1] == pytest.approx(400e6)


def test_the_two_grids_agree_where_they_overlap(solved):
    """The dense grid starts and ends on the requested frequencies, so those points must
    match exactly -- they are the same DFT of the same record."""
    art = post.build_artifacts(str(solved), {"F.Cu": "Hf_F_Cu"}, [200e6, 400e6], ["p1"])
    p = json.loads(art.files["ports.json"])["ports"][0]
    for key in ("v_real", "v_imag", "i_real", "i_imag"):
        assert p["dense"][key][0] == pytest.approx(p["at_dump_frequencies"][key][0])
        assert p["dense"][key][-1] == pytest.approx(p["at_dump_frequencies"][key][-1])


def test_a_run_with_no_probes_still_produces_a_result(solved):
    """A solve with no excited port has no spectra, and must not claim to."""
    (solved / "p1_ut").unlink()
    (solved / "p1_it").unlink()
    art = post.build_artifacts(str(solved), {"F.Cu": "Hf_F_Cu"}, [200e6, 400e6], ["p1"])
    assert art.manifest["has_port_spectra"] is False
    assert "ports.json" not in art.files
    assert art.manifest["format_version"] == 2


def test_the_manifest_carries_the_modelled_parts_list(solved):
    """§12: results gain a list of reference, model, owner and source."""
    parts = [{"ref": "C12", "component": "100 nF 0402 (generic)", "generic": True,
              "self_resonance_hz": 23.7e6}]
    art = post.build_artifacts(str(solved), {"F.Cu": "Hf_F_Cu"}, [200e6, 400e6], ["p1"],
                               modelled_parts=parts)
    assert art.manifest["modelled_parts"] == parts


def test_a_solve_that_modelled_nothing_says_so_with_an_empty_list(solved):
    """Not absent: a reader must be able to tell 'nothing was modelled' from 'this result
    predates the list', and format_version already answers the second question."""
    art = post.build_artifacts(str(solved), {"F.Cu": "Hf_F_Cu"}, [200e6, 400e6], ["p1"])
    assert art.manifest["modelled_parts"] == []


def test_an_unconverged_run_marks_every_derived_number_unusable(solved):
    """The maps are still written; the port spectra and cable transfers are not usable."""
    t, v = _tone(200e6, 0.5)
    _write_probe(solved / "cable_J1_ut", t, v)
    cable = [{"ref": "J1", "probe": "cable_J1_ut"}]
    for converged in (True, False):
        art = post.build_artifacts(str(solved), {"F.Cu": "Hf_F_Cu"}, [200e6, 400e6], ["p1"],
                                   cable_ports=cable, run_meta={"converged": converged})
        ports = json.loads(art.files["ports.json"])["ports"]
        transfer = json.loads(art.files["cable_ports.json"])["ports"][0]["transfer"]
        assert ports[0]["usable"] is converged
        assert all(u is converged for u in transfer["usable"])
        assert art.manifest["layers"], "the field maps are written either way"
