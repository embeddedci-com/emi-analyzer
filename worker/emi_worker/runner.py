"""The worker's run loop.

Responsibilities, in order of importance:

1. Stay connected. A worker that silently stops dialling in looks identical, from the
   user's side, to a queue that never drains.
2. Never claim work it cannot finish. Capabilities are declared up front, but the
   stages re-check at mesh time where the real numbers are known.
3. Always finish a run it started, in one direction or the other. A run left
   ``in_progress`` sits there until the server's sweeper times it out hours later, and
   the user gets no explanation.
4. Keep nothing. A worker is disposable by design: its identity is the API key in its
   environment, its queue position is the server's business, and the only thing it puts
   on disk is one scratch directory per run, deleted when that run ends. Nothing carries
   from one run to the next, or from one process to the next -- which is what makes it
   safe to run the same worker on a droplet, a workstation or a laptop, and to kill it at
   any moment without leaving the server or the disk in a state someone has to clean up.
"""

from __future__ import annotations

import json
import logging
import queue
import random
import shutil
import threading
import time
import traceback
from dataclasses import dataclass

import websockets
from websockets.sync.client import connect as ws_connect

from . import scratch
from .client import Client, RunReassigned, RunToken, ServerError
from .config import Capabilities, Config
from .stages import STAGES, StageContext, StageError, Stopped

log = logging.getLogger(__name__)


@dataclass
class _Job:
    run_id: str
    kind: str
    payload: dict


class Worker:
    def __init__(self, cfg: Config, caps: Capabilities):
        self.cfg = cfg
        self.caps = caps
        self.client = Client(cfg.api, cfg.api_key, verify_tls=cfg.verify_tls)

        self._queue: queue.Queue[_Job] = queue.Queue()
        self._seen: set[str] = set()
        self._seen_lock = threading.Lock()
        self._stop_requested: set[str] = set()
        self._shutdown = threading.Event()

    # ---- lifecycle ----

    def run(self) -> None:
        scratch.prepare_workdir(self.cfg.workdir)
        self._clear_scratch()
        reg = self.client.register(self.cfg.name, self.caps.as_dict())
        log.info(
            "registered as %s (org=%s) cores=%d ram=%.1fGB max_cells=%s kinds=%s",
            self.cfg.name, reg.get("org_id"), self.caps.cores, self.caps.ram_gb,
            f"{self.caps.max_cells:,}", ",".join(self.caps.kinds),
        )
        if "solve" not in self.caps.kinds:
            log.warning("openEMS not found on PATH -- this worker will only take ingest runs")

        threading.Thread(target=self._ws_loop, name="ws", daemon=True).start()
        threading.Thread(target=self._poll_loop, name="poll", daemon=True).start()

        workers = [
            threading.Thread(target=self._work_loop, name=f"run-{i}", daemon=True)
            for i in range(max(1, self.cfg.max_concurrent))
        ]
        for t in workers:
            t.start()

        try:
            while not self._shutdown.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown.set()
            self.client.close()

    def stop(self) -> None:
        self._shutdown.set()

    def _clear_scratch(self) -> None:
        """Remove the run directories a previous process of this worker left behind.

        A process that was killed mid-solve leaves a run directory holding a mesh and
        field dumps -- gigabytes, for a large run. Nothing will ever read them again: the
        run is the server's to reassign, and a reassigned run is downloaded fresh. So the
        only thing keeping them would do is fill the disk of the machine this happens to
        be running on.

        Only directories the worker marked as its own are removed. Pointed at a folder
        that holds anything else, it leaves that alone. Two workers on one machine still
        need two folders, or each start would remove the other's runs in flight.
        """
        if self.cfg.keep_scratch:
            return
        removed = scratch.clear_leftovers(self.cfg.workdir)
        if removed:
            log.info("cleared %d leftover run director%s in %s",
                     len(removed), "y" if len(removed) == 1 else "ies", self.cfg.workdir)

    def _drop_scratch(self, ctx: StageContext) -> None:
        """Delete one run's scratch directory, however that run ended.

        Called from a finally, so it also covers the paths that return early -- a stop, a
        reassignment, a stage that raised. Results are already in object storage by this
        point: a stage uploads them itself and hands back names and sizes, never paths.
        """
        if self.cfg.keep_scratch:
            log.info("keeping scratch for run %s at %s", ctx.token.run_id, ctx.rundir)
            return
        shutil.rmtree(ctx.rundir, ignore_errors=True)

    # ---- discovery ----

    def _ws_loop(self) -> None:
        """Dial out to the server and stay connected.

        The worker always initiates, which is what lets it run on a laptop behind NAT or
        an office box with no inbound firewall rule.
        """
        backoff = self.cfg.reconnect_min
        while not self._shutdown.is_set():
            try:
                with ws_connect(
                    self.cfg.ws_url,
                    additional_headers={"Authorization": f"Bearer {self.cfg.api_key}"},
                    open_timeout=20,
                    close_timeout=5,
                    max_size=1 << 20,
                ) as ws:
                    log.info("connected to %s", self.cfg.ws_url)
                    backoff = self.cfg.reconnect_min
                    ws.send(json.dumps({"type": "hello", "capabilities": self.caps.as_dict()}))
                    for raw in ws:
                        if self._shutdown.is_set():
                            break
                        self._on_ws_message(raw)
            except Exception as exc:  # noqa: BLE001 -- reconnect on anything
                if self._shutdown.is_set():
                    return
                log.warning("websocket dropped (%s); reconnecting in %.0fs", exc, backoff)

            if self._shutdown.wait(backoff):
                return
            # Exponential backoff with jitter, so a server restart does not bring every
            # worker back in the same millisecond.
            backoff = min(self.cfg.reconnect_max, backoff * 2) * random.uniform(0.8, 1.2)

    def _on_ws_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        kind = msg.get("type")
        if kind == "new_run":
            self._enqueue(msg.get("run") or {})
        elif kind == "stop_run":
            run_id = msg.get("run_id")
            if run_id:
                log.info("stop requested for run %s", run_id)
                self._stop_requested.add(run_id)

    def _poll_loop(self) -> None:
        """REST fallback for the WebSocket push.

        Push is an optimisation, never the delivery guarantee: a run created while this
        worker was reconnecting arrives here instead.
        """
        while not self._shutdown.wait(self.cfg.poll_interval):
            try:
                for run in self.client.list_runs():
                    self._enqueue(run)
            except Exception as exc:  # noqa: BLE001
                log.debug("poll failed: %s", exc)

    def _enqueue(self, run: dict) -> None:
        run_id = run.get("id")
        kind = run.get("kind")
        if not run_id or not kind:
            return
        # The id names a scratch directory, so one that is not in the server's format is
        # refused before it reaches a path.
        if not scratch.valid_run_id(run_id):
            log.warning("ignoring a run with a malformed id: %r", run_id)
            return
        if kind not in self.caps.kinds:
            return
        # De-duplicate: the same run legitimately arrives twice when a push and a poll
        # race, and claiming it twice would mean two threads solving the same thing.
        with self._seen_lock:
            if run_id in self._seen:
                return
            self._seen.add(run_id)
        self._queue.put(_Job(run_id=run_id, kind=kind, payload=run))

    # ---- execution ----

    def _work_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._execute(job)
            except Exception:  # noqa: BLE001 -- a crash here must not kill the thread
                log.error("run %s crashed:\n%s", job.run_id, traceback.format_exc())
            finally:
                with self._seen_lock:
                    self._seen.discard(job.run_id)

    def _execute(self, job: _Job) -> None:
        started = time.monotonic()
        try:
            tok = self.client.mint_run_token(job.run_id)
        except RunReassigned:
            log.info("run %s already taken by another worker", job.run_id)
            return
        except ServerError as exc:
            log.warning("could not take run %s: %s", job.run_id, exc)
            return

        log.info("claimed run %s (%s)", job.run_id, job.kind)
        try:
            self.client.claim(tok)
        except RunReassigned:
            return

        ctx = StageContext(
            client=self.client,
            token=tok,
            run=job.payload,
            workdir=self.cfg.workdir,
            cores=self.caps.cores,
            max_cells=self.caps.max_cells,
            should_stop=lambda: job.run_id in self._stop_requested or self._shutdown.is_set(),
        )

        stage = STAGES.get(job.kind)
        if stage is None:
            self._fail(tok, f"worker does not implement run kind {job.kind!r}")
            return

        try:
            result = stage(ctx)
        except Stopped:
            log.info("run %s stopped on request", job.run_id)
            self._fail(tok, "stopped on request")
            return
        except StageError as exc:
            # An expected, explainable failure -- a board that will not parse, a mesh that
            # does not fit. The user gets the message verbatim, so it is written for them.
            log.warning("run %s failed: %s", job.run_id, exc)
            self._fail(tok, str(exc))
            return
        except RunReassigned:
            log.info("run %s was reassigned mid-flight; abandoning", job.run_id)
            return
        except Exception as exc:  # noqa: BLE001
            log.error("run %s errored:\n%s", job.run_id, traceback.format_exc())
            self._fail(tok, f"internal worker error: {exc}")
            return
        finally:
            self._stop_requested.discard(job.run_id)
            self._drop_scratch(ctx)

        elapsed = time.monotonic() - started
        summary = dict(result.summary)
        summary["worker"] = self.cfg.name
        summary["elapsed_seconds"] = round(elapsed, 2)

        try:
            self.client.complete(
                tok, "done",
                summary=summary,
                artifacts=result.artifacts,
                estimate=result.estimate,
                board=result.board,
            )
            log.info("run %s done in %.1fs", job.run_id, elapsed)
        except ServerError as exc:
            log.error("could not record completion of %s: %s", job.run_id, exc)

    def _fail(self, tok: RunToken, message: str) -> None:
        try:
            self.client.complete(tok, "failed", error=message)
        except ServerError as exc:
            # Nothing more we can do; the server's sweeper will time the run out.
            log.error("could not record failure of %s: %s", tok.run_id, exc)
