"""What KiCad runs when the toolbar button is pressed.

Each press is a fresh process. The first one takes the window; a later one asks the window
already open to come forward with the board as it is now, and exits before paying for Qt, a
web view and a second connection to the app.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
ICON = PLUGIN_DIR / "icons" / "emi-48.png"


def plugin_identifier() -> str:
    """The identifier of the installed copy running this code.

    A development install links this source into KiCad under a plugin.json of its own, with
    its own identifier; that file sits beside the entry point KiCad ran, not beside this
    source. KICAD_PLUGIN_DIR is not something KiCad promises to set, so the entry point's own
    directory is asked first.
    """
    import json

    for d in (Path(sys.argv[0]).absolute().parent, PLUGIN_DIR):
        try:
            return json.loads((d / "plugin.json").read_text())["identifier"]
        except (OSError, ValueError, KeyError):
            continue
    return "com.embeddedci.emi-analyzer"


def _app(argv):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName("EMI Analyzer")
    return app


def _fatal(title: str, err: BaseException) -> int:
    from PySide6.QtWidgets import QMessageBox

    detail = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    if os.environ.get("EMI_ANALYZER_DEBUG"):
        print(detail, file=sys.stderr)
    box = QMessageBox(QMessageBox.Icon.Critical, title, str(err))
    box.setDetailedText(detail)
    box.exec()
    return 1


def analyze(argv=None) -> int:
    """Open the EMI Analyzer on the board open in pcbnew."""
    argv = list(sys.argv if argv is None else argv)
    from .single import HEARTBEAT_S, SingleInstance

    instance = SingleInstance(name=plugin_identifier())
    if not instance.acquire():
        if instance.ask_to_show():
            return 0
        # The window holding the lock did not answer: take over from it.
        if not instance.acquire():
            return 0
    try:
        return _analyze(argv, instance, HEARTBEAT_S)
    finally:
        instance.release()


def _analyze(argv, instance, heartbeat_s: float) -> int:
    app = _app(argv)
    from PySide6.QtCore import QTimer

    from .controller import Controller
    from .window import AnalyzerWindow

    ctl = Controller(identifier=plugin_identifier())
    try:
        win = AnalyzerWindow(ctl, ICON)
    except Exception as e:  # noqa: BLE001
        ctl.close()
        return _fatal("The EMI Analyzer window could not be opened", e)
    win.show()
    win.raise_()
    win.activateWindow()
    win.start()

    def tick() -> None:
        instance.heartbeat()
        if instance.take_request():
            win.bring_forward()

    timer = QTimer()
    timer.setInterval(int(heartbeat_s * 1000))
    timer.timeout.connect(tick)
    timer.start()
    code = app.exec()
    timer.stop()
    # The page before its profile, and both before the threads they call into.
    win.release_web()
    win.deleteLater()
    app.processEvents()
    ctl.close()
    return code
