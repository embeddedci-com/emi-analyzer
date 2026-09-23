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

from emi_worker.openems.run import (
    _SIGNIFICANT_WARNINGS,
    DIVERGENCE_RATIO,
    TIMESTEP_LIMIT_NEEDLE,
    RunResult,
    divergence_ratio,
)


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


# ---- a run that grows from the start ---------------------------------------------------

def _growing(n: int = 60, per_sample: float = 1.8) -> tuple[list[float], list[int]]:
    """Energy that never turns around: the shape the 20 dB gate alone cannot see."""
    energies = [1e-20 * per_sample ** k for k in range(n)]
    steps = [1000 * (k + 1) for k in range(n)]
    return energies, steps


def test_growth_from_the_start_is_invisible_without_the_source_length():
    """The limit the gate had: a run that never decays 20 dB never arms it."""
    energies, _ = _growing()
    assert divergence_ratio(energies) == 1.0


def test_growth_after_the_source_has_finished_is_caught():
    energies, steps = _growing()
    assert divergence_ratio(energies, steps, source_ends_at_step=10_000) >= DIVERGENCE_RATIO


def test_growth_while_the_source_is_still_on_is_not_divergence():
    """The whole series is the excitation arriving, so nothing may be called unstable."""
    energies, steps = _growing()
    assert divergence_ratio(energies, steps, source_ends_at_step=steps[-1]) == 1.0


def test_the_source_length_does_not_disturb_a_healthy_run():
    from tests.test_openems import REAL_RUN_ENERGY

    steps = [1500 * (k + 1) for k in range(len(REAL_RUN_ENERGY))]
    # Its energy peaks at sample 34, so the source was still on until then; after it the
    # energy still wobbles 2.5x (3.21e-14 to 8.04e-14), which is the ripple the ratio allows.
    assert divergence_ratio(REAL_RUN_ENERGY, steps, source_ends_at_step=steps[34]) < 10


def test_a_ringing_tail_after_the_source_is_not_divergence():
    """After the source, a structure exchanging energy between E and H wobbles a few dB."""
    ramp = [1e-20 * 10 ** k for k in range(8)]
    tail = [ramp[-1] * (0.9 ** k) * (1.0 + 0.5 * (k % 2)) for k in range(40)]
    steps = [100 * (k + 1) for k in range(len(ramp) + len(tail))]
    assert divergence_ratio(ramp + tail, steps, source_ends_at_step=steps[7]) < 10


# ---- convergence and the warnings that mean the model is not the one asked for ---------

def _result(log_text: str, energy_db: float = -41.0) -> RunResult:
    return RunResult(returncode=0, cells=1, dt_seconds=1e-13, max_timesteps=358_695,
                     final_timestep=358_695, final_energy_db=energy_db, elapsed_s=1.0,
                     warnings=[], log_text=log_text)


def test_a_run_that_hit_its_cap_is_not_converged_and_says_why():
    r = _result(f"...\n{TIMESTEP_LIMIT_NEEDLE}!\n", energy_db=-22.7)
    assert r.converged is False
    why = r.unconverged_reason()
    assert "358,695 timesteps" in why and "22.7 dB" in why


def test_a_run_that_stopped_on_its_end_criterion_is_converged():
    r = _result("Time for 84987 iterations with 1.89e+06 cells : 402 sec")
    assert r.converged is True
    assert r.unconverged_reason() is None


@pytest.mark.parametrize("line", [
    "Operator::Calc_LumpedElements(): Warning: Lumped Element R or C not specified! skipping. "
    " ID: 6 @ Property: cap_C1_l",
    "Operator::Calc_LumpedElements(): Warning: Lumped Element capacity is too small for its "
    "size! skipping.",
])
def test_a_skipped_lumped_element_is_surfaced(line):
    """openEMS 0.0.35 drops an inductor-only element and carries on (research/verify_lumped_rlc.py)."""
    assert any(needle in line for needle, _ in _SIGNIFICANT_WARNINGS)


def test_a_run_that_stopped_inside_its_own_source_is_not_converged():
    """openEMS 0.0.35 checks its end criterion while the pulse is still on (verify_record_length)."""
    r = RunResult(returncode=0, cells=1, dt_seconds=8.3e-14, max_timesteps=1_383_981,
                  final_timestep=50_995, final_energy_db=-53.2, elapsed_s=1.0, warnings=[],
                  log_text="Time for 50995 iterations", excitation_steps=66_806)
    assert r.stopped_inside_the_source and not r.converged
    assert "66,806" in r.unconverged_reason()
    after = RunResult(**{**r.__dict__, "final_timestep": 70_000})
    assert after.converged and after.unconverged_reason() is None


def test_the_excitation_length_and_the_closing_line_are_parsed(tmp_path, monkeypatch):
    """Both come from openEMS's own log, so a fake binary that prints them is enough."""
    from emi_worker.openems import run as runmod

    fake = tmp_path / "openEMS"
    fake.write_text("#!/bin/sh\n"
                    "echo 'Excitation signal length is: 66806 timesteps (5.56271e-09s)'\n"
                    "echo 'Max. number of timesteps: 1383981 ( --> 20.7 * Excitation signal length)'\n"
                    "echo '[@ 8m19s] Timestep: 50995 || Speed: 132.4 MC/s (9.8e-03 s/TS) || "
                    "Energy: ~7.01e-21 (-53.18dB)'\n"
                    "echo 'Time for 50995 iterations with 1299456.00 cells : 499.52 sec'\n")
    fake.chmod(0o755)
    monkeypatch.setattr(runmod, "OPENEMS_BIN", str(fake))
    r = runmod.run_openems("model.xml", str(tmp_path))
    assert (r.excitation_steps, r.final_timestep, r.max_timesteps) == (66_806, 50_995, 1_383_981)
    assert r.converged is False


def _fake_openems(tmp_path, energies_db: list[tuple[int, float]]):
    """An openEMS that prints its source length and progress, then waits for an ABORT file."""
    lines = ["echo 'Excitation signal length is: 1000 timesteps (1e-10s)'",
             "echo 'Max. number of timesteps: 9000 ( --> 9 * Excitation signal length)'"]
    for step, db in energies_db:
        lines.append(f"echo '[@ 1s] Timestep: {step} || Speed: 100 MC/s (1e-03 s/TS) || "
                     f"Energy: ~1e-15 ({db:+.2f}dB)'")
        lines.append("sleep 0.05")
    lines.append("for i in $(seq 1 40); do [ -f ABORT ] && echo 'Time for 3000 iterations' "
                 "&& exit 0; sleep 0.05; done")
    lines.append("echo 'Max. number of timesteps was reached before the end-criteria!'")
    fake = tmp_path / "openEMS"
    fake.write_text("#!/bin/sh\n" + "\n".join(lines) + "\n")
    fake.chmod(0o755)
    return str(fake)


def test_the_runner_stops_openems_only_after_its_source(tmp_path, monkeypatch):
    """A -60 dB dip inside the source is ignored; -45 dB after it ends the run normally."""
    from emi_worker.openems import run as runmod

    monkeypatch.setattr(runmod, "OPENEMS_BIN", _fake_openems(
        tmp_path, [(400, -60.0), (800, -0.0), (2000, -30.0), (3000, -45.0)]))
    r = runmod.run_openems("model.xml", str(tmp_path), stop_below_db=-40.0)
    assert r.stopped_on_energy_at == 3000
    assert r.converged is True


def test_without_the_runner_criterion_the_run_goes_to_its_cap(tmp_path, monkeypatch):
    from emi_worker.openems import run as runmod

    monkeypatch.setattr(runmod, "OPENEMS_BIN", _fake_openems(
        tmp_path, [(400, -60.0), (2000, -30.0), (3000, -45.0)]))
    r = runmod.run_openems("model.xml", str(tmp_path))
    assert r.stopped_on_energy_at == 0
    assert r.converged is False
