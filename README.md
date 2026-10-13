# Klipper for Mac

Run [Klipper](https://www.klipper3d.org) on any Mac — Intel or Apple Silicon —
like it were the Raspberry Pi: plug your printer board into USB and the Mac
becomes the host. No Linux VM, no SD card, no fork — Klipper, Moonraker,
Mainsail and Fluidd are **pristine upstream** at pinned versions. macOS support
lives entirely in small compatibility shims injected at compile time; upstream
files are never edited (`klipperformac doctor` proves it with `git status`).

![alt text]( https://github.com/ProtonKicker/Klipper-for-Mac/blob/main/screenshots/cli%20-%20oct%203%202026.png )

## Quick start

```sh
curl -fsSL https://raw.githubusercontent.com/ProtonKicker/Klipper-for-Mac/main/scripts/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"   # once, if prompted

klipperformac setup          # fetch upstream components, build, verify
klipperformac serial --auto  # plug in the printer board first
klipperformac up             # start everything, keep the Mac awake
```

Fluidd dashboard: **http://localhost:8081** (Mainsail :8080). Bare
`klipperformac` opens the interactive TUI.

Requirements: Xcode Command Line Tools (`xcode-select --install`),
Python ≥ 3.10, libsodium (`brew install python@3.12 libsodium`).

`printer.cfg` starts as upstream's `generic-rambo.cfg` example — replace it
with your printer's real config (`klipperformac presets` can import one).

## GUI app

A native SwiftUI app wrapping the same CLI — same data folder, same running
stack. Install the CLI first, then get the GUI from
[Releases](https://github.com/ProtonKicker/Klipper-for-Mac/releases): unzip
`Klipper-for-Mac.zip` and drag **Klipper for Mac.app** into Applications,
Chrome-style. It's ad-hoc signed (notarization planned), so right-click →
**Open** works around the first-launch warning. The CLI installer never
downloads or builds the GUI — they're fully separate.

Building from source (full clone needed): `KlipperMac/build-app.sh`, Command
Line Tools only; `KLIPPERFORMAC_ZIP=1` also emits the zip. A prebuilt zip is
committed at `KlipperMac/dist/` as a fallback.

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
| `setup` | clone pinned Klipper/Moonraker, fetch Fluidd + Mainsail, build venv, verify |
| `up` / `down` / `restart` | start/stop the stack (`caffeinate` keeps Mac awake) |
| `killall` | force-kill the stack plus stray leftovers |
| `status` | service state, pinned versions, available updates |
| `lan [on\|off]` | show/toggle access for other devices |
| `serial` | list USB serials; `--auto`/`--set PATH` writes `[mcu] serial:` into printer.cfg |
| `data` | show data folder; `--set PATH` moves everything there |
| `presets` | config presets (`--save/--use/--import`, auto-backup) |
| `ui [fluidd\|mainsail]` | switch the default dashboard and open it |
| `gcode …` / `--status` | send G-code / query printer state |
| `logs [klipper\|moonraker] [-f]` | tail logs |
| `update [--apply]` | check upstream; re-fetch pinned tags and rebuild (never automatic) |
| `doctor` / `selftest` | environment / end-to-end link checks |
| `uninstall` | remove everything; asks before deleting the data folder (`--data`/`--keep-data`) |

## Where things live

- `~/Documents/Klipper for Mac/` — **your files**: configs, presets, g-codes,
  logs, web UIs (`klipperformac data --set PATH` to move it).
- `~/.klipperformac/` — app internals (checkouts, venv); deleting it
  factory-resets while your data survives. `uninstall` removes both, asking
  first about the data folder.

## How it works

Klipper's host is nearly portable C + Python already; on macOS only two system
headers (`<linux/can.h>`, `<sys/prctl.h>`) are missing — this project ships
~20-line stubs via `CPATH`, plus a tiny `sitecustomize.py` and `ip` shim in the
venv so Moonraker/Mainsail run unmodified. Timing is comfortable: Klipper
tolerates hundreds of ms of host latency; macOS delivers sub-millisecond.
The one rule: **the Mac must stay awake while printing** — `up` runs
`caffeinate` for you; keep it on AC.

## Limitations

- Not supported by the Klipper project ("possible but unsupported"); Klipper
  bugs still go upstream.
- `SHAPER_CALIBRATE` needs Python 3.14: `KLIPPERFORMAC_PYTHON=…3.14 klipperformac setup`.
- No CAN bus, host GPIO, KlipperScreen, or webcam in v1.
- `.app` GUI is ad-hoc signed; right-click → Open on first launch.

## License

Klipper for Mac: MIT. Upstream components remain unmodified under their own
licenses (Klipper/Moonraker/Mainsail/Fluidd: GPLv3 — see [NOTICE](NOTICE)).
Not affiliated with the Klipper project.
