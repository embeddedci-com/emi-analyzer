"""Worker configuration and self-inspection.

The worker has to tell the server what it can actually do, because EMI runs span four
orders of magnitude and the server refuses to dispatch a run no connected worker could
finish. Getting these numbers wrong in the optimistic direction means a laptop claims a
40-hour solve and dies on it, so everything here errs low.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field

from .estimate import BYTES_PER_CELL
from .scratch import default_workdir

#: Fraction of visible RAM we are willing to fill with field arrays.
#:
#: openEMS needs headroom beyond the six field/operator arrays -- dump buffers, the mesh
#: itself, the process image -- and a box that starts swapping mid-solve does not slow
#: down gracefully, it stops making progress entirely. 0.6 is deliberately cautious.
RAM_SAFETY_FRACTION = 0.6

#: Tools the worker looks for at startup. Their presence is advertised so the server can
#: tell a user "no connected worker has openEMS" instead of failing the run an hour in.
KNOWN_TOOLS = ("openEMS", "nf2ff", "nec2c", "ngspice", "kicad-cli", "AppCSXCAD")


@dataclass
class Config:
    server_url: str
    api_key: str
    name: str
    max_concurrent: int = 1
    poll_interval: float = 30.0
    reconnect_min: float = 1.0
    reconnect_max: float = 60.0
    workdir: str = field(default_factory=default_workdir)
    #: Override the auto-detected ceiling. Useful when the container has a cgroup limit
    #: the kernel does not report through the usual files.
    max_cells_override: int = 0
    verify_tls: bool = True
    #: Keep each run's scratch directory instead of deleting it. Debugging only -- it is
    #: the one thing that would otherwise accumulate on disk between runs.
    keep_scratch: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        url = os.environ.get("EMBEDDEDCI_URL", "http://localhost:8090").rstrip("/")
        key = os.environ.get("EMBEDDEDCI_API_KEY", "")
        if not key:
            raise SystemExit(
                "EMBEDDEDCI_API_KEY is not set.\n"
                "Issue one with: make key   (local stack)"
            )
        return cls(
            server_url=url,
            api_key=key,
            # Empty counts as unset. An env file that carries `EMI_WORKER_NAME=` with no
            # value -- which is how .env.example ships it -- otherwise registers a worker
            # with no name at all, and it shows up as a blank row in the UI.
            name=(os.environ.get("EMI_WORKER_NAME", "").strip()
                  or platform.node() or "emi-worker"),
            max_concurrent=int(os.environ.get("EMI_MAX_CONCURRENT", "1")),
            poll_interval=float(os.environ.get("EMI_POLL_INTERVAL", "30")),
            workdir=os.environ.get("EMI_WORKDIR", "").strip() or default_workdir(),
            max_cells_override=int(os.environ.get("EMI_MAX_CELLS", "0")),
            verify_tls=os.environ.get("EMI_VERIFY_TLS", "1") != "0",
            keep_scratch=os.environ.get("EMI_KEEP_SCRATCH", "") not in ("", "0"),
        )

    @property
    def api(self) -> str:
        return f"{self.server_url}/api"

    @property
    def ws_url(self) -> str:
        base = self.api.replace("https://", "wss://").replace("http://", "ws://")
        return f"{base}/emi-agent/ws"


@dataclass
class Capabilities:
    cores: int
    ram_gb: float
    max_cells: int
    openems_version: str = ""
    tools: list[str] = field(default_factory=list)
    kinds: list[str] = field(default_factory=lambda: ["ingest", "solve"])

    def as_dict(self) -> dict:
        return {
            "cores": self.cores,
            "ram_gb": round(self.ram_gb, 2),
            "max_cells": self.max_cells,
            "openems_version": self.openems_version,
            "tools": self.tools,
            "kinds": self.kinds,
        }


def _cgroup_memory_limit() -> int | None:
    """Return the container's memory limit in bytes, if one is set.

    ``docker run --memory 24g`` shows up here but *not* in /proc/meminfo, which still
    reports the host's total. Reading the host figure inside a limited container is how a
    worker ends up promising four times the RAM it can actually use.
    """
    for path in (
        "/sys/fs/cgroup/memory.max",  # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
    ):
        try:
            with open(path) as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        # cgroup v1 reports an absurd sentinel when unlimited.
        if value <= 0 or value >= 1 << 62:
            return None
        return value
    return None


def _total_memory_bytes() -> int:
    limit = _cgroup_memory_limit()
    if limit:
        return limit
    try:  # Linux
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    try:  # macOS / BSD
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
        )
        if out.returncode == 0:
            return int(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return 4 * 1024**3  # a pessimistic floor beats guessing high


def _cgroup_cpu_quota() -> float | None:
    """Return the container's CPU quota in whole cores, if one is set.

    ``docker run --cpus 4`` is a *quota*, not a cpuset: the process still sees every core
    through sched_getaffinity and would advertise 14 cores while being throttled to 4.
    That over-reporting is the CPU twin of reading the host's MemTotal inside a limited
    container.
    """
    try:  # cgroup v2: "<quota> <period>", or "max <period>" when unlimited
        with open("/sys/fs/cgroup/cpu.max") as fh:
            quota, period = fh.read().split()
        if quota != "max" and int(period) > 0:
            return int(quota) / int(period)
        return None
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") as fh:
            quota = int(fh.read().strip())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as fh:
            period = int(fh.read().strip())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass
    return None


def _cpu_count() -> int:
    # sched_getaffinity respects --cpuset-cpus; os.cpu_count() does not.
    try:
        affinity = len(os.sched_getaffinity(0))
    except AttributeError:
        affinity = os.cpu_count() or 1

    quota = _cgroup_cpu_quota()
    if quota:
        # Round down: half a core of headroom is worth more than an optimistic count.
        return max(1, min(affinity, int(quota)))
    return max(1, affinity)


def _version_line(text: str) -> str:
    """The line of openEMS's banner that names its version.

    openEMS frames its banner in dashes and "|"; the first non-empty line may be the frame,
    which says nothing about which solver this is.
    """
    lines = [ln.strip().strip("|").strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and ln.strip("-")]
    for line in lines:
        if "version" in line.lower():
            return line[:120]
    return lines[0][:120] if lines else "unknown"


def _openems_version() -> str:
    exe = shutil.which("openEMS")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
        return _version_line(out.stdout or out.stderr or "")
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def detect_capabilities(cfg: Config) -> Capabilities:
    """Inspect the machine and describe it to the server."""
    cores = _cpu_count()
    ram_bytes = _total_memory_bytes()

    max_cells = cfg.max_cells_override
    if max_cells <= 0:
        max_cells = int(ram_bytes * RAM_SAFETY_FRACTION / BYTES_PER_CELL)

    tools = [name for name in KNOWN_TOOLS if shutil.which(name)]

    kinds = ["ingest"]
    # Advertising "solve" without a solver present would let the server hand this worker
    # work it can only fail. Better to be visibly ingest-only.
    if shutil.which("openEMS"):
        kinds.append("solve")
    # The same rule for the circuit simulator. ESD transients are seconds of work, so any
    # worker with ngspice takes them -- including the ingest-only one on the droplet.
    if shutil.which("ngspice"):
        kinds.append("transient")
    # And the antenna solver. A cable budget is a few hundred milliseconds of method-of-moments
    # on a wire, so it belongs with the other cheap kinds rather than behind a solver worker.
    if shutil.which("nec2c"):
        kinds.append("cable")
    # Compliance is arithmetic on artifacts other runs already produced -- no solver at all --
    # so every worker takes it, including the ingest-only one. A user changing a cable length
    # or swapping a driver gets the answer back without waiting for a solver worker to be free.
    kinds.append("compliance")

    return Capabilities(
        cores=cores,
        ram_gb=ram_bytes / 1e9,
        max_cells=max_cells,
        openems_version=_openems_version(),
        tools=tools,
        kinds=kinds,
    )
