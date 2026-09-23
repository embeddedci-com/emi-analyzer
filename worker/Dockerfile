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
# trixie dropped the package entirely. The released image uses 0.0.35. OPENEMS_SOURCE=build
# compiles a current openEMS instead, so moving to a newer one is a flag, not a rewrite.
#
# Licences: the worker is Apache-2.0; openEMS (GPL-3+), nec2c (GPL-2+) and the rest of the
# image keep their own. NOTICE, copied into the image below, lists them and says where their
# source is.
ARG OPENEMS_SOURCE=apt

# Pinned by digest so a rebuild of an old tag gets the Debian it was tested on. The tag stays
# in the reference for readers; Dependabot bumps the digest.
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251 AS base

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
# Solver toolchain, from Debian packages.
# ---------------------------------------------------------------------------
FROM base AS solver-apt
RUN apt-get update && apt-get install -y --no-install-recommends \
        openems \
    && rm -rf /var/lib/apt/lists/*
ENV EMI_EXPECTED_TOOLS="${EMI_EXPECTED_TOOLS} openEMS nf2ff"

# ---------------------------------------------------------------------------
# Solver toolchain, from source. Slow (tens of minutes) but current.
# Selected with: docker build --build-arg OPENEMS_SOURCE=build
#
# Compiled in a stage of its own and copied out, so the compilers and -dev packages stay
# behind: the build stage is 4.4 GB, the image made from it 1.4 GB. /usr/local carries
# everything the build installed, the Python bindings' venv (/usr/local/venv) included.
# ---------------------------------------------------------------------------
FROM base AS openems-build
# A commit of thliebig/openEMS-Project, not a branch: with `master`, two builds of the same
# worker commit could get two different solvers. A branch or tag name also works here.
ARG OPENEMS_REF=f73bf97906a38ef1693f59f311ce13cc1b531474
# Leave unset for a native build (defaults to nproc). Set to 1 when building linux/amd64
# under QEMU emulation on an arm64 host (e.g. colima/lima on Apple Silicon) -- parallel
# emulated GCC jobs there are prone to spurious SIGSEGV mid-compile (seen on
# openEMS/Common/engine_interface_base.cpp), which looks like a compile error but isn't.
ARG OPENEMS_NJOBS=
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git \
        libhdf5-dev libvtk9-dev libboost-all-dev libcgal-dev \
        libtinyxml-dev qtbase5-dev libvtk9-qt-dev \
        python3-setuptools-scm cython3 python3-numpy python3-h5py python3-matplotlib \
    && rm -rf /var/lib/apt/lists/*
# `clone --branch` takes no commit, so this fetches the one commit it needs.
RUN git init -q /src/openEMS-Project \
    && cd /src/openEMS-Project \
    && git remote add origin https://github.com/thliebig/openEMS-Project.git \
    && git fetch -q --depth 1 origin "${OPENEMS_REF}" \
    && git checkout -q FETCH_HEAD \
    && git submodule update -q --init --recursive --depth 1 \
    && ./update_openEMS.sh /usr/local --python ${OPENEMS_NJOBS:+--njobs="$OPENEMS_NJOBS"} \
    && rm -rf /src
# The shared libraries the build links against, as Debian package names, so the runtime
# stage installs exactly those and nothing only the compiler needed. bookworm is half
# merged-/usr, so a library ldd finds under /lib may be registered under /usr/lib.
RUN ldconfig \
    && find /usr/local -type f -exec ldd {} + 2>/dev/null \
        | awk '$2 == "=>" && $3 ~ /^\// && $3 !~ /^\/usr\/local\// {print $3}' | sort -u \
        | while read -r lib; do \
            dpkg -S "$lib" 2>/dev/null || dpkg -S "/usr$lib" 2>/dev/null \
              || dpkg -S "$(readlink -f "$lib")" 2>/dev/null || echo "unowned: $lib" >&2; \
          done \
        | cut -d: -f1 | sort -u > /usr/local/share/openems-runtime-packages.txt \
    && test -s /usr/local/share/openems-runtime-packages.txt \
    && cat /usr/local/share/openems-runtime-packages.txt

FROM base AS solver-build
COPY --from=openems-build /usr/local /usr/local
RUN apt-get update \
    && xargs -a /usr/local/share/openems-runtime-packages.txt \
        apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && ldconfig
# The worker looks for nf2ff in /usr/bin by default, where the Debian package puts it.
ENV NF2FF_BIN=/usr/local/bin/nf2ff \
    EMI_EXPECTED_TOOLS="${EMI_EXPECTED_TOOLS} openEMS nf2ff"

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
# package versions it points to, so the matching source can be found.
COPY NOTICE /usr/share/doc/emi-worker/NOTICE
RUN dpkg-query -W -f='${Package} ${Version}\n' > /usr/share/doc/emi-worker/debian-packages.txt \
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
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python3 -c "$EMI_HEALTHCHECK" || exit 1

ENTRYPOINT ["python3", "-m", "emi_worker"]
