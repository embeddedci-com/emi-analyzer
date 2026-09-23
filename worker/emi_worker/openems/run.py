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
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
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
_EXCITATION_LINE = re.compile(r"Excitation signal length is:\s*(?P<n>\d+)\s*timesteps")
_DONE_LINE = re.compile(r"Time for\s*(?P<n>\d+)\s*iterations")

#: An end criterion openEMS cannot meet, for a run whose end ``run_openems`` decides instead.
OPENEMS_NEVER_STOPS = 1e-30

#: What openEMS prints when a run stops on its timestep cap rather than its end criterion.
TIMESTEP_LIMIT_NEEDLE = "Max. number of timesteps was reached before the end-criteria"

#: Warnings openEMS prints and then carries on regardless. Each one means the model that
#: was solved is not the model that was asked for, so they are surfaced rather than logged.
_SIGNIFICANT_WARNINGS = (
    ("no excitation properties found",
     "openEMS found no excitation: every field in this result is zero"),
    ("Not enough lines in direction",
     "an absorbing boundary fell back to a reflecting wall; the result contains "
     "reflections that are not on the board"),
    (TIMESTEP_LIMIT_NEEDLE,
     "the run hit its timestep limit before the energy decayed, so its fields had not "
     "settled; no level, impedance or transfer function from it is used"),
    ("Unknown Property found",
     "part of the model was not understood by the solver"),
    # openEMS 0.0.35 models a lumped R or C and nothing else. An element with only L set is
    # dropped with this warning, and its cell is left as whatever material is there: a series
    # R-L-C built from three cells becomes R, an open gap and C, which is an open circuit.
    # Measured with research/verify_lumped_rlc.py: a 10 nH element read as -j9921 ohm at
    # 100 MHz, a 0.16 pF gap, instead of +j6.3.
    ("R or C not specified",
     "a lumped element was skipped by the solver, so part of the model is an open gap "
     "instead of the component that was asked for"),
    ("capacity is too small for its size",
     "a lumped capacitor was skipped by the solver because it is smaller than the cell it "
     "sits in, so part of the model is missing"),
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
#: run ends 40 dB down, which is the end criterion every solve is given (``end_criteria`` =
#: 1e-4 of the peak energy). 20 dB sits between the two with room to spare, and it is the
#: number that stops a healthy ramp being read as a blow-up.
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
    #: How long openEMS says its own excitation lasts, in its timesteps. 0 when not reported.
    excitation_steps: int = 0
    #: The step at which the runner asked openEMS to stop because the energy had fallen past
    #: ``stop_below_db`` after the source. 0 when it did not.
    stopped_on_energy_at: int = 0

    @property
    def stopped_inside_the_source(self) -> bool:
        """openEMS stopped on its end criterion while its own excitation was still running.

        openEMS 0.0.35 checks the end criterion at every progress report, source or no source,
        against the energy's running maximum. A wideband pulse is barely one cycle of its
        carrier, and in a small region energy leaves as fast as it arrives, so between two lobes
        the energy can fall 50 dB and the run stops: measured on a real board
        (research/verify_record_length.py), a 30 MHz-1 GHz solve stopped at "-53 dB" after
        50,995 of the 66,806 steps its pulse lasts, 4.2 ns into a record that needs 100 ns.
        That is not a settled run, whatever the energy says.
        """
        return 0 < self.final_timestep < self.excitation_steps

    @property
    def converged(self) -> bool:
        """Whether the run reached its energy cutoff, after its source, not its timestep cap."""
        return TIMESTEP_LIMIT_NEEDLE not in self.log_text and not self.stopped_inside_the_source

    def unconverged_reason(self) -> str | None:
        """Why nothing derived from this run may be used, in the user's words. None if it may."""
        if self.converged:
            return None
        if self.stopped_inside_the_source:
            return (
                f"openEMS stopped at timestep {self.final_timestep:,}, before its own source "
                f"had finished ({self.excitation_steps:,} timesteps), because the energy dipped "
                f"between two lobes of the pulse. The record is cut short, so no level, "
                f"impedance or transfer function from this run is used"
            )
        return (
            f"the run reached its limit of {self.max_timesteps:,} timesteps with its energy "
            f"only {abs(self.final_energy_db):.1f} dB down, before the fields settled. A "
            f"transform of fields that are still ringing is not the board's response, so no "
            f"level, impedance or transfer function from this run is used"
        )


class OpenEMSError(RuntimeError):
    """openEMS failed. The message is written for the user.

    ``log_text`` is the solver's output when there was any, so a refused run can still be
    looked at.
    """

    log_text: str = ""


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
    source_ends_at_step: int | None = None,
    stop_below_db: float | None = None,
) -> RunResult:
    """Run openEMS to completion, streaming progress.

    ``should_stop`` is polled between output lines. A solve runs for hours, so a cancel
    that only takes effect at the end is not a cancel.

    ``source_ends_at_step`` is the timestep after which the excitation has finished. Given it,
    the divergence check also catches a run that grows from the start (see
    ``divergence_ratio``).

    ``stop_below_db`` moves the end criterion out of openEMS and into this loop: once openEMS's
    own excitation has finished and its energy is that far below its running maximum, an
    ``ABORT`` file is written in ``workdir``, which openEMS polls for and treats as a normal
    end, writing every dump. openEMS checks its own criterion while the source is still on,
    and stopped a real board's 30 MHz-1 GHz solve on a dip between two lobes of the pulse
    (``RunResult.stopped_inside_the_source``); a caller using this gives openEMS an
    unreachable criterion of its own.
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
    excitation_steps = 0
    done_steps = 0
    last = RunProgress(0, 0, 0.0, 0.0, 0.0)
    energies: list[float] = []
    energy_steps: list[int] = []
    cancelled = False
    aborted_at = 0

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
            m = _EXCITATION_LINE.search(line)
            if m:
                excitation_steps = int(m.group("n"))
                continue
            m = _DONE_LINE.search(line)
            if m:
                done_steps = int(m.group("n"))
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
                source_done = excitation_steps or source_ends_at_step or 0
                if (stop_below_db is not None and not aborted_at and source_done
                        and last.timestep > source_done and last.energy_db <= stop_below_db):
                    aborted_at = last.timestep
                    try:
                        with open(os.path.join(workdir, "ABORT"), "w"):
                            pass
                    except OSError as exc:
                        log.warning("could not ask openEMS to stop: %s", exc)
                        aborted_at = 0
    finally:
        proc.stdout.close()

    returncode = proc.wait()
    elapsed = time.monotonic() - started

    if cancelled:
        raise Stopped()

    log_text = "\n".join(lines)
    warnings = [msg for needle, msg in _SIGNIFICANT_WARNINGS if needle in log_text]

    rise = divergence_ratio(energies, energy_steps, source_ends_at_step)
    if rise >= DIVERGENCE_RATIO:
        err = OpenEMSError(
            f"the simulation went unstable: after the excitation passed, its energy climbed "
            f"back by a factor of {rise:.3g} instead of decaying. Every number in this run is "
            f"noise, so it is refused rather than reported. This is a property of the mesh "
            f"rather than of the board — try a finer preset, or a smaller region."
        )
        err.log_text = log_text
        raise err

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
        # The closing line is exact; the last progress line can be seconds stale.
        final_timestep=done_steps or last.timestep,
        final_energy_db=last.energy_db,
        elapsed_s=elapsed,
        warnings=warnings,
        log_text=log_text,
        excitation_steps=excitation_steps,
        stopped_on_energy_at=aborted_at,
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
    steps: list[int] | None = None,
    source_ends_at_step: int | None = None,
) -> float:
    """How far the energy climbed back after the run had really started to decay.

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

    **When the caller knows when the source stops, that replaces the 20 dB gate** (``steps``
    and ``source_ends_at_step``): the floor arms at the first sample after it, decayed or not,
    and never before. Both halves were measured:

    * A run that grows from the start never decays 20 dB, so on the energy alone the floor
      never arms and nothing is reported: the fields grow, openEMS prints "- 0.0 dB" to the
      end, and the result is published. Once the excitation has finished nothing feeds a
      passive structure, so its energy can only ring down, and a thousandfold climb from there
      is the grid.
    * The 20 dB gate is not safe while the source is on. A 30 MHz-1 GHz pulse is barely one
      cycle of its carrier, and on a real board (research/verify_record_length.py) its energy
      fell more than 20 dB between two lobes and then rose 2,070x on the next, at step 46,624
      of a pulse that lasts 66,806. The gate called that a divergence and refused the run.

    openEMS's end criterion stops a healthy run 40 dB down, well past the 20 dB gate.
    """
    if len(energies) < 3:
        return 1.0

    decay_factor = 10.0 ** (DECAY_MARGIN_DB / 10.0)
    quiet_from = source_ends_at_step if steps is not None else None
    if quiet_from is not None and len(steps) != len(energies):
        raise ValueError("steps and energies must be the same length")
    peak = 0.0
    floor: float | None = None
    worst = 1.0
    for k, e in enumerate(energies):
        if e <= 0:
            continue
        peak = max(peak, e)
        if floor is None:
            # Still rising, or not yet clearly past the excitation.
            if quiet_from is not None:
                if steps[k] > quiet_from:
                    floor = e
            elif e * decay_factor <= peak:
                floor = e
            continue
        floor = min(floor, e)
        worst = max(worst, e / floor)
    return worst


@lru_cache(maxsize=1)
def solver_has_series_rlc() -> bool:
    """Whether the installed openEMS models a series lumped R-L-C (``LEtype``).

    Asked of the binary rather than assumed from a version: it is given a one-cell inductor
    and one timestep, and a solver that cannot model it says "R or C not specified" while
    setting up. openEMS 0.0.35, the Debian package, cannot; a current build can. A
    second or so, once per worker process.
    """
    from . import csx

    lines = [float(v) for v in range(12)]
    doc = csx.CSXDocument(
        excitation=csx.Excitation(type=0, f0=1e9, fc=1e9),
        x_lines=lines, y_lines=lines, z_lines=lines, f_max=2e9, max_timesteps=1,
        boundaries=csx.Boundaries(*(["PEC"] * 6)),
    )
    probe_box = csx.Box(p1=(5.0, 5.0, 5.0), p2=(5.0, 5.0, 6.0), priority=csx.PRIORITY_PORT)
    doc.add(csx.ExcitationProperty(name="exc", excite=(0.0, 0.0, 1.0), primitives=[probe_box]))
    # Inductance alone. With R and C set as well, 0.0.35 ignores LEtype and L without a word
    # and builds R parallel to C, which is why the question is asked this way.
    doc.add(csx.LumpedElement(
        name="probe", direction=2, resistance=None, inductance=1e-9, le_type=csx.LE_SERIES,
        primitives=[csx.Box(p1=(3.0, 3.0, 3.0), p2=(4.0, 4.0, 4.0), priority=csx.PRIORITY_PORT)]))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "probe.xml")
            with open(path, "w") as fh:
                fh.write(doc.to_string())
            out = subprocess.run([OPENEMS_BIN, path], cwd=tmp, capture_output=True, text=True,
                                 timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not ask openEMS about lumped elements: %s", exc)
        return False
    text = out.stdout + out.stderr
    ok = out.returncode == 0 and "R or C not specified" not in text
    log.info("openEMS %s a series lumped R-L-C", "models" if ok else "does not model")
    return ok
