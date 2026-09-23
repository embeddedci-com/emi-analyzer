"""Solve: mesh a region of the board and run openEMS over it.

The board is re-read from the original upload at full precision rather than from the
viewer's triangle buffer, which is decimated for rendering. Meshing the decimated geometry
would bake that decimation into the physics.

What this produces is **comparative**: surface current density per copper layer at named
frequencies, showing which trace is carrying the current that radiates. Absolute field
strengths need a real driver spectrum, which is a later phase.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time

from ..kicad.normalize import _board_extent
from ..openems import model as emmodel
from ..openems import post, run
from ..openems.model import Port, SolveParams
from . import StageContext, StageError, StageResult
from .ingest import load_board

log = logging.getLogger(__name__)

#: Progress is posted at most this often. A solve runs for hours; posting every timestep
#: The longest cable a gap port will model. Beyond this the grid extension is larger than
#: the board by two orders of magnitude and the run is a cable simulation with a board
#: attached, which is Tier C's job and not something to arrive at by typing a big number.
MAX_CABLE_LENGTH_M = 5.0

#: Where the far field is evaluated, in metres. FCC Part 15 Class B is measured at 3 m, and
#: evaluating the transform there rather than at 1 m and scaling means the number in the
#: artifact is directly comparable with a limit, with no 1/r correction living somewhere else.
FAR_FIELD_DISTANCE_M = 3.0

#: would be millions of HTTP calls and tell the user nothing extra.
PROGRESS_INTERVAL_S = 3.0

#: Throughput assumed when revising the estimate at the mesh stage, in MCells/s. Measured
#: around 210 on a realistic grid; 150 is deliberately pessimistic so the revised figure
#: errs long rather than short.
ASSUMED_THROUGHPUT_MCELLS_S = 150.0


def _params_from_run(ctx: StageContext) -> SolveParams:
    """Read the solve parameters the UI sent.

    Every message here is written for the user, because a rejected solve at this point is
    the cheapest possible failure — two seconds instead of six hours.
    """
    p = ctx.params
    roi = p.get("roi") or {}
    mesh = p.get("mesh") or {}

    try:
        x0 = float(roi["min_x_mm"])
        y0 = float(roi["min_y_mm"])
        x1 = float(roi["max_x_mm"])
        y1 = float(roi["max_y_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StageError(
            "the region of interest is missing or malformed; it needs min_x_mm, min_y_mm, "
            "max_x_mm and max_y_mm"
        ) from exc

    freqs = [float(f) for f in (p.get("frequencies_hz") or [])]
    if not freqs:
        raise StageError(
            "no frequencies were requested. Name the clock harmonics you care about — the "
            "solver records a field map at each one."
        )

    ports_in = p.get("ports") or []
    if not ports_in:
        raise StageError(
            "no port was placed. openEMS solves a passive structure, so without a source "
            "there is nothing to excite and every field would come out zero."
        )

    ports = []
    for i, raw in enumerate(ports_in):
        try:
            ports.append(Port(
                name=str(raw.get("name") or f"port{i + 1}"),
                x=float(raw["x_mm"]),
                y=float(raw["y_mm"]),
                layer=str(raw["layer"]),
                half_width_mm=float(raw.get("half_width_mm", 0.2)),
                resistance=float(raw.get("resistance", 50.0)),
                excited=bool(raw.get("excited", i == 0)),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise StageError(f"port {i + 1} is malformed: {exc}") from exc

    return SolveParams(
        roi=(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
        frequencies_hz=sorted(freqs),
        ports=ports,
        dx_um=float(mesh.get("dx_um", 50.0)),
        dy_um=float(mesh.get("dy_um", 50.0)),
        dz_um=float(mesh.get("dz_um", 25.0)),
        air_mm=float(p.get("air_mm", emmodel.DEFAULT_AIR_MM)),
        f_max=float(p.get("f_max_hz", 0.0)),
        end_criteria=float(p.get("end_criteria", 1e-4)),
        # 0 means "derive it from the excitation length and the bandwidth". A fixed
        # default here silently caps every run: the fixture solve needed 593,728 timesteps
        # and was being stopped at 60,000, which is how a run finishes without converging
        # and with an under-resolved result.
        max_timesteps=int(p.get("max_timesteps", 0)),
        # §12. Off unless asked for, so a run created before components existed — or by a
        # client that knows nothing about them — solves exactly as it did.
        model_components=bool(p.get("model_components", False)),
        component_candidates=_component_candidates(p),
        cable_ports=_cable_ports(p),
        # §16.2. Off unless asked for: twelve dumps and a transform are not wanted by a solve
        # that is only being read as a hotspot map.
        far_field=bool(p.get("far_field", False)),
        far_field_frequencies_hz=[float(f) for f in (p.get("far_field_frequencies_hz") or [])],
    )


def _cable_ports(p: dict) -> dict:
    """Connectors this solve should fit a Tier B gap port to (§7).

    ``{"USB1": {"type": "usb2-shielded", "length_m": 1.0}}``. Absent means no gap ports,
    which is what every solve did before Tier B existed.

    A cable that is not in the library is refused rather than skipped. Skipping would extend
    the region, mesh the stub, spend the extra cells and then produce a result with no
    transfer function in it — a solve that looks like it modelled the cable and did not.
    """
    raw = p.get("cable_ports") or {}
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise StageError("cable_ports must be an object keyed by connector reference")

    from emi_worker.cables import CableError, get

    out: dict = {}
    for ref, spec in raw.items():
        if not isinstance(spec, dict):
            raise StageError(f"the cable for {ref} must be an object with a type and a length")
        cable_id = str(spec.get("type") or "")
        try:
            cable = get(cable_id)
        except CableError as exc:
            raise StageError(f"{ref}: {exc}") from exc
        try:
            length_m = float(spec.get("length_m", cable.length_m))
        except (TypeError, ValueError) as exc:
            raise StageError(f"{ref}: the cable length is not a number") from exc
        if not 0.0 < length_m <= MAX_CABLE_LENGTH_M:
            raise StageError(
                f"{ref}: a cable length of {length_m:g} m is outside the range this models "
                f"(0 to {MAX_CABLE_LENGTH_M:g} m)"
            )
        out[str(ref)] = {"type": cable_id, "length_m": length_m}
    return out


def _component_candidates(p: dict) -> list:
    """Components the run carries with it, already in precedence order.

    The server resolves who owns what and sends a resolved copy, so a later edit to a
    component never changes a result that has already been reported (§12). This is also how a
    signed-out visitor's unsaved components reach the solve: inline, with the run.

    A malformed component is refused rather than skipped. Silently dropping one would solve a
    board the user thinks is modelled and report a result that looks the same as if it were.
    """
    raw = p.get("components") or []
    if not raw:
        return []

    from emi_worker.components.document import ComponentError
    from emi_worker.components.document import parse as parse_component
    from emi_worker.components.resolve import TIER_MINE, Candidate

    out = []
    for i, doc in enumerate(raw):
        try:
            out.append(Candidate(parse_component(doc), TIER_MINE))
        except ComponentError as exc:
            raise StageError(f"component {i + 1} is malformed: {exc}") from exc
    return out


def run_solve(ctx: StageContext) -> StageResult:
    params = _params_from_run(ctx)

    ctx.progress("fetch", 2, "fetching board")
    info = ctx.client.run_input(ctx.token)
    url = info.get("input_url")
    if not url:
        raise StageError("this run has no board file to solve")
    data = ctx.client.download(url)

    # Re-read the original upload at full precision, in whichever format it arrived. The
    # viewer's geometry is decimated for rendering; meshing that would bake the decimation
    # into the physics.
    ctx.progress("parse", 6, "re-reading the board at full precision")
    try:
        board, filename, _ = load_board(data)
    except StageError:
        raise
    except ValueError as exc:
        raise StageError(f"the board file could not be read: {exc}") from exc
    transform = _board_extent(board)

    if params.model_components:
        params.solver_series_rlc = run.solver_has_series_rlc()

    ctx.progress("mesh", 10, "building the mesh")
    try:
        built = emmodel.build_model(board, transform, params)
    except emmodel.ModelError as exc:
        raise StageError(str(exc)) from exc
    except ValueError as exc:
        raise StageError(f"the mesh could not be built: {exc}") from exc

    mesh_summary = built.mesh.summary()
    cells = mesh_summary["cells"]

    # Admission control, second and final gate. Refusing here costs the user ten seconds;
    # discovering it by running out of memory four hours in costs them the afternoon.
    if ctx.max_cells and cells > ctx.max_cells:
        raise StageError(
            f"this mesh needs {cells:,} cells ({cells * 72 / 1e9:.1f} GB) but this worker "
            f"is limited to {ctx.max_cells:,}. Shrink the region of interest, coarsen the "
            f"mesh, or run it on a larger worker."
        )

    # The client's estimate was made before meshing, from the region and the mesh
    # resolution alone. It cannot know how many grid lines the copper in that region will
    # force, and on a dense board it can be an order of magnitude low. Now that the real
    # mesh exists, publish the real numbers and let the UI correct itself.
    steps = built.doc.max_timesteps
    revised_eta = (cells * steps * max(1, sum(1 for p in params.ports if p.excited))) / (
        ASSUMED_THROUGHPUT_MCELLS_S * 1e6
    )
    ctx.client.progress(
        ctx.token, stage="mesh", pct=14,
        message=(
            f"{cells:,} cells, smallest {mesh_summary['min_cell_um']:.0f} um, "
            f"{steps:,} timesteps"
        ),
        cells=cells, total_timesteps=steps,
    )

    # Record what the run actually is, not what was estimated before meshing.
    est = {
        "cells": cells,
        "ram_bytes": cells * 72,
        "timesteps": steps,
        "dt_seconds": mesh_summary["dt_seconds"],
        "sim_time_seconds": steps * mesh_summary["dt_seconds"],
        "eta_seconds": revised_eta,
    }

    log.info(
        "mesh: %s cells (%s lines), min %.1f um, dt %.3g s, %s steps, eta %.0f s",
        f"{cells:,}", mesh_summary["lines"], mesh_summary["min_cell_um"],
        mesh_summary["dt_seconds"], f"{steps:,}", revised_eta,
    )

    # Write the model where openEMS will run.
    workdir = os.path.join(ctx.rundir, "openems")
    os.makedirs(workdir, exist_ok=True)
    xml_path = os.path.join(workdir, "model.xml")
    # The end criterion is enforced by run_openems, after the source, not by openEMS: openEMS
    # checks its own while the pulse is still on and stopped a real board's solve on a dip
    # between two lobes (research/verify_record_length.py).
    built.doc.end_criteria = run.OPENEMS_NEVER_STOPS
    with open(xml_path, "w") as fh:
        fh.write(built.doc.to_string())

    ctx.progress("solve", 15, "starting openEMS", cells=cells)

    last_post = [0.0]

    def on_progress(p: run.RunProgress) -> None:
        now = time.monotonic()
        if now - last_post[0] < PROGRESS_INTERVAL_S:
            return
        last_post[0] = now
        frac = p.timestep / p.total_timesteps if p.total_timesteps else 0.0
        ctx.progress(
            "solve", 15 + 75 * min(1.0, frac),
            f"timestep {p.timestep:,} / {p.total_timesteps:,} at {p.speed_mcells_s:.0f} MC/s",
            cells=cells,
            timestep=p.timestep,
            total_timesteps=p.total_timesteps,
            energy_db=p.energy_db,
        )

    try:
        result = run.run_openems(
            xml_path, workdir,
            threads=ctx.cores,
            on_progress=on_progress,
            should_stop=ctx.should_stop,
            stop_below_db=10.0 * math.log10(params.end_criteria),
            excitation_s=emmodel.excitation_seconds(built.doc.excitation.fc),
        )
    except run.Stopped:
        from . import Stopped
        raise Stopped() from None
    except run.OpenEMSError as exc:
        raise StageError(str(exc)) from exc

    ctx.progress("post", 92, "reading field data")

    # A run that stopped on its timestep cap is published, because its field maps still show
    # where the current is, but nothing derived from it is usable: every transfer function,
    # impedance and far-field level is marked so, and the compliance estimate refuses it.
    # Publishing it with only a flag and a log line, as this did, left every consumer to
    # notice on its own, and none did.
    unusable = result.unconverged_reason()

    try:
        artifacts = post.build_artifacts(
            workdir,
            built.dump_names,
            params.frequencies_hz,
            [p.name for p in params.ports if p.excited],
            modelled_parts=built.modelled_parts,
            cable_ports=built.cable_ports,
            run_meta={
                "cells": result.cells or cells,
                "timesteps": result.final_timestep,
                "max_timesteps": result.max_timesteps,
                "dt_seconds": result.dt_seconds,
                "final_energy_db": result.final_energy_db,
                "converged": result.converged,
                "unusable_reason": unusable,
                "elapsed_seconds": round(result.elapsed_s, 1),
                "mesh": mesh_summary,
                "roi_mm": list(params.roi),
                # What a driver attached later needs to replace this solve's source (§8): the
                # source impedance each port was driven from. And the cell size asked for,
                # which sets the mesh term in the compliance budget (§17.2).
                "ports": [{"name": p.name, "resistance_ohm": p.resistance,
                           "excited": p.excited} for p in params.ports],
                "mesh_request": {"dx_um": params.dx_um, "dy_um": params.dy_um,
                                 "dz_um": params.dz_um},
                "warnings": result.warnings + built.notes,
            },
        )
    except ValueError as exc:
        raise StageError(str(exc)) from exc

    _add_antenna_terms(ctx, params, built, artifacts, board, transform)
    _add_far_field(ctx, params, built, artifacts, workdir)

    ctx.progress("upload", 95, "uploading results")

    uploaded = []
    for name, blob in artifacts.files.items():
        ct = "application/json" if name.endswith(".json") else "application/octet-stream"
        uploaded.append(ctx.client.upload_artifact(ctx.token, name, blob, ct))

    # The solver log is worth keeping: when a result looks wrong, its warnings are the
    # first place anyone will look.
    uploaded.append(ctx.client.upload_artifact(
        ctx.token, "solver.log", result.log_text.encode(), "text/plain",
    ))

    ctx.progress("done", 100, "solve complete")

    summary = {
        "stage": "solve",
        "filename": filename,
        "cells": result.cells or cells,
        "mesh": mesh_summary,
        "timesteps": result.final_timestep,
        "max_timesteps": result.max_timesteps,
        "final_energy_db": result.final_energy_db,
        "converged": result.converged,
        "elapsed_seconds": round(result.elapsed_s, 1),
        "frequencies_hz": params.frequencies_hz,
        "layers": list(built.dump_names),
        "warnings": result.warnings + built.notes,
    }
    if unusable:
        summary["unusable_reason"] = unusable
        log.warning("run did not converge: energy only fell to %.1f dB", result.final_energy_db)

    return StageResult(summary=summary, artifacts=uploaded, estimate=est)


def _add_antenna_terms(ctx, params, built, artifacts, board, transform) -> None:
    """Run the antenna solver for each gap port and put its terms beside the transfer function.

    §10 attaches a driver in the browser, after the run. That only works if the result already
    carries everything the composition needs except the driver — so Z_ant and E_per_amp are
    computed here, on the same dense grid ``cable_ports.json`` uses, rather than left for a
    client that has no nec2c.

    A failure here does not fail the solve. The field maps are the run's main product and they
    are already correct; losing the antenna terms costs the cable emissions chart, and the
    manifest says so rather than the chart appearing empty.
    """
    from emi_worker.cables.emission import antenna_terms
    from emi_worker.cables.nec import available as nec_available

    refs = artifacts.manifest.get("cable_ports") or []
    if not refs:
        return
    grid = artifacts.manifest.get("dense_frequencies_hz") or []
    if not grid:
        return
    if not nec_available():
        artifacts.manifest["cable_antenna_note"] = (
            "the antenna solver is not installed on this worker, so this result carries the "
            "transfer function but not the cable impedance it has to be composed with"
        )
        return

    # The board is the antenna's other arm, and its span along each cable's exit decides
    # where the structure resonates. The outline knows it; a 0.1 m default does not.
    x0, y0, x1, y1 = _board_span(board, transform)

    docs = []
    for meta in built.cable_ports:
        if meta["ref"] not in refs:
            continue
        spec = params.cable_ports.get(meta["ref"]) or {}
        along = abs(meta["exit_normal"][0]) >= abs(meta["exit_normal"][1])
        span_m = ((x1 - x0) if along else (y1 - y0)) / 1000.0
        try:
            doc = antenna_terms(
                spec["type"], float(spec["length_m"]), grid,
                board_span_m=max(0.01, span_m),
            )
        except Exception as exc:  # nec2c is a subprocess; a failure is not a solve failure
            log.warning("antenna terms for %s failed: %s", meta["ref"], exc)
            artifacts.manifest["cable_antenna_note"] = (
                f"the antenna solver failed for {meta['ref']} ({exc}), so this result carries "
                f"the transfer function but not the cable impedance"
            )
            continue
        doc["ref"] = meta["ref"]
        docs.append(doc)

    if docs:
        artifacts.files["cable_antenna.json"] = json.dumps(
            {"format": "emi-cable-antenna", "format_version": 1, "cables": docs},
            separators=(",", ":"),
        ).encode()
        artifacts.manifest["cable_antenna"] = [d["ref"] for d in docs]
        # Rewrite the manifest now that it has grown.
        artifacts.files["manifest.json"] = json.dumps(artifacts.manifest, indent=2).encode()


def _board_span(board, transform) -> tuple[float, float, float, float]:
    pts = [transform.pt(*p) for o in board.outline for p in o] or \
          [transform.pt(*p) for t in board.tracks for p in t.pts]
    if not pts:
        return (0.0, 0.0, 100.0, 100.0)
    return (min(p[0] for p in pts), min(p[1] for p in pts),
            max(p[0] for p in pts), max(p[1] for p in pts))


#: Version 3 is the field at the scan's antenna positions (3 m from the board's boundary, 1-4 m
#: over the plane), exact at any distance, with the plane as image currents. Version 2 was the
#: far-field transform on a 3 m sphere with nf2ff's own mirror, which images horizontal currents
#: wrongly (docs/verification/far-field.md); version 1 was in the solver's pulse units.
FAR_FIELD_FORMAT_VERSION = 3

#: How negative a port's resistance may read, as a fraction of |Z_in|, before the frequency is
#: taken as truncated rather than solved. See ``far_field_document``.
PASSIVE_TOLERANCE = 0.05

#: How far the scan's reading may differ from `nf2ff` in the far-field limit before the far field
#: is refused. The two integrate the same dumps and agree to 0.001 dB when both are right, so
#: anything near this is a reading or a units error, not a numerical difference.
NF2FF_AGREEMENT_DB = 0.5


def _add_far_field(ctx, params, built, artifacts, workdir: str) -> None:
    """The board's field where a radiated scan reads it (§16.2), per volt of source.

    The NF2FF box's surface currents are integrated with the full Green's function at the
    antenna positions a scan visits, plus their image in the ground plane (``openems.scan``).
    This replaced the `nf2ff` transform for the level, for two measured reasons. The transform
    only exists in the far-field limit, which 3 m is not below about 100 MHz and which a source
    and its image 1.6 m apart are not at any frequency: on a dipole it read up to 7.6 dB high.
    And its PEC mirror images horizontal currents wrongly: on a horizontal dipole it read 19 dB
    high at 30 MHz, and on a board, where every current is horizontal, it left the quasi-static
    field uncancelled and made the far field rise about 20 dB/decade per volt where physics says
    40. docs/verification/far-field.md has the numbers.

    `nf2ff` still runs, without a mirror, as a guard: in the far-field limit it must agree with
    the scan's integral of the same dumps, and if it does not, one of them read the dumps wrongly
    and the far field is refused rather than published.

    **The result is per volt of source.** A solve is excited by openEMS's Gaussian pulse, so the
    raw field is in units of that pulse and means nothing on its own. It is divided here by the
    solve's Thévenin source at each frequency, and carries the port's input impedance beside it,
    so a driver attached later is arithmetic: E = e_per_volt · |Z_s + Z_in| / |Z_d + Z_in| · |V_d|.

    Like the antenna terms, a failure here does not fail the solve: the field maps are the run's
    main product and are already correct. The manifest says why the far field is missing rather
    than the compliance view finding an empty file.
    """
    if not built.far_field:
        return
    from emi_worker.openems import nf2ff as nf2ff_mod
    from emi_worker.openems import scan

    meta = built.far_field
    freqs = meta["frequencies_hz"]
    excited = [p for p in params.ports if p.excited]
    ctx.progress("post", 93, "computing the far field")
    try:
        if not excited:
            raise nf2ff_mod.NF2FFError("no port was excited")
        surface = scan.read_surface(workdir, freqs)
        field = scan_board(surface, freqs, meta)
        check_against_nf2ff(workdir, freqs, surface, meta)
        source = post.source_spectrum(workdir, excited[0].name, excited[0].resistance, freqs)
    except (nf2ff_mod.NF2FFError, scan.ScanError, OSError, ValueError, KeyError) as exc:
        log.warning("far field failed: %s", exc)
        artifacts.manifest["far_field_note"] = (
            f"the far field could not be computed for this run ({exc}), so it contributes "
            f"nothing to the compliance estimate rather than contributing zero"
        )
        artifacts.files["manifest.json"] = json.dumps(artifacts.manifest, indent=2).encode()
        return

    converged = (artifacts.manifest.get("run") or {}).get("converged") is not False
    doc = far_field_document(field, source, excited, meta, converged=converged)
    artifacts.files["farfield.json"] = json.dumps(doc, separators=(",", ":")).encode()
    artifacts.manifest["far_field"] = {
        "format_version": FAR_FIELD_FORMAT_VERSION,
        "distance_m": FAR_FIELD_DISTANCE_M,
        "frequencies_hz": doc["frequencies_hz"],
        "ground_plane": True,
        "driven_by": doc["driven_by"],
    }
    artifacts.files["manifest.json"] = json.dumps(artifacts.manifest, indent=2).encode()


def scan_board(surface, freqs: list[float], meta: dict) -> dict:
    """The scan's reading around the board, in the solve's pulse units, before any division.

    The board stands on the table by its bottom copper, and the antenna is 3 m from the smallest
    circle around the copper in plan, as the cable path measures (ANSI C63.4 measures from the
    periphery of the equipment).
    """
    import numpy as np

    from emi_worker.openems import scan
    from emi_worker.openems.nf2ff import TABLE_HEIGHT_M

    x0, y0, x1, y1, z0, _z1 = (float(v) / 1000.0 for v in meta["copper_mm"])
    ground = z0 - TABLE_HEIGHT_M
    radius = FAR_FIELD_DISTANCE_M + 0.5 * float(np.hypot(x1 - x0, y1 - y0))
    points = scan.ring(((x0 + x1) / 2, (y0 + y1) / 2), radius, ground)
    e = scan.reading(scan.field(surface, freqs, points, ground_z_m=ground))
    by_height = e.reshape(len(freqs), len(scan.SCAN_HEIGHTS_M), -1).max(axis=2)
    return {
        "frequencies_hz": [float(f) for f in freqs],
        "e_max_v_per_m": [float(v) for v in e.max(axis=1)],
        "e_by_height_v_per_m": [[float(v) for v in row] for row in by_height],
        "scan_radius_m": radius,
    }


def check_against_nf2ff(workdir: str, freqs: list[float], surface, meta: dict) -> None:
    """Refuse the far field if the scan's integral and `nf2ff` disagree in the far-field limit."""
    import numpy as np

    from emi_worker.openems import nf2ff as nf2ff_mod
    from emi_worker.openems import scan

    # Far enough that the 1/r^2 terms are gone at 30 MHz (kr = 630).
    r = 1000.0
    out_h5 = os.path.join(workdir, "farfield.h5")
    job = nf2ff_mod.write_job(workdir, freqs, out_h5, centre_mm=tuple(meta["centre_mm"]),
                              radius_m=r)
    nf2ff_mod.run_job(job, out_h5)
    theirs = nf2ff_mod.read_field(out_h5, freqs)
    th = np.deg2rad(nf2ff_mod.THETA_DEG)
    ph = np.deg2rad(nf2ff_mod.PHI_DEG)
    c = np.asarray(meta["centre_mm"], dtype=float) / 1000.0
    dirs = np.array([[np.sin(t) * np.cos(p), np.sin(t) * np.sin(p), np.cos(t)]
                     for p in ph for t in th])
    e = scan.field(surface, freqs, c + r * dirs)
    # The better of the two polarisations as nf2ff defines them, theta and phi.
    t_hat = np.array([[np.cos(t) * np.cos(p), np.cos(t) * np.sin(p), -np.sin(t)]
                      for p in ph for t in th])
    p_hat = np.array([[-np.sin(p), np.cos(p), 0.0] for p in ph for t in th])
    ours = np.maximum(np.abs((e * t_hat[None]).sum(-1)), np.abs((e * p_hat[None]).sum(-1)))
    for k, f in enumerate(freqs):
        a, b = float(ours[k].max()), float(theirs["e_max_v_per_m"][k])
        if a <= 0 or b <= 0:
            raise nf2ff_mod.NF2FFError(f"the far field is zero at {f / 1e6:g} MHz")
        if abs(20 * np.log10(a / b)) > NF2FF_AGREEMENT_DB:
            raise nf2ff_mod.NF2FFError(
                f"the far field's own check failed at {f / 1e6:g} MHz: the scan integral and "
                f"nf2ff read the same box {20 * np.log10(a / b):+.1f} dB apart"
            )


def far_field_document(field: dict, source: dict, excited: list, meta: dict,
                       converged: bool = True) -> dict:
    """``farfield.json``: the scan's reading per volt of the solve's own source.

    A frequency where the source delivered nothing has no transfer function, only a ratio of two
    small numbers, and is marked unusable rather than published -- the same rule as the cable
    transfer function. So is every frequency of a run that did not converge.
    """
    import numpy as np

    from emi_worker.openems import scan
    from emi_worker.openems.nf2ff import TABLE_HEIGHT_M

    v_src = np.asarray(source["v_src"])
    z_in = np.asarray(source["z_in"])
    mags = np.abs(v_src)
    # A passive port cannot have a negative resistance. When the transform says it does, the
    # record stopped before the structure stopped ringing and what it holds at that frequency is
    # the truncation, not the board: a real 4-layer board stopped at -40 dB reported -25 kOhm
    # against |Z| = 25.5 kOhm at 30 MHz. The dipoles, whose records are complete, stay above
    # -0.5 % of |Z| everywhere, so the line is drawn well clear of them.
    passive = [bool(not np.isfinite(z) or z.real >= -PASSIVE_TOLERANCE * abs(z)) for z in z_in]
    # And none of a run that did not converge: its transform is of fields still ringing.
    usable = [bool(converged and m > 0 and np.isfinite(z)) and ok
              for m, z, ok in zip(mags, z_in, passive)]
    e_per_volt = [
        float(e / m) if ok else 0.0 for e, m, ok in zip(field["e_max_v_per_m"], mags, usable)
    ]
    by_height = [
        [float(v / m) if ok else 0.0 for v in row]
        for row, m, ok in zip(field.get("e_by_height_v_per_m") or [], mags, usable)
    ]
    return {
        "format": "emi-far-field",
        "format_version": FAR_FIELD_FORMAT_VERSION,
        "frequencies_hz": field["frequencies_hz"],
        "unit": "V/m per V of source",
        "e_per_volt": e_per_volt,
        # The highest reading over the turntable at each antenna height.
        "e_by_height_per_volt": by_height,
        "heights_m": list(scan.SCAN_HEIGHTS_M),
        "usable": usable,
        # Frequencies dropped because the port read as a negative resistance: the run stopped
        # too early for them, and a longer run (a lower end criterion) would recover them.
        "truncated_hz": [float(f) for f, ok in zip(field["frequencies_hz"], passive) if not ok],
        # What a driver needs to replace the solve's source with its own.
        "driven_by": excited[0].name,
        "source_impedance_ohm": float(excited[0].resistance),
        "z_in_real": [float(z.real) if ok else 0.0 for z, ok in zip(z_in, usable)],
        "z_in_imag": [float(z.imag) if ok else 0.0 for z, ok in zip(z_in, usable)],
        # More than one excited port means the field is the response to all of them at once,
        # and no single driver can be attached to it.
        "excited_ports": [p.name for p in excited],
        "distance_m": FAR_FIELD_DISTANCE_M,
        "scan_radius_m": field.get("scan_radius_m"),
        "table_height_m": TABLE_HEIGHT_M,
        "ground_plane": True,
        "faces_mm": meta["faces_mm"],
        "clearance_mm": meta.get("clearance_mm"),
        "tenth_wavelength_above_hz": meta.get("tenth_wavelength_above_hz"),
        "face_resolution_mm": meta.get("face_resolution_mm"),
    }
