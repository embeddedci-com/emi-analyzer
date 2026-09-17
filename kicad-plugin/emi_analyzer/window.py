"""The EMI Analyzer's window beside pcbnew.

The middle of it is the app's own page, loaded over http://127.0.0.1 from the desktop app
this plugin found. The toolbar is the part that only makes sense here: read the board again,
and select in KiCad whatever the checks flagged.

There is no custom URL scheme and no proxy. The app is already an HTTP server on this
computer, so the web view talks to it the way a browser tab would, and the only thing added
is a channel back to pcbnew (bridge.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QToolBar,
    QWidget,
)

from .bridge import Bridge, adapter_source
from .controller import Controller
from .desktop import INSTALL_URL, AppUnavailable


class _Bridge(QObject):
    """Carries results from the worker thread back to the Qt thread."""

    done = Signal(object, object)  # callback, (result, error)


class _Page(QWebEnginePage):
    """Opens anything that is not the local app in the system browser."""

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:  # noqa: N802
        if url.host() in ("127.0.0.1", "localhost", "::1") or url.scheme() in ("about", "data", "blob"):
            return True
        if url.scheme() in ("http", "https"):
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(url)
        return False

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        import os
        import sys

        if os.environ.get("EMI_ANALYZER_DEBUG"):
            print(f"page: {source}:{line}: {message}", file=sys.stderr)


class AnalyzerWindow(QMainWindow):
    SELECTION_POLL_MS = 1500

    def __init__(self, controller: Controller, icon: Optional[Path] = None):
        super().__init__()
        self.ctl = controller
        self.setWindowTitle("EMI Analyzer")
        if icon and icon.is_file():
            self.setWindowIcon(QIcon(str(icon)))
        self.resize(1400, 900)

        self._results = _Bridge()
        self._results.done.connect(self._on_done)

        # A profile of our own, off the record: nothing the page stores is written to disk,
        # and it belongs to this window rather than to the whole process.
        self._profile = QWebEngineProfile(self)
        self.bridge_error = ""
        self._inject_bridge()
        self.view = QWebEngineView(self)
        page = _Page(self._profile, self.view)
        self._channel = None
        self._attach_channel(page)
        self.view.setPage(page)
        self.setCentralWidget(self.view)

        self._build_toolbar()
        self.statusBar().showMessage("Looking for the EMI Analyzer app…")

        self._last_selection: List[str] = []
        self._poll = QTimer(self)
        self._poll.setInterval(self.SELECTION_POLL_MS)
        self._poll.timeout.connect(self._poll_selection)
        self._polling = False

    # ---- the channel to KiCad ----

    def _inject_bridge(self) -> None:
        """Put the channel adapter into every page this window loads.

        Recorded when it cannot be done, rather than passed over: without it the pages lose
        "Show in KiCad" and there is nothing on screen to say why. The toolbar still selects,
        so the window is worth showing either way.
        """
        try:
            source = adapter_source()
        except Exception as e:  # noqa: BLE001 -- a missing Qt resource must not stop the window
            self.bridge_error = str(e)
            return
        script = QWebEngineScript()
        script.setName("emi-analyzer-kicad-bridge")
        script.setSourceCode(source)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        self._profile.scripts().insert(script)

    def _attach_channel(self, page: QWebEnginePage) -> None:
        try:
            from PySide6.QtWebChannel import QWebChannel
        except ImportError:
            return
        bridge = Bridge(
            {
                "select": lambda a: {"selected": self.ctl.select([str(n) for n in a.get("nets") or []])},
                "attention": lambda a: {"selected": self.ctl.select_attention()},
            },
            run=self._run_kicad,
            parent=self,
        )
        self._channel = QWebChannel(self)
        self._channel.registerObject("kicad", bridge)
        page.setWebChannel(self._channel)

    # ---- toolbar ----

    def _build_toolbar(self) -> None:
        tb = QToolBar("EMI Analyzer", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        tb.addAction("Rescan", self.rescan).setToolTip(
            "Read the board from KiCad again, with any edits made since, and analyze it"
        )
        tb.addAction("Select what needs attention", self.select_attention).setToolTip(
            "Select in KiCad every net a check flagged as critical or a warning"
        )
        tb.addSeparator()

        self._follow = QCheckBox("Follow KiCad selection")
        self._follow.setToolTip("Show which net is selected in the PCB Editor")
        self._follow.setChecked(True)
        self._follow.toggled.connect(self._follow_toggled)
        tb.addWidget(self._follow)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        self._net_line = QLabel("")
        self.statusBar().addPermanentWidget(self._net_line, 1)

    # ---- actions ----

    def start(self) -> None:
        """Find the app, hand it the board, and show the result."""
        self.statusBar().showMessage("Looking for the EMI Analyzer app…")
        self._run_kicad(self._open, self._opened)

    def _open(self):
        self.ctl.connect_app(on_wait=self._starting)
        return self.ctl.rescan()

    def _starting(self, waited: float) -> None:
        # Called from the worker thread; the status bar is set on the Qt thread.
        self._results.done.emit(
            lambda _r, _e: self.statusBar().showMessage(
                f"Starting the EMI Analyzer app… ({waited:.0f}s)"
            ),
            (None, None),
        )

    def _opened(self, opened: Any, err: Optional[BaseException]) -> None:
        if self.view is None:
            return
        if err:
            if isinstance(err, AppUnavailable):
                self._app_missing(err)
                return
            self.statusBar().showMessage("The board could not be analyzed")
            QMessageBox.warning(self, "The board could not be analyzed", str(err))
            return
        where = "already analyzed" if not opened.uploaded else f"sent from {self.ctl.source}"
        version = f" (app {self.ctl.app.version})" if self.ctl.app and self.ctl.app.version else ""
        note = "  Show in KiCad is unavailable: " + self.bridge_error if self.bridge_error else ""
        self.statusBar().showMessage(f"{self.ctl.filename}: {where}{version}{note}", 10000)
        self.view.setUrl(QUrl(self.ctl.project_url()))
        self._last_selection = []
        if self._follow.isChecked():
            self._poll.start()

    def _app_missing(self, err: AppUnavailable) -> None:
        """Say what is missing, and offer the one thing that fixes it.

        A plugin that reports "app not found" and stops leaves the user to work out what app,
        where from and what to do next. The button does the first two.
        """
        self.statusBar().showMessage("EMI Analyzer is not running")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("EMI Analyzer is not running")
        box.setText(
            "This plugin is a front end. EMI Analyzer itself holds your boards and runs the "
            "analysis, on this computer."
        )
        box.setInformativeText(str(err))
        get = box.addButton("Get the app", QMessageBox.ButtonRole.ActionRole)
        retry = box.addButton("Rescan", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.setDefaultButton(retry)
        box.exec()

        clicked = box.clickedButton()
        if clicked is get:
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl(INSTALL_URL))
            self.statusBar().showMessage(
                "Install EMI Analyzer, start it once, then press Rescan", 0
            )
        elif clicked is retry:
            self.rescan()

    def bring_forward(self) -> None:
        """The button was pressed again: show this window, with the board as it is now."""
        from PySide6.QtCore import Qt

        if self.isMinimized():
            self.showNormal()
        self.show()
        # macOS will not hand focus to a background process on request. A moment on top gets
        # the window in front of pcbnew either way.
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.show()
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(300, self._drop_on_top)
        self.rescan()

    def _drop_on_top(self) -> None:
        from PySide6.QtCore import Qt

        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        self.show()

    def rescan(self) -> None:
        """Read the board again and show the analysis of it.

        The page is pointed at the project afresh rather than reloaded: a new analysis of
        the same board is a new run in the same project, and the page picks that up by
        itself once it is looking at the right one.
        """
        self.statusBar().showMessage("Reading the board from KiCad…")
        self._run_kicad(self._open, self._opened)

    def select_attention(self) -> None:
        def done(n: Any, err: Optional[BaseException]) -> None:
            if err:
                QMessageBox.warning(self, "Could not select", str(err))
            elif not n:
                self.statusBar().showMessage(
                    "Nothing to select: no check has flagged a net on this board", 8000
                )
            else:
                self.statusBar().showMessage(f"Selected {n} items in KiCad", 8000)

        self._run_kicad(self.ctl.select_attention, done)

    # ---- following KiCad's selection ----

    def _follow_toggled(self, on: bool) -> None:
        if on:
            self._last_selection = []
            self._poll.start()
        else:
            self._poll.stop()
            self._net_line.setText("")

    def _poll_selection(self) -> None:
        if self._polling:
            return
        self._polling = True

        def work():
            nets = self.ctl.selected_nets()
            return None if nets == self._last_selection else nets

        def done(nets: Any, err: Optional[BaseException]) -> None:
            self._polling = False
            if err or nets is None:
                return
            self._last_selection = nets
            if not nets:
                self._net_line.setText("")
                return
            shown = ", ".join(nets[:6])
            more = f"  (+{len(nets) - 6} more)" if len(nets) > 6 else ""
            self._net_line.setText(f"Selected in KiCad: {shown}{more}")
            self._net_line.setToolTip("\n".join(nets))

        self._run_kicad(work, done)

    # ---- plumbing ----

    def _run_kicad(self, fn, callback) -> None:
        """Run ``fn`` on the worker thread and call ``callback(result, error)`` on this one."""
        fut = self.ctl.submit(fn)

        def finished(f):
            try:
                self._results.done.emit(callback, (f.result(), None))
            except BaseException as e:  # noqa: BLE001
                self._results.done.emit(callback, (None, e))

        fut.add_done_callback(finished)

    @Slot(object, object)
    def _on_done(self, callback, outcome) -> None:
        result, err = outcome
        callback(result, err)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._poll.stop()
        self.release_web()
        super().closeEvent(event)

    def release_web(self) -> None:
        """Destroy the page, then the view, now -- before the profile.

        Qt destroys a window's children in the order they were made, which puts the profile
        before the page that uses it: Qt warns "Release of profile requested but
        WebEnginePage still not deleted", and the process then dies with a bus error on the
        way out. Deleting the page first, at once rather than with deleteLater, is the order
        QtWebEngine needs.
        """
        import shiboken6

        view = getattr(self, "view", None)
        if view is None or not shiboken6.isValid(view):
            return
        page = view.page()
        view.hide()
        if page is not None and shiboken6.isValid(page):
            shiboken6.delete(page)
        shiboken6.delete(view)
        self.view = None
