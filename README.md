# EMI Analyzer

PCB EMI and EMC analysis on your own computer. Open a KiCad board or a zip of Gerber files and
get:

- **geometric EMI/EMC checks** — return paths, plane gaps and stitching, decoupling, impedance,
  ESD protection at connectors, shield grounding, reset lines, switching-regulator layout;
- **ESD discharge simulation** — IEC 61000-4-2 contact discharge into each exposed line, with
  ngspice;
- **a cable budget** — how much common-mode current each connector's cable can carry before it
  radiates past a limit, with nec2c;
- **the FCC Part 15 and CISPR 32 limit tables** the results are compared against.

Your boards and results stay on your computer. Nothing is uploaded anywhere.

The results are for comparing versions of your own board. They are not a pre-compliance test —
see the limitations page in the app, and [known issues](docs/known-issues.md).

> **Full-wave simulation (openEMS) is experimental and turned off.** Long solves are not yet
> numerically stable. See [Experimental features](#experimental-features).

---

## Contents

1. [What you need](#what-you-need)
2. [Step 1 — Install Docker](#step-1--install-docker)
3. [Step 2 — Install EMI Analyzer](#step-2--install-emi-analyzer)
4. [Step 3 — First launch](#step-3--first-launch)
5. [Step 4 — Analyse a board](#step-4--analyse-a-board)
6. [Running without the desktop app](#running-without-the-desktop-app)
7. [Where your data is](#where-your-data-is)
8. [Updating and uninstalling](#updating-and-uninstalling)
9. [Troubleshooting](#troubleshooting)
10. [Experimental features](#experimental-features)
11. [More documentation](#more-documentation)

---

## What you need

| | |
|---|---|
| **Operating system** | macOS 11 or newer (Apple Silicon or Intel), Windows 10 or 11 (64-bit), or 64-bit Linux on x86-64 |
| **Memory** | 8 GB or more |
| **Disk** | About 3 GB free: 1 GB for the worker image, the rest for Docker and your results |
| **Docker** | Docker Desktop on macOS and Windows; Docker Engine or Docker Desktop on Linux |
| **Internet** | Only to download the app and, once per version, the worker image |

EMI Analyzer has two parts. The **app** is a small download that shows your boards and results.
The **worker** does the analysis. It runs inside Docker, so it behaves the same on every
operating system, and the app starts and stops it for you.

---

## Step 1 — Install Docker

Skip this step if `docker ps` already works in a terminal.

### macOS

1. Download **Docker Desktop for Mac** from
   <https://docs.docker.com/desktop/setup/install/mac-install/>. Choose **Apple Silicon** or
   **Intel** to match your Mac (Apple menu → *About This Mac* → *Chip* or *Processor*).
2. Open the downloaded `Docker.dmg` and drag **Docker** into **Applications**.
3. Open **Docker** from Applications. Accept the service agreement and, when asked, allow the
   helper with your password.
4. Wait until the Docker whale in the menu bar stops animating.
5. Check it in Terminal:

   ```bash
   docker ps
   ```

   A header line with nothing under it means Docker is ready.

[OrbStack](https://orbstack.dev) works too, instead of Docker Desktop.

### Windows

1. Open **Terminal as Administrator** (right-click the Start button → *Terminal (Admin)*) and
   install the Windows Subsystem for Linux:

   ```powershell
   wsl --install
   ```

   Restart the computer when it finishes.
2. Download **Docker Desktop for Windows** from
   <https://docs.docker.com/desktop/setup/install/windows-install/> and run the installer. Keep
   **Use WSL 2 instead of Hyper-V** selected.
3. Sign out and back in if the installer asks, then start **Docker Desktop** from the Start menu
   and accept the service agreement.
4. Wait until Docker Desktop shows **Engine running**.
5. Check it in a new terminal:

   ```powershell
   docker ps
   ```

   A header line with nothing under it means Docker is ready.

### Linux (Ubuntu or Debian)

1. Install Docker Engine by following <https://docs.docker.com/engine/install/ubuntu/>, or the
   page for your distribution. On Ubuntu, Docker's install script does it in one step:

   ```bash
   curl -fsSL https://get.docker.com | sh
   ```

2. Let your user run Docker without `sudo`, which the app needs:

   ```bash
   sudo usermod -aG docker $USER
   ```

3. **Log out and log back in** (or restart) so the change takes effect.
4. Check it:

   ```bash
   docker ps
   ```

   A header line with nothing under it means Docker is ready. *Permission denied* means you have
   not logged out and back in since step 2.

---

## Step 2 — Install EMI Analyzer

Go to the [latest release](https://github.com/embeddedci-com/emi-analyzer/releases/latest) and
download the file for your computer:

| Computer | Download |
|---|---|
| Mac with Apple Silicon (M1, M2, M3, …) | `EMI.Analyzer_<version>_aarch64.dmg` |
| Mac with an Intel processor | `EMI.Analyzer_<version>_x64.dmg` |
| Windows | `EMI.Analyzer_<version>_x64-setup.exe` (or the `.msi`) |
| Ubuntu / Debian | `EMI.Analyzer_<version>_amd64.deb` |
| Fedora / RHEL | `EMI.Analyzer-<version>-1.x86_64.rpm` |
| Other Linux | `EMI.Analyzer_<version>_amd64.AppImage` |

The installers are **not code-signed** yet, so your operating system warns you the first time you
open the app. That is expected; here is how to get past it.

### macOS

1. Open the downloaded `.dmg` and drag **EMI Analyzer** into **Applications**.
2. Open **EMI Analyzer** from Applications. macOS says it cannot verify the developer: click
   **Done**, not *Move to Trash*.
3. Open **System Settings → Privacy & Security**, scroll to the message about EMI Analyzer, click
   **Open Anyway**, and confirm with your password.
4. If macOS instead says the app **"is damaged and can't be opened"**, that is the same check
   worded differently. Remove the download quarantine in Terminal and open the app again:

   ```bash
   xattr -dr com.apple.quarantine "/Applications/EMI Analyzer.app"
   ```

### Windows

1. Run the downloaded `-setup.exe`.
2. Windows SmartScreen shows **"Windows protected your PC"**. Click **More info**, then
   **Run anyway**.
3. Follow the installer. It installs Microsoft WebView2 if your computer does not have it yet.
4. Start **EMI Analyzer** from the Start menu.

### Ubuntu / Debian (`.deb`)

```bash
sudo apt install ./EMI.Analyzer_*_amd64.deb
```

Start **EMI Analyzer** from your applications menu.

### Fedora / RHEL (`.rpm`)

```bash
sudo dnf install ./EMI.Analyzer-*-1.x86_64.rpm
```

Start **EMI Analyzer** from your applications menu.

### Other Linux (`.AppImage`)

AppImages need FUSE. On Ubuntu 24.04 install it with `sudo apt install libfuse2t64`, on older
releases with `sudo apt install libfuse2`. Then make the file executable and run it:

```bash
chmod +x EMI.Analyzer_*_amd64.AppImage
```

```bash
./EMI.Analyzer_*_amd64.AppImage
```

---

## Step 3 — First launch

1. **Make sure Docker is running** (Step 1).
2. **Open EMI Analyzer.** A window opens on the analyzer's home page.
3. **Watch the badge in the top-right corner.** On the first launch it says
   **Downloading worker** while Docker pulls `ghcr.io/embeddedci-com/emi-worker:<version>`, about
   1 GB. That takes a few minutes and happens once per app version.
4. When the badge turns green and says **Worker running**, the app is ready.

Click the badge at any time to see the worker's log or restart it.

The worker container starts when you open the app and is removed when you close it. Nothing keeps
running in the background.

---

## Step 4 — Analyse a board

1. On the home page, click **Choose a board file** and pick one of:
   - a `.kicad_pcb` file;
   - a zipped KiCad project;
   - a zip of Gerber files **including the drill file and an IPC-D-356 netlist** — Gerbers carry
     no net names on their own.

   No board to hand? Download the small test board
   [`tiny.kicad_pcb`](https://raw.githubusercontent.com/embeddedci-com/emi-analyzer/main/worker/tests/fixtures/tiny.kicad_pcb).
2. The board appears within a few seconds, with its layers on the left and the **Findings** tab on
   the right. Click a finding to zoom to it.
3. Look at the other tabs:
   - **ESD** — simulate a discharge into a connector's lines, and compare the voltage at the IC pin
     with the clamp where it is against the clamp moved to the connector.
   - **Cables** — say which cable plugs into which connector, and see its common-mode budget.
   - **Board** — stackup, nets and layer settings.
4. Opening the same file again takes you to the existing project instead of creating a new one.
5. The header has two more things: **Runs**, which lists what has been analysed and lets you
   retry anything that failed, and a **⋯** menu to rename or delete the board. Deleting removes
   its board file and every result from this computer.

---

## Running without the desktop app

The desktop app is a window around one program, `emi-local`, which also runs on its own and opens
the analyzer in your browser. Use it on a Linux machine without a desktop, or if you prefer a
browser tab. You still need Docker (Step 1).

1. Download `emi-local-<version>-<os>-<arch>` from the
   [latest release](https://github.com/embeddedci-com/emi-analyzer/releases/latest).
2. On macOS and Linux, make it executable:

   ```bash
   chmod +x emi-local-*
   ```

   On macOS, also remove the download quarantine, for the same reason as the app:

   ```bash
   xattr -d com.apple.quarantine emi-local-*
   ```

3. Run it (the file name depends on what you downloaded):

   ```bash
   ./emi-local-0.1.0-linux-amd64
   ```

   It prints its address and opens it in your browser: `http://127.0.0.1:7465`, or the next free
   port if that one is taken. Stop it with **Ctrl+C**, which also removes the worker container.

It only listens on this computer. To use it from another machine, forward the port over SSH
instead of exposing it, because the app has no sign-in:

```bash
ssh -L 7465:127.0.0.1:7465 your-server
```

| Flag | Default | |
|---|---|---|
| `-data-dir` | see below | where boards, results and the database are kept |
| `-open=false` | opens | do not open a browser tab |
| `-addr` | `127.0.0.1:7465` | listen address; loopback addresses only |
| `-worker none` | `docker` | do not start a worker; [run your own](docs/running-a-worker.md) |
| `-worker-image` | `ghcr.io/embeddedci-com/emi-worker:<version>` | run a different worker image |
| `-worker-concurrency` | `1` | how many runs the worker takes at once |
| `-experimental` | none | enable [experimental features](#experimental-features) |
| `-issue-key` | | print a key for a worker you run yourself, and exit |
| `-version` | | print the version and exit |

---

## Where your data is

| | Desktop app | `emi-local` on its own |
|---|---|---|
| macOS | `~/Library/Application Support/com.embeddedci.emi-analyzer` | `~/Library/Application Support/emi-analyzer` |
| Windows | `%APPDATA%\com.embeddedci.emi-analyzer` | `%APPDATA%\emi-analyzer` |
| Linux | `~/.local/share/com.embeddedci.emi-analyzer` | `~/.config/emi-analyzer` |

The folder holds `emi.db` (projects, runs and components, in SQLite), `blobs/` (your board files
and results) and `secret.key` (signs this installation's download links and worker keys). Back up
the folder to keep your projects; delete it to start over. To remove one board and its results,
use **⋯ → Delete this board** in the app.

---

## Updating and uninstalling

**Update:** download and install the newer release over the old one. Your data folder is kept. The
new version downloads its own worker image on first launch. To reclaim disk, list the old images
and remove the ones you no longer need:

```bash
docker image ls ghcr.io/embeddedci-com/emi-worker
```

```bash
docker image rm ghcr.io/embeddedci-com/emi-worker:<old-version>
```

**Uninstall:**

1. Remove the app: drag it from Applications to the Bin (macOS), use *Settings → Apps* (Windows),
   run `sudo apt remove emi-analyzer` (`.deb`), or delete the AppImage.
2. Delete the data folder listed above.
3. Remove the worker images:

   ```bash
   docker image rm $(docker image ls -q ghcr.io/embeddedci-com/emi-worker)
   ```

---

## Troubleshooting

| What you see | What to do |
|---|---|
| **Docker not installed**, but it is | The app looks on your `PATH` and in the usual install locations. Start the app from a terminal where `docker ps` works, or use `emi-local`. |
| **Docker not running** | Start Docker Desktop, or run `sudo systemctl start docker` on Linux. The worker starts by itself a few seconds later. |
| **Downloading worker** never finishes | Check your connection and free disk space, then click the badge → **Restart worker**. Behind a proxy, set it in Docker Desktop → *Settings* → *Resources* → *Proxies*. |
| **Worker stopped** | Click the badge and read the log. On Linux, check that `docker ps` works without `sudo` (Step 1). |
| Worker running, but a board never finishes processing | The worker cannot reach the app. With an unusual Docker setup, run `emi-local -worker-url http://<address>:7465`, where the address is how containers reach your computer. |
| **"Interrupted: the app was closed"** on a run | Runs do not survive closing the app. Retry the run. |
| macOS: **"EMI Analyzer is damaged"** | See Step 2 → macOS → item 4. |
| The Linux AppImage does nothing | Install FUSE (Step 2), or use the `.deb`. |

Found a bug? Open an issue at <https://github.com/embeddedci-com/emi-analyzer/issues> and include
the worker log from the badge.

---

## Experimental features

Some features are built but not yet reliable, and are switched off. Off means the app refuses to
run them, not just that the buttons are hidden.

| Name | What it enables | Why it is off |
|---|---|---|
| `full-wave` | openEMS full-wave simulation of a board region, hotspot maps, the far field, cable emissions and the compliance estimate — the Solve, Drivers, Components, Results and Compliance tabs | Long simulations are not yet numerically stable. The worker detects a run that diverges and refuses its numbers, but such a run can take many minutes to fail. See [known issues](docs/known-issues.md). |

To try it anyway, set `EMI_EXPERIMENTAL=full-wave` where the app is started:

- **`emi-local`:**

  ```bash
  ./emi-local -experimental full-wave
  ```

- **Desktop app on macOS:**

  ```bash
  EMI_EXPERIMENTAL=full-wave "/Applications/EMI Analyzer.app/Contents/MacOS/emi-analyzer"
  ```

- **Desktop app on Windows:** add a user environment variable `EMI_EXPERIMENTAL` with the value
  `full-wave` (Start → *Edit environment variables for your account*), then start the app.
- **Desktop app on Linux:** `EMI_EXPERIMENTAL=full-wave emi-analyzer`.

Full-wave simulation needs a lot of memory and CPU. On macOS and Windows, give Docker as much
memory as you can spare under Docker Desktop → *Settings* → *Resources*.

---

## More documentation

| | |
|---|---|
| [docs/known-issues.md](docs/known-issues.md) | What is verified, what is experimental, what is not built yet |
| [docs/running-a-worker.md](docs/running-a-worker.md) | Running the worker yourself: on another machine, or from source |
| [docs/rules-file.md](docs/rules-file.md) | Tuning the checks for a board with a rules file |
| [docs/emi-driver-format.md](docs/emi-driver-format.md) | The driver file format |
| [docs/](docs/README.md) | Everything else: how the model works, and design notes |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Building from source, tests, and releasing |

## Security

The app has no sign-in, so it protects itself by where it listens and who may call it. It binds
only to loopback addresses; answers only requests addressed to `localhost` or `127.0.0.1`, which
stops web pages reaching it through DNS rebinding; refuses changes requested by other websites;
serves files only on links signed with a per-installation secret that expire after 15 minutes; and
gives the worker a key that is revoked when the app stops.

## License

[Apache License 2.0](LICENSE). The worker image also contains third-party programs under their own
licences — notably openEMS and nec2c (GPL) and ngspice (BSD) — which it runs as separate programs.
