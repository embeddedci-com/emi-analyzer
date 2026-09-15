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
