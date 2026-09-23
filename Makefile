# EMI Analyzer — build, test, and the development stacks.
#
#   make local / desktop      the local app: one binary, or the desktop installer around it
#   make up / smoke           a Postgres + MinIO stack that exercises the Postgres store and
#                             S3 presigned URLs, the other Store and Blob the emi package ships

COMPOSE := docker compose -f deploy/docker-compose.yaml
ENVFILE := deploy/.env

# Board the smoke test uploads. Override to try a real design:
#   make smoke SMOKE_BOARD=/fixtures/<name>.kicad_pcb   (after dropping it in worker/tests/fixtures)
SMOKE_BOARD ?= /fixtures/tiny.kicad_pcb

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "EMI Analyzer"
	@echo
	@echo "The local app (see README.md and CONTRIBUTING.md):"
	@echo "  make local         build bin/emi-local: server + webapp in one binary"
	@echo "  make run-local     build and run it (opens a browser; worker in Docker)"
	@echo "  make smoke-local   end-to-end test against a running emi-local"
	@echo "  make desktop       build the desktop app installer for this machine (needs Rust)"
	@echo "  make desktop-dev   run the desktop app in development mode"
	@echo "  make worker-image  build the worker image as $(WORKER_IMAGE)"
	@echo "  make worker-lock   re-pin the Python packages in the worker image"
	@echo "  make worker-local  run a worker from source against emi-local (EMBEDDEDCI_API_KEY=...)"
	@echo
	@echo "The KiCad plugin (kicad-plugin/), a front end for the app:"
	@echo "  make plugin-test     its Python tests (PLUGIN_PY= a Python with PySide6 + kicad-python)"
	@echo "  make plugin-install  install a (dev) copy of this checkout into KiCad, beside any release"
	@echo "  make pcm-release VERSION=x.y.z  package it for KiCad's Plugin and Content Manager"
	@echo
	@echo "Tests:"
	@echo "  make test          go test + pytest"
	@echo "  make fixtures      regenerate the shared cost-model fixtures"
	@echo "  make driver-fixtures  regenerate the shared driver-spectrum fixtures"
	@echo
	@echo "Postgres store integration stack (postgres + minio + emi-server + worker):"
	@echo "  make up / key / smoke / logs / down / clean"
	@echo

# ---- the local app ----

# Release builds pass VERSION=1.2.3; it names the worker image tag the binary starts.
VERSION ?= dev
WORKER_IMAGE ?= ghcr.io/embeddedci-com/emi-worker:$(if $(filter dev,$(VERSION)),dev,$(VERSION))
GO_LDFLAGS := -s -w -X main.version=$(VERSION)
LOCAL_WEB := server/cmd/emi-local/web

.PHONY: local local-webapp run-local worker-image desktop desktop-dev desktop-sidecar

# The webapp is embedded into the binary, so it is built and copied in first.
local-webapp:
	cd webapp && ([ -d node_modules ] || npm ci) && npm run build:app
	find $(LOCAL_WEB) -mindepth 1 ! -name .gitkeep -exec rm -rf {} +
	cp -R webapp/dist-app/. $(LOCAL_WEB)/

local: local-webapp
	cd server && CGO_ENABLED=0 go build -trimpath -ldflags "$(GO_LDFLAGS)" -o ../bin/emi-local ./cmd/emi-local
	@echo "built bin/emi-local"

# With VERSION=dev the binary starts $(WORKER_IMAGE); `make worker-image` builds exactly that.
run-local: local
	EMI_WORKER_IMAGE=$(WORKER_IMAGE) ./bin/emi-local

worker-image:
	docker build -t $(WORKER_IMAGE) worker

# Rewrites worker/requirements.lock: the worker's dependencies installed fresh in the image's
# own base, then frozen. Run it after changing pyproject.toml, then rebuild and test the image.
WORKER_BASE := $(shell sed -n 's/^FROM \(debian:[^ ]*\) AS base$$/\1/p' worker/Dockerfile)

.PHONY: worker-lock
worker-lock:
	@test -n "$(WORKER_BASE)" || { echo "no base image found in worker/Dockerfile"; exit 1; }
	{ sed -n '/^#/p' worker/requirements.lock; \
	  docker run --rm -v "$$PWD/worker/pyproject.toml:/w/pyproject.toml:ro" $(WORKER_BASE) sh -ec ' \
	    apt-get update -qq >/dev/null; \
	    apt-get install -y -qq --no-install-recommends python3 python3-venv >/dev/null; \
	    python3 -m venv /v; /v/bin/pip install -q --upgrade pip setuptools wheel; \
	    cp -r /w /b; mkdir /b/emi_worker; touch /b/emi_worker/__init__.py; \
	    /v/bin/pip install -q /b; /v/bin/pip install -q --no-deps "gerbonara==$(GERBONARA)"; \
	    /v/bin/pip freeze --all --exclude emi-worker'; \
	} > worker/requirements.lock.new
	mv worker/requirements.lock.new worker/requirements.lock
	@echo "wrote worker/requirements.lock; now: make worker-image, and run the worker tests in it"

# Tauri looks for the sidecar as binaries/emi-local-<rust target triple>.
RUST_TARGET ?= $(shell rustc -vV 2>/dev/null | sed -n 's/^host: //p')
SIDECAR_EXT := $(if $(findstring windows,$(RUST_TARGET)),.exe,)

desktop-sidecar: local-webapp
	@test -n "$(RUST_TARGET)" || { echo "Rust is not installed: https://rustup.rs"; exit 1; }
	mkdir -p desktop/src-tauri/binaries
	cd server && CGO_ENABLED=0 GOOS=$(GOOS) GOARCH=$(GOARCH) go build -trimpath -ldflags "$(GO_LDFLAGS)" \
		-o ../desktop/src-tauri/binaries/emi-local-$(RUST_TARGET)$(SIDECAR_EXT) ./cmd/emi-local

desktop: desktop-sidecar
	cd desktop && ([ -d node_modules ] || npm ci) && npx tauri build $(TAURI_ARGS)

desktop-dev: desktop-sidecar
	cd desktop && ([ -d node_modules ] || npm ci) && EMI_WORKER_IMAGE=$(WORKER_IMAGE) npx tauri dev

# ---- stack ----

.PHONY: up
up: key
	$(COMPOSE) up -d --build
	@echo
	@echo "  server   http://localhost:8090/api/health"
	@echo "  minio    http://localhost:9001  (minioadmin / minioadmin)"
	@echo
	@echo "  next: make smoke"

# The worker key has to exist before the worker container starts, and issuing it needs the
# database — so this brings up just enough of the stack, then runs the server binary in
# one-shot mode.
.PHONY: key
key:
	@if [ -f $(ENVFILE) ] && grep -q '^EMI_WORKER_KEY=eci_' $(ENVFILE); then \
		echo "worker key already present in $(ENVFILE)"; \
	else \
		echo "issuing a worker key..."; \
		$(COMPOSE) up -d --build postgres minio >/dev/null; \
		$(COMPOSE) run --rm --no-deps --entrypoint /emi-server server \
			-issue-key -name "local worker key" > $(ENVFILE).tmp 2>/dev/null || \
			{ echo "failed to issue key; try: make logs-server"; rm -f $(ENVFILE).tmp; exit 1; }; \
		printf 'EMI_WORKER_KEY=%s\n' "$$(tail -n1 $(ENVFILE).tmp | tr -d '\r\n')" > $(ENVFILE); \
		rm -f $(ENVFILE).tmp; \
		echo "wrote $(ENVFILE)"; \
	fi

# The same end-to-end test, against emi-local on this machine instead of the compose stack.
.PHONY: smoke-local
smoke-local:
	EMI_API=http://127.0.0.1:7465/api EMI_SMOKE_BOARD=worker/tests/fixtures/tiny.kicad_pcb python3 deploy/smoke.py

.PHONY: smoke
smoke:
	@$(COMPOSE) run --rm --no-deps --entrypoint python3 \
		-v $$(pwd)/deploy/smoke.py:/smoke.py:ro \
		-v $$(pwd)/worker/tests/fixtures:/fixtures:ro \
		-e EMI_API=http://server:8090/api \
		-e EMI_SMOKE_BOARD=$(SMOKE_BOARD) \
		worker /smoke.py

.PHONY: logs logs-worker logs-server
logs:
	$(COMPOSE) logs -f
logs-worker:
	$(COMPOSE) logs -f worker
logs-server:
	$(COMPOSE) logs -f server

.PHONY: down clean
down:
	$(COMPOSE) down
clean:
	$(COMPOSE) down -v
	rm -f $(ENVFILE)

.PHONY: restart-worker
restart-worker:
	$(COMPOSE) up -d --build worker

# ---- tests ----

.PHONY: test test-go test-py
test: test-go test-py

test-go:
	cd server && go build ./... && go vet ./... && go test ./...
	@# The emi package is what a hosted server mounts. It must not import the local app's
	@# packages, or SQLite would be linked into the hosted server.
	@! (cd server && go list -deps ./emi | grep -q 'emi-analyzer/server/local\\|modernc.org/sqlite') || \
		{ echo "server/emi depends on the local package or SQLite"; exit 1; }

# gerbonara the way the image installs it: no dependency tree, the version from the lock.
# Without it the Gerber tests skip themselves, and a dev machine reports green having tested
# none of the Gerber ingest.
GERBONARA := $(shell sed -n 's/^gerbonara==//p' worker/requirements.lock)

test-py:
	@cd worker && \
	if [ ! -d .venv ]; then python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]'; fi && \
	.venv/bin/pip install -q --no-deps "gerbonara==$(GERBONARA)" && \
	.venv/bin/python -m pytest -q

# Regenerating rewrites server/emi/testdata/estimate_fixtures.json, which BOTH the Go and
# Python suites assert against. Run the tests afterwards: a change here is a change to
# every estimate a user has ever been shown.
.PHONY: fixtures
fixtures:
	cd worker && python3 scripts/gen_fixtures.py

# Rewrites server/emi/testdata/component_fixtures.json, which the Python and TypeScript
# halves of the component library both assert against.
.PHONY: component-fixtures
component-fixtures:
	cd worker && python3 scripts/gen_component_fixtures.py
	cd worker && .venv/bin/python -m pytest -q tests/test_component_document.py
	cd webapp && npx vitest run src/lib/componentDocument.test.ts

# Rewrites server/emi/testdata/driver_fixtures.json, which the Python and TypeScript halves
# of the driver spectrum both assert against. A change here changes every absolute level the
# tool reports, so run the suites afterwards.
.PHONY: driver-fixtures
driver-fixtures:
	cd worker && python3 scripts/gen_driver_fixtures.py
	cd worker && python3 scripts/gen_driver_doc_fixtures.py
	cd worker && .venv/bin/python -m pytest -q tests/test_driver_spectrum.py tests/test_driver_document.py tests/test_driver_resolve.py tests/test_driver_apply.py
	cd webapp && npx vitest run src/lib/driverSpectrum.test.ts src/lib/driverDocument.test.ts src/lib/driverResolve.test.ts src/lib/driverApply.test.ts
	@$(MAKE) --no-print-directory test

# ---- the KiCad plugin ----
#
# kicad-plugin/ is the plugin folder exactly as KiCad loads it: plain Python, nothing built
# into it. It talks to the app built above, so there is nothing to compile first.

PLUGIN    := kicad-plugin
PLUGIN_PY ?= python3

ifeq ($(shell uname -s),Darwin)
KICAD_PLUGINS ?= $(HOME)/Documents/KiCad/10.0/plugins
else
KICAD_PLUGINS ?= $(HOME)/.local/share/kicad/10.0/plugins
endif

.PHONY: plugin-test plugin-install plugin-uninstall plugin-icons
plugin-test:
	cd $(PLUGIN) && $(PLUGIN_PY) -m pytest -q tests

# A development copy beside any released install: its own identifier (".dev"), its own
# window and its own settings. Restart KiCad (or Preferences -> Plugins -> Reload) after the
# first install; after that, changes apply on the next press.
plugin-install:
	$(PLUGIN_PY) $(PLUGIN)/scripts/dev_install.py --plugins "$(KICAD_PLUGINS)"

plugin-uninstall:
	$(PLUGIN_PY) $(PLUGIN)/scripts/dev_install.py --plugins "$(KICAD_PLUGINS)" --uninstall

# The committed toolbar icons, redrawn (needs Pillow).
plugin-icons:
	$(PLUGIN_PY) $(PLUGIN)/scripts/make_icons.py

# ---- the Plugin and Content Manager repository ----
#
# The index PCM reads is its own public repo, shared with the other EmbeddedCI plugins:
# repository.json, packages.json and resources.zip on its main branch. The plugin's source
# and the archive stay here; this target builds the archive into dist/pcm/ and updates a
# checkout of that repo. Publishing is attaching the archive to a release here and
# committing that checkout.
#
# Users add https://raw.githubusercontent.com/$(PCM_GITHUB)/main/repository.json under
# Plugin and Content Manager -> Manage repositories.

PCM_GITHUB  ?= embeddedci-com/kicad-plugins
PCM_REPO    ?= ../kicad-plugins
PCM_STATUS  ?= testing
PCM_TAG      = kicad-plugin-v$(VERSION)
PCM_ZIP      = emi-analyzer-kicad-plugin-$(VERSION).zip
PCM_RAW_URL ?= https://raw.githubusercontent.com/$(PCM_GITHUB)/main
PCM_DL_URL  ?= https://github.com/embeddedci-com/emi-analyzer/releases/download/$(PCM_TAG)/$(PCM_ZIP)

.PHONY: pcm-release
pcm-release:
	@case "$(VERSION)" in [0-9]*.[0-9]*.[0-9]*) ;; *) \
	  echo "usage: make pcm-release VERSION=0.1.0 [PCM_STATUS=stable] [PCM_REPO=../kicad-plugins]"; \
	  echo "  (VERSION defaults to dev, which is not a version to release)"; exit 1;; esac
	@$(MAKE) --no-print-directory plugin-test
	$(PLUGIN_PY) $(PLUGIN)/scripts/pcm_release.py --version $(VERSION) --status $(PCM_STATUS) \
	  --repo "$(PCM_REPO)" --download-url "$(PCM_DL_URL)" --repo-url "$(PCM_RAW_URL)"
	@echo
	@echo "next:"
	@echo "  gh release create $(PCM_TAG) dist/pcm/$(PCM_ZIP) --title 'KiCad plugin $(VERSION)'"
	@echo "  cd $(PCM_REPO) && git add -A && git commit -m 'emi-analyzer $(VERSION)' && git push"

# ---- a worker from source ----
#
# For working on the worker. Start the app without its own worker and issue a key first:
#   ./bin/emi-local -worker none
#   ./bin/emi-local -issue-key
#   make worker-local EMBEDDEDCI_API_KEY=eci_...
EMBEDDEDCI_URL ?= http://127.0.0.1:7465
.PHONY: worker-local
worker-local:
	@test -n "$(EMBEDDEDCI_API_KEY)" || { echo "set EMBEDDEDCI_API_KEY"; exit 1; }
	cd worker && \
	if [ ! -d .venv ]; then python3 -m venv .venv && .venv/bin/pip install -q -e . && .venv/bin/pip install -q --no-deps "gerbonara==$(GERBONARA)"; fi && \
	EMBEDDEDCI_URL="$(EMBEDDEDCI_URL)" EMBEDDEDCI_API_KEY="$(EMBEDDEDCI_API_KEY)" \
	.venv/bin/python -m emi_worker
