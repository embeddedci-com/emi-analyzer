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
# On openEMS versions: Debian bookworm ships openems 0.0.35 (a 2019 git snapshot), and
# trixie dropped the package entirely. 0.0.35 is fine for P0/P1 and for first light in P2,
# but Antmicro's gerber2ems pins a much newer commit, so P2 will likely need
# OPENEMS_SOURCE=build. The build path is kept working here so that switch is a flag, not
# a rewrite.
ARG OPENEMS_SOURCE=apt

FROM debian:bookworm-slim AS base

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

# ---------------------------------------------------------------------------
# Solver toolchain, from Debian packages.
# ---------------------------------------------------------------------------
FROM base AS solver-apt
RUN apt-get update && apt-get install -y --no-install-recommends \
        openems \
        gerbv \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Solver toolchain, from source. Slow (tens of minutes) but current.
# Selected with: docker build --build-arg OPENEMS_SOURCE=build
# ---------------------------------------------------------------------------
FROM base AS solver-build
ARG OPENEMS_REF=master
# Leave unset for a native build (defaults to nproc). Set to 1 when building linux/amd64
# under QEMU emulation on an arm64 host (e.g. colima/lima on Apple Silicon) -- parallel
# emulated GCC jobs there are prone to spurious SIGSEGV mid-compile (seen on
# openEMS/Common/engine_interface_base.cpp), which looks like a compile error but isn't.
ARG OPENEMS_NJOBS=
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git \
        libhdf5-dev libvtk9-dev libboost-all-dev libcgal-dev \
        libtinyxml-dev qtbase5-dev libvtk9-qt-dev \
        gerbv \
        python3-setuptools-scm cython3 python3-numpy python3-h5py python3-matplotlib \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --recursive --depth 1 --branch "${OPENEMS_REF}" \
        https://github.com/thliebig/openEMS-Project.git /src/openEMS-Project \
    && cd /src/openEMS-Project \
    && ./update_openEMS.sh /usr/local --python ${OPENEMS_NJOBS:+--njobs="$OPENEMS_NJOBS"} \
    && rm -rf /src

# ---------------------------------------------------------------------------
# No solver at all. Board ingest and the geometric checks are pure Python (numpy,
# mapbox_earcut, gerbonara), and nothing on that path shells out, so this stage needs
# nothing beyond `base`. The result is small enough for a 1 vCPU
# machine, and capabilities detection reports kinds=["ingest"] without being told to.
# Selected with: docker build --build-arg OPENEMS_SOURCE=none
# ---------------------------------------------------------------------------
FROM base AS solver-none

# ---------------------------------------------------------------------------
FROM solver-${OPENEMS_SOURCE} AS final

WORKDIR /app

# Dependencies first, so editing worker source does not re-resolve the wheel set.
COPY pyproject.toml /app/pyproject.toml
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && mkdir -p /app/emi_worker \
    && touch /app/emi_worker/__init__.py \
    && /opt/venv/bin/pip install /app

# gerbonara parses Gerber and Excellon. It is installed without its dependency tree on
# purpose: it declares flask, quart, hypercorn and rtree for a web preview feature we never
# touch, and rtree needs a system library this image does not carry. Verified that the
# parser loads *no* third-party modules at all in our usage — it is pure stdlib once
# imported — so --no-deps is correct here rather than a shortcut.
# >=1.5 rather than a newer pin: gerbonara 1.6 requires Python 3.12, and this image is
# Debian bookworm (Python 3.11) because that is where the openEMS package lives. 1.5 and
# 1.6 expose the same parser API — both were checked against the same boards.
RUN /opt/venv/bin/pip install --no-deps "gerbonara>=1.5"

COPY emi_worker /app/emi_worker
RUN /opt/venv/bin/pip install --no-deps /app

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
# rather than exit, so health is "the process is alive and its toolchain resolves".
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python3 -c "import emi_worker, shutil, sys; sys.exit(0)" || exit 1

ENTRYPOINT ["python3", "-m", "emi_worker"]
