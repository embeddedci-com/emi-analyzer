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
import re
import zipfile
from dataclasses import dataclass, field

from .. import stackup, topology
from ..gerber import GerberError, NetlistError, load_gerber_board
from ..kicad import netclass, parse, parse_board
from ..kicad.board import ZONES_UNFILLED_NOTE
from ..kicad.normalize import board_extent, normalize, to_json
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

#: Archive limits. The per-member cap alone let a zip of many 199 MB members through, each
#: read into memory (twice: once for the board, once for the sidecars). So there is also a
#: cap on the members read in total, one on what the archive claims to hold altogether, and
#: one on how many entries it may list. A KiCad project with its fabrication output is a
#: few dozen files and a few MB.
MAX_ARCHIVE_MEMBERS = 5_000
MAX_ARCHIVE_READ_BYTES = 300 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024


#: Extensions that mark a file as part of a Gerber set.
GERBER_SUFFIXES = (".gbr", ".gbrjob", ".drl", ".gtl", ".gbl", ".gm1", ".d356")

#: Everything the Gerber reader might use: copper, outline, drill, netlist and job files,
#: under the names KiCad and the usual Protel-style exporters give them. Silkscreen, mask,
#: paste and 3D models are never read.
_GERBER_SET = re.compile(
    r"\.(gbr|gbrjob|drl|xln|exc|txt|d356|ipc|net|gtl|gbl|gko|gm\d*|g\d+)$", re.IGNORECASE,
)


def _skipped(name: str) -> bool:
    """Archiver noise and KiCad's own backup copies."""
    base = name.rsplit("/", 1)[-1]
    return "__MACOSX" in name or base.startswith((".", "_autosave-"))


@dataclass
class _Archive:
    """An opened upload zip: its usable entries, checked against the limits, not yet read."""

    zf: zipfile.ZipFile
    infos: list[zipfile.ZipInfo]
    read_bytes: int = 0

    @classmethod
    def open(cls, data: bytes) -> "_Archive":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise StageError("the upload looks like a zip file but could not be opened") from exc
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise StageError(
                f"the archive lists {len(infos):,} files, more than the "
                f"{MAX_ARCHIVE_MEMBERS:,} limit. Zip only the project folder."
            )
        total = sum(i.file_size for i in infos)
        if total > MAX_ARCHIVE_TOTAL_BYTES:
            raise StageError(
                f"the archive unpacks to {total / 1e6:,.0f} MB, more than the "
                f"{MAX_ARCHIVE_TOTAL_BYTES / 1e6:,.0f} MB limit"
            )
        return cls(zf, [i for i in infos if not i.is_dir() and not _skipped(i.filename)])

    def names(self) -> list[str]:
        return [i.filename for i in self.infos]

    def read(self, name: str) -> bytes:
        """One member, counted against the per-member and total limits before it is read.

        The sizes checked are the ones the archive declares. zipfile stops at the declared
        size and fails the CRC check on anything that lies about it, so a member cannot
        inflate past what was checked here.
        """
        info = self.zf.getinfo(name)
        if info.file_size > MAX_BOARD_BYTES:
            raise StageError(
                f"{info.filename} is {info.file_size / 1e6:.0f} MB, larger than the "
                f"{MAX_BOARD_BYTES / 1e6:.0f} MB limit"
            )
        if self.read_bytes + info.file_size > MAX_ARCHIVE_READ_BYTES:
            raise StageError(
                f"the board files in the archive add up to more than the "
                f"{MAX_ARCHIVE_READ_BYTES / 1e6:.0f} MB limit"
            )
        self.read_bytes += info.file_size
        try:
            return self.zf.read(info)
        except zipfile.BadZipFile as exc:
            raise StageError(f"{info.filename} in the archive is damaged ({exc})") from exc


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
    #: The rules file that was found, whether or not it could be read. The app says which
    #: file was applied, and "built-in defaults" when this is empty or the file was refused.
    settings_file: str = ""
    settings_error: str = ""
    notes: list[str] = field(default_factory=list)


#: Names recognised as a committed rules document, in preference order.
SETTINGS_NAMES = ("emi.rules.yaml", "emi.rules.yml", "emi.rules.json", ".emi.yaml")


def _short_error(exc: Exception) -> str:
    """One line for a settings file that would not parse.

    A YAML error runs to several lines with a caret diagram, which is unreadable in a note;
    what it found and on which line is the part anyone can act on.
    """
    problem, mark = getattr(exc, "problem", None), getattr(exc, "problem_mark", None)
    if problem and mark is not None:
        return f"{problem} on line {mark.line + 1}"
    msg = str(exc).strip()
    return msg.splitlines()[0] if msg else type(exc).__name__


def _read_sidecars(archive: _Archive) -> Sidecars:
    out = Sidecars()
    names = archive.names()

    projects = sorted(
        (n for n in names if n.lower().endswith(".kicad_pro")),
        key=lambda n: len(n.rsplit("/", 1)[-1]),
    )
    if projects:
        out.project = archive.read(projects[0])
    else:
        out.notes.append(
            "No .kicad_pro in the upload, so net classes and pairs were guessed from net "
            "names. Upload the zipped project folder to use them."
        )

    # Settings files may be dotfiles (.emi.yaml), which _skipped leaves out of the listing.
    every = [i.filename for i in archive.zf.infolist() if not i.is_dir()]
    for name in SETTINGS_NAMES:
        hit = next((n for n in every if n.rsplit("/", 1)[-1].lower() == name), None)
        if hit:
            label = hit.rsplit("/", 1)[-1]
            out.settings_file = label
            try:
                out.settings = settings.parse_document(
                    archive.read(hit).decode("utf-8", "replace"))
                out.notes.append(f"Rules read from {label}.")
            except StageError:
                raise
            except Exception as exc:  # noqa: BLE001
                out.settings_error = _short_error(exc)
                out.notes.append(
                    f"{label} could not be read ({out.settings_error}), so built-in defaults "
                    "were used. Fix the file and upload again."
                )
            break
    return out


def load_sidecars(data: bytes) -> Sidecars:
    if data[:2] != b"PK":
        return Sidecars()
    try:
        return _read_sidecars(_Archive.open(data))
    except StageError:
        return Sidecars()


def _board_from_archive(archive: _Archive) -> tuple[object, str, str]:
    names = archive.names()
    kicad = [n for n in names if n.lower().endswith(".kicad_pcb")]
    if kicad:
        # Shallowest path wins: a project's own board sits above any imported one.
        kicad.sort(key=lambda n: (n.count("/"), len(n)))
        name = kicad[0]
        text = archive.read(name).decode("utf-8", errors="replace")
        return parse_board(parse(text)), name, "kicad"

    if any(n.lower().endswith(GERBER_SUFFIXES) for n in names):
        # Keyed by base name, which is what a job file's paths refer to.
        members = {
            n.rsplit("/", 1)[-1]: archive.read(n) for n in names if _GERBER_SET.search(n)
        }
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


def read_upload(data: bytes) -> tuple[object, str, str, Sidecars]:
    """The board and its sidecars from one pass over the upload.

    Returns ``(model, filename, source_kind, sidecars)``. Each member that is needed is read
    once; anything else in the archive is never decompressed.
    """
    if data[:2] != b"PK":
        text, filename = _extract_board(data)
        return parse_board(parse(text)), filename, "kicad", Sidecars()
    archive = _Archive.open(data)
    model, filename, kind = _board_from_archive(archive)
    return model, filename, kind, _read_sidecars(archive)


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
    return _board_from_archive(_Archive.open(data))


def _extract_board(data: bytes) -> tuple[str, str]:
    """Return ``(text, filename)`` for a bare ``.kicad_pcb`` upload."""
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
        # One pass over the upload for the board and the files that came with it
        # (netclasses, committed rules): each member is read once, and only if it is needed.
        model, filename, source_kind, sidecars = read_upload(data)
    except StageError:
        raise
    except ValueError as exc:
        raise StageError(f"the board file could not be read: {exc}") from exc

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
        try:
            fmax = settings.check_value(
                "max_frequency_hz", ctx.params["max_frequency_hz"],
                settings.BOARD_DEFAULTS["max_frequency_hz"], positive=True,
            )
        except ValueError as exc:
            raise StageError(f"this run's settings are not valid: {exc}") from None
        cfg.board["max_frequency_hz"] = settings.Value(fmax, "run")
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
        transform=board_extent(model),
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
    kept, hidden = [], []
    for f in rules_doc["findings"]:
        sup = cfg.suppressed(f["rule"], f.get("net", ""))
        if sup:
            # Enough to list what was hidden and why, not the finding itself: a suppressed
            # finding is not something to act on.
            hidden.append({"rule": f["rule"], "net": f.get("net", ""), "title": f["title"],
                           "reason": sup.reason, "source": sup.source})
            continue
        kept.append(f)
    rules_doc["findings"] = kept
    rules_doc["suppressed"] = len(hidden)
    rules_doc["suppressed_findings"] = hidden
    rules_doc["settings_warnings"] = cfg.warnings
    # What each check ran with and where every value came from, and the same without this
    # run's own layer: the app edits that layer, so it needs to know what is underneath to
    # show a value the user has just cleared, and to export an equivalent emi.rules.yaml.
    rules_doc["settings"] = {
        "file": "" if sidecars.settings_error else sidecars.settings_file,
        "file_error": sidecars.settings_error,
        "applied": settings.snapshot(cfg),
        "base": settings.snapshot(settings.load(
            ("project", ctx.params.get("project_settings")),
            ("file", sidecars.settings),
        )),
    }
    # What came with the board, and what did not. "No .kicad_pro, so groups were inferred from
    # names" is exactly the sentence that explains a missing netclass column, and it was being
    # collected and then dropped on the floor.
    rules_doc["notes"] = (
        list(rules_doc.get("notes") or []) + list(sidecars.notes) + list(electrics.notes)
        # A project file that could not be read says so, rather than netclasses vanishing.
        + list(classes.warnings if classes else [])
        # Unfilled zones switch the plane checks off, so the note belongs with the findings
        # as well as with the board's warnings.
        + [w for w in model.warnings if w.endswith(ZONES_UNFILLED_NOTE.rsplit("}", 1)[-1])]
    )
    if hidden:
        rules_doc["notes"].append(
            f"{len(hidden)} finding{'s' if len(hidden) != 1 else ''} hidden by suppressions."
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
            # The hash of the bytes actually read. The server records this, not the hash
            # the uploader claimed, so deduplication only matches what was checked.
            "content_sha256": digest,
        },
    )
