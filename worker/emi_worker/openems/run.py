"""Invoke openEMS and stream its progress.

openEMS reports its own timestep, throughput and energy decay:

    [@   17s] Timestep: 3708 || Speed: 4.7 MC/s (4.83e-03 s/TS) || Energy: ~2.27e-15 (- 0.00dB)

Those numbers are parsed rather than estimated from wall clock, because the energy figure
is the only honest answer to "is this run going to finish": a curve falling toward the
cutoff is converging, a curve that flattens is trapped in a resonance and will run to the
timestep cap no matter how long it is left.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

OPENEMS_BIN = os.environ.get("OPENEMS_BIN", "openEMS")

#: [@ 1m01s] Timestep: 12978 || Speed: 4.9 MC/s (4.7e-03 s/TS) || Energy: ~3.2e-12 (- 0.06dB)
_PROGRESS = re.compile(
    r"Timestep:\s*(?P<step>\d+)"
    r".*?Speed:\s*(?P<speed>[\d.]+)\s*MC/s"
    r".*?Energy:\s*~?(?P<energy>[\d.eE+-]+)\s*\(\s*(?P<db>[-+]?\s*[\d.]+)\s*dB\)"
)

_TIMESTEP_LINE = re.compile(r"FDTD timestep is:\s*(?P<dt>[\d.eE+-]+)\s*s")
#: Excitation signal length is: 26811 timesteps (2.866e-09s)
_EXCITATION_LINE = re.compile(r"Excitation signal length is:\s*(?P<n>\d+)\s*timesteps")
_MAXSTEPS_LINE = re.compile(r"Max\. number of timesteps:\s*(?P<n>\d+)")
_CELLS_LINE = re.compile(r"FDTD simulation size:.*?-->\s*(?P<cells>[\d.]+)\s*FDTD cells")

#: Warnings openEMS prints and then carries on regardless. Each one means the model that
#: was solved is not the model that was asked for, so they are surfaced rather than logged.
_SIGNIFICANT_WARNINGS = (
    ("no excitation properties found",
     "openEMS found no excitation: every field in this result is zero"),
    ("Not enough lines in direction",
     "an absorbing boundary fell back to a reflecting wall; the result contains "
     "reflections that are not on the board"),
    ("Max. number of timesteps was reached before the end-criteria",
     "the run hit its timestep limit before the energy decayed; the result is "
     "under-resolved at the low end of the band"),
    ("Unknown Property found",
     "part of the model was not understood by the solver"),
)

#: How far the energy may climb back above its own low point before the run is called unstable.
#:
#: openEMS prints energy in dB **relative to its running maximum**, so a diverging run reports
#: "- 0.0 dB" forever and exits zero: the fields grow, the maximum grows with them, and the
#: ratio never moves. Nothing in the log says anything is wrong. Measured on a run that did
#: diverge, the probe voltage went from 1e-4 to 5e+12 over the second half — twenty orders of
#: magnitude — while every warning openEMS produced was about the timestep limit.
#:
#: The absolute figure it prints alongside is what catches it. A stable run's energy rises to a
#: peak and then decays, so the lowest value is the last one. An unstable one turns around. A
#: factor of a thousand is far beyond anything a physical structure does after its excitation
#: has passed, and far beyond the sampling noise of a progress line printed every few seconds.
DIVERGENCE_RATIO = 1e3

#: How far the energy must fall below its own peak before a rise counts as divergence rather
#: than as the excitation still arriving. The ripple measured on the way up is 3-5 dB; a real
#: run ends 50 dB down, which is openEMS's own end criterion. 20 dB sits between the two with
#: room to spare, and it is the number that stops a healthy ramp being read as a blow-up.
DECAY_MARGIN_DB = 20.0


@dataclass
class RunProgress:
    timestep: int
    total_timesteps: int
    energy_db: float
    speed_mcells_s: float
    elapsed_s: float


@dataclass
class RunResult:
    returncode: int
    cells: int
    dt_seconds: float
    max_timesteps: int
    final_timestep: int
    final_energy_db: float
    elapsed_s: float
    warnings: list[str]
    log_text: str

    @property
    def converged(self) -> bool:
        """Whether the run reached its energy cutoff rather than its timestep cap."""
        return not any("under-resolved" in w for w in self.warnings)


class OpenEMSError(RuntimeError):
    """openEMS failed. The message is written for the user."""


class Stopped(RuntimeError):
    """The run was cancelled."""


def run_openems(
    xml_path: str,
    workdir: str,
    *,
    threads: int | None = None,
    on_progress: Callable[[RunProgress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    poll_interval: float = 0.25,
    excitation_s: float | None = None,
) -> RunResult:
    """Run openEMS to completion, streaming progress.

    ``should_stop`` is polled between output lines. A solve runs for hours, so a cancel
    that only takes effect at the end is not a cancel.

    ``excitation_s`` is how long the source runs, for the stability check to wait out. openEMS
    reports its own figure in the log and that one wins; this is the fallback for a log that
    lacks the line (``GAUSSIAN_SUPPORT_OVER_FC / fc`` for a Gaussian).
    """
    env = dict(os.environ)
    if threads and threads > 0:
        # openEMS reads OMP_NUM_THREADS. Left alone it takes every core it can see, which
        # on a shared worker means one run starving the others.
        env["OMP_NUM_THREADS"] = str(threads)

    cmd = [OPENEMS_BIN, xml_path]
    if threads and threads > 0:
        cmd.append(f"--numThreads={threads}")

    log.info("running %s in %s", " ".join(cmd), workdir)
    started = time.monotonic()

    try:
        proc = subprocess.Popen(
            cmd, cwd=workdir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            # Its own process group, so a cancel can take the whole thing down rather than
            # orphaning worker threads.
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise OpenEMSError(
            f"the {OPENEMS_BIN} binary is not installed on this worker"
        ) from exc

    lines: list[str] = []
    cells = 0
    dt = 0.0
    max_steps = 0
    excitation_steps: int | None = None
    last = RunProgress(0, 0, 0.0, 0.0, 0.0)
    energies: list[float] = []
    energy_steps: list[int] = []
    cancelled = False

    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            lines.append(line)

            if should_stop is not None and should_stop():
                cancelled = True
                _terminate(proc)
                break

            m = _CELLS_LINE.search(line)
            if m:
                cells = int(float(m.group("cells")))
                continue
            m = _TIMESTEP_LINE.search(line)
            if m:
                dt = float(m.group("dt"))
                continue
            m = _EXCITATION_LINE.search(line)
            if m:
                excitation_steps = int(m.group("n"))
                continue
            m = _MAXSTEPS_LINE.search(line)
            if m:
                max_steps = int(m.group("n"))
                continue

            m = _PROGRESS.search(line)
            if m:
                last = RunProgress(
                    timestep=int(m.group("step")),
                    total_timesteps=max_steps,
                    # openEMS prints "- 0.06dB" with a space after the sign.
                    energy_db=float(m.group("db").replace(" ", "")),
                    speed_mcells_s=float(m.group("speed")),
                    elapsed_s=time.monotonic() - started,
                )
                try:
                    energies.append(float(m.group("energy")))
                    energy_steps.append(last.timestep)
                except ValueError:
                    pass
                if on_progress:
                    on_progress(last)
    finally:
        proc.stdout.close()

    returncode = proc.wait()
    elapsed = time.monotonic() - started

    if cancelled:
        raise Stopped()

    log_text = "\n".join(lines)
    warnings = [msg for needle, msg in _SIGNIFICANT_WARNINGS if needle in log_text]

    if excitation_steps is None and excitation_s and dt > 0:
        excitation_steps = int(excitation_s / dt)
    rise = divergence_ratio(energies, energy_steps, excitation_end=excitation_steps or 0)
    if rise >= DIVERGENCE_RATIO:
        raise OpenEMSError(
            f"the simulation went unstable: after the excitation passed, its energy climbed "
            f"back by a factor of {rise:.3g} instead of decaying. Every number in this run is "
            f"noise, so it is refused rather than reported. This is a property of the mesh "
            f"rather than of the board — try a finer preset, or a smaller region."
        )

    if returncode != 0:
        tail = "\n".join(lines[-15:])
        raise OpenEMSError(
            f"openEMS exited with status {returncode}.\n{tail}"
        )

    return RunResult(
        returncode=returncode,
        cells=cells,
        dt_seconds=dt,
        max_timesteps=max_steps,
        final_timestep=last.timestep,
        final_energy_db=last.energy_db,
        elapsed_s=elapsed,
        warnings=warnings,
        log_text=log_text,
    )


def _terminate(proc: subprocess.Popen) -> None:
    """Stop openEMS, escalating if it does not go quietly."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def divergence_ratio(
    energies: list[float],
    timesteps: list[int] | None = None,
    *,
    excitation_end: int = 0,
) -> float:
    """How far the energy climbed back after the run had really started to decay.

    ``timesteps`` gives the timestep of each energy sample, and ``excitation_end`` the
    timestep at which the source stops. No sample before that is judged at all.

    1.0 for a healthy run. A diverging FDTD grid pumps energy, so its stored energy turns
    around and grows without bound; that is what this has to catch, and nothing else.

    **Deciding when the excitation has passed is the whole difficulty, and getting it wrong
    was worse than not checking at all.** The first version took the first sample lower than
    the one before it. Measured on a real run, the energy oscillates by 3-5 dB the whole way
    up the excitation ramp -- it dips at the sixth sample of a rise that continues for another
    seven orders of magnitude -- so the floor was set at 1.07e-19 while the run was still
    ramping, and the legitimate climb to its 1.14e-13 peak was then reported as a divergence
    by a factor of 1.07e6. Every long solve was refused this way, which is where "long solves
    always diverge" came from.

    So the excitation counts as passed only once the energy has fallen ``DECAY_MARGIN_DB``
    below the highest value seen so far -- far more than the ripple on the way up, and far
    less than the decay a healthy run finishes with. After that point the measure is the
    largest rise above a running minimum, which is what a pumping grid does and a ringing
    structure does not.

    Comparing against the global peak instead would find nothing at all: a diverging run's
    largest energy is its last one.

    **The margin alone is not enough on a wide-band pulse.** With f0 == fc the Gaussian has
    lobes, and a small structure that empties fast (a board strip with a 10 mm cable stub and a
    1 MOhm gap) loses more than 20 dB between them. The next lobe then refills it, and a real
    run was refused at timestep ~14,000 (9.1 ns) with "climbed back by a factor of 2.95e+03"
    while its 9 ns excitation was still running. So when the excitation length is known, the
    margin only starts counting once the source has stopped; before that the samples only
    raise the peak.

    The limit of this: a grid that blew up before its energy had fallen that far would not be
    reported. Every divergence on record has the same shape -- the excitation passes, the
    energy decays, and only then does the grid start feeding it -- so the gate is where the
    evidence says it should be, and openEMS's own end-criterion stops a healthy run 50 dB
    down, which is well past it.
    """
    if len(energies) < 3:
        return 1.0
    if timesteps is not None and len(timesteps) != len(energies):
        raise ValueError("divergence_ratio needs one timestep per energy sample")

    decay_factor = 10.0 ** (DECAY_MARGIN_DB / 10.0)
    peak = 0.0
    floor: float | None = None
    worst = 1.0
    for i, e in enumerate(energies):
        if e <= 0:
            continue
        peak = max(peak, e)
        if timesteps is not None and timesteps[i] < excitation_end:
            # The source is still driving the structure: a dip here is between two lobes.
            continue
        if floor is None:
            # Still rising, or not yet clearly past the excitation.
            if e * decay_factor <= peak:
                floor = e
            continue
        floor = min(floor, e)
        worst = max(worst, e / floor)
    return worst
