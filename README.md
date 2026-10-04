# Klipper for Mac

Run [Klipper](https://www.klipper3d.org) on any Mac — Intel or Apple Silicon —
like it were the Raspberry Pi. Plug your printer board into USB, and the Mac
becomes the host. No Linux VM, no SD card, no fork: Klipper, Moonraker, Mainsail
and Fluidd are downloaded **pristine from upstream** at pinned versions. macOS
support comes entirely from two small compatibility headers injected at compile
time; upstream files are never edited (`klipperformac doctor` proves it with
`git status`).

![alt text](https://github.com/ProtonKicker/Klipper-for-Mac/blob/main/screenshots/cli%20-%20oct%203%202026.png)

## Quick start

```sh
curl -fsSL https://raw.githubusercontent.com/ProtonKicker/Klipper-for-Mac/main/scripts/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"   # once, if prompted

klipperformac setup          # fetch upstream components, build, verify
klipperformac serial --auto  # plug in the printer board first
klipperformac up             # start everything, keep the Mac awake
```

Open **http://localhost:8081** for the Fluidd dashboard (Mainsail is on :8080).
Just type `klipperformac` alone for the interactive control panel.

Requirements: Xcode Command Line Tools (`xcode-select --install`),
Python ≥ 3.10 and libsodium (`brew install python@3.12 libsodium`).

## Slicing in Orca Slicer

Orca can slice on the Mac and send jobs straight to the printer, with live
progress in the slicer panel. In Orca (≥ 2.3), under **Prepare → device
selector → + (Add Printer)**:

- **Printer type / connection:** `Moonraker (Klipper)`
- **Host:** `http://localhost:7125` — the Moonraker API port, *not* the
  dashboard ports (:8080 Mainsail / :8081 Fluidd)
- **Name:** anything, e.g. "Klipper on this Mac"

`localhost` is already in Moonraker's trusted clients, so no key or login is
needed. By default the stack only listens on this Mac; to slice from another
computer, turn on LAN access first (see below) and use
`http://<Mac's LAN IP>:7125` as the host. Uploads, start/pause/cancel and the
progress tab then work exactly as they do against a Raspberry Pi host.

## Access from other devices

```sh
klipperformac lan on     # or `lan off` to lock back down; status with bare `lan`
klipperformac restart    # applies the new bind addresses
```

With LAN access on, the command prints the URLs to use from other devices —
dashboards (`http://192.168.x.x:8080/8081`) and the Moonraker API
(`:7125`, e.g. for Orca Slicer on another computer). The TUI shows the same
addresses on its home screen; `lan off` returns everything to
localhost-only.

Tradeoff: Moonraker trusts private network ranges (192.168.0.0/16, 10.0.0.0/8,
172.16.0.0/12) without passwords, so anyone on your local network can control
the printer. Fine on home Wi-Fi — turn it off before joining a café or campus
network.

## Commands

Run `klipperformac` with no arguments for the TUI; these are the CLI equivalents.

| command | what it does |
|---|---|
| `setup` | clone pinned Klipper/Moonraker, fetch Fluidd + Mainsail, build venv, verify everything compiles |
| `up` / `down` / `restart` | start/stop the stack (with `caffeinate`, so sleep can't kill a print) |
| `killall` | force-kill the whole stack plus stray leftovers (e.g. a Fluidd/Mainsail server still running after the CLI was closed) |
| `status` | service state, pinned versions, available updates |
| `lan [on\|off]` | show/toggle access for other devices on your network |
| `serial` | list USB serial devices; `--auto` / `--set PATH` writes `[mcu] serial:` into printer.cfg |
| `data` | show the data folder; `--set PATH` moves everything there and fixes config paths |
| `presets` | manage config presets (`--save/--use/--import`, auto-backup) |
| `ui [fluidd\|mainsail]` | switch the default dashboard and open it |
| `gcode …` / `gcode --status` | send G-code / query printer state from the terminal |
| `logs [klipper\|moonraker] [-f]` | tail logs |
| `update` | check upstream; `--apply` re-fetches pinned tags and rebuilds (never automatic) |
| `doctor` / `selftest` | environment checks / end-to-end link checks |

## Where things live

- `~/Documents/Klipper for Mac/` — **your files**: configs, presets, g-codes,
  logs, web UIs (`klipperformac data --set PATH` to move it).
  Delete `~/.klipperformac/` (app internals: checkouts, venv) to factory-reset —
  your data survives. Everything binds to localhost only.

`printer.cfg` starts as upstream's `generic-rambo.cfg` example — replace it with
your printer's real config (`klipperformac presets` can import one).

## How it works

Klipper's host is nearly portable C + Python already; on macOS only two system
headers it expects (`<linux/can.h>`, `<sys/prctl.h>`) are missing. MaKlipper
ships two ~20-line stubs and sets `CPATH` so the compiler finds them first.
At run time the venv it creates carries a tiny generated `sitecustomize.py`
(Linux-only clock constants Moonraker touches) and an `ip` shim on Moonraker's
PATH, so Mainsail's boot sequence completes and network stats work — without
editing a single upstream file. Timing is comfortable: Klipper allows hundreds
of milliseconds of host latency, and macOS delivers sub-millisecond timer
precision. The one rule: **the Mac must stay awake while printing** — `up`
runs `caffeinate` for you; keep it on AC.

## Limitations

- Not supported by the Klipper project ("possible but unsupported"); Klipper
  bugs still go upstream.
- `SHAPER_CALIBRATE` needs Python 3.14: `KLIPPERFORMAC_PYTHON=…3.14 klipperformac setup`.
- No CAN bus, host GPIO, KlipperScreen, or webcam in v1.
- Notarized one-click `.app` is planned v2; v1 is CLI + TUI + web dashboard.

## License

MaKlipper: MIT. Upstream components remain unmodified under their own licenses
(Klipper/Moonraker/Mainsail/Fluidd: GPLv3 — see [NOTICE](NOTICE)).
Not affiliated with the Klipper project.
