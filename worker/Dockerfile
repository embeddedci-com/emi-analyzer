# EMI worker image.
#
# Bundles the solver toolchain so that `docker run ghcr.io/embeddedci-com/emi-worker` on any
# machine -- a laptop, a workstation, a server -- becomes a worker with one command and no host
# setup. The EMI Analyzer desktop app starts this image by itself.
#
# OPENEMS_SOURCE=none builds an ingest-only worker: no solver, no solver toolchain, a
# fraction of the size. The worker detects the missing binary and advertises
# kinds=["ingest"], so the server never hands it a solve. That is what makes it small enough to run
# on a modest machine, with a solver-capable worker elsewhere.
#
# On openEMS versions: Debian bookworm ships openems 0.0.35 (a 2019 git snapshot), which skips
# a lumped element that has only an inductance, so it cannot model a capacitor
# (docs/verification/solver-and-components.md §3). trixie dropped the package entirely. So the
# released image is built on ghcr.io/embeddedci-com/emi-openems, openEMS compiled from a pinned
# upstream commit by openems-image/Dockerfile. That compile takes tens of minutes natively and
# far longer under QEMU, so it happens once per openEMS commit in its own workflow, and neither
# a worker build nor CI's test build compiles anything.
#
#   OPENEMS_SOURCE=build   (default) FROM the prebuilt openEMS image, OPENEMS_IMAGE below
#   OPENEMS_SOURCE=apt     Debian's 0.0.35: no component models. A fallback that needs no
#                          image from ghcr.io
#   OPENEMS_SOURCE=none    ingest-only, as above
#
# To try another openEMS, build openems-image/Dockerfile with a different OPENEMS_REF and pass
# the result as --build-arg OPENEMS_IMAGE=<tag>.
#
# Licences: the worker is Apache-2.0; openEMS (GPL-3+), nec2c (GPL-2+) and the rest of the
# image keep their own. NOTICE, copied into the image below, lists them and says where their
# source is.
ARG OPENEMS_SOURCE=build

# The tag names the openEMS commit; test_openems_image checks it matches
# openems-image/Dockerfile. Append the digest the openEMS workflow prints (tag@sha256:...):
# with it, a rebuild of the openEMS image changes no worker until this line is moved, and the
# worker workflow refuses to build a release without one.
ARG OPENEMS_IMAGE=ghcr.io/embeddedci-com/emi-openems:f73bf97906a3

# Pinned by digest so a rebuild of an old tag gets the Debian it was tested on. The tag stays
# in the reference for readers; Dependabot bumps the digest. openems-image/Dockerfile uses the
# same one, so every OPENEMS_SOURCE gets the same Debian.
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251 AS os-apt
FROM os-apt AS os-none
# Debian, openEMS's runtime libraries and openEMS in /usr/local. BuildKit fetches it only
# when OPENEMS_SOURCE=build asks for this stage.
FROM ${OPENEMS_IMAGE} AS os-build

FROM os-${OPENEMS_SOURCE} AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ngspice and nec2c are in the base stage, not a solver stage, for the same reason: both are
# seconds of work rather than hours, so an ingest-only worker runs them too.
#
#   ngspice  Debian bookworm ships 39.3 (BSD-3-Clause). ESD transient simulation.
#   nec2c    1.3-4, a C translation of NEC-2 (GPL-2). The antenna solver for cable
#            radiation. Run as a subprocess, never linked, so its licence stays its own --
#            which is also why PyNEC is not used despite being the obvious binding.
#
# The worker advertises a run kind only when the binary it needs resolves, so an image
# without one is never handed that kind of work.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        ca-certificates \
        ngspice \
        nec2c \
    && rm -rf /var/lib/apt/lists/*

# What this image must be able to run. The health check reads it, and so does a build-time
# check below, so an image that lost a tool fails its build instead of quietly advertising
# fewer run kinds. Each solver stage adds its own.
ENV EMI_EXPECTED_TOOLS="ngspice nec2c"

# ---------------------------------------------------------------------------
# Solver toolchain, from Debian packages: openEMS 0.0.35, no inductors.
# Selected with: docker build --build-arg OPENEMS_SOURCE=apt
# ---------------------------------------------------------------------------
FROM base AS solver-apt
RUN apt-get update && apt-get install -y --no-install-recommends \
        openems \
    && rm -rf /var/lib/apt/lists/*
ENV EMI_EXPECTED_TOOLS="${EMI_EXPECTED_TOOLS} openEMS nf2ff" \
    EMI_EXPECT_SERIES_RLC=0

# ---------------------------------------------------------------------------
# Solver toolchain, compiled from source: already in the base, from OPENEMS_IMAGE, with the
# list of Debian packages it needs installed.
# ---------------------------------------------------------------------------
FROM base AS solver-build
# The worker looks for nf2ff in /usr/bin by default, where the Debian package puts it. The
# openEMS image sets this too; it is repeated so the worker does not depend on that.
ENV NF2FF_BIN=/usr/local/bin/nf2ff \
    EMI_EXPECTED_TOOLS="${EMI_EXPECTED_TOOLS} openEMS nf2ff" \
    EMI_EXPECT_SERIES_RLC=1

# ---------------------------------------------------------------------------
# No solver at all. Board ingest and the geometric checks are pure Python (numpy,
# mapbox_earcut, gerbonara); ngspice and nec2c are already in `base`, so this stage needs
# nothing beyond `base`. The result is small enough for a 1 vCPU
# machine, and capabilities detection reports kinds=["ingest"] without being told to.
# Selected with: docker build --build-arg OPENEMS_SOURCE=none
# ---------------------------------------------------------------------------
FROM base AS solver-none

# ---------------------------------------------------------------------------
FROM solver-${OPENEMS_SOURCE} AS final

WORKDIR /app

# Dependencies first, so editing worker source does not re-resolve the wheel set.
# requirements.lock pins every version, pip and setuptools included; pyproject.toml keeps
# ranges for anyone installing the worker outside the image. No build isolation, or pip
# would fetch whatever setuptools is newest just to build the worker.
COPY pyproject.toml requirements.lock /app/
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install -c /app/requirements.lock --upgrade pip setuptools wheel \
    && mkdir -p /app/emi_worker \
    && touch /app/emi_worker/__init__.py \
    && /opt/venv/bin/pip install --no-build-isolation -c /app/requirements.lock /app

# gerbonara parses Gerber and Excellon. It is installed without its dependency tree on
# purpose: it declares flask, quart, hypercorn and rtree for a web preview feature we never
# touch, and rtree needs a system library this image does not carry. Verified that the
# parser loads *no* third-party modules at all in our usage — it is pure stdlib once
# imported — so --no-deps is correct here rather than a shortcut.
# The version comes from requirements.lock. It is 1.5, not 1.6: gerbonara 1.6 requires
# Python 3.12, and this image is Debian bookworm (Python 3.11) because that is where the
# openEMS package lives. 1.5 and 1.6 expose the same parser API; both were checked against
# the same boards.
RUN /opt/venv/bin/pip install --no-deps -c /app/requirements.lock gerbonara

COPY emi_worker /app/emi_worker
RUN /opt/venv/bin/pip install --no-deps --no-build-isolation /app

# What is in the image and under which licence: NOTICE, plus the exact Debian and Python
# package versions it points to, and the openEMS commits when it was compiled, so the
# matching source can be found.
COPY NOTICE /usr/share/doc/emi-worker/NOTICE
RUN dpkg-query -W -f='${Package} ${Version}\n' > /usr/share/doc/emi-worker/debian-packages.txt \
    && if [ -f /usr/local/share/openems-source.txt ]; then \
         cp /usr/local/share/openems-source.txt /usr/share/doc/emi-worker/openems-source.txt; fi \
    && /opt/venv/bin/pip list --format=freeze > /usr/share/doc/emi-worker/python-packages.txt

ENV PATH="/opt/venv/bin:${PATH}" \
    EMI_WORKDIR=/var/lib/emi-worker

# The worker parses untrusted uploads -- somebody else's PCB files -- so it does not run as
# root. It needs no privileges: it dials out, reads from presigned URLs and writes to a
# scratch directory.
RUN useradd --system --create-home --uid 10001 emi \
    && mkdir -p /var/lib/emi-worker \
    && chown -R emi:emi /var/lib/emi-worker /app
USER emi

# A worker that cannot reach the server is useless, but it is *supposed* to keep retrying
# rather than exit, so health is "the worker imports and every tool this image promises
# resolves". It used to import only the package and resolve nothing, so it could not fail.
# The same check runs once here at build time, so an image missing a tool never ships.
ENV EMI_HEALTHCHECK="import os, shutil, sys, gerbonara, emi_worker.runner; missing = [t for t in os.environ['EMI_EXPECTED_TOOLS'].split() if not shutil.which(t)]; missing and print('missing tools:', *missing); sys.exit(1 if missing else 0)"
RUN python3 -c "$EMI_HEALTHCHECK"
# And whether the solver models an inductor, asked the way a solve asks it
# (run.solver_has_series_rlc, one timestep). Components are placed only when it says yes, so
# an image whose answer is not the one its OPENEMS_SOURCE promises fails here: a build that
# says no would quietly drop every capacitor, and 0.0.35 saying yes would put open circuits
# back. Not in the health check, which runs every 30 s.
RUN if [ -n "${EMI_EXPECT_SERIES_RLC:-}" ]; then \
        python3 -c "import os, sys; from emi_worker.openems import run; got = run.solver_has_series_rlc(); want = os.environ['EMI_EXPECT_SERIES_RLC'] == '1'; print('openEMS models a series R-L-C:', got); sys.exit(0 if got == want else 1)"; \
    fi
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python3 -c "$EMI_HEALTHCHECK" || exit 1

# The release this image is, recorded in every run's summary so a shared report can name the
# worker that produced it. Last, so changing it rebuilds nothing above.
ARG EMI_WORKER_VERSION=dev
ENV EMI_WORKER_VERSION=${EMI_WORKER_VERSION}

ENTRYPOINT ["python3", "-m", "emi_worker"]
