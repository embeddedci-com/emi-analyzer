"""Getting the open board into the app, and remembering which project it became.

Qt-free, so the whole flow -- pack the board, ask whether the app already has it, upload,
find what the checks said -- can be driven from a test with a stand-in for the app.

One board file is one project. A board is analysed over and over while it is being worked
on, and each pass is a new board in the same project: the project page already shows the
newest finished analysis and says when a newer one is running, so the history of a design
stays in one place instead of scattering into a project per press.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .boardio import BoardFiles

RELEASE_ID = "com.embeddedci.emi-analyzer"

#: A fixed timestamp inside the archive. Uploads are content-addressed, so the same board
#: has to give the same bytes: with a real timestamp every press would look like a new
#: board and re-analyse a design that had not changed.
ZIP_TIME = (2020, 1, 1, 0, 0, 0)

#: Findings worth stopping for. "info" is context, not a problem, and selecting all of it
#: would select most of the board.
ATTENTION = ("critical", "warning")


def shared_identifier(identifier: str) -> str:
    """The identifier settings are kept under: a ".dev" copy shares the release's."""
    return identifier[: -len(".dev")] if identifier.endswith(".dev") else identifier


def fallback_dir(identifier: str = RELEASE_ID) -> Path:
    """Where to keep settings when KiCad cannot say (an older KiCad, or no connection)."""
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / identifier
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / identifier
    return Path.home() / ".config" / identifier


def archive(files: BoardFiles) -> bytes:
    """The board and its sidecars as one zip, exactly as the analyzer reads a project.

    A bare .kicad_pcb would be accepted too, but then the netclasses and the differential
    pairs are guessed from net names. The project file is right there on disk.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in [(files.filename, files.text)] + sorted(files.sidecars.items()):
            info = zipfile.ZipInfo(name, ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 | 0o100000) << 16
            info.create_system = 3
            z.writestr(info, data)
    return buf.getvalue()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ProjectStore:
    """Which project a board file became, kept between runs of the plugin.

    One JSON file per board, keyed by the board's path, in the settings folder KiCad gives
    the plugin. A development copy and a released one share it: the project is the board's,
    not the build's.
    """

    def __init__(self, directory: Path):
        self.dir = Path(directory) / "boards"

    def _file(self, board: str) -> Path:
        key = hashlib.sha1(board.encode("utf-8")).hexdigest()[:20]
        return self.dir / f"{key}.json"

    def load(self, board: str) -> Optional[str]:
        try:
            data = json.loads(self._file(board).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if data.get("board") != board:
            return None
        project = data.get("project")
        return project if isinstance(project, str) and project else None

    def save(self, board: str, project: str) -> None:
        """Written atomically: a crash mid-write keeps the old file."""
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._file(board)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps({"board": board, "saved_at": int(time.time()), "project": project}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def forget(self, board: str) -> None:
        try:
            self._file(board).unlink()
        except FileNotFoundError:
            pass


@dataclass
class Opened:
    """What became of a board that was handed to the app."""

    project_id: str
    #: The run analysing it, or None when the app already had this exact board analysed.
    run_id: Optional[str] = None
    board_id: Optional[str] = None
    #: False when nothing was sent because the app recognised the bytes.
    uploaded: bool = True


def open_board(client, store: Optional[ProjectStore], files: BoardFiles) -> Opened:
    """Hand the board to the app, and return the project to show.

    Nothing is sent when the app already holds exactly these bytes, analysed -- which is the
    common case of opening a board again without having touched it.
    """
    data = archive(files)
    sha = sha256_hex(data)
    remembered = store.load(files.key) if store else None
    # A project deleted in the app is not a project. Checked before it is preferred over what
    # the app already holds, or the board would be analyzed again for nothing.
    if remembered and not _exists(client, remembered):
        remembered = None

    found = _lookup(client, sha)
    if found and (remembered is None or found[0] == remembered):
        project_id, board_id = found
        if store:
            store.save(files.key, project_id)
        return Opened(project_id=project_id, board_id=board_id, uploaded=False)

    project_id = remembered or client.create_project(files.stem or files.filename)["id"]

    got = client.upload_board(project_id, files.filename.rsplit(".", 1)[0] + ".zip", data, sha)
    if store:
        store.save(files.key, project_id)
    return Opened(
        project_id=project_id,
        run_id=(got.get("run") or {}).get("id"),
        board_id=(got.get("board") or {}).get("id"),
    )


def _lookup(client, sha256: str):
    """``(project_id, board_id)`` for a board the app has already analysed, or None."""
    try:
        got = client.lookup_board(sha256)
    except Exception:  # noqa: BLE001 -- a lookup that fails costs an upload, not the run
        return None
    if not got.get("found") or not got.get("parsed"):
        return None
    project = (got.get("project") or {}).get("id")
    board = (got.get("board") or {}).get("id")
    return (project, board) if project else None


def _exists(client, project_id: str) -> bool:
    try:
        client.get_project(project_id)
        return True
    except Exception:  # noqa: BLE001 -- deleted in the app, or never there
        return False


def latest_ingest(runs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The newest finished analysis, which is the one the page is showing."""
    ingests = sorted(
        (r for r in runs if r.get("kind") == "ingest"),
        key=lambda r: str(r.get("created_at") or ""),
        reverse=True,
    )
    return next((r for r in ingests if r.get("status") == "done"), None)


def attention_nets(client, project_id: str) -> List[str]:
    """Every net a check has something to say about, worst first, without repeats.

    Read from the app rather than from the page, so the button works whatever the window is
    showing -- and on an app whose webapp is older than this plugin.
    """
    run = latest_ingest(client.list_runs(project_id))
    if not run:
        return []
    rules = client.artifact(run["id"], "rules.json") or {}
    seen: Dict[str, None] = {}
    for severity in ATTENTION:
        for f in rules.get("findings") or []:
            if f.get("severity") == severity and f.get("net"):
                seen.setdefault(f["net"], None)
    return list(seen)
