#!/usr/bin/env python3
"""End-to-end smoke test for the EMI Analyzer stack.

Drives the full path a user takes, using only the public API:

    create project
      -> presigned upload of a board file, straight to object storage
      -> create board (queues an ingest run)
      -> watch a worker claim it, report progress, and upload artifacts
      -> create a solve run with a cost estimate
      -> watch it stream timesteps and energy decay
      -> read a result artifact back through a presigned GET

If this passes, the P0 contract holds: a worker that dialled in from somewhere else did
real work, and no bulk bytes went through the control plane.

Run it inside the compose network so that presigned URLs and service names resolve:

    make smoke
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("EMI_API", "http://server:8090/api")
BOARD = os.environ.get("EMI_SMOKE_BOARD", "/fixtures/tiny.kicad_pcb")
# No identity header. The smoke test is a signed-out visitor like any other, which means
# the server hands it a cookie on the first call and everything it creates lives in that
# visitor's own space -- so the cookie has to be kept for the rest of the run, or each
# request would arrive as a different visitor and find an empty project list.
HEADERS = {"Content-Type": "application/json"}

_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)
# Generous, because the solve step is a real FDTD run and not a stub: the fixture's small
# region is ~350k cells and a few hundred thousand timesteps, which is two to five minutes
# depending on the machine and on what else it is doing. Too tight a bound here reports a
# converging solve as a failure.
TIMEOUT = float(os.environ.get("EMI_SMOKE_TIMEOUT", "600"))


class Fail(SystemExit):
    def __init__(self, msg: str):
        super().__init__(f"\n  FAIL: {msg}\n")


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=HEADERS, method=method)
    try:
        with _opener.open(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise Fail(f"{method} {path} -> {exc.code}: {exc.read().decode()[:400]}") from exc
    except urllib.error.URLError as exc:
        raise Fail(f"{method} {path} unreachable: {exc}") from exc


def put_blob(url: str, data: bytes, content_type: str) -> None:
    req = urllib.request.Request(url, data=data, method="PUT",
                                 headers={"Content-Type": content_type})
    try:
        with _opener.open(req, timeout=120) as resp:
            if resp.status >= 300:
                raise Fail(f"presigned PUT returned {resp.status}")
    except urllib.error.HTTPError as exc:
        raise Fail(f"presigned PUT failed {exc.code}: {exc.read().decode()[:300]}") from exc


def get_blob(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise Fail(f"presigned GET failed {exc.code}") from exc


def step(n: int, msg: str) -> None:
    print(f"  {n}. {msg}", flush=True)


def wait_for_run(run_id: str, label: str) -> dict:
    """Poll a run to completion, printing progress as the worker reports it."""
    deadline = time.time() + TIMEOUT
    last = ""
    while time.time() < deadline:
        run = call("GET", f"/emi/runs/{run_id}")
        status = run["status"]
        prog = run.get("progress") or {}
        line = f"{status:12} {prog.get('stage', ''):8} {prog.get('pct', 0):5.1f}%"
        if prog.get("timestep"):
            line += f"  step {prog['timestep']:,}/{prog.get('total_timesteps', 0):,}"
        if prog.get("energy_db") is not None:
            line += f"  energy {prog['energy_db']:.1f} dB"
        if line != last:
            print(f"     {label}: {line}", flush=True)
            last = line
        if status == "done":
            return run
        if status in ("failed", "timed_out"):
            raise Fail(f"{label} ended as {status}: {run.get('error', '(no message)')}")
        time.sleep(1.0)
    raise Fail(f"{label} did not finish within {TIMEOUT:.0f}s (last status: {last})")


def main() -> int:
    print("\nEMI Analyzer — end-to-end smoke test")
    print(f"API: {API}\n")

    step(1, "waiting for a worker to dial in")
    deadline = time.time() + 90
    workers: list = []
    while time.time() < deadline:
        out = call("GET", "/emi/workers")
        workers = [w for w in out.get("workers", []) if w.get("online")]
        if workers:
            break
        time.sleep(1.0)
    if not workers:
        raise Fail("no worker connected — check `make logs-worker`")
    w = workers[0]
    caps = w.get("capabilities") or {}
    print(f"     worker {w['name']!r}: {caps.get('cores')} cores, "
          f"{caps.get('ram_gb')} GB, max_cells={caps.get('max_cells'):,}, "
          f"kinds={','.join(caps.get('kinds') or [])}")
    if "solve" not in (caps.get("kinds") or []):
        print("     NOTE: worker has no openEMS on PATH; it is ingest-only")

    step(2, "creating a project")
    project = call("POST", "/emi/projects", {"name": "smoke board", "source_kind": "kicad"})
    pid = project["id"]

    step(3, "uploading a real KiCad board straight to object storage")
    try:
        payload = open(BOARD, "rb").read()
    except OSError as exc:
        raise Fail(f"could not read the test board at {BOARD}: {exc}")
    up = call("POST", f"/emi/projects/{pid}/uploads",
              {"filename": "smoke.kicad_pcb", "content_type": "application/octet-stream"})
    put_blob(up["upload_url"], payload, "application/octet-stream")
    print(f"     uploaded {len(payload)} bytes to {up['key']}")

    step(4, "registering the board (queues an ingest run)")
    created = call("POST", f"/emi/projects/{pid}/boards", {"input_key": up["key"]})
    board_id = created["board"]["id"]
    ingest_run = created["run"]["id"]

    step(5, "waiting for the ingest run")
    run = wait_for_run(ingest_run, "ingest")
    summary = run.get("summary") or {}
    if summary.get("input_bytes") != len(payload):
        raise Fail(f"worker saw {summary.get('input_bytes')} bytes, we uploaded {len(payload)}")
    print(f"     parsed by {summary.get('worker')}: KiCad {summary.get('kicad_version')}, "
          f"{summary.get('layers')} layers, {summary.get('nets')} nets, "
          f"{summary.get('tracks')} tracks, {summary.get('vias')} vias")
    print(f"     geometry: {summary.get('triangles'):,} triangles, "
          f"board {summary.get('board_mm')} mm")
    print(f"     findings: {summary.get('findings')}")

    arts = call("GET", f"/emi/runs/{ingest_run}/artifacts")["artifacts"]
    names = {a["name"] for a in arts}
    for want in ("board.json", "geometry.bin", "rules.json"):
        if want not in names:
            raise Fail(f"ingest produced no {want} (got: {sorted(names)})")

    # Pull board.json and geometry.bin back the way the browser will, and check that the
    # index in one actually describes the other. A mismatch here draws the wrong net's
    # copper, which is the kind of bug that looks like a rendering glitch for a week.
    board_doc = json.loads(get_blob(
        call("GET", f"/emi/runs/{ingest_run}/artifacts/board.json")["url"]))
    geo = get_blob(call("GET", f"/emi/runs/{ingest_run}/artifacts/geometry.bin")["url"])
    gidx = board_doc["geometry"]
    if len(geo) != gidx["byte_length"]:
        raise Fail(f"geometry.bin is {len(geo)} bytes, board.json says {gidx['byte_length']}")
    if gidx["vertex_count"] * 8 != len(geo):
        raise Fail("vertex_count does not match the buffer length")
    covered = sum(g["count"] for g in gidx["groups"])
    if covered != gidx["vertex_count"]:
        raise Fail(f"groups cover {covered} vertices of {gidx['vertex_count']}")
    print(f"     board.json indexes geometry.bin exactly "
          f"({len(gidx['groups'])} groups, {gidx['vertex_count'] // 3:,} triangles)")

    layer_names = [layer["name"] for layer in board_doc["layers"]]
    print(f"     layers {layer_names}, stackup "
          f"{[s['name'] for s in board_doc['stackup'] if s['role'] == 'dielectric']}")

    rules_doc = json.loads(get_blob(
        call("GET", f"/emi/runs/{ingest_run}/artifacts/rules.json")["url"]))
    if not rules_doc["findings"]:
        raise Fail("the rules tier found nothing at all on a board with a deliberate "
                   "plane slot in it")
    for f in rules_doc["findings"][:3]:
        print(f"       [{f['severity']:8}] {f['title']}")

    step(6, "creating a solve run with a cost estimate")
    # Full-wave solving is experimental and off unless the server enables it. Off has to mean
    # refused by the server, so that is what the smoke test checks in that case.
    if not call("GET", "/emi/features").get("full_wave"):
        try:
            call("POST", f"/emi/projects/{pid}/runs", {"board_id": board_id, "kind": "solve"})
        except Fail as exc:
            if "-> 403" not in str(exc):
                raise
            print("     refused, as it should be: full-wave solving is not enabled on this server")
            print("\n  PASS (ingest path; solves correctly refused)\n")
            return 0
        raise Fail("a solve was accepted although the server reports full_wave off")
    if "solve" not in (caps.get("kinds") or []):
        print("     skipped: this worker cannot solve")
        print("\n  PASS (ingest path only)\n")
        return 0

    # A small region around the CLK trace, not the whole board: the fixture board solved
    # end to end is a 12-million-cell, ten-hour job, and this is a smoke test.
    roi = {"min_x_mm": 18.0, "min_y_mm": 16.0, "max_x_mm": 32.0, "max_y_mm": 24.0}
    solve_freqs = [1e9]

    est_input = {
        "roi_x_mm": roi["max_x_mm"] - roi["min_x_mm"],
        "roi_y_mm": roi["max_y_mm"] - roi["min_y_mm"],
        "roi_z_mm": 10,
        "dx_um": 50, "dy_um": 50, "dz_um": 25,
        "f_min_hz": 100e6, "ports": 1, "fill_factor": 0.13,
    }
    check = call("POST", "/emi/estimate", est_input)
    print(f"     estimate: {check['estimate']['cells']:,} cells, "
          f"{check['ram_gb']:.2f} GB, eta {check['eta_human']}")

    solve = call("POST", f"/emi/projects/{pid}/runs", {
        "board_id": board_id,
        "kind": "solve",
        "estimate_input": est_input,
        "params": {
            "roi": roi,
            "mesh": {"dx_um": 50, "dy_um": 50, "dz_um": 25, "fill_factor": 0.13},
            "f_min_hz": 100e6,
            "frequencies_hz": solve_freqs,
            # One driven port on the CLK trace where it crosses the plane channel. openEMS
            # solves a passive structure, so without a source every field comes out zero.
            "ports": [{
                "name": "clk", "x_mm": 19.0, "y_mm": 20.0,
                "layer": "F.Cu", "half_width_mm": 0.2, "resistance": 50.0,
            }],
        },
    })

    step(7, "waiting for the solve run")
    run = wait_for_run(solve["id"], "solve")

    step(8, "reading a result artifact back through a presigned GET")
    arts = call("GET", f"/emi/runs/{solve['id']}/artifacts")["artifacts"]
    names = {a["name"] for a in arts}
    expected = {"manifest.json", "solver.log", "sparams.json"}
    missing = expected - names
    if missing:
        raise Fail(f"solve did not produce {sorted(missing)} (got: {sorted(names)})")

    manifest_url = call("GET", f"/emi/runs/{solve['id']}/artifacts/manifest.json")["url"]
    manifest = json.loads(get_blob(manifest_url))
    if manifest["frequencies_hz"] != solve_freqs:
        raise Fail(f"manifest lists {manifest['frequencies_hz']}, requested {solve_freqs}")
    if not manifest["layers"]:
        raise Fail("manifest names no copper layers")
    print(f"     manifest: {len(manifest['layers'])} layers "
          f"({', '.join(l['layer'] for l in manifest['layers'])}), "
          f"{len(manifest['frequencies_hz'])} frequency, "
          f"metric '{manifest['metric']}'")

    # Every grid the manifest names must be there, and be exactly the size it claims --
    # this is the check that a truncated or half-uploaded result cannot pass.
    for layer in manifest["layers"]:
        for grid in layer["grids"]:
            if grid["file"] not in names:
                raise Fail(f"manifest names {grid['file']}, which was never uploaded")
            blob = get_blob(call("GET", f"/emi/runs/{solve['id']}/artifacts/{grid['file']}")["url"])
            want = grid["width"] * grid["height"] * 4
            if len(blob) != want:
                raise Fail(f"{grid['file']} is {len(blob)} bytes, "
                           f"expected {grid['width']}x{grid['height']} float32 = {want}")
            values = struct.unpack(f"<{grid['width'] * grid['height']}f", blob)
            print(f"     {grid['file']}: {grid['width']}x{grid['height']}, "
                  f"{min(values):.1f}–{max(values):.1f} dB")

    run_info = manifest["run"]
    if not run_info["converged"]:
        raise Fail(f"solve did not converge (energy fell only to "
                   f"{run_info['final_energy_db']:.1f} dB)")
    print(f"     converged to {run_info['final_energy_db']:.1f} dB in "
          f"{run_info['timesteps']:,} timesteps, {run_info['cells']:,} cells")

    print("\n  PASS — worker dial-in, claim, progress, artifacts and presigned "
          "round-trip all work.\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
