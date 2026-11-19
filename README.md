# Klipper for Mac

Run [Klipper](https://www.klipper3d.org) on any Mac — Intel or Apple Silicon —
like it were the Raspberry Pi: plug your printer board into USB and the Mac
becomes the host. No Linux VM, no SD card, no fork — Klipper, Moonraker,
Mainsail and Fluidd are **pristine upstream** at pinned versions. macOS support
lives entirely in small compatibility shims injected at compile time; upstream
files are never edited (`klipperformac doctor` proves it with `git status`).

![alt text]( https://github.com/ProtonKicker/Klipper-for-Mac/blob/main/screenshots/cli%20-%20oct%203%202026.png )

## Quick start — GUI app

Download `Klipper-for-Mac.zip` from
[Releases](https://github.com/ProtonKicker/Klipper-for-Mac/releases), unzip it,
drag **Klipper for Mac.app** into Applications, and launch it. The ZIP contains
a working `klipperformac` CLI **and its own Python 3.12 runtime** for both
architectures — no separate CLI installer, no separate Python, no Homebrew
Python, and no `scripts/install.sh` are needed. The universal GUI supports
**macOS 10.15+ on Intel** and **macOS 11.0+ on Apple Silicon** (per-arch
floors); the bundled CLI runtimes keep matching floors.

In the app choose **Printer → Setup → Install stack**. That first-time step
still requires these *system* prerequisites (the app shows a checklist and
setup performs the authoritative checks; the app does not install them for
you):

- **git + a C compiler** — install Xcode Command Line Tools: `xcode-select --install`
- **libsodium** — required by Moonraker's authentication: `brew install libsodium` (or install it from another source)
- **internet access** — setup fetches the pinned pristine upstream Klipper,
  Moonraker, Mainsail and Fluidd components

After setup, plug in the printer board, pick its serial device in the Printer
pane, and press Start. Fluidd is at **http://localhost:8081** (Mainsail :8080).
`printer.cfg` starts as upstream's `generic-rambo.cfg` example — replace it
with your printer's real config (the Presets picker can import one).

The app includes Printer, Recording, and Settings panes. **Settings → About**
can check for and install app updates from GitHub Releases. Closing the window
hides the UI while the stack keeps running if a Klipper instance is started; the
app fully quits only when the stack is stopped.

The app is ad-hoc signed (notarization planned), so right-click → **Open**
works around the first-launch warning. It resolves its bundled CLI from inside
its own bundle, so it works from any install location, not just /Applications.

## Terminal CLI (optional)

The command-line/TUI installer is still available for scripting, but is **not**
needed for the GUI:

```sh
curl -fsSL https://raw.githubusercontent.com/ProtonKicker/Klipper-for-Mac/main/scripts/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"   # once, if prompted

klipperformac setup          # fetch upstream components, build, verify
klipperformac serial --auto  # plug in the printer board first
klipperformac up             # start everything, keep the Mac awake
```

That installer needs **macOS 10.15+ (Apple Silicon 11+)**, Xcode Command Line
Tools, a **Python ≥ 3.10 you provide** (python.org works on 10.15), and
libsodium. The GUI does not have these Python/CLI requirements because it ships
its own. Bare `klipperformac` opens the interactive TUI.

## Building the GUI from source

A full clone is needed: `./scripts/fetch-python-runtimes.sh` downloads the two
relocatable CPython runtimes into `.build/pythons` (requires `uv`), then
`KlipperMac/build-app.sh` bundles them with the CLI; `KLIPPERFORMAC_ZIP=1` also
emits the ZIP. Command Line Tools are the only compiler requirement. The build
compiles universal GUI and camera-helper binaries per arch: **x86_64 targets
macOS 10.15** and **arm64 targets macOS 11.0** (`-warnings-as-errors`), so API
floors are enforced by construction. `scripts/verify_bundle.py` checks the app
and ZIP.

![alt text](https://github.com/ProtonKicker/Klipper-for-Mac/blob/main/screenshots/gui%20-%20oct%2012%202026%20-%202.png)

## Orca Slicer

Slice on the Mac and send jobs straight to the printer. In Orca (≥ 2.3),
**Prepare → Add Printer**: type `Moonraker (Klipper)`, host
`http://localhost:7125` — the API port, *not* the dashboard ports
(:8080/:8081). No key needed; `localhost` is already trusted. From another
computer, turn on LAN access below and use `http://<Mac's LAN IP>:7125`.
Uploads, start/pause/cancel and progress work as against a Pi host.

## Access from other devices

```sh
klipperformac lan on && klipperformac restart   # prints the LAN URLs; `lan off` locks back to localhost
```

Dashboards at `http://192.168.x.x:8080/8081`, API at `:7125`; the TUI shows
the same addresses. Tradeoff: Moonraker trusts private network ranges
password-free, so anyone on your LAN can control the printer — fine at home,
off at the café.

## Commands

`klipperformac` with no arguments is the TUI; these are the CLI equivalents.

| command | what it does |
|---|---|
| `setup` | clone pinned Klipper/Moonraker, fetch Fluidd + Mainsail, build venv, verify (`--with-shaper` builds the Python 3.14 shaper venv too) |
| `up` / `down` / `restart` | start/stop the stack (`caffeinate` keeps Mac awake). `up`/restart best-effort auto-sync USB MCU serial ports into `printer.cfg` (only matching `serial:` lines) before Klipper reads it |
| `killall` | force-kill the stack plus stray leftovers |
| `status` | service state, pinned versions, available updates |
| `lan [on\|off]` | show/toggle access for other devices |
| `serial` | list USB serials/MCU sections; `--auto`/`--set PATH [MCU]` writes a section's `serial:`; `--sync` resolves ports by USB identity. Serials are also auto-synced best-effort before stack start/repair and on Klipper `RESTART`/`FIRMWARE_RESTART` (only matching MCU `serial:` lines change) |
| `data` | show data folder; `--set PATH` moves everything there |
| `presets` | config presets (`--save/--use/--import`, auto-backup) |
| `backup create/list/verify/restore/prune` | archive the whole data folder to `data/backups/`, verify an archive actually restores, restore it, keep the newest 10 |
| `ui [fluidd\|mainsail]` | switch the default dashboard and open it |
| `gcode …` / `--status` | send G-code / query printer state |
| `cam on/off/status/list/snapshot/build/config` | Mac or iPhone camera streamed to the dashboard (localhost-only) |
| `logs [klipper\|moonraker] [-f]` | tail logs |
| `update [--apply]` | check upstream; re-fetch pinned tags and rebuild (never automatic) |
| `doctor` / `selftest` | environment / end-to-end link checks |
| `timing [--json]` | read-only scheduling + power telemetry: what macOS is giving klippy right now (safe mid-print; `doctor` prints the short form) |
| `uninstall` | remove everything; asks before deleting the data folder (`--data`/`--keep-data`) |

## Where things live

- `~/.klipperformac/data/` — **your files**: configs, presets, g-codes,
  logs, web UIs (`klipperformac data --set PATH` to move it). Newer installs
  keep Documents out of the loop entirely, so macOS never asks the app for
  folder permission. Existing installs that already have
  `~/Documents/Klipper for Mac` keep using it — nothing is moved on you
  without you asking.
- `~/.klipperformac/` — app internals (checkouts, venvs, camera helper, data).
  `klipperformac uninstall` removes it all, asking first about the data
  folder — and when you say keep, it moves the data folder out safely
  beforehand.

## How it works

Klipper's host is nearly portable C + Python already; on macOS only two system
headers (`<linux/can.h>`, `<sys/prctl.h>`) are missing — this project ships
~20-line stubs via `CPATH`, plus a tiny `sitecustomize.py` and `ip` shim in the
venv so Moonraker/Mainsail run unmodified. Timing is comfortable: Klipper
tolerates hundreds of ms of host latency; macOS delivers sub-millisecond.
The one rule: **the Mac must stay awake while printing** — `up` runs
`caffeinate` for you; keep it on AC.

## macOS hardware limits

CAN bus at runtime, host GPIO/I2C/SPI, and Linux-style real-time priorities
are **not possible on macOS** — verified against the pinned upstream sources,
with the exact code paths and the working alternatives (USB-CDC firmware
flashing, second USB MCU, network-pluggable power/sensor devices) documented
in [docs/macos-hardware-limits.md](docs/macos-hardware-limits.md).

## Limitations

- Not supported by the Klipper project ("possible but unsupported"); Klipper
  bugs still go upstream.
- No CAN bus, host GPIO, or KlipperScreen equivalent yet. (True Mac-native
  GPIO is impossible — no pin headers exposed to software; the standard Klipper
  answer applies: any $5 Arduino/STM32 flashed as an extra `[mcu]` works.)
- `.app` GUI is ad-hoc signed; right-click → Open on first launch.

## Input-shaper calibration (SHAPER_CALIBRATE)

Klipper computes shaper fits with numpy **inside** the klippy process, so that
one process needs a Python that has numpy. Klipper for Mac builds a dedicated
Python 3.14 venv (`~/.klipperformac/venv-shaper`) automatically when your
`printer.cfg` mentions `[input_shaper]` / `[resonance_tester]` — no env vars,
no manual steps. `klipperformac setup --with-shaper` forces it;
`klipperformac status` and `doctor` show which interpreter klippy will run on.
Moonraker always stays on the main venv, and upstream files are still pristine.

## Webcam (Mac or iPhone camera)

```sh
klipperformac cam build     # one time: compiles the Swift camera helper
klipperformac cam on        # AVFoundation -> MJPEG at localhost:8082
klipperformac cam off       # off = nothing streams (privacy by default)
```

Mainsail/Fluidd pick the camera up via Moonraker's standard `[webcam]` config
(no Moonraker edits). Supports built-in FaceTime cameras, USB UVC webcams, and
iPhone Continuity Camera. `klipperformac cam snapshot` grabs one frame (saved
to the data folder, great for timelapses); the `.app` has a Recording/Camera pane
with camera controls and a live preview. The stream binds to localhost only — other
devices can't watch unless you deliberately turn LAN access on.

## License

Klipper for Mac: MIT. Upstream components remain unmodified under their own
licenses (Klipper/Moonraker/Mainsail/Fluidd: GPLv3 — see [NOTICE](NOTICE)).
Not affiliated with the Klipper project.
