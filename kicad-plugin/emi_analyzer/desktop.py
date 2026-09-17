"""Finding the EMI Analyzer desktop app on this computer, and starting it if it is not up.

The app publishes where it is listening in one file that never moves -- the user's config
folder, ``emi-analyzer/endpoint.json`` -- because its port is not fixed and its data folder
is not either (see server/cmd/emi-local/endpoint.go). That file is a hint and nothing more:
it outlives a crash, a kill and a reboot, so it is never believed without asking the URL in
it whether anything is there.

Starting the app means starting the *app*, not a server of our own. It owns the data folder
and the worker container, it knows how to stop them, and a user who opens it from the Dock
afterwards must find the same boards. A plugin that ran its own copy would give them a second
set of everything.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

#: Point the plugin at a server you started yourself: `make run-local`, or an app on another
#: port. Discovery and starting are both skipped.
URL_ENV = "EMI_ANALYZER_URL"
#: The application to start, when it is somewhere this module would not look.
APP_ENV = "EMI_ANALYZER_APP"

#: How long to wait for an app we started to answer. It has a database to open and a webapp
#: to serve, both local; the Docker worker starts later and is not waited for.
START_TIMEOUT_S = 90.0
PROBE_TIMEOUT_S = 2.0
#: How long one way of starting the app gets before another is tried. Generous, because the
#: cost of being impatient is a second copy of the app with a second worker container.
PER_CANDIDATE_S = 20.0

#: Where to get the app. The window offers this as a button, so keep it a page a person can
#: act on rather than a file.
INSTALL_URL = "https://github.com/embeddedci-com/emi-analyzer#install"

NOT_INSTALLED = (
    "EMI Analyzer is not installed on this computer, or it is somewhere this plugin does not "
    "look.\n\n"
    "Install it, start it once, and press Rescan. It needs Docker, which the install guide "
    "covers.\n\n"
    f"Already installed somewhere unusual? Set {APP_ENV} to the application. Running the "
    f"server from source? Set {URL_ENV} to its address, for example http://127.0.0.1:7465."
)


class AppUnavailable(RuntimeError):
    """The desktop app is not installed, or it did not come up."""


@dataclass
class Endpoint:
    """A running app: where it is, and what it says about itself."""

    url: str
    version: str = ""
    data_dir: str = ""

    @property
    def api(self) -> str:
        return self.url.rstrip("/") + "/api"


def config_dir() -> Path:
    """The folder Go's os.UserConfigDir returns, which is where the app writes the file."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))


def endpoint_file() -> Path:
    return config_dir() / "emi-analyzer" / "endpoint.json"


def _read_endpoint() -> Optional[Endpoint]:
    try:
        data = json.loads(endpoint_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = data.get("url")
    if not isinstance(url, str) or not url.startswith("http://127.0.0.1"):
        # Only ever a loopback address: this file is the one thing that decides where a board
        # is sent, and a file is easier to write than a process is to run.
        return None
    return Endpoint(url=url, version=str(data.get("version") or ""), data_dir=str(data.get("data_dir") or ""))


def probe(url: str, timeout: float = PROBE_TIMEOUT_S) -> Optional[Endpoint]:
    """Ask a URL whether the app is there. None when nothing answers."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/local/status", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    if not isinstance(data, dict):
        return None
    return Endpoint(url=url.rstrip("/"), version=str(data.get("version") or ""), data_dir=str(data.get("data_dir") or ""))


def running() -> Optional[Endpoint]:
    """The app, if one is up. Honours EMI_ANALYZER_URL before looking anything up."""
    override = os.environ.get(URL_ENV)
    if override:
        return probe(override)
    found = _read_endpoint()
    return probe(found.url) if found else None


# ---- starting it ----


def app_candidates() -> List[List[str]]:
    """Commands that start the installed app, best first."""
    out: List[List[str]] = []
    override = os.environ.get(APP_ENV)
    if override:
        p = Path(override)
        if sys.platform == "darwin" and p.suffix == ".app":
            out.append(["open", "-a", str(p)])
        else:
            out.append([str(p)])
    if sys.platform == "darwin":
        # By bundle id first: it finds the app wherever the user filed it.
        out.append(["open", "-b", "com.embeddedci.emi-analyzer"])
        for base in (Path("/Applications"), Path.home() / "Applications"):
            bundle = base / "EMI Analyzer.app"
            if bundle.is_dir():
                out.append(["open", "-a", str(bundle)])
    elif sys.platform == "win32":
        for base in (
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
            Path(os.environ.get("PROGRAMFILES", "")),
            Path(os.environ.get("PROGRAMFILES(X86)", "")),
        ):
            exe = base / "EMI Analyzer" / "EMI Analyzer.exe"
            if exe.is_file():
                out.append([str(exe)])
    else:
        for name in ("emi-analyzer", "EMI Analyzer"):
            found = shutil.which(name)
            if found:
                out.append([found])
        for p in (
            Path("/opt/EMI Analyzer/emi-analyzer"),
            Path.home() / "Applications" / "EMI Analyzer.AppImage",
            Path.home() / ".local" / "bin" / "emi-analyzer",
        ):
            if p.is_file() and os.access(p, os.X_OK):
                out.append([str(p)])
    return out


def _spawn(command: List[str]) -> bool:
    """Start the app and let go of it. It must outlive KiCad's plugin process."""
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # DETACHED_PROCESS: no console window, and not killed with this one.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    elif command[0] != "open":
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(command, **kwargs)
        return True
    except OSError:
        return False


def start(timeout: float = START_TIMEOUT_S, on_wait: Optional[Callable[[float], None]] = None) -> Endpoint:
    """Start the installed app and wait for it to answer.

    One way of starting it at a time, each given its own chance. Starting them all at once
    would be faster and wrong: the ways of naming the app can resolve to different copies of
    it, and then the user has two apps, two data folders and two worker containers.

    ``on_wait`` is called with the seconds waited so far, so a caller can keep a window
    honest about what it is doing.
    """
    began = time.monotonic()
    deadline = began + timeout
    candidates = app_candidates()
    started = False

    for i, command in enumerate(candidates):
        if not _spawn(command):
            continue
        started = True
        last = i == len(candidates) - 1
        until = deadline if last else min(deadline, time.monotonic() + PER_CANDIDATE_S)
        while time.monotonic() < until:
            app = running()
            if app:
                return app
            if on_wait:
                on_wait(time.monotonic() - began)
            time.sleep(0.5)
        if time.monotonic() >= deadline:
            break

    if not started:
        raise AppUnavailable(NOT_INSTALLED)
    raise AppUnavailable(
        f"EMI Analyzer was started but did not answer within {timeout:.0f} seconds.\n\n"
        "Open it yourself, wait until it shows your boards, and press Rescan."
    )


def attach_or_start(
    timeout: float = START_TIMEOUT_S, on_wait: Optional[Callable[[float], None]] = None
) -> Endpoint:
    """The running app, starting it first if it is not up."""
    app = running()
    if app:
        return app
    if os.environ.get(URL_ENV):
        raise AppUnavailable(
            f"Nothing answered at {os.environ[URL_ENV]} ({URL_ENV}). Start it, or unset the "
            "variable to use the installed app."
        )
    return start(timeout=timeout, on_wait=on_wait)
