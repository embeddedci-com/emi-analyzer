# EMI Analyzer, in KiCad

Check the board open in the PCB Editor for the layout causes of EMI and EMC failures, without
exporting anything.

The plugin is a front end. The analysis, your boards and the results belong to the **EMI
Analyzer desktop app**, which runs everything on your own computer. The plugin finds the app,
hands it the board as it is in the editor (unsaved edits included), and shows the app's own
pages in a window beside pcbnew. Nothing is uploaded, and the plugin opens no network
connection except to the app on localhost.

![The plugin's toolbar button](icons/emi-48.png)

## Install

**1. Install the EMI Analyzer desktop app.** It is a separate download, and it needs Docker.
Follow the install guide: <https://github.com/embeddedci-com/emi-analyzer#readme>. Start it
once and check that a board analyzes.

**2. Add the plugin repository to KiCad.** *Plugin and Content Manager* → *Manage
repositories* → *+*, and add:

```
https://raw.githubusercontent.com/embeddedci-com/kicad-plugins/main/repository.json
```

**3. Install "EMI Analyzer"** from that repository and restart KiCad.

**4. Turn the API server on**, if it is not already: *Preferences* → *Plugins* → *Enable KiCad
API*. The plugin reads the board through it.

The first run installs PySide6 into the plugin's own Python environment. That is a few hundred
megabytes and it happens once.

### Requirements

| | |
|---|---|
| KiCad | 10.0.1 or newer, with the API server enabled |
| EMI Analyzer app | 0.2.0 or newer, installed and able to start its Docker worker |
| Disk | About 500 MB for the plugin's Python environment |

## Use it

Press **EMI Analyzer** in the PCB Editor toolbar. The first press starts the app if it is not
already running, which takes a few seconds.

The window shows the board's project in the app: findings, the board viewer, ESD simulation
and the cable budget, the same pages the app itself shows. Two things are added because the
board is open in KiCad:

- **Click a finding, then "Show in KiCad"** to select that net on the board and zoom to it.
- **"Select what needs attention"** in the toolbar selects every net flagged as critical or a
  warning, all at once.

**Rescan** reads the board again with whatever you have changed since, and analyzes it. A
board you have not touched costs nothing: the app recognizes the bytes and shows the analysis
it already has.

Each board file is one project in the app, so every analysis of a design stays in one place
and you can compare today's layout with last week's.

### What is sent where

The board text comes from pcbnew, not from your file, so what is analyzed is what you are
looking at and your file is never saved on your behalf. It is packed with its `.kicad_pro`
(that is where netclasses and differential pairs live) and an `emi.rules.yaml` if you keep one
beside the board, and posted to the app on `127.0.0.1`. The app stores it in its own data
folder. See "Where your data is" in the app's README.

## When EMI Analyzer is not running

The plugin needs the app. It looks for one already running, and starts the installed one if
there is none, so most of the time there is nothing to do.

If it cannot find one, it says so and offers **Get the app**, which opens the install guide.
Install it, start it once so it can download its Docker worker, then press **Rescan**.

### Keep it out of the way

While you work in KiCad the app only needs to be running, not on screen. In the app's own
window press **Minimize to menu bar** (**Minimize to tray** on Windows and Linux), or just
close it: your boards, your results and the worker stay exactly as they are, and it carries on
answering the plugin. Its icon stays there, and that menu is how you open it again or quit.

The button is in the app's window, not in this one. The plugin's window is yours to close
whenever you like, and it does not mind which state the app is in.

### Without the app

`emi-local`, the single binary the app is built around, serves the plugin just as well. Start
it and leave it running:

```bash
emi-local -open=false
```

The plugin finds it the same way, because that is the program that publishes where it is
listening. Anything on another port or another data folder:

```bash
EMI_ANALYZER_URL=http://127.0.0.1:7465
```

set in the environment KiCad starts in.

## Troubleshooting

**"EMI Analyzer is not installed on this computer."** See
[When EMI Analyzer is not running](#when-emi-analyzer-is-not-running).

**"Could not reach KiCad."** The API server is off: *Preferences* → *Plugins* → *Enable KiCad
API*.

**The button does nothing.** Only one window opens per plugin install; pressing again brings
the open one forward and rereads the board. If there is no window at all, run KiCad from a
terminal with `EMI_ANALYZER_DEBUG=1` to see what the plugin reports.

**The window says the worker is starting, and stays that way.** That is Docker, in the app.
Open the app itself and look at the worker status there.

## Develop

The plugin is plain Python. Install this checkout beside any released copy:

```bash
make plugin-install
```

It appears as "EMI Analyzer (dev)" with its own button, its own window and its own settings.
Python changes take effect on the next press; only the first install needs a KiCad restart.

```bash
make plugin-test        # PLUGIN_PY=... a Python with PySide6 and kicad-python runs them all
make plugin-uninstall
```

Point it at a server from a source build instead of the installed app:

```bash
make run-local                                    # in one terminal
EMI_ANALYZER_URL=http://127.0.0.1:7465 <kicad>    # KiCad started with it set
```

### Layout

| | |
|---|---|
| `analyze.py` | what KiCad runs |
| `emi_analyzer/desktop.py` | finding the app, and starting it |
| `emi_analyzer/client.py` | the part of the app's API the plugin uses |
| `emi_analyzer/boardio.py` | reading the board and selecting nets, through KiCad's IPC API |
| `emi_analyzer/project.py` | packing the board, and which project it belongs to |
| `emi_analyzer/bridge.py` | the channel the app's pages call KiCad through |
| `emi_analyzer/window.py` | the window: a toolbar over the app's pages |
| `emi_analyzer/single.py` | one window, however many times the button is pressed |

### Release

```bash
make pcm-release VERSION=0.1.1 PCM_STATUS=testing
```

Bump `emi_analyzer/__init__.py` first. The archive is built into `dist/pcm/` and the
Plugin and Content Manager index files are written into a checkout of
[embeddedci-com/kicad-plugins](https://github.com/embeddedci-com/kicad-plugins), which is
where every EmbeddedCI plugin is published from, so a user who added that repository once
gets this plugin too. The plugin's source and its archive stay in this repository: attach the
archive to a release here, and commit the index files there.

## License

Apache-2.0, with the rest of the EMI Analyzer. See [LICENSE](../LICENSE).
