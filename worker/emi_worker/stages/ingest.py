"""Ingest: turn an upload into the normalised board the rest of the system reads.

Produces three artifacts:

``board.json``   stackup with z heights, layers, nets, vias, pads, outline, and the index
                 into the triangle buffer
``geometry.bin`` flat float32 triangles, ready to hand straight to a WebGL vertex buffer
``rules.json``   findings from the fast geometry tier

The solve stage does **not** read geometry.bin. That buffer is simplified for rendering;
meshing re-reads the original upload at full precision. Keeping those two paths separate is
what lets the viewer be cheap without the physics being wrong.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from dataclasses import dataclass, field

from .. import stackup, topology
from ..gerber import GerberError, NetlistError, load_gerber_board
from ..kicad import netclass, parse, parse_board
from ..kicad.normalize import _board_extent, normalize, to_json
from ..rules import run_rules, settings
from ..rules import matching, netreport
from ..rules.model import RuleContext
from . import StageContext, StageError, StageResult

log = logging.getLogger(__name__)

#: Default top frequency for the length- and stub-based checks when the user has not said
#: what their fastest edge is. 1 GHz is the top of the band this tool claims to model.
DEFAULT_MAX_FREQUENCY_HZ = 1e9

#: A board file larger than this is refused rather than parsed. The largest board in this
#: codebase is 12 MB; 200 MB is not a PCB, it is either a mistake or an attack.
MAX_BOARD_BYTES = 200 * 1024 * 1024


#: Extensions that mark a file as part of a Gerber set.
GERBER_SUFFIXES = (".gbr", ".gbrjob", ".drl", ".gtl", ".gbl", ".gm1", ".d356")


def _archive_members(data: bytes) -> dict[str, bytes]:
    """Read a zip into {name: bytes}, skipping directories and archiver noise."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    out: dict[str, bytes] = {}
    for info in zf.infolist():
        if info.is_dir() or "__MACOSX" in info.filename:
            continue
        if info.file_size > MAX_BOARD_BYTES:
            raise StageError(
                f"{info.filename} is {info.file_size / 1e6:.0f} MB, larger than the "
                f"{MAX_BOARD_BYTES / 1e6:.0f} MB limit"
            )
        name = info.filename.split("/")[-1]
        if name.startswith((".", "_autosave-")):
            continue
        out[name] = zf.read(info)
    return out


@dataclass
class Sidecars:
    """Files that travel with the board but are not the board.

    The netclasses and diff-pair geometry live in the KiCad *project* file, and rules that
    should be reviewed with the design live in a settings document beside it. Both are only
    present when somebody uploads a zipped project rather than a bare .kicad_pcb, which is
    worth surfacing rather than silently doing without.
    """

    project: bytes | None = None
    settings: dict | None = None
    notes: list[str] = field(default_factory=list)


#: Names recognised as a committed rules document, in preference order.
SETTINGS_NAMES = ("emi.rules.yaml", "emi.rules.yml", "emi.rules.json", ".emi.yaml")


def load_sidecars(data: bytes) -> Sidecars:
    out = Sidecars()
    if data[:2] != b"PK":
        return out
    try:
        members = _archive_members(data)
    except zipfile.BadZipFile:
        return out

    projects = sorted((n for n in members if n.lower().endswith(".kicad_pro")), key=len)
    if projects:
        out.project = members[projects[0]]
    else:
        out.notes.append(
            "no .kicad_pro in the upload, so netclasses and differential-pair geometry are "
            "unavailable; groups and pairs were inferred from net names instead"
        )

    for name in SETTINGS_NAMES:
        hit = next((n for n in members if n.rsplit("/", 1)[-1].lower() == name), None)
        if hit:
            try:
                out.settings = settings.parse_document(members[hit].decode("utf-8", "replace"))
                out.notes.append(f"rules read from {hit}")
            except Exception as exc:  # noqa: BLE001
                out.notes.append(f"{hit} could not be read ({exc}); built-in defaults used")
            break
    return out


def load_board(data: bytes) -> tuple[object, str, str]:
    """Read an upload into a BoardModel, whichever format it is.

    Returns ``(model, filename, source_kind)``. A bare file is KiCad; an archive is
    whichever of the two it turns out to hold, decided by what is actually inside rather
    than by what the project was labelled — a user who picks the wrong one in the UI should
    still get their board.
    """
    if data[:2] != b"PK":
        text, filename = _extract_board(data)
        return parse_board(parse(text)), filename, "kicad"

    try:
        members = _archive_members(data)
    except zipfile.BadZipFile as exc:
        raise StageError("the upload looks like a zip file but could not be opened") from exc

    kicad = [n for n in members if n.lower().endswith(".kicad_pcb")]
    gerber = [n for n in members if n.lower().endswith(GERBER_SUFFIXES)]

    if kicad:
        kicad.sort(key=len)
        text = members[kicad[0]].decode("utf-8", errors="replace")
        return parse_board(parse(text)), kicad[0], "kicad"

    if gerber:
        try:
            return load_gerber_board(members), "gerber set", "gerber"
        except (GerberError, NetlistError) as exc:
            raise StageError(str(exc)) from exc
        except ValueError as exc:
            raise StageError(str(exc)) from exc

    raise StageError(
        "the archive contains neither a .kicad_pcb nor a Gerber set. Upload a KiCad board, "
        "or a zip of your fabrication output including the drill file and an IPC-D-356 "
        "netlist."
    )


def _extract_board(data: bytes) -> tuple[str, str]:
    """Return ``(text, filename)`` for a KiCad board file inside an upload.

    Accepts either a bare ``.kicad_pcb`` or a zipped KiCad project, because both are things
    a person will reasonably drag into a browser.
    """
    if data[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise StageError("the upload looks like a zip file but could not be opened") from exc

        candidates = [
            n for n in zf.namelist()
            if n.lower().endswith(".kicad_pcb")
            # Skip KiCad's autosave and backup copies, and anything a zip archiver added.
            and not n.split("/")[-1].startswith(("_autosave-", "."))
            and "__MACOSX" not in n
        ]
        if not candidates:
            raise StageError(
                "no .kicad_pcb file found in the archive. Zip the KiCad project folder, "
                "or upload the .kicad_pcb file on its own."
            )
        if len(candidates) > 1:
            # Shallowest path wins: a project's own board sits above any imported one.
            candidates.sort(key=lambda n: (n.count("/"), len(n)))

        name = candidates[0]
        info = zf.getinfo(name)
        if info.file_size > MAX_BOARD_BYTES:
            raise StageError(
                f"{name} is {info.file_size / 1e6:.0f} MB, larger than the "
                f"{MAX_BOARD_BYTES / 1e6:.0f} MB limit"
            )
        return zf.read(name).decode("utf-8", errors="replace"), name

    if len(data) > MAX_BOARD_BYTES:
        raise StageError(
            f"the board file is {len(data) / 1e6:.0f} MB, larger than the "
            f"{MAX_BOARD_BYTES / 1e6:.0f} MB limit"
        )
    text = data.decode("utf-8", errors="replace")
    if "(kicad_pcb" not in text[:4096]:
        raise StageError(
            "this does not look like a KiCad board file. Upload a .kicad_pcb, a zip of the "
            "KiCad project folder, or a zip of your Gerber output including the drill file "
            "and an IPC-D-356 netlist."
        )
    return text, "board.kicad_pcb"


def run_ingest(ctx: StageContext) -> StageResult:
    ctx.progress("fetch", 4, "fetching upload")

    info = ctx.client.run_input(ctx.token)
    url = info.get("input_url")
    if not url:
        raise StageError("this run has no uploaded board file")

    data = ctx.client.download(url)
    if not data:
        raise StageError("the uploaded board file is empty")
    digest = hashlib.sha256(data).hexdigest()

    ctx.progress("parse", 15, "reading board file")
    ctx.check_stop()
    try:
        model, filename, source_kind = load_board(data)
    except StageError:
        raise
    except ValueError as exc:
        raise StageError(f"the board file could not be read: {exc}") from exc
    # Files that came with the board but are not the board: netclasses, committed rules.
    sidecars = load_sidecars(data)

    ctx.progress("parse", 35, "extracting geometry")

    if not model.tracks and not model.zones and not model.pads:
        raise StageError(
            "this board has no copper on it. If it is a fresh layout, route it first."
        )

    ctx.progress(
        "parse", 55,
        f"{len(model.copper_layers)} layers, {len(model.nets)} nets, {len(model.tracks)} tracks",
    )

    doc, geometry = normalize(model, {
        "filename": filename,
        "key": info.get("input_key", ""),
        "size_bytes": len(data),
        "sha256": digest,
    })

    ctx.check_stop()
    ctx.progress("rules", 65, "running EMI checks")

    # Settings, most general first: built-in defaults, then the project's, then a file
    # committed beside the board, then this run's parameters. See rules/settings.py.
    cfg = settings.load(
        ("project", ctx.params.get("project_settings")),
        ("file", sidecars.settings),
        ("run", ctx.params.get("settings")),
    )
    if ctx.params.get("max_frequency_hz"):
        cfg.board["max_frequency_hz"] = settings.Value(
            float(ctx.params["max_frequency_hz"]), "run"
        )
    max_freq = float(cfg.value("max_frequency_hz") or DEFAULT_MAX_FREQUENCY_HZ)

    # Which layers are reference planes is decided once, from the pours, and handed to both
    # the plane checks and the electrical model -- two answers to that question would mean
    # two boards.
    planes = {
        layer["name"] for layer in doc["layers"]
        if layer.get("plane_net") and layer.get("plane_coverage", 0) >= 0.3
    }
    electrics = stackup.analyse(
        model, planes,
        epsilon_override=float(cfg.value("epsilon_r") or 0.0),
        via_ps=float(cfg.value("via_ps") or 2.0),
    )

    classes = netclass.parse_project(sidecars.project) if sidecars.project else None
    pairs = netclass.find_pairs([n for n in model.nets if n], classes)

    ctx.progress("rules", 68, "tracing net connectivity")
    topo = topology.build(model)
    pads_by_net = {net: t.pads for net, t in topo.items()}
    groups = matching.find_groups(
        [n for n in model.nets if n], pads_by_net, classes, pairs,
        min_group_size=int(cfg.param("ddr-skew", "min_group_size") or 3),
    )

    ctx.progress("rules", 72, "running EMI checks")
    rules = run_rules(RuleContext(
        model=model,
        transform=_board_extent(model),
        max_frequency_hz=max_freq,
        settings=cfg,
        electrics=electrics,
        topology=topo,
        netclasses=classes,
        pairs=pairs,
        groups=groups,
    ))
    rules_doc = rules.as_dict()

    # Suppressed findings are removed here rather than never generated, so the count of what
    # was hidden can be reported -- a silent filter is how a suppression file rots.
    kept, hidden = [], 0
    for f in rules_doc["findings"]:
        if cfg.suppressed(f["rule"], f.get("net", "")):
            hidden += 1
            continue
        kept.append(f)
    rules_doc["findings"] = kept
    rules_doc["suppressed"] = hidden
    rules_doc["settings_warnings"] = cfg.warnings
    # What came with the board, and what did not. "No .kicad_pro, so groups were inferred from
    # names" is exactly the sentence that explains a missing netclass column, and it was being
    # collected and then dropped on the floor.
    rules_doc["notes"] = (
        list(rules_doc.get("notes") or []) + list(sidecars.notes) + list(electrics.notes)
    )
    if hidden:
        rules_doc["notes"].append(
            f"{hidden} finding{'s' if hidden != 1 else ''} hidden by suppressions in your settings"
        )

    ctx.progress("upload", 80, "uploading normalised board")

    artifacts = [
        ctx.client.upload_artifact(ctx.token, "board.json", to_json(doc), "application/json"),
        ctx.client.upload_artifact(
            ctx.token, "geometry.bin", geometry, "application/octet-stream"
        ),
        ctx.client.upload_artifact(
            ctx.token, "rules.json",
            json.dumps(rules_doc, separators=(",", ":")).encode(), "application/json",
        ),
        # The net list as rows, for the CSV export. Structured rather than CSV here: the
        # browser formats it for the user's spreadsheet locale, which the worker cannot know.
        ctx.client.upload_artifact(
            ctx.token, "nets.json",
            json.dumps(netreport.build(
                model, topo, electrics, groups, pairs, classes, cfg,
            ), separators=(",", ":")).encode(),
            "application/json",
        ),
    ]

    ctx.progress("done", 100, "ingest complete")

    summary = {
        "stage": "ingest",
        "filename": filename,
        "source_kind": source_kind,
        "input_bytes": len(data),
        "input_sha256": digest,
        "kicad_version": model.version,
        "layers": len(model.copper_layers),
        "nets": len(doc["nets"]),
        "tracks": len(model.tracks),
        "vias": len(model.vias),
        "pads": len(model.pads),
        "zones": len(model.zones),
        "triangles": doc["geometry"]["vertex_count"] // 3,
        "board_mm": [doc["board"]["width_mm"], doc["board"]["height_mm"]],
        "findings": rules_doc["summary"],
        "warnings": len(model.warnings),
    }
    log.info(
        "ingested %s: %d layers, %d nets, %d triangles, %d findings",
        filename, summary["layers"], summary["nets"], summary["triangles"],
        len(rules_doc["findings"]),
    )

    return StageResult(
        summary=summary,
        artifacts=artifacts,
        board={
            "board_key": artifacts[0]["key"],
            "layer_count": len(model.copper_layers),
            "net_count": len(doc["nets"]),
            "outline_mm": {
                "width": doc["board"]["width_mm"],
                "height": doc["board"]["height_mm"],
            },
            "stackup": doc["stackup"],
        },
    )
