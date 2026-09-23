"""From a finished solve and a driver to the radiating paths compliance combines (§7, §16.2).

The compliance run used to take its paths from the request, already composed, and the browser
sent none -- so every estimate came back "nothing radiating has been modelled". The paths are
built here instead, in the worker, from what the solve itself wrote:

* ``farfield.json`` -- the board's own field at 3 m, per volt of the solve's source;
* ``cable_ports.json`` -- each gap port's H_cm, per volt of the same source;
* ``cable_antenna.json`` -- each cable's Z_ant and E per amp;
* ``ports.json`` -- the port's input impedance, which a driver with a different source
  impedance needs.

and from the driver the user attached. Nothing that decides the answer comes from the request.

**Transfer functions are evaluated at the driver's own frequencies.** A clock is a line
spectrum. Composing it on the solve's 60-point log grid, as the cable view does, drove a
harmonic only where one happened to land within 100 ppm of a grid point -- two of sixty for a
25 MHz clock. Here the transfer functions are interpolated to every harmonic instead: in dB
against log frequency for magnitudes, linearly in log frequency for the real and imaginary
parts of an impedance. Only inside the band each one covers; never extrapolated.

**A driver is not the solve's source.** The solve drove its port from Z_s (the port's own
resistance, 50 ohm by default). Everything inside the structure is proportional to the port
current, so a driver of open-circuit voltage V_d behind Z_d changes every field by

    I_d / I_solve = (V_d / (Z_d + Z_in)) / (V_src / (Z_s + Z_in))

and a transfer function per volt of V_src becomes a field per volt of V_d times
``|Z_s + Z_in| / |Z_d + Z_in|``. Leaving that factor out is exact only when Z_d = Z_s.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from emi_worker.compliance.limits import in_range, standard
from emi_worker.compliance.predict import (
    SIGMA_CABLE_DB,
    SIGMA_FAR_FIELD_INTERP_DB,
    SIGMA_MESH_DB,
    SIGMA_ASSUMED_EPSILON_DB,
    Path,
    PathPoint,
)
from emi_worker.drivers.apply import NULL_FLOOR
from emi_worker.drivers.document import Driver
from emi_worker.drivers.resolve import resolve
from emi_worker.drivers.spectrum import DriverError

#: Far-field documents older than this are in the solve pulse's units and cannot take a driver.
FAR_FIELD_VERSION = 2

#: A clock's harmonics are evaluated one by one. A 10 kHz regulator to 1 GHz is 100,000 of
#: them; past this the estimate says so rather than spending minutes on arithmetic.
MAX_LINES = 50_000


@dataclass
class SolveArtifacts:
    """The JSON a solve run wrote, as the compliance run downloaded it."""

    manifest: dict
    ports: dict | None = None
    farfield: dict | None = None
    cable_ports: dict | None = None
    cable_antenna: dict | None = None


@dataclass
class AttachedDriver:
    id: str
    driver: Driver


@dataclass
class Assembly:
    paths: list[Path] = field(default_factory=list)
    #: σ terms shared by every path (§17.2): the driver's provenance.
    shared_sigma: dict = field(default_factory=dict)
    #: Excited ports of the solve, and which of them have a far field.
    excited_ports: list[str] = field(default_factory=list)
    far_field_ports: list[str] = field(default_factory=list)
    #: Connector -> {"cable_id", "length_m"} for every cable the solve carries antenna terms for.
    modelled_cables: dict = field(default_factory=dict)
    #: Path label -> (lo, hi) Hz that its transfer function covers.
    covered: dict = field(default_factory=dict)
    #: Frequency -> why the driver or the solve says nothing there.
    undriven: dict = field(default_factory=dict)
    #: The highest frequency the drivers say the product uses, for the scan's upper edge.
    highest_hz: float | None = None
    #: Problems with the solve's artifacts, as (key, message) -- each one a gap.
    problems: list = field(default_factory=list)
    #: Per path label, the nets and board point recommendations are drawn from.
    path_meta: dict = field(default_factory=dict)
    #: Distance every field was computed at, from the artifacts themselves.
    distances_m: set = field(default_factory=set)


# ---- interpolation, never extrapolation ------------------------------------------------

def _bracket(fs: list[float], f: float) -> tuple[int, int, float] | None:
    """Indices either side of ``f`` and how far along in log frequency, or None outside."""
    if not fs or f < fs[0] * (1 - 1e-9) or f > fs[-1] * (1 + 1e-9):
        return None
    for k in range(len(fs) - 1):
        if fs[k] <= f <= fs[k + 1]:
            if fs[k + 1] == fs[k]:
                return k, k, 0.0
            return k, k + 1, math.log(f / fs[k]) / math.log(fs[k + 1] / fs[k])
    k = 0 if f <= fs[0] else len(fs) - 1
    return k, k, 0.0


def interp_db(fs: list[float], mags: list[float], f: float) -> float | None:
    """A magnitude at ``f``, interpolated in dB against log frequency."""
    b = _bracket(fs, f)
    if b is None:
        return None
    i, j, t = b
    a, c = mags[i], mags[j]
    if a <= 0 or c <= 0:
        # No logarithm of a zero. At a point it is that point; between, the larger
        # neighbour is the conservative read.
        return a if t == 0.0 else c if t == 1.0 else max(a, c)
    return math.exp((1 - t) * math.log(a) + t * math.log(c))


def interp_complex(fs: list[float], re: list[float], im: list[float], f: float) -> complex | None:
    """A complex value at ``f``, its parts interpolated linearly in log frequency."""
    b = _bracket(fs, f)
    if b is None:
        return None
    i, j, t = b
    return complex((1 - t) * re[i] + t * re[j], (1 - t) * im[i] + t * im[j])


def _usable_series(fs, usable, *series):
    """Keep only the usable points, and report any unusable one inside the usable span."""
    keep = [k for k, ok in enumerate(usable) if ok]
    if not keep:
        return [], [[] for _ in series], []
    lo, hi = fs[keep[0]], fs[keep[-1]]
    holes = [fs[k] for k, ok in enumerate(usable) if not ok and lo < fs[k] < hi]
    return [fs[k] for k in keep], [[s[k] for k in keep] for s in series], holes


# ---- the driver --------------------------------------------------------------------------

def _period(d: Driver) -> float | None:
    if d.kind == "trapezoid":
        return d.trapezoid().period_s
    if d.kind == "waveform":
        return d.values["period_s"].value
    return None


def _evaluation_frequencies(d: Driver, band: tuple[float, float], grid: list[float],
                            standard_id: str) -> tuple[list[float], bool]:
    """Where to evaluate one path for this driver, and whether they are spectral lines.

    A periodic driver is evaluated at its harmonics inside the band; an uploaded spectrum is
    continuous and is read at the path's own grid points inside its range.
    """
    lo, hi = band
    period = _period(d)
    if period is not None:
        n0 = max(1, math.ceil(lo * period * (1 - 1e-12)))
        n1 = math.floor(hi * period * (1 + 1e-12))
        if n1 - n0 + 1 > MAX_LINES:
            raise DriverError(
                f"this driver has {n1 - n0 + 1:,} harmonics in {lo / 1e6:g}-{hi / 1e6:g} MHz, "
                f"more than the {MAX_LINES:,} a compliance run evaluates"
            )
        freqs = [n / period for n in range(n0, n1 + 1)]
        return [f for f in freqs if in_range(standard_id, f)], True
    pts = d.payload.get("points") or []
    if not pts:
        return [], False
    s_lo, s_hi = pts[0][0], pts[-1][0]
    return [f for f in grid if s_lo <= f <= s_hi and in_range(standard_id, f)], False


def _volts(d: Driver, freqs: list[float], asm: Assembly) -> list[float | None]:
    """RMS source volts at ``freqs``; ``None`` where the driver says nothing (recorded)."""
    if not freqs:
        return []
    r = resolve(d, freqs)
    out: list[float | None] = []
    for f, v in zip(freqs, r.volts):
        if v is None:
            out.append(None)
            continue
        mag = abs(v)
        # A null of the driver's own spectrum is a real zero, not a small number (apply.py).
        out.append(0.0 if r.scale_v > 0 and mag < r.scale_v * NULL_FLOOR else mag)
    for f, why in r.undriven.items():
        asm.undriven[f] = why
    return out


def _source_factor(z_s: float, z_d: float, z_in: complex) -> float:
    den = abs(z_d + z_in)
    if den == 0:
        raise DriverError("the driver's source impedance cancels the port's input impedance")
    return abs(z_s + z_in) / den


def mesh_preset(manifest: dict) -> str:
    """The preset the solve's requested cell size corresponds to; coarse when not recorded.

    Coarse is the conservative reading: it carries the largest σ term."""
    req = (manifest.get("run") or {}).get("mesh_request") or {}
    dx = req.get("dx_um")
    if dx is None:
        return "coarse"
    dx = float(dx)
    return "fine" if dx <= 50 else "normal" if dx <= 75 else "coarse"


# ---- the paths -----------------------------------------------------------------------------

def assemble(art: SolveArtifacts, attached: AttachedDriver | None,
             standard_id: str) -> Assembly:
    """Every radiating path the solve supports, driven by ``attached``."""
    asm = Assembly()
    std = standard(standard_id)
    std_lo, std_hi = std.range_hz()
    manifest = art.manifest or {}
    run_meta = manifest.get("run") or {}

    ports_meta = run_meta.get("ports")
    if ports_meta is None:
        asm.problems.append((
            "solve-format",
            "This solve predates the record of its port resistances, so a driver cannot "
            "replace its source. Run the solve again.",
        ))
        return asm
    resistance = {p["name"]: float(p.get("resistance_ohm", 50.0)) for p in ports_meta}
    asm.excited_ports = [p["name"] for p in ports_meta if p.get("excited")]

    # A run that stopped on its timestep cap transformed fields that were still ringing. Its
    # artifacts mark every point unusable, but a result from before they did says so only
    # here, so this is where it is refused: no path at all, and the gate says why.
    if run_meta.get("converged") is False:
        energy = run_meta.get("final_energy_db")
        down = f" only {abs(float(energy)):.0f} dB down" if energy is not None else ""
        asm.problems.append((
            "solve-unconverged",
            f"The solve hit its timestep limit with its energy{down}, before its fields "
            f"settled, so none of its levels are used. Run it again over a smaller region.",
        ))
        return asm

    if attached is not None:
        d = attached.driver
        asm.shared_sigma[f"driver provenance ({d.weakest_source()})"] = d.sigma_db()
        period = _period(d)
        if period is not None:
            asm.highest_hz = 1.0 / period

    # -- the board's own far field --------------------------------------------------------
    ff = art.farfield
    if ff is not None:
        if int(ff.get("format_version") or 1) < FAR_FIELD_VERSION:
            asm.problems.append((
                "far-field-format",
                "This solve's far field is in the solver's own pulse units, not per volt of "
                "source, so a driver cannot be applied to it. Run the solve again.",
            ))
        elif len(ff.get("excited_ports") or []) > 1:
            asm.problems.append((
                "far-field-sources",
                "This solve excited more than one port at once, so its far field is the "
                "response to all of them and no single driver can be attached to it.",
            ))
        else:
            asm.far_field_ports.append(ff["driven_by"])
            asm.distances_m.add(float(ff.get("distance_m") or 0.0))
            fs, (e, zr, zi), holes = _usable_series(
                ff["frequencies_hz"], ff["usable"], ff["e_per_volt"], ff["z_in_real"],
                ff["z_in_imag"])
            for f in holes:
                asm.undriven[f] = (f"the solve put no source energy near {f / 1e6:g} MHz, so "
                                   f"the board's far field is unknown there")
            if fs:
                label = "board (solved region)"
                band = (max(fs[0], std_lo), min(fs[-1], std_hi))
                asm.covered[label] = (fs[0], fs[-1])
                if attached is not None and band[0] <= band[1]:
                    path = _board_path(label, attached, fs, e, zr, zi,
                                       float(ff.get("source_impedance_ohm") or 50.0),
                                       band, standard_id, asm, manifest)
                    if path is not None:
                        asm.paths.append(path)
                        asm.path_meta[label] = {"nets": [attached.driver.net]
                                                if attached.driver.net else []}

    # -- the cables -------------------------------------------------------------------------
    transfers = {p["ref"]: p for p in ((art.cable_ports or {}).get("ports") or [])}
    antennas = {c["ref"]: c for c in ((art.cable_antenna or {}).get("cables") or [])}
    dense = _dense_port(art.ports)
    for ref, tr in sorted(transfers.items()):
        ant = antennas.get(ref)
        if ant is None:
            asm.problems.append((
                f"cable-antenna:{ref}",
                f"The solve fitted a gap port to {ref} but has no antenna terms for its "
                f"cable, so its emission cannot be computed. Run the solve again on a worker "
                f"with the antenna solver.",
            ))
            continue
        asm.modelled_cables[ref] = {"cable_id": ant.get("cable_id"),
                                    "length_m": ant.get("length_m")}
        asm.distances_m.add(float(ant.get("distance_m") or 0.0))
        driven_by = tr.get("driven_by")
        port = dense.get(driven_by)
        if port is None:
            asm.problems.append((
                f"cable-port:{ref}",
                f"The solve recorded no port spectrum for {driven_by}, which drives {ref}.",
            ))
            continue
        h = tr["transfer"]
        fs_h, (hr, hi_), holes = _usable_series(
            h["frequencies_hz"], h.get("usable") or [True] * len(h["frequencies_hz"]),
            h["h_real"], h["h_imag"])
        for f in holes:
            asm.undriven[f] = (f"the solve put no source energy near {f / 1e6:g} MHz, so "
                               f"{ref}'s transfer function is unknown there")
        if not fs_h:
            continue
        label = f"cable {ref} ({ant.get('cable_id')}, {float(ant.get('length_m') or 0):g} m)"
        lo = max(fs_h[0], ant["frequencies_hz"][0], port["f"][0])
        hi = min(fs_h[-1], ant["frequencies_hz"][-1], port["f"][-1])
        asm.covered[label] = (lo, hi)
        band = (max(lo, std_lo), min(hi, std_hi))
        if attached is None or band[0] > band[1]:
            continue
        path = _cable_path(label, attached, fs_h, [abs(complex(a, b)) for a, b in zip(hr, hi_)],
                           ant, port, resistance.get(driven_by, 50.0), band, standard_id,
                           asm)
        if path is not None:
            asm.paths.append(path)
            asm.path_meta[label] = {"ref": ref}
    return asm


def _dense_port(ports: dict | None) -> dict:
    out = {}
    for p in (ports or {}).get("ports") or []:
        block = p.get("dense")
        if not block:
            continue
        f = block["frequencies_hz"]
        v = [complex(a, b) for a, b in zip(block["v_real"], block["v_imag"])]
        i = [complex(a, b) for a, b in zip(block["i_real"], block["i_imag"])]
        z = [vv / ii if ii != 0 else complex("nan") for vv, ii in zip(v, i)]
        ok = [ii != 0 for ii in i]
        fs, (zr, zi), _ = _usable_series(f, ok, [x.real for x in z], [x.imag for x in z])
        out[p["port"]] = {"f": fs, "zr": zr, "zi": zi}
    return out


def _board_path(label, attached, fs, e, zr, zi, z_s, band, standard_id, asm,
                manifest) -> Path | None:
    d = attached.driver
    freqs, line = _evaluation_frequencies(d, band, fs, standard_id)
    volts = _volts(d, freqs, asm)
    z_d = d.source_impedance_ohm()
    points = []
    for f, v in zip(freqs, volts):
        if v is None:
            continue
        per_volt = interp_db(fs, e, f)
        z_in = interp_complex(fs, zr, zi, f)
        if per_volt is None or z_in is None:
            continue
        points.append(PathPoint(f, per_volt * _source_factor(z_s, z_d, z_in) * v))
    preset = mesh_preset(manifest)
    return Path(
        kind="board", label=label, driver_id=attached.id, points=points, line=line,
        covered_hz=band,
        sigma_terms={
            f"mesh preset ({preset})": SIGMA_MESH_DB[preset],
            "far-field interpolation": SIGMA_FAR_FIELD_INTERP_DB,
            # Whether the stackup was confirmed is not recorded with the solve, so the term
            # is carried rather than assumed away.
            "permittivity (not confirmed)": SIGMA_ASSUMED_EPSILON_DB,
        },
    )


def _cable_path(label, attached, fs_h, h_mag, ant, port, z_s, band, standard_id,
                asm) -> Path | None:
    d = attached.driver
    freqs, line = _evaluation_frequencies(d, band, fs_h, standard_id)
    volts = _volts(d, freqs, asm)
    z_d = d.source_impedance_ohm()
    fa = ant["frequencies_hz"]
    points = []
    for f, v in zip(freqs, volts):
        if v is None:
            continue
        h = interp_db(fs_h, h_mag, f)
        z_ant = interp_complex(fa, ant["z_real"], ant["z_imag"], f)
        e_amp = interp_db(fa, ant["e_per_amp"], f)
        z_in = interp_complex(port["f"], port["zr"], port["zi"], f)
        if h is None or z_ant is None or e_amp is None or z_in is None or abs(z_ant) == 0:
            continue
        i_cm = h * _source_factor(z_s, z_d, z_in) * v / abs(z_ant)
        points.append(PathPoint(f, i_cm * e_amp))
    return Path(
        kind="cable", label=label, driver_id=attached.id, points=points, line=line,
        covered_hz=band,
        sigma_terms={"cable idealisation": SIGMA_CABLE_DB,
                     "transfer-function interpolation": SIGMA_FAR_FIELD_INTERP_DB},
    )
