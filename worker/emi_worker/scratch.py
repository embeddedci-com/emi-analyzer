"""The worker's scratch space on disk, and the run ids that name directories in it.

A run id comes from the server and becomes a directory name that is later removed with
rmtree. So it is checked before it touches a path, and the startup sweep only removes
directories this worker marked as its own: pointed at a shared folder by mistake, it must
not empty it.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile

#: What the server issues: 32 hex characters, or a UUID with its hyphens.
_SERVER_RUN_ID = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")

#: Any name that is safe as one path segment. Looser than the server's format, so tests and
#: tools can use readable ids, but never a separator, a dot segment or an empty name.
_PATH_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

#: The file that marks a run directory as this worker's.
MARKER = ".emi-worker-run"


def valid_run_id(run_id: object) -> bool:
    """Whether a run id from the server has the server's format."""
    return isinstance(run_id, str) and bool(_SERVER_RUN_ID.match(run_id.lower()))


def run_dir(workdir: str, run_id: str) -> str:
    """The scratch directory for one run, created and marked. Raises on an unsafe id."""
    if not isinstance(run_id, str) or not _PATH_SAFE.match(run_id):
        raise ValueError(f"refusing a run id that is not a plain name: {run_id!r}")
    path = os.path.join(workdir, run_id)
    os.makedirs(path, mode=0o700, exist_ok=True)
    with open(os.path.join(path, MARKER), "w", encoding="utf-8") as fh:
        fh.write(run_id + "\n")
    return path


def clear_leftovers(workdir: str) -> list[str]:
    """Remove run directories this worker left behind. Returns the names removed.

    Only a directory holding the marker counts. Anything else in the folder, whoever put
    it there, is left alone.
    """
    if not os.path.isdir(workdir):
        return []
    removed = []
    for entry in os.listdir(workdir):
        path = os.path.join(workdir, entry)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        if not os.path.isfile(os.path.join(path, MARKER)):
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(entry)
    return removed


def default_workdir() -> str:
    """A scratch folder of this user's own, rather than one shared name in /tmp.

    A fixed /tmp/emi-worker can be created first by another user of the machine, who then
    owns the folder the worker writes board files into.
    """
    uid = os.getuid() if hasattr(os, "getuid") else os.getpid()
    return os.path.join(tempfile.gettempdir(), f"emi-worker-{uid}")


def prepare_workdir(workdir: str) -> None:
    """Create the scratch folder private to this user, or refuse one somebody else owns."""
    os.makedirs(workdir, mode=0o700, exist_ok=True)
    if not hasattr(os, "getuid"):
        return
    st = os.stat(workdir)
    if st.st_uid != os.getuid():
        raise SystemExit(
            f"the scratch folder {workdir} belongs to another user. "
            "Set EMI_WORKDIR to a folder of your own."
        )
    # Tightened only for the default folder: one the operator chose is theirs to set up.
    if workdir == default_workdir() and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        os.chmod(workdir, 0o700)
