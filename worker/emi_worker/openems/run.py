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
) -> RunResult:
    """Run openEMS to completion, streaming progress.

    ``should_stop`` is polled between output lines. A solve runs for hours, so a cancel
    that only takes effect at the end is not a cancel.
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
    last = RunProgress(0, 0, 0.0, 0.0, 0.0)
    energies: list[float] = []
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

    rise = divergence_ratio(energies)
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


def divergence_ratio(energies: list[float]) -> float:
    """How far the energy ever climbed back above a low point. 1.0 for a healthy run.

    Measured as the largest rise from a **running** minimum, counted only once the series has
    started to fall. Both halves of that matter:

    * counting from the first sample would call every run unstable, because the energy at the
      start of a solve is many orders below the peak by construction — that rise is the
      excitation arriving, which is the one thing that is supposed to grow;
    * comparing against the *global* peak finds nothing, because a diverging run's largest
      energy is its last one. An earlier version did exactly this and scored 1.0 on the case
      it existed to catch.
    """
    if len(energies) < 3:
        return 1.0
    # Where the excitation has passed: the first sample lower than the one before it.
    start = next((k for k in range(1, len(energies)) if energies[k] < energies[k - 1]), None)
    if start is None:
        return 1.0

    floor = float("inf")
    worst = 1.0
    for e in energies[start:]:
        if e <= 0:
            continue
        floor = min(floor, e)
        worst = max(worst, e / floor)
    return worst
