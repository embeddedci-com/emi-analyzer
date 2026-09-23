"""Turn openEMS output into the artifacts the browser reads.

The browser must never see HDF5. Everything here flattens to float32 grids plus a JSON
manifest, so the frontend fetches exactly the one frequency it is displaying and needs no
scientific-data library at all.

openEMS's frequency-domain dump layout, verified against a real run:

    /FieldData/FD                     attrs: frequency = [f0, f1, ...]
    /FieldData/FD/f<N>_real           shape (3, nz, ny, nx), float32
    /FieldData/FD/f<N>_imag           same
    /Mesh/x, /Mesh/y, /Mesh/z         line coordinates, in **metres**

The mesh in the file is the *dump box's* subgrid, not the whole simulation grid.
"""

from __future__ import annotations

import json
import logging
import math
import os
import struct
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)

#: Floor for the dB conversion, relative to the peak. Field magnitude spans many orders of
#: magnitude and the bottom of that range is numerical noise; clamping keeps the colour
#: scale spent on the part of the map that means something.
DYNAMIC_RANGE_DB = 60.0


@dataclass
class FieldGrid:
    """One frequency's field magnitude over a plane."""

    frequency_hz: float
    #: Magnitude, shape (ny, nx), row-major.
    magnitude: np.ndarray
    #: Grid line coordinates in mm.
    x_mm: np.ndarray
    y_mm: np.ndarray
    z_mm: float

    @property
    def extent_mm(self) -> tuple[float, float, float, float]:
        return (
            float(self.x_mm[0]), float(self.y_mm[0]),
            float(self.x_mm[-1]), float(self.y_mm[-1]),
        )

    def to_db(self, reference: float | None = None) -> np.ndarray:
        """Magnitude in dB relative to ``reference`` (default: this grid's peak).

        A shared reference across frequencies is what makes two maps comparable; using each
        grid's own peak would make a quiet frequency look as hot as a loud one.
        """
        peak = reference if reference is not None else float(self.magnitude.max())
        if peak <= 0:
            return np.zeros_like(self.magnitude, dtype=np.float32)
        with np.errstate(divide="ignore"):
            db = 20.0 * np.log10(np.maximum(self.magnitude, 1e-30) / peak)
        return np.maximum(db, -DYNAMIC_RANGE_DB).astype(np.float32)


def read_fd_dump(path: str) -> list[FieldGrid]:
    """Read an openEMS frequency-domain dump into one FieldGrid per frequency."""
    import h5py  # imported lazily so ingest-only workers need no HDF5 stack

    grids: list[FieldGrid] = []
    with h5py.File(path, "r") as f:
        if "FieldData/FD" not in f:
            raise ValueError(f"{os.path.basename(path)} has no frequency-domain data")
        fd = f["FieldData/FD"]
        freqs = np.asarray(fd.attrs.get("frequency", []), dtype=np.float64).ravel()
        if freqs.size == 0:
            raise ValueError(f"{os.path.basename(path)} names no frequencies")

        # Metres in the file, millimetres everywhere in this system.
        x_mm = np.asarray(f["Mesh/x"][:], dtype=np.float64) * 1000.0
        y_mm = np.asarray(f["Mesh/y"][:], dtype=np.float64) * 1000.0
        z_mm = np.asarray(f["Mesh/z"][:], dtype=np.float64) * 1000.0

        for i, freq in enumerate(freqs):
            re = np.asarray(fd[f"f{i}_real"][:], dtype=np.float64)
            im = np.asarray(fd[f"f{i}_imag"][:], dtype=np.float64)
            # (components, nz, ny, nx) -> magnitude of the complex vector, per cell.
            mag = np.sqrt((re ** 2 + im ** 2).sum(axis=0))
            if mag.ndim == 3:
                # A dump on a plane still carries a singleton z axis.
                mag = mag[mag.shape[0] // 2]
            grids.append(FieldGrid(
                frequency_hz=float(freq),
                magnitude=mag.astype(np.float64),
                x_mm=x_mm, y_mm=y_mm,
                z_mm=float(z_mm[len(z_mm) // 2]) if len(z_mm) else 0.0,
            ))
    return grids


@dataclass
class ProbeTrace:
    time_s: np.ndarray
    values: np.ndarray


def read_probe(path: str) -> ProbeTrace:
    """Read an openEMS probe file: '%' comment header, then time/value columns."""
    times: list[float] = []
    values: list[float] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("%"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                try:
                    times.append(float(parts[0]))
                    values.append(float(parts[1]))
                except ValueError:
                    continue
    return ProbeTrace(
        time_s=np.asarray(times, dtype=np.float64),
        values=np.asarray(values, dtype=np.float64),
    )


def _dft(trace: ProbeTrace, frequencies: np.ndarray) -> np.ndarray:
    """Discrete Fourier transform at arbitrary frequencies.

    A direct DFT rather than an FFT because the frequencies of interest are a handful of
    named clock harmonics, not a uniform bin grid — and evaluating exactly at them avoids
    the spectral leakage that reading the nearest FFT bin would introduce.
    """
    if trace.time_s.size < 2:
        return np.zeros(frequencies.shape, dtype=np.complex128)
    dt = float(np.mean(np.diff(trace.time_s)))
    phase = np.exp(-2j * np.pi * np.outer(frequencies, trace.time_s))
    return phase @ trace.values * dt


def port_s11(
    voltage: ProbeTrace,
    current: ProbeTrace,
    frequencies: list[float],
    z0: float = 50.0,
) -> dict:
    """S11 and impedance at a lumped port.

    A lumped port needs no de-embedding: the probes sit at the port itself, so the
    impedance seen there is the port impedance and the reflection follows directly.
    """
    f = np.asarray(frequencies, dtype=np.float64)
    u = _dft(voltage, f)
    i = _dft(current, f)

    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(np.abs(i) > 0, u / i, np.inf)
        s11 = (z - z0) / (z + z0)

    out = []
    for k, freq in enumerate(f):
        zk, sk = z[k], s11[k]
        finite = np.isfinite(zk) and np.isfinite(sk)
        out.append({
            "frequency_hz": float(freq),
            "z_real": float(zk.real) if finite else None,
            "z_imag": float(zk.imag) if finite else None,
            "s11_db": float(20 * math.log10(max(abs(sk), 1e-12))) if finite else None,
            "s11_mag": float(abs(sk)) if finite else None,
        })
    return {"reference_impedance": z0, "points": out}


#: How many log-spaced points the dense port grid carries across the solved band.
#: §16.2 wants "about 60 frequencies across the band" to interpolate a spectrum; cables
#: (§7) read the same grid. It is cheap: a direct DFT of one probe at 60 frequencies is
#: microseconds against a solve measured in hours.
DENSE_GRID_POINTS = 60


def dense_grid(frequencies: list[float], points: int = DENSE_GRID_POINTS) -> list[float]:
    """A log-spaced grid spanning the requested frequencies.

    It does not extend below the lowest requested frequency. A DFT can be evaluated at any
    frequency at all, and that is exactly the trap: below f_min the record is shorter than
    the three periods ``required_timesteps`` sized it for, so the answer would be confidently
    wrong rather than absent. Whatever needs 30 MHz has to ask the solve for 30 MHz.
    """
    lo, hi = min(frequencies), max(frequencies)
    if lo <= 0:
        raise ValueError("frequencies must be positive")
    if hi == lo:
        return [lo]
    step = (hi / lo) ** (1.0 / (points - 1))
    return [lo * step ** k for k in range(points)]


def port_spectra(
    voltage: ProbeTrace, current: ProbeTrace, frequencies: list[float]
) -> dict:
    """Complex V and I at a port, which is everything a driver needs to re-weight (§8).

    Kept separate from ``port_s11``: S11 is a display quantity reduced to magnitude, while
    this is the raw complex pair. Re-weighting divides by ``i``, so throwing away phase here
    would silently drop the reactive part of every input impedance.
    """
    f = np.asarray(frequencies, dtype=np.float64)
    u = _dft(voltage, f)
    i = _dft(current, f)
    return {
        "frequencies_hz": [float(v) for v in f],
        "v_real": [float(v) for v in u.real],
        "v_imag": [float(v) for v in u.imag],
        "i_real": [float(v) for v in i.real],
        "i_imag": [float(v) for v in i.imag],
    }


def cable_transfer(
    gap: ProbeTrace,
    port_voltage: ProbeTrace,
    port_current: ProbeTrace,
    frequencies: list[float],
    source_impedance_ohm: float = 50.0,
) -> dict:
    """§7's H_cm(f) = V_oc / V_src: the gap's open-circuit voltage per volt of source.

    The denominator is the solve's own **Thévenin source**, not the voltage that appeared at
    the port. openEMS excites a lumped element, so the port's terminals see whatever is left
    after the structure has loaded the source; dividing by that would fold the port's input
    impedance into a number that is supposed to be independent of it.

        V_src = V_port + I_port · Z_s

    The result is dimensionless and excitation-independent, which is the point: a driver
    attached later supplies its own V_src, and M0 measured that re-weighting this way is exact
    to 0.01 dB wherever the record has energy at the frequency (D2).
    """
    f = np.asarray(frequencies, dtype=np.float64)
    v_gap = _dft(gap, f)
    v_port = _dft(port_voltage, f)
    i_port = _dft(port_current, f)
    v_src = v_port + i_port * source_impedance_ohm

    with np.errstate(divide="ignore", invalid="ignore"):
        h = np.where(np.abs(v_src) > 0, v_gap / v_src, 0.0)

    return {
        "frequencies_hz": [float(v) for v in f],
        "h_real": [float(v) for v in np.real(h)],
        "h_imag": [float(v) for v in np.imag(h)],
        # Where the source delivered nothing there is no transfer function, only a ratio of
        # two small numbers. Saying so beats publishing the noise.
        "usable": [bool(v) for v in (np.abs(v_src) > 0)],
    }


#: openEMS writes its frequency-domain field dumps single-sided: 2 · Σ x(t)·e^(-jωt)·Δt
#: (``ProcessFieldsFD``, "*2 for single-sided spectrum"), and its own port post-processing
#: (``DFT_time2freq``) uses the same factor. ``_dft`` above has no factor of 2, because every
#: quantity it has fed so far is a *ratio* of two of its own transforms and the factor cancels.
#: A far field divided by a source voltage is a ratio across the two conventions, so the port's
#: side has to be brought onto openEMS's before dividing -- or every far field is 6 dB high.
OPENEMS_FD_SCALE = 2.0


def source_spectrum(
    workdir: str, port: str, source_impedance_ohm: float, frequencies: list[float],
) -> dict:
    """The solve's own Thévenin source and input impedance at ``frequencies``.

    The source is ``V_port + I_port · Z_s``, as in ``cable_transfer``, in openEMS's
    frequency-domain convention so a field dump can be divided by it. ``z_in`` is what a driver
    attached later will see, and is convention-free.
    """
    u = read_probe(os.path.join(workdir, f"{port}_ut"))
    i = read_probe(os.path.join(workdir, f"{port}_it"))
    f = np.asarray(frequencies, dtype=np.float64)
    v_port = _dft(u, f)
    i_port = _dft(i, f)
    v_src = (v_port + i_port * source_impedance_ohm) * OPENEMS_FD_SCALE
    with np.errstate(divide="ignore", invalid="ignore"):
        z_in = np.where(np.abs(i_port) > 0, v_port / i_port, np.nan + 0j)
    return {"v_src": v_src, "z_in": z_in}


@dataclass
class PostResult:
    """Files to upload, plus the manifest describing them."""

    files: dict[str, bytes] = field(default_factory=dict)
    manifest: dict = field(default_factory=dict)


def build_artifacts(
    workdir: str,
    dump_names: dict[str, str],
    frequencies: list[float],
    port_names: list[str],
    *,
    run_meta: dict | None = None,
    modelled_parts: list[dict] | None = None,
    cable_ports: list[dict] | None = None,
) -> PostResult:
    """Collect openEMS output into the browser-facing artifact set."""
    result = PostResult()
    layers: list[dict] = []

    # One shared dB reference across every layer and frequency, so the maps are comparable
    # rather than each normalised to its own peak.
    peak = 0.0
    collected: dict[str, list[FieldGrid]] = {}
    for layer, dump in dump_names.items():
        path = os.path.join(workdir, f"{dump}.h5")
        if not os.path.exists(path):
            log.warning("dump %s produced no file", dump)
            continue
        try:
            grids = read_fd_dump(path)
        except (OSError, ValueError) as exc:
            log.warning("could not read dump %s: %s", dump, exc)
            continue
        collected[layer] = grids
        for g in grids:
            peak = max(peak, float(g.magnitude.max()))

    if not collected:
        raise ValueError(
            "the solver produced no field data. Check the solver log for warnings about "
            "the excitation or the mesh."
        )

    for layer, grids in collected.items():
        entries = []
        for g in grids:
            db = g.to_db(reference=peak)
            name = f"nearfield/{layer.replace('.', '_')}/{int(g.frequency_hz)}.bin"
            result.files[name] = struct.pack(f"<{db.size}f", *db.ravel(order="C").tolist())
            entries.append({
                "frequency_hz": g.frequency_hz,
                "file": name,
                "width": int(db.shape[1]),
                "height": int(db.shape[0]),
                "extent_mm": [round(v, 4) for v in g.extent_mm],
                "z_mm": round(g.z_mm, 4),
                "peak_db": round(float(db.max()), 2),
            })
        layers.append({"layer": layer, "z_mm": grids[0].z_mm, "grids": entries})

    # S-parameters, one per excited port.
    sparams = []
    for port in port_names:
        u_path = os.path.join(workdir, f"{port}_ut")
        i_path = os.path.join(workdir, f"{port}_it")
        if not (os.path.exists(u_path) and os.path.exists(i_path)):
            continue
        try:
            sparams.append({
                "port": port,
                **port_s11(read_probe(u_path), read_probe(i_path), frequencies),
            })
        except (OSError, ValueError) as exc:
            log.warning("could not post-process port %s: %s", port, exc)

    if sparams:
        result.files["sparams.json"] = json.dumps(
            {"ports": sparams}, separators=(",", ":")
        ).encode()

    # Port spectra, the input to every driver re-weighting (§8, §10). Written at the dump
    # frequencies and again on a dense log grid, because cables and the far field
    # interpolate a spectrum while the near-field maps only exist at the dump frequencies.
    dense = dense_grid(frequencies) if frequencies else []
    ports: list[dict] = []
    for port in port_names:
        u_path = os.path.join(workdir, f"{port}_ut")
        i_path = os.path.join(workdir, f"{port}_it")
        if not (os.path.exists(u_path) and os.path.exists(i_path)):
            continue
        try:
            u, i = read_probe(u_path), read_probe(i_path)
            ports.append({
                "port": port,
                "at_dump_frequencies": port_spectra(u, i, frequencies),
                "dense": port_spectra(u, i, dense) if dense else None,
            })
        except (OSError, ValueError) as exc:
            log.warning("could not read port spectra for %s: %s", port, exc)

    if ports:
        result.files["ports.json"] = json.dumps(
            {"ports": ports}, separators=(",", ":")
        ).encode()

    # Cable gap ports (§7). One transfer function per port, against the first excited port's
    # source, on the same dense grid the far field and the cables use.
    cable_transfers: list[dict] = []
    if cable_ports and ports and dense:
        driver_port = port_names[0]
        u_path = os.path.join(workdir, f"{driver_port}_ut")
        i_path = os.path.join(workdir, f"{driver_port}_it")
        for meta in cable_ports:
            gap_path = os.path.join(workdir, meta["probe"])
            if not (os.path.exists(gap_path) and os.path.exists(u_path)
                    and os.path.exists(i_path)):
                log.warning("cable port %s has no probe output", meta.get("ref"))
                continue
            try:
                cable_transfers.append({
                    **{k: v for k, v in meta.items() if k != "probe"},
                    "driven_by": driver_port,
                    "transfer": cable_transfer(
                        read_probe(gap_path), read_probe(u_path), read_probe(i_path), dense),
                })
            except (OSError, ValueError) as exc:
                log.warning("could not read cable port %s: %s", meta.get("ref"), exc)

    if cable_transfers:
        result.files["cable_ports.json"] = json.dumps(
            {"ports": cable_transfers}, separators=(",", ":")
        ).encode()

    result.manifest = {
        # Version 2 adds ports.json. A version-1 result cannot have a driver attached to it
        # -- the complex port spectra were never recorded and cannot be recovered from the
        # artifacts -- so the UI offers a re-run rather than a broken control.
        "format_version": 2,
        "metric": "surface_current_density",
        "metric_detail": (
            "tangential magnetic field on the grid line immediately above each copper "
            "layer, which is the surface current density |J_s| = |n x H|"
        ),
        "units": {"nearfield": "dB relative to the peak across all layers and frequencies"},
        "dynamic_range_db": DYNAMIC_RANGE_DB,
        "reference_magnitude": peak,
        "frequencies_hz": frequencies,
        "layers": layers,
        "has_sparams": bool(sparams),
        "has_port_spectra": bool(ports),
        # §12: which parts were modelled rather than left as bare copper, and on whose
        # authority. An empty list is the honest answer for a solve that modelled none, and
        # is what every result carried before this existed.
        "modelled_parts": modelled_parts or [],
        # §7: which connectors carry a gap port, so a result can be composed with a cable
        # without re-reading the solve's parameters.
        "cable_ports": [m["ref"] for m in cable_transfers],
        "dense_frequencies_hz": dense,
        "run": run_meta or {},
    }
    result.files["manifest.json"] = json.dumps(
        result.manifest, indent=2
    ).encode()
    return result
