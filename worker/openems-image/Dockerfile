# openEMS, compiled from a pinned upstream commit: the base the worker image is built on.
#
#   ghcr.io/embeddedci-com/emi-openems:<first 12 characters of OPENEMS_REF>
#
# Why it exists: Debian bookworm's openems 0.0.35 skips a lumped element that has only an
# inductance, so every capacitor model was an open circuit
# (docs/verification/solver-and-components.md §3). A current openEMS models it. Compiling one
# takes tens of minutes natively and hours under QEMU, so it is done here, once per openEMS
# commit, by .github/workflows/openems-image.yml on a native runner per architecture. The
# worker image starts FROM the result and compiles nothing.
#
# Changing OPENEMS_REF or this file publishes a new image when it reaches main. The worker
# picks it up only when its OPENEMS_IMAGE digest is moved by hand, so a rebuild here never
# changes a released worker.
#
# Licences: openEMS and nf2ff are GPL-3.0-or-later, CSXCAD LGPL-3.0-or-later. The exact
# source is the upstream commit below; worker/NOTICE says so inside the worker image.

# A commit of thliebig/openEMS-Project, not a branch: with `master`, two builds of the same
# worker commit could get two different solvers. The workflow takes the image tag from here.
ARG OPENEMS_REF=f73bf97906a38ef1693f59f311ce13cc1b531474

# The same Debian as the worker's own base stage (worker/Dockerfile), because the worker
# installs its Python and tools on top of this image. test_openems_image checks the two match.
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251 AS os

ENV DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# The compile. Compilers and -dev packages stay in this stage: it is 4.4 GB, the image made
# from it about 1.4 GB. /usr/local carries everything the build installed, the Python
# bindings' venv (/usr/local/venv) included.
# ---------------------------------------------------------------------------
FROM os AS build
ARG OPENEMS_REF
# Leave unset for a native build (defaults to nproc). Set to 1 when building under QEMU
# emulation (e.g. linux/amd64 on colima/lima on Apple Silicon): parallel emulated GCC jobs
# there are prone to spurious SIGSEGV mid-compile (seen on
# openEMS/Common/engine_interface_base.cpp), which looks like a compile error but isn't.
ARG OPENEMS_NJOBS=
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates build-essential cmake git \
        libhdf5-dev libvtk9-dev libboost-all-dev libcgal-dev \
        libtinyxml-dev qtbase5-dev libvtk9-qt-dev \
        python3 python3-pip python3-venv python3-setuptools-scm cython3 python3-numpy python3-h5py \
        python3-matplotlib \
    && rm -rf /var/lib/apt/lists/*
# `clone --branch` takes no commit, so this fetches the one commit it needs.
RUN git init -q /src/openEMS-Project \
    && cd /src/openEMS-Project \
    && git remote add origin https://github.com/thliebig/openEMS-Project.git \
    && git fetch -q --depth 1 origin "${OPENEMS_REF}" \
    && git checkout -q FETCH_HEAD \
    && git submodule update -q --init --recursive --depth 1 \
    && git submodule status --recursive > /usr/local/share/openems-source.txt \
    && sed -i "1i openEMS-Project ${OPENEMS_REF}" /usr/local/share/openems-source.txt \
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

# ---------------------------------------------------------------------------
# What is published: Debian, the runtime libraries and /usr/local. No compiler.
# ---------------------------------------------------------------------------
FROM os AS runtime
COPY --from=build /usr/local /usr/local
# python3 for the bindings' venv, which ldd does not see.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && xargs -a /usr/local/share/openems-runtime-packages.txt \
        apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && ldconfig
# The worker looks for nf2ff in /usr/bin by default, where the Debian package puts it.
ENV NF2FF_BIN=/usr/local/bin/nf2ff

# The reason this image exists, checked on the image itself: the worker's own probe
# (run.series_rlc_probe_xml), one cell of inductance for one timestep. 0.0.35 prints
# "R or C not specified" and skips it. A build that cannot model it must not be published.
COPY probe-inductor.xml /tmp/probe/probe.xml
RUN cd /tmp/probe \
    && status=0 && { openEMS probe.xml > log.txt 2>&1 || status=$?; } \
    && cat log.txt \
    && if [ "$status" -ne 0 ]; then echo "openEMS exited $status on the probe" >&2; exit 1; fi \
    && if grep -q "R or C not specified" log.txt; then \
         echo "this openEMS skips an inductor-only lumped element" >&2; exit 1; fi \
    && test -x "$NF2FF_BIN" \
    && cd / && rm -rf /tmp/probe

ARG OPENEMS_REF
LABEL org.opencontainers.image.title="openEMS for the EMI Analyzer worker" \
      org.opencontainers.image.description="openEMS, CSXCAD and nf2ff built from thliebig/openEMS-Project ${OPENEMS_REF}" \
      org.opencontainers.image.licenses="GPL-3.0-or-later AND LGPL-3.0-or-later" \
      com.embeddedci.openems.ref="${OPENEMS_REF}"
