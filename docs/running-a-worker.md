# Running a worker yourself

The app starts its own worker in Docker, and most people never need this page. Read on if you
want the analysis to run somewhere else — a bigger machine, a machine without Docker, or the
worker's source code while you change it.

## What a worker is

The worker is the program that does the analysis: it parses the board, runs the checks and the
simulations, and uploads the results. It is the image `ghcr.io/embeddedci-com/emi-worker`, built
from [`worker/`](../worker).

It **dials out**. The app never connects to a worker: the worker opens a WebSocket to the app,
says what it can do, and is handed runs over that connection.

It **keeps nothing**. Its identity is a key in its environment, and its disk is one scratch
directory per run, deleted when the run ends. Stop it, restart it, run two — there is no state to
migrate.

## 1. Start the app without its own worker

```bash
emi-local -worker none
```

## 2. Issue a key for your worker

Run this with the same data folder as the app (the default, unless you passed `-data-dir`):

```bash
emi-local -issue-key
```

It prints a key like `eci_0123456789abcdef_…` once. Keys issued this way survive restarts. The
app's own automatic worker keys are revoked every time the app stops; these are not.

## 3a. Run the worker in Docker, on the same machine

```bash
docker run --rm --name emi-worker \
  --add-host host.docker.internal:host-gateway \
  -e EMBEDDEDCI_URL=http://host.docker.internal:7465 \
  -e EMBEDDEDCI_API_KEY=eci_... \
  -e EMI_WORKER_NAME=my-worker \
  ghcr.io/embeddedci-com/emi-worker:latest
```

Use the tag that matches the app you are feeding, which `emi-local -version` prints: `:0.2.0`
for a release, `:dev` for a build from source. `:latest` is the newest release, which is only
the right tag if that is what you are running: a worker and an app from different versions can
disagree about what a run means.

On Linux with Docker Engine (not Docker Desktop), use the host network instead, because the app
only listens on `127.0.0.1`:

```bash
docker run --rm --name emi-worker --network host \
  -e EMBEDDEDCI_URL=http://127.0.0.1:7465 \
  -e EMBEDDEDCI_API_KEY=eci_... \
  ghcr.io/embeddedci-com/emi-worker:latest
```

Or with compose, from a checkout of this repository:

```bash
cp worker/.env.example worker/.env
```

```bash
docker compose -f worker/docker-compose.worker.yaml up -d
```

## 3b. Run the worker on another machine

The app has no sign-in, so it only listens on this computer. Give the other machine a tunnel to
it instead of opening a port. From the machine running the app:

```bash
ssh -R 7465:127.0.0.1:7465 worker-host
```

Then, on `worker-host`, run the Linux `--network host` command above. The worker reaches the app
through the tunnel at `127.0.0.1:7465`.

## 3c. Run the worker from source

For working on the worker itself. Python 3.11 or newer:

```bash
cd worker && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

```bash
.venv/bin/pip install --no-deps "gerbonara>=1.5"
```

```bash
EMBEDDEDCI_URL=http://127.0.0.1:7465 EMBEDDEDCI_API_KEY=eci_... .venv/bin/python -m emi_worker
```

Board ingest and the rule checks need nothing else. The ESD simulation needs `ngspice`, the cable
budget needs `nec2c`, and full-wave solves need openEMS; a worker advertises only the run kinds
whose tools it finds, and is only handed those.

Component models (capacitors in a solve) also need an openEMS that models a lumped inductor.
Debian's and Ubuntu's `openems` 0.0.35 does not, so a worker on it solves bare copper and says so
in the result. The worker asks the binary before each such solve.

## 3d. Build the image yourself

```bash
docker build -t emi-worker worker
```

This starts from `ghcr.io/embeddedci-com/emi-openems`, openEMS compiled from a pinned upstream
commit, so it compiles nothing. The image is about 1.4 GB. `--build-arg` picks another solver:

| `OPENEMS_SOURCE` | openEMS | Size |
|---|---|---|
| `build` (default) | prebuilt from source, models components | about 1.4 GB |
| `apt` | Debian's 0.0.35, no component models, needs nothing from ghcr.io | about 270 MB |
| `none` | none: ingest, rule checks, ESD and cable budgets only | smaller still |

To try another openEMS commit, build its image first. The compile takes tens of minutes, and
under emulation (an amd64 image on Apple Silicon) add `--build-arg OPENEMS_NJOBS=1`:

```bash
docker build --build-arg OPENEMS_REF=<commit> -t emi-openems:local worker/openems-image
```

```bash
docker build --build-arg OPENEMS_IMAGE=emi-openems:local -t emi-worker worker
```

Either image's build fails if the solver's answer on inductors is not the one its
`OPENEMS_SOURCE` promises.

## Configuration

| Variable | Default | |
|---|---|---|
| `EMBEDDEDCI_URL` | `http://localhost:8090` | The app's address; `/api` is appended. |
| `EMBEDDEDCI_API_KEY` | — | Required. From `emi-local -issue-key`. |
| `EMI_WORKER_NAME` | hostname | Shown in the worker list. |
| `EMI_MAX_CONCURRENT` | `1` | Runs at once. Solves are memory-bound, so more than one is rarely faster. |
| `EMI_POLL_INTERVAL` | `30` | Seconds between polls. Only a fallback: work normally arrives over the WebSocket. |
| `EMI_WORKDIR` | `emi-worker-<uid>` in the temp folder (`/var/lib/emi-worker` in the image) | Scratch space. Meshes and field dumps are large; use a fast disk. At startup the worker removes only the run folders it created there. |
| `EMI_MAX_CELLS` | from RAM | Override the largest solve advertised (72 bytes per cell). |
| `EMI_VERIFY_TLS` | `1` | `0` disables certificate checks, for a self-signed test server only. |
| `EMI_KEEP_SCRATCH` | unset | `1` keeps each run's scratch directory, for debugging a failed run. |
| `EMI_LOG_LEVEL` | `INFO` | `DEBUG` logs every mesh decision and solver line. |

The worker detects its own cores, memory and tools at startup. In a container it reads the
container's limits, not the host's, so a 4 GB container advertises 4 GB and is never handed a run
it cannot fit.

## When it does not work

**The worker exits with a key error.** The key belongs to a different data folder, or was
revoked. Issue a new one with `emi-local -issue-key` against the data folder the app is using.

**It registers but never gets work.** Check what it advertised in the worker log. A worker
without openEMS is not offered solves; a worker whose memory is too small for a solve is skipped
rather than handed a run it would die on. Full-wave solves are also refused entirely unless the
app was started with `-experimental full-wave` — see [known-issues.md](known-issues.md).

**A solve places no capacitors.** The worker log says "openEMS does not model a series lumped
R-L-C": the worker runs openEMS 0.0.35 (an `apt` image or a distribution package). Use the
released image or a default build.

**It cannot connect.** From inside the container, `localhost` is the container, not your
computer. Use `host.docker.internal` (Docker Desktop) or `--network host` (Linux).
