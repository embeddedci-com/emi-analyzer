"""The one thing the app's pages cannot do on their own: point at the board in KiCad.

The pages come from the app over http://127.0.0.1, so they are not ours to change from here
and they have no way of their own to reach pcbnew. A QWebChannel gives them one: a small
object injected into every page, offering exactly two calls and nothing else.

    window.kicadBridge.select(['GND', 'DDR_A0'])  ->  Promise<number of items selected>
    window.kicadBridge.selectAttention()          ->  Promise<number of items selected>

A page that does not know about it is unaffected, and a page in an ordinary browser tab
never sees it. Calls are answered from the worker thread, so a slow KiCad cannot freeze the
window; the page waits on a promise rather than on the UI.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict

from PySide6.QtCore import QObject, Signal, Slot

#: Bumped when the shape of window.kicadBridge changes, so a page can tell what it is
#: talking to. The app's webapp reads it; an older webapp ignores the whole object.
BRIDGE_VERSION = 1

#: Injected into every page in this window. Waits for the channel, then puts the object on
#: window and says so -- a page that loaded before the channel was ready listens for the
#: event instead of polling.
ADAPTER_JS = """
(function () {
  if (window.kicadBridge || !window.qt || !qt.webChannelTransport) return;
  new QWebChannel(qt.webChannelTransport, function (channel) {
    var kicad = channel.objects.kicad;
    if (!kicad) return;
    var pending = {}, seq = 0;
    kicad.resultReady.connect(function (id, payload) {
      var waiting = pending[id];
      if (!waiting) return;
      delete pending[id];
      var result;
      try { result = JSON.parse(payload); } catch (e) { waiting.reject(e); return; }
      if (result.error) waiting.reject(new Error(result.error));
      else waiting.resolve(result);
    });
    function call(name, args) {
      return new Promise(function (resolve, reject) {
        var id = 'r' + ++seq;
        pending[id] = { resolve: resolve, reject: reject };
        kicad.call(id, name, JSON.stringify(args || {}));
      });
    }
    window.kicadBridge = {
      version: __VERSION__,
      select: function (nets) {
        return call('select', { nets: [].concat(nets || []) }).then(function (r) { return r.selected; });
      },
      selectAttention: function () {
        return call('attention', {}).then(function (r) { return r.selected; });
      },
    };
    window.dispatchEvent(new Event('kicad-bridge-ready'));
  });
})();
"""


def adapter_source() -> str:
    """qwebchannel.js and the adapter, as one script to inject at document creation.

    QtWebChannel is imported first, and not for its API: qwebchannel.js lives in that
    module's Qt resources, and they are not registered until it has been loaded. Without the
    import the file simply is not there, the script is never injected, and the pages lose
    every way of reaching KiCad -- silently, because nothing has failed.
    """
    import PySide6.QtWebChannel  # noqa: F401 -- registers :/qtwebchannel/
    from PySide6.QtCore import QFile, QIODevice

    f = QFile(":/qtwebchannel/qwebchannel.js")
    if not f.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError("Qt's qwebchannel.js is missing from this PySide6 installation")
    try:
        qwebchannel = bytes(f.readAll().data()).decode("utf-8")
    finally:
        f.close()
    return qwebchannel + "\n" + ADAPTER_JS.replace("__VERSION__", str(BRIDGE_VERSION))


class Bridge(QObject):
    """The object the pages call. One slot, so there is one place to see what they can ask.

    ``handlers`` maps a name to a function run on the worker thread; ``run`` submits it and
    calls back with ``(result, error)``. Both are supplied by the window, which owns the
    threading -- this class only translates between JSON and Python.
    """

    resultReady = Signal(str, str)

    def __init__(self, handlers: Dict[str, Callable[[Dict[str, Any]], Any]], run, parent=None):
        super().__init__(parent)
        self._handlers = handlers
        self._run = run

    @Slot(str, str, str)
    def call(self, request_id: str, name: str, args_json: str) -> None:
        handler = self._handlers.get(name)
        if handler is None:
            self._answer(request_id, None, f"the plugin has no {name!r} call")
            return
        try:
            args = json.loads(args_json or "{}")
        except ValueError as e:
            self._answer(request_id, None, f"bad arguments: {e}")
            return
        if not isinstance(args, dict):
            self._answer(request_id, None, "arguments must be an object")
            return
        self._run(lambda: handler(args), lambda result, error: self._answer(request_id, result, error))

    def _answer(self, request_id: str, result: Any, error: Any) -> None:
        if error is not None:
            payload = {"error": str(error)}
        elif isinstance(result, dict):
            payload = result
        else:
            payload = {"result": result}
        self.resultReady.emit(request_id, json.dumps(payload))
