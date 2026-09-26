"""Shared harness for the small-part verification: the product's own solve stage, offline.

``solve()`` runs ``stages.solve.run_solve`` -- the function a worker runs for a queued solve --
against a stand-in client that serves one board file and keeps what the stage uploads. So the
coupon cut, the band and end criterion, the budget, the mesh, openEMS, the post-processing and
``network.json`` are all the production path; only the HTTP is missing.

Every script here runs inside the worker image, with this folder's parent mounted:

    docker run --rm --cpus 3 --memory 6g -v "$PWD/worker:/spike" \\
        -v "$PWD/spike_out:/spike/spike_out" -w /spike -e PYTHONPATH=/spike \\
        --entrypoint python3 emi-worker:smallpart research/sp_verify_lines.py
"""

from __future__ import annotations

import json
import math
import os
import resource
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from emi_worker.client import RunToken
from emi_worker.kicad import parse, parse_board
from emi_worker.kicad.normalize import board_extent
from emi_worker.openems import coupon, post
from emi_worker.stages import StageContext, StageError
from emi_worker.stages import small_part
from emi_worker.stages.solve import run_solve

C0 = 299_792_458.0
ETA0 = 376.730313668
THREADS = int(os.environ.get("THREADS", "3"))
OUT = Path(os.environ.get("OUT", "/spike/spike_out"))

# A study may lift the budget to see where a part converges on its own; the product never does.
if os.environ.get("SP_BUDGET"):
    small_part.MAX_CELL_STEPS = float(os.environ["SP_BUDGET"])
    small_part.MAX_CELLS = int(float(os.environ.get("SP_MAX_CELLS", small_part.MAX_CELLS)))

#: The presets the app offers, (dx, dy, dz) in um.
PRESETS = {"coarse": (150, 150, 100), "normal": (75, 75, 50), "fine": (50, 50, 25)}


class _Client:
    """Just enough of ``client.Client`` for a stage: one board in, artifacts kept in memory."""

    def __init__(self, data: bytes):
        self.data = data
        self.files: dict[str, bytes] = {}
        self.events: list[dict] = []

    def run_input(self, token):
        return {"input_url": "memory://board"}

    def download(self, url):
        return self.data

    def progress(self, token, **fields):
        self.events.append(fields)

    def upload_artifact(self, token, name, blob, content_type):
        self.files[name] = blob
        return {"name": name, "size": len(blob)}


@dataclass
class Solved:
    summary: dict
    files: dict[str, bytes]
    workdir: Path
    elapsed_s: float
    #: Peak resident memory of openEMS, MB (the largest child this process has waited for).
    peak_mb: float
    events: list[dict] = field(default_factory=list)

    def json(self, name: str) -> dict:
        return json.loads(self.files[name])

    @property
    def manifest(self) -> dict:
        return self.json("manifest.json")


def solve(board_text: str, params: dict, name: str, *, keep: bool = True) -> Solved:
    """Run one solve through the production stage. ``params`` is what the app would send."""
    work = OUT / "smallpart" / name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    client = _Client(board_text.encode())
    token = RunToken(token="t", run_id="run", jti="j", expires_in=3600)
    ctx = StageContext(client=client, token=token, run={"params": params}, workdir=str(work),
                       cores=THREADS, max_cells=0, should_stop=lambda: False)
    started = time.monotonic()
    result = run_solve(ctx)
    elapsed = time.monotonic() - started
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024.0
    (work / "summary.json").write_text(json.dumps(result.summary, indent=2) + "\n")
    for fname in ("manifest.json", "network.json", "sparams.json"):
        if fname in client.files:
            (work / fname).write_bytes(client.files[fname])
    return Solved(summary=result.summary, files=client.files,
                  workdir=work / "run" / "openems", elapsed_s=elapsed, peak_mb=peak,
                  events=client.events)


def coupon_params(board_text: str, nets: list[str], *, preset: str = "normal",
                  margin_mm: float | None = None, freqs_hz: list[float] | None = None,
                  band_hz: tuple[float, float] | None = None) -> tuple[dict, coupon.Coupon]:
    """The params the app sends for a coupon, planned the way the app plans it."""
    board = parse_board(parse(board_text))
    c = coupon.plan(board, board_extent(board), nets, margin_mm=margin_mm)
    return small_part_params(c.roi, c.ports, preset=preset, nets=nets, freqs_hz=freqs_hz,
                             band_hz=band_hz, pads=c.port_pads), c


def small_part_params(roi, ports, *, preset: str = "normal", nets: list[str] | None = None,
                      freqs_hz=None, band_hz=None, pads=None) -> dict:
    dx, dy, dz = PRESETS[preset]
    p = {
        "mode": "small_part",
        "roi": {"min_x_mm": roi[0], "min_y_mm": roi[1], "max_x_mm": roi[2], "max_y_mm": roi[3]},
        "frequencies_hz": list(freqs_hz or []),
        "mesh": {"dx_um": dx, "dy_um": dy, "dz_um": dz},
        "ports": [
            {"name": q.name, "x_mm": q.x, "y_mm": q.y, "layer": q.layer,
             "half_width_mm": q.half_width_mm, "resistance": q.resistance,
             "excited": q.excited, "reference_layer": q.reference_layer,
             **({"pad": pads[i]} if pads and pads[i] else {})}
            for i, q in enumerate(ports)
        ],
    }
    if nets:
        p["coupon"] = {"nets": list(nets)}
    if band_hz:
        p["band_hz"] = list(band_hz)
    return p


# ---- closed forms ----

def hammerstad_jensen(w: float, h: float, er: float) -> tuple[float, float]:
    """Z0 and eps_eff of a zero-thickness microstrip (Hammerstad and Jensen, 1980)."""
    u = w / h
    f = 6.0 + (2 * math.pi - 6.0) * math.exp(-((30.666 / u) ** 0.7528))
    z01 = ETA0 / (2 * math.pi) * math.log(f / u + math.sqrt(1.0 + 4.0 / u ** 2))
    a = (1.0 + math.log((u ** 4 + (u / 52.0) ** 2) / (u ** 4 + 0.432)) / 49.0
         + math.log(1.0 + (u / 18.1) ** 3) / 18.7)
    b = 0.564 * ((er - 0.9) / (er + 3.0)) ** 0.053
    eeff = (er + 1) / 2 + (er - 1) / 2 * (1.0 + 10.0 / u) ** (-a * b)
    return z01 / math.sqrt(eeff), eeff


def ipc2141_microstrip(w: float, h: float, er: float, t: float = 0.0) -> float:
    return 87.0 / math.sqrt(er + 1.41) * math.log(5.98 * h / (0.8 * w + t))


def _ellipk(k: float) -> float:
    """Complete elliptic integral of the first kind K(k), by the arithmetic-geometric mean."""
    a, b = 1.0, math.sqrt(1.0 - k * k)
    for _ in range(40):
        a, b = (a + b) / 2.0, math.sqrt(a * b)
    return math.pi / (2.0 * a)


def stripline_cohn(w: float, b: float, er: float) -> float:
    """Z0 of a zero-thickness strip centred between planes ``b`` apart (Cohn, 1954). Exact."""
    k = 1.0 / math.cosh(math.pi * w / (2.0 * b))
    kp = math.tanh(math.pi * w / (2.0 * b))
    return ETA0 / (4.0 * math.sqrt(er)) * _ellipk(k) / _ellipk(kp)


def ipc2141_stripline(w: float, b: float, er: float, t: float = 0.0) -> float:
    return 60.0 / math.sqrt(er) * math.log(4.0 * b / (0.67 * math.pi * (0.8 * w + t)))


def width_for(z_target: float, fn, *args) -> float:
    lo, hi = 0.01, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if fn(mid, *args) > z_target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def goldfarb_pucel(r_mm: float, h_mm: float) -> float:
    """Inductance of a via of radius r through h to a ground plane, H (Goldfarb and Pucel, 1991)."""
    r, h = r_mm / 1000.0, h_mm / 1000.0
    root = math.sqrt(r * r + h * h)
    return 2e-7 * (h * math.log((h + root) / r) + 1.5 * (r - root))


# ---- two-port reading ----

def z_params(workdir: Path, freqs: np.ndarray, a: str = "p1", b: str = "p2") -> tuple:
    """Z11 and Z12 of a symmetric reciprocal two-port from one run with one port driven."""
    def spectra(name: str):
        u = post.read_probe(str(workdir / f"{name}_ut"))
        i = post.read_probe(str(workdir / f"{name}_it"))
        return post._dft(u, freqs), post._dft(i, freqs)

    v1, i1 = spectra(a)
    v2, i2 = spectra(b)
    det = i1 ** 2 - i2 ** 2
    z11 = (v1 * i1 - v2 * i2) / det
    z12 = (v2 * i1 - v1 * i2) / det
    return z11, z12


def line_from_z(z11, z12, length_m: float, freqs: np.ndarray) -> dict:
    """Z0 and eps_eff of a uniform line from its Z-parameters (Z11 = Z0 coth gl, Z12 = Z0/sinh gl)."""
    z0 = np.sqrt(z11 ** 2 - z12 ** 2)
    z0 = np.where(z0.real < 0, -z0, z0)
    gl = np.arccosh(z11 / z12)
    beta = np.abs(gl.imag) / length_m
    k0 = 2 * np.pi * freqs / C0
    return {"z0": z0, "eps_eff": (beta / k0) ** 2, "beta": beta}


def unwrapped_gl(z11, z12) -> np.ndarray:
    """gamma * l of a uniform line from its Z-parameters, phase unwrapped over a dense grid.

    cosh(gl) = Z11 / Z12 has two roots, e^gl and e^-gl; the lossy line's is the one outside
    the unit circle. arccosh alone folds the phase into [0, pi], which a line longer than half
    a wavelength at the top of the band passes.
    """
    x = z11 / z12
    root = x + np.sqrt(x * x - 1)
    root = np.where(np.abs(root) >= 1, root, 1 / root)
    gl = np.log(root)
    return gl.real + 1j * np.unwrap(gl.imag)


def write_report(name: str, report: dict) -> Path:
    path = OUT / "smallpart" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=float) + "\n")
    return path


def refused(fn, *args, **kwargs) -> str | None:
    """The refusal message if the stage refused, else None (and the result is discarded)."""
    try:
        fn(*args, **kwargs)
    except StageError as exc:
        return str(exc)
    return None
