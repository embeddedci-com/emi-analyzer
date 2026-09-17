"""Everything this plugin does with pcbnew, through KiCad's IPC API.

Reading the board the user is looking at, pointing at nets on it, and finding out which net
they have clicked. Nothing here writes to the board: the EMI Analyzer reports on a layout, it
does not edit one.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: A rules document committed beside the board, in the order the worker prefers them. Sent
#: with the board when one is there, so the checks a team agreed on are the checks that run.
SETTINGS_NAMES = ("emi.rules.yaml", "emi.rules.yml", "emi.rules.json", ".emi.yaml")


class KiCadUnavailable(RuntimeError):
    """KiCad is not running, its API server is off, or no board is open."""


API_HELP = (
    "Could not reach KiCad. Open the board in the PCB Editor, and make sure the API server is "
    "on: Preferences -> Plugins -> Enable KiCad API. (KiCad starts plugins with the connection "
    "details already set; running this script by hand needs KiCad open as well.)"
)


def connect():
    """The running KiCad and the board open in it."""
    try:
        from kipy import KiCad
    except ImportError as e:  # pragma: no cover - only without the dependency
        raise KiCadUnavailable("kicad-python is not installed in the plugin's environment") from e
    try:
        kicad = KiCad(client_name="emi-analyzer")
        kicad.ping()
        board = kicad.get_board()
    except Exception as e:  # noqa: BLE001 -- kipy raises several unrelated types here
        raise KiCadUnavailable(f"{API_HELP}\n\n({e})") from e
    return kicad, board


@dataclass
class BoardFiles:
    text: bytes
    filename: str
    #: Files that travel with the board: the .kicad_pro, which is where netclasses and
    #: differential-pair geometry live, and a committed rules document if there is one.
    #: Keyed by the name they are sent under. Empty is fine -- the analyzer says so and
    #: falls back to inferring pairs from net names.
    sidecars: Dict[str, bytes]
    project_dir: Optional[Path]
    #: How the board text was obtained, for the status line.
    source: str

    @property
    def stem(self) -> str:
        return self.filename[: -len(".kicad_pcb")] if self.filename.endswith(".kicad_pcb") else self.filename

    @property
    def key(self) -> str:
        """What identifies this board between runs of the plugin: its path, or its name."""
        return str(self.project_dir / self.filename) if self.project_dir else self.filename


def read_board(board) -> BoardFiles:
    """The board as it is in the editor, unsaved edits included, plus its project file.

    The board comes from pcbnew itself rather than from disk, so what is analysed is what the
    user is looking at, and their file is never saved on their behalf.
    """
    filename = Path(board.name).name or "board.kicad_pcb"
    text: Optional[bytes] = None
    source = "the editor"
    try:
        text = board.get_as_string().encode("utf-8")
    except Exception:  # noqa: BLE001 -- an older KiCad without SaveDocumentToString
        text = None
    if not text:
        # Save a copy -- not the user's file -- and read that.
        with tempfile.TemporaryDirectory(prefix="emi-analyzer-") as tmp:
            dest = os.path.join(tmp, filename)
            board.save_as(dest, overwrite=True, include_project=False)
            text = Path(dest).read_bytes()
        source = "a saved copy"

    project_dir: Optional[Path] = None
    try:
        p = board.get_project().path
        if p:
            project_dir = Path(p)
    except Exception:  # noqa: BLE001
        project_dir = None

    return BoardFiles(
        text=text,
        filename=filename,
        sidecars=_sidecars(project_dir, filename),
        project_dir=project_dir,
        source=source,
    )


def _sidecars(project_dir: Optional[Path], filename: str) -> Dict[str, bytes]:
    """The project file and any committed rules document, read from disk beside the board."""
    out: Dict[str, bytes] = {}
    if project_dir is None:
        return out
    stem = filename[: -len(".kicad_pcb")] if filename.endswith(".kicad_pcb") else filename
    wanted = [stem + ".kicad_pro"]
    for name in SETTINGS_NAMES:
        if (project_dir / name).is_file():
            wanted.append(name)
            break
    for name in wanted:
        f = project_dir / name
        try:
            if f.is_file():
                out[name] = f.read_bytes()
        except OSError:
            continue
    return out


# ---- nets ----


def _copper_types():
    from kipy.proto.common.types import KiCadObjectType

    return [KiCadObjectType.KOT_PCB_TRACE, KiCadObjectType.KOT_PCB_ARC, KiCadObjectType.KOT_PCB_VIA]


def copper_of_nets(board, nets: Sequence[str]) -> List[Any]:
    """Tracks, arcs and vias on these nets."""
    from kipy.board_types import Net

    wanted = set(nets)
    if not wanted:
        return []
    try:
        return list(board.get_items_by_net([Net(name=n) for n in wanted], _copper_types()))
    except Exception:  # noqa: BLE001 -- before KiCad 10.0.1; filter everything instead
        items = list(board.get_tracks()) + list(board.get_vias())
        return [i for i in items if i.net.name in wanted]


def select_nets(kicad, board, nets: Sequence[str]) -> int:
    """Select these nets' copper and bring it into view. Returns how many items."""
    items = copper_of_nets(board, nets)
    board.clear_selection()
    if items:
        board.add_to_selection(items)
        zoom_to_selection(kicad)
    return len(items)


def zoom_to_selection(kicad) -> None:
    # KiCad does not promise action names are stable, so a missing one is not an error: the
    # selection is made either way, and only the framing is lost. run_action reports an unknown
    # action as a status, not an exception.
    from kipy.proto.common.commands.editor_commands_pb2 import RunActionStatus

    for action in ("common.Control.zoomFitSelection", "pcbnew.Control.zoomFitSelection"):
        try:
            if kicad.run_action(action).status == RunActionStatus.RAS_OK:
                return
        except Exception:  # noqa: BLE001
            continue


def selected_nets(board) -> List[str]:
    """The nets of whatever copper or pads are selected, in selection order."""
    seen: Dict[str, None] = {}
    try:
        items = board.get_selection()
    except Exception:  # noqa: BLE001
        return []
    for item in items:
        net = getattr(item, "net", None)
        name = getattr(net, "name", "") if net is not None else ""
        if name:
            seen.setdefault(name, None)
    return list(seen)
