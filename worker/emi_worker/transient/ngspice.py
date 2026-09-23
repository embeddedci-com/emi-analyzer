"""Run ngspice on a netlist, in a box, and read back what it computed.

The netlist is ours, but it can carry an uploaded vendor model, so ngspice is treated as
untrusted code even after spice_model has vetted that model:

  * it runs in a fresh temporary directory, which is also HOME, so the only ``.spiceinit`` it
    can find is the one written here;
  * batch mode, stdin closed, a wall-clock timeout, and CPU, file-size and (on Linux) memory
    limits;
  * the environment is emptied down to PATH and locale.

Results come back through an ASCII raw file rather than stdout, because that format is
documented and stable across ngspice versions while console output is not.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

import numpy as np

BINARY = "ngspice"
TIMEOUT_S = 120.0

#: PSpice compatibility, so vendor models written for PSpice -- VALUE= expressions, PARAMS: on
#: subcircuits -- load as their authors intended. Verified against ngspice 39.
SPICEINIT = "set ngbehavior=psa\n"

_ERROR_LINE = re.compile(
    r"error|fatal|abort|singular|too small|unknown|could not|cannot|not found|undefined", re.I
)


class SimulationError(Exception):
    def __init__(self, message: str, log: str = ""):
        super().__init__(message)
        self.log = log


@dataclass
class Waveforms:
    time: np.ndarray
    vectors: dict[str, np.ndarray]

    def __getitem__(self, name: str) -> np.ndarray:
        return self.vectors[name.lower()]

    def has(self, name: str) -> bool:
        return name.lower() in self.vectors


def available() -> bool:
    return shutil.which(BINARY) is not None


def _limits(timeout_s: float):
    def apply() -> None:  # runs in the child, before exec
        import resource

        limits = [
            (resource.RLIMIT_CPU, int(timeout_s) + 5),
            (resource.RLIMIT_FSIZE, 512 * 1024 * 1024),
            (resource.RLIMIT_CORE, 0),
        ]
        if sys.platform.startswith("linux"):
            limits.append((resource.RLIMIT_AS, 4 * 1024 ** 3))
        for which, value in limits:
            try:
                resource.setrlimit(which, (value, value))
            except (ValueError, OSError):
                pass

    return apply


#: Directives no deck of ours contains. ngspice runs a ``.control`` block, which can call
#: ``shell``, and the others read files from disk. Uploaded models are vetted for these in
#: spice_model; this is the last check, on the whole deck, for text from anywhere else.
_FORBIDDEN = re.compile(r"^\s*\.(control|endc|include|inc|lib|osdi)\b", re.I | re.M)


def check_deck(netlist: str) -> None:
    """Refuse a deck that could make ngspice do more than simulate."""
    # The first line is the title, which ngspice never reads as a directive.
    body = netlist.split("\n", 1)[1] if "\n" in netlist else ""
    m = _FORBIDDEN.search(body)
    if m:
        raise SimulationError(f"refused to run a netlist containing .{m.group(1).lower()}")


def _summarise(log: str) -> str:
    seen: list[str] = []
    for line in log.splitlines():
        line = line.strip()
        if line and _ERROR_LINE.search(line) and line not in seen:
            seen.append(line)
    return "; ".join(seen[:5])


def run(netlist: str, timeout_s: float = TIMEOUT_S, expect_end_s: float | None = None) -> Waveforms:
    """Simulate. Raises SimulationError with ngspice's own words when it fails."""
    check_deck(netlist)
    binary = shutil.which(BINARY)
    if not binary:
        raise SimulationError("ngspice is not installed on this worker")
    with tempfile.TemporaryDirectory(prefix="emi-spice-") as tmp:
        with open(os.path.join(tmp, ".spiceinit"), "w", encoding="ascii") as f:
            f.write(SPICEINIT)
        with open(os.path.join(tmp, "circuit.cir"), "w", encoding="utf-8") as f:
            f.write(netlist)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tmp,
            "SPICE_ASCIIRAWFILE": "1",
            "LC_ALL": "C",
            "LANG": "C",
        }
        try:
            proc = subprocess.run(
                [binary, "-b", "-r", "out.raw", "circuit.cir"],
                cwd=tmp, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                errors="replace", timeout=timeout_s,
                preexec_fn=_limits(timeout_s) if os.name == "posix" else None,
            )
        except subprocess.TimeoutExpired as exc:
            raise SimulationError(f"ngspice did not finish within {timeout_s:.0f} s") from exc
        log = (proc.stdout or "") + (proc.stderr or "")
        raw_path = os.path.join(tmp, "out.raw")
        if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
            raise SimulationError(
                _summarise(log) or f"ngspice produced no results (exit status {proc.returncode})", log
            )
        with open(raw_path, encoding="ascii", errors="replace") as f:
            text = f.read()

    result = parse_raw(text)
    if expect_end_s is not None and (len(result.time) == 0 or result.time[-1] < 0.95 * expect_end_s):
        reached = result.time[-1] * 1e9 if len(result.time) else 0.0
        raise SimulationError(
            f"the simulation stopped at {reached:.2f} ns of {expect_end_s * 1e9:.0f} ns"
            + (f": {_summarise(log)}" if _summarise(log) else ""), log,
        )
    return result


def _vector_name(name: str) -> str:
    name = name.lower()
    if name.endswith("#branch"):
        return f"i({name[: -len('#branch')]})"
    return name


def parse_raw(text: str) -> Waveforms:
    """Read a single-plot ASCII raw file into arrays."""
    head, sep, body = text.partition("\nValues:\n")
    if not sep:
        raise SimulationError("ngspice wrote a results file with no values in it")
    if re.search(r"^Flags:.*complex", head, re.M):
        raise SimulationError("expected a transient analysis, got complex results")

    names: list[str] = []
    in_vars = False
    for line in head.splitlines():
        if line.startswith("Variables:"):
            in_vars = True
            continue
        if in_vars:
            fields = line.split()
            if len(fields) >= 2:
                names.append(_vector_name(fields[1]))
    if not names or names[0] != "time":
        raise SimulationError("the results file has no time axis")

    # A second plot would follow its own header; only one analysis is ever run.
    body = body.split("\nTitle:", 1)[0]
    tokens = body.split()
    per_point = len(names) + 1  # the point index, then one value per vector
    points = len(tokens) // per_point
    values = np.array(tokens[: points * per_point], dtype=float).reshape(points, per_point)[:, 1:]
    vectors = {name: values[:, i] for i, name in enumerate(names)}
    return Waveforms(time=vectors.pop("time"), vectors=vectors)
