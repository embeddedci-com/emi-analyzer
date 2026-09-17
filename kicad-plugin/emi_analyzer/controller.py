"""The plugin's state: one app, one KiCad connection, the project the board became.

Qt-free, so the flows -- find the app, read the board into it, select nets, ask what the
checks found -- can be driven from a test with stand-ins for KiCad and the app.

Everything runs on one worker thread. KiCad is the reason: the IPC client is not documented
as safe to share across threads, and pcbnew answers on its own UI thread anyway. The app is
put on the same thread because the two are always used together, and one thread is one order
of events to reason about.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, List, Optional

from . import boardio, desktop, project as projectlib
from .client import Client
from .desktop import Endpoint
from .project import Opened, ProjectStore, RELEASE_ID


class Controller:
    def __init__(
        self,
        identifier: str = RELEASE_ID,
        kicad: Any = None,
        board: Any = None,
        client: Optional[Client] = None,
        store: Optional[ProjectStore] = None,
        app: Optional[Endpoint] = None,
    ):
        self.identifier = identifier
        self.kicad = kicad
        self.board = board
        self.client = client
        self.app = app
        #: Found on the first read of the board, since KiCad names the folder.
        self.store = store
        self.project_id: str = ""
        self.filename: str = ""
        self.source: str = ""
        self._thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="emi-analyzer")
        self._lock = threading.Lock()

    # ---- threading ----

    def submit(self, fn: Callable[[], Any]) -> "Future[Any]":
        return self._thread.submit(fn)

    def close(self) -> None:
        self._thread.shutdown(wait=False, cancel_futures=True)

    # ---- the app ----

    def connect_app(self, on_wait: Optional[Callable[[float], None]] = None) -> Endpoint:
        """Attach to the running app, starting the installed one if there is none."""
        if self.client is not None and self.app is not None:
            return self.app
        app = desktop.attach_or_start(on_wait=on_wait)
        with self._lock:
            self.app = app
            if self.client is None:
                self.client = Client(app.url)
        return app

    def project_url(self) -> str:
        """The page this window shows: the board's project in the app."""
        base = (self.app.url if self.app else "").rstrip("/")
        return f"{base}/tools/emi/{self.project_id}" if self.project_id else f"{base}/tools/emi"

    # ---- KiCad ----

    def ensure_connected(self) -> None:
        if self.board is None:
            self.kicad, self.board = boardio.connect()

    def _settings(self) -> ProjectStore:
        if self.store is None:
            path = None
            try:
                if self.kicad is not None:
                    path = self.kicad.get_plugin_settings_path(
                        projectlib.shared_identifier(self.identifier)
                    )
            except Exception:  # noqa: BLE001 -- an older KiCad: the user's config folder instead
                path = None
            self.store = ProjectStore(
                path or projectlib.fallback_dir(projectlib.shared_identifier(self.identifier))
            )
        return self.store

    # ---- flows ----

    def rescan(self) -> Opened:
        """Read the board from the editor and hand it to the app.

        Called on every press of the button and by Rescan, because the board has usually
        changed in between. When it has not, the app recognises the bytes and nothing is
        sent or re-analysed.
        """
        self.connect_app()
        self.ensure_connected()
        files = boardio.read_board(self.board)
        opened = projectlib.open_board(self.client, self._settings(), files)
        with self._lock:
            self.project_id = opened.project_id
            self.filename, self.source = files.filename, files.source
        return opened

    def select(self, nets: List[str]) -> int:
        self.ensure_connected()
        return boardio.select_nets(self.kicad, self.board, nets)

    def selected_nets(self) -> List[str]:
        self.ensure_connected()
        return boardio.selected_nets(self.board)

    def attention_nets(self) -> List[str]:
        if not self.project_id or self.client is None:
            return []
        return projectlib.attention_nets(self.client, self.project_id)

    def select_attention(self) -> int:
        """Select every net a check flagged. Returns how many copper items that is."""
        nets = self.attention_nets()
        return self.select(nets) if nets else 0

    def worker_state(self) -> str:
        """How the analysis worker is doing, for the status line. Empty when unknown.

        The app writes a sentence for the user as well as a state; the sentence is better
        when there is one ("Downloading the analysis worker image").
        """
        if self.client is None:
            return ""
        try:
            worker = self.client.status().get("worker") or {}
        except Exception:  # noqa: BLE001 -- a status line must never be the thing that fails
            return ""
        return str(worker.get("message") or worker.get("state") or "")
