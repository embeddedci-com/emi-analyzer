# Contributing

Bug reports and pull requests are welcome at
<https://github.com/embeddedci-com/emi-analyzer>. For a bug, include the worker log (click the
worker badge in the app) and, if you can share it, the board file.

## Repository layout

```
server/        Go module
  emi/           the control plane: API, run lifecycle, worker protocol, cost estimate.
                 Knows nothing about where it runs; storage and keys come in through the
                 interfaces in emi/deps.go.
  local/         SQLite store, file storage with signed URLs, worker keys — for the local app
  cmd/emi-local  the local app: one binary with the webapp embedded; starts the worker
  cmd/emi-server a second host on Postgres + S3-compatible storage, for the integration stack
webapp/        React + Mantine
  src/           the analyzer: pages, board renderer, API client
  app/           the local app's shell around it
worker/        Python: ingest, rule checks, ngspice, nec2c, openEMS. Published as a Docker image.
desktop/       Tauri 2: a window around emi-local, which it runs as a sidecar
kicad-plugin/  the KiCad plugin: a front end that hands the app the board open in pcbnew
deploy/        Postgres + MinIO integration stack, and the end-to-end smoke test
docs/          documentation
```

## Build and run from source

You need Go 1.26, Node.js 22 and Docker. For the desktop app you also need
[Rust](https://rustup.rs) and the [Tauri prerequisites](https://v2.tauri.app/start/prerequisites/)
for your platform.

```bash
git clone https://github.com/embeddedci-com/emi-analyzer.git && cd emi-analyzer
```

Build the worker image. A development build of the app starts the `:dev` tag, which is what this
builds:

```bash
make worker-image
```

Build and run the local app. It opens in your browser:

```bash
make run-local
```

Or the desktop app, in development mode:

```bash
make desktop-dev
```

| Target | |
|---|---|
| `make local` | `bin/emi-local`, with the webapp embedded |
| `make desktop` | the installer for this machine, in `desktop/src-tauri/target/release/bundle/` |
| `make worker-local EMBEDDEDCI_API_KEY=eci_...` | the worker from source, against `emi-local -worker none` |
| `make smoke-local` | the end-to-end test against a running `emi-local` |

### Working on the webapp

Run `emi-local` and the Vite dev server side by side; Vite proxies `/api` and `/blob` to it.

```bash
./bin/emi-local -open=false
```

```bash
cd webapp && npm ci && npm run dev:app
```

Then open <http://localhost:5175>.

### Experimental features

`full-wave` is off by default; see [docs/known-issues.md](docs/known-issues.md). To work on it:

```bash
./bin/emi-local -experimental full-wave
```

The gate lives in `server/emi/features.go` and is enforced by the server: gated run kinds are
refused at creation and on retry, and never handed to a worker. The webapp reads
`GET /api/emi/features` only to hide what it cannot use.

## Tests

```bash
make test
```

runs the Go tests and the worker's Python tests. The webapp's:

```bash
cd webapp && npm test && npm run typecheck
```

The KiCad plugin's:

```bash
make plugin-test
```

They stand in for KiCad and for the app, so they need neither; the handful that need PySide6
skip themselves. `PLUGIN_PY=` a Python that has PySide6 and kicad-python runs those too.

Tests that need `ngspice`, `nec2c` or openEMS skip where those are not installed. CI runs the
worker tests inside the worker image, so they are covered there
([`.github/workflows/test.yml`](.github/workflows/test.yml)). A few tests also run against real
boards if `EMI_TEST_BOARDS` names a directory of `<board>/<board>.kicad_pcb` files.

Some numbers are pinned in fixtures shared by the Go, Python and TypeScript halves. After changing
the cost model or the driver spectrum, regenerate them and run every suite: `make fixtures`,
`make driver-fixtures`, `make component-fixtures`.

### Postgres integration stack

`server/emi` also ships a Postgres store and S3 presigned URLs. To exercise those end to end —
Postgres, MinIO, `emi-server` and a worker in compose:

```bash
make up
```

```bash
make smoke
```

```bash
make down
```

`make smoke` includes a real openEMS solve when `EMI_EXPERIMENTAL=full-wave` is set, which takes
minutes; otherwise it checks that the solve is refused.

## Releasing

1. Make sure CI is green on `main`.
2. Tag and push:

   ```bash
   git tag v0.1.0 && git push origin v0.1.0
   ```

3. Two workflows run:
   - [`worker-image`](.github/workflows/worker-image.yml) pushes
     `ghcr.io/embeddedci-com/emi-worker:0.1.0` and `:latest`, for amd64 and arm64. Pushes to
     `main` publish `:dev`, `:main` and `:sha-<short>` instead; `:dev` is what a build from
     source starts, so it is the one to keep working.
   - [`release`](.github/workflows/release.yml) builds the installers for macOS (Apple Silicon and
     Intel), Windows and Linux, plus the standalone `emi-local` binaries, and attaches them to a
     **draft** GitHub release.
4. Install the draft's builds on each platform and run through the README's steps.
5. Publish the release.

The KiCad plugin is versioned and released on its own, because it changes far less often than
the app and its users install it through KiCad rather than by downloading anything:

```bash
make pcm-release VERSION=0.1.1
```

after bumping `kicad-plugin/emi_analyzer/__init__.py`. That builds the archive into `dist/pcm/`
and updates a checkout of [embeddedci-com/kicad-plugins](https://github.com/embeddedci-com/kicad-plugins),
the Plugin and Content Manager index every EmbeddedCI plugin is published from; it prints the two
commands that publish it. The plugin needs an app that publishes where it is listening
(`endpoint.json`, 0.2.0 and later), so it cannot be released ahead of one.

A release build starts the worker image tagged with its own version, so the app and its worker
always come from the same commit — which is why the image must exist before the release is
published. The `emi-worker` package on GHCR must be **public**, or the app cannot pull it.

The installers are not code-signed. On macOS they are ad-hoc signed, which is why users see
*Open Anyway* rather than an outright refusal.

## License

By contributing you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
