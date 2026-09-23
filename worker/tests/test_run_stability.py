"""Catching a run that went unstable (§19, "every solver step asserts twice").

openEMS prints energy in dB **relative to its running maximum**. A diverging run therefore
reports "- 0.0 dB" from the moment it turns around until it finishes, exits zero, and produces
files full of numbers — every warning it emits is about something else. Measured on a real
case: the probe voltage went from 1e-4 to 5e+12 over the second half of the run, and the only
warning in the log was that the timestep limit had been reached.

The absolute energy openEMS prints alongside the dB is what catches it, which is why the
parser keeps it.
"""

from __future__ import annotations

import pytest

from emi_worker.openems.run import DIVERGENCE_RATIO, divergence_ratio


def _decay(peak_at: int, n: int, per_step: float) -> list[float]:
    """A healthy run: rises into a peak, then falls by a constant factor each sample."""
    rise = [10.0 ** (-6 + 6 * k / peak_at) for k in range(peak_at)]
    fall = [rise[-1] * per_step ** k for k in range(n - peak_at)]
    return rise + fall


def test_a_healthy_run_scores_one():
    assert divergence_ratio(_decay(5, 60, 0.8)) == pytest.approx(1.0)


def test_the_rise_into_the_peak_is_not_divergence():
    """The excitation arriving is the one thing that is supposed to grow.

    A ratio measured from the first sample would call every run unstable, since the energy at
    the start of a solve is many orders below the peak by construction.
    """
    series = _decay(30, 60, 0.7)
    assert series[29] / series[0] > 1e5          # the rise really is enormous
    assert divergence_ratio(series) == pytest.approx(1.0)


def test_a_run_that_turns_around_is_caught():
    series = _decay(5, 40, 0.7)
    # It bottoms out and then climbs back, which is what late-time instability looks like.
    series += [series[-1] * 4.0 ** k for k in range(1, 20)]
    assert divergence_ratio(series) > DIVERGENCE_RATIO


def test_a_small_wobble_is_not_divergence():
    """A structure that rings does not decay monotonically, and must not be refused for it."""
    series = _decay(5, 40, 0.7)
    series += [series[-1] * 8.0, series[-1] * 3.0, series[-1] * 1.5]
    assert 1.0 < divergence_ratio(series) < DIVERGENCE_RATIO


@pytest.mark.parametrize("series", [[], [1.0], [1.0, 2.0], [0.0, 0.0, 0.0]])
def test_too_little_to_judge_is_called_healthy(series):
    """A run with no progress lines is a different failure, reported elsewhere.

    Guessing "unstable" from two samples would turn a very short run — or one whose output was
    truncated — into a false accusation about the mesh.
    """
    assert divergence_ratio(series) == 1.0


def test_the_progress_line_carries_the_absolute_energy():
    """The regex has to keep the number before the parenthesis, not only the dB inside it."""
    from emi_worker.openems.run import _PROGRESS

    m = _PROGRESS.search(
        "[@ 1m01s] Timestep: 12978 || Speed: 4.9 MC/s (4.7e-03 s/TS) || "
        "Energy: ~3.2e-12 (- 0.06dB)"
    )
    assert m is not None
    assert float(m.group("energy")) == pytest.approx(3.2e-12)
    assert float(m.group("db").replace(" ", "")) == pytest.approx(-0.06)


# --- The excitation is still running --------------------------------------------------------
#
# A wide-band Gaussian (f0 == fc) has lobes. A small structure that empties fast -- a board
# strip with a 10 mm cable stub and a 1 MOhm gap -- loses more than DECAY_MARGIN_DB between
# them, and the next lobe refills it. A real run was refused at timestep ~14,000 with "climbed
# back by a factor of 2.95e+03" while its 9 ns excitation was still running.

#: Timestep of each sample, one progress line every 500 steps.
_STEP = 500
#: Where the source stops, in the same units: 9 ns at dt ~0.65 ps.
_EXCITATION_END = 14_000


def _lobed_then_decaying() -> tuple[list[float], list[int]]:
    """Two lobes with a 30 dB dip between them, both inside the excitation, then a decay."""
    lobe1 = [1e-18 * 10.0 ** k for k in range(7)]                  # up to 1e-12
    dip = [1e-13, 1e-14, 1e-15]                                     # 30 dB below lobe 1
    lobe2 = [1e-14, 1e-13, 1e-12, 3e-12]                            # refills it, x3e3
    ramp = lobe1 + dip + lobe2
    # Pad the excitation out so it ends exactly at _EXCITATION_END.
    n_exc = _EXCITATION_END // _STEP
    ramp += [3e-12 * 0.9 ** k for k in range(1, n_exc - len(ramp) + 1)]
    decay = [ramp[-1] * 0.7 ** k for k in range(1, 40)]
    energies = ramp + decay
    return energies, [k * _STEP for k in range(len(energies))]


def test_a_dip_between_excitation_lobes_is_not_divergence():
    energies, steps = _lobed_then_decaying()
    assert steps[len(steps) - 39] == _EXCITATION_END  # the decay starts as the source stops
    assert divergence_ratio(energies, steps, excitation_end=_EXCITATION_END) == pytest.approx(1.0)


def test_without_the_excitation_length_the_lobes_look_like_divergence():
    """Why the length is needed: the margin alone reads the second lobe as a blow-up."""
    energies, _ = _lobed_then_decaying()
    assert divergence_ratio(energies) >= DIVERGENCE_RATIO


@pytest.mark.parametrize("tail", [
    # Late-time instability: bottoms out and climbs back steadily.
    [4.0 ** k for k in range(1, 20)],
    # A sudden blow-up of many orders.
    [10.0 ** k for k in range(1, 12)],
])
def test_divergence_after_the_excitation_is_still_caught(tail):
    energies, steps = _lobed_then_decaying()
    energies = energies + [energies[-1] * t for t in tail]
    steps = steps + [steps[-1] + _STEP * k for k in range(1, len(tail) + 1)]
    assert divergence_ratio(energies, steps, excitation_end=_EXCITATION_END) >= DIVERGENCE_RATIO


def test_a_small_wobble_after_the_excitation_is_not_divergence():
    energies, steps = _lobed_then_decaying()
    last = energies[-1]
    energies = energies + [last * 8.0, last * 3.0, last * 1.5]
    steps = steps + [steps[-1] + _STEP * k for k in (1, 2, 3)]
    assert 1.0 < divergence_ratio(energies, steps, excitation_end=_EXCITATION_END) < DIVERGENCE_RATIO


def test_timesteps_must_line_up_with_the_energies():
    with pytest.raises(ValueError):
        divergence_ratio([1.0, 2.0, 3.0], [0, 1], excitation_end=1)


def test_the_excitation_line_is_parsed():
    """openEMS's own report, verbatim from openems.cpp SetupFDTD."""
    from emi_worker.openems.run import _EXCITATION_LINE

    m = _EXCITATION_LINE.search("Excitation signal length is: 26811 timesteps (2.86607e-09s)")
    assert m is not None
    assert int(m.group("n")) == 26811


_FAKE_LOG = """\
FDTD simulation size: 50x50x50 --> 125000 FDTD cells
FDTD timestep is: 6.5e-13 s; Nyquist rate: 10 timesteps @7.7e+10 Hz
{excitation}Max. number of timesteps: 60000 ( --> 4.3 * Excitation signal length)
{progress}"""


def _fake_openems(tmp_path, monkeypatch, *, with_excitation_line: bool):
    """A stand-in binary that prints the lobed run's log, so run_openems is tested end to end."""
    from emi_worker.openems import run

    energies, steps = _lobed_then_decaying()
    progress = "".join(
        f"[@ {k}s] Timestep: {s} || Speed: 4.9 MC/s (4.7e-03 s/TS) || "
        f"Energy: ~{e:.2e} (- 0.00dB)\n"
        for k, (s, e) in enumerate(zip(steps, energies))
    )
    excitation = (
        f"Excitation signal length is: {_EXCITATION_END} timesteps (9.1e-09s)\n"
        if with_excitation_line else ""
    )
    log = tmp_path / "log.txt"
    log.write_text(_FAKE_LOG.format(excitation=excitation, progress=progress))
    exe = tmp_path / "openEMS"
    exe.write_text(f"#!/bin/sh\ncat '{log}'\n")
    exe.chmod(0o755)
    monkeypatch.setattr(run, "OPENEMS_BIN", str(exe))
    return run


def test_run_openems_waits_out_the_excitation_it_reports(tmp_path, monkeypatch):
    run = _fake_openems(tmp_path, monkeypatch, with_excitation_line=True)
    result = run.run_openems("model.xml", str(tmp_path))
    assert result.returncode == 0


def test_run_openems_falls_back_to_the_known_support(tmp_path, monkeypatch):
    run = _fake_openems(tmp_path, monkeypatch, with_excitation_line=False)
    with pytest.raises(run.OpenEMSError, match="unstable"):
        run.run_openems("model.xml", str(tmp_path))
    result = run.run_openems("model.xml", str(tmp_path), excitation_s=_EXCITATION_END * 6.5e-13)
    assert result.returncode == 0
