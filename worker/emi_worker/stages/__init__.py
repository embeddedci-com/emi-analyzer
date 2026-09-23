"""Run stages.

A stage takes a :class:`StageContext` and returns a :class:`StageResult`, or raises. The
runner owns claiming, completion and error reporting; a stage owns only the work. Keeping
that split means a new kind of run slots in without touching the agent protocol at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .. import scratch
from ..client import Client, RunToken


class StageError(Exception):
    """An expected, explainable failure.

    The message is shown to the user verbatim, so write it for them: "board has no copper
    layers" rather than "IndexError: list index out of range".
    """


class Stopped(Exception):
    """Raised when a stop was requested and the stage unwound cleanly."""


@dataclass
class StageContext:
    client: Client
    token: RunToken
    run: dict
    workdir: str
    cores: int
    max_cells: int
    should_stop: Callable[[], bool]

    def __post_init__(self) -> None:
        # The id becomes a directory that is later removed with rmtree, so it is checked
        # before it touches a path.
        self.rundir = scratch.run_dir(self.workdir, self.token.run_id)

    @property
    def params(self) -> dict:
        return self.run.get("params") or {}

    def check_stop(self) -> None:
        """Call between units of work. A solve that ignores this cannot be cancelled."""
        if self.should_stop():
            raise Stopped()

    def progress(self, stage: str, pct: float, message: str = "", **fields) -> None:
        self.check_stop()
        self.client.progress(self.token, stage=stage, pct=pct, message=message, **fields)


@dataclass
class StageResult:
    summary: dict = field(default_factory=dict)
    artifacts: list[dict] = field(default_factory=list)
    estimate: dict | None = None
    board: dict | None = None


from .ingest import run_ingest  # noqa: E402
from .cable import run_cable  # noqa: E402
from .compliance import run_compliance  # noqa: E402
from .solve import run_solve  # noqa: E402
from .transient import run_transient  # noqa: E402

#: Dispatch table, keyed by the run kinds the server knows about.
STAGES: dict[str, Callable[[StageContext], StageResult]] = {
    "ingest": run_ingest,
    "solve": run_solve,
    "cable": run_cable,
    "compliance": run_compliance,
    "transient": run_transient,
}

__all__ = [
    "STAGES",
    "StageContext",
    "StageError",
    "StageResult",
    "Stopped",
    "run_ingest",
    "run_cable",
    "run_compliance",
    "run_solve",
    "run_transient",
]
