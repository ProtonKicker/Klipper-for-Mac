# Klipper for Mac

*project codename: MaKlipper*

Run [Klipper](https://www.klipper3d.org) on any Mac — Intel or Apple Silicon —
like it were the Raspberry Pi. Plug your printer board into USB, and the Mac
becomes the host. No Linux box, no SD card, no fork: Klipper, Moonraker, Mainsail
and Fluidd are downloaded **pristine from upstream** at pinned versions. macOS
support comes entirely from two small compatibility headers injected at compile
time; upstream files are never edited (`klipperformac doctor` proves it with
`git status`).

## Quick start

```sh
curl -fsSL https://raw.githubusercontent.com/YOURHANDLE/MaKlipper/main/scripts/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"   # once, if prompted

klipperformac setup          # fetch upstream components, build, verify
klipperformac serial --auto  # plug in the printer board first
klipperformac up             # start everything, keep the Mac awake
```

Open **http://localhost:8081** for the Fluidd dashboard (Mainsail is on :8080).
Just type `klipperformac` alone for the interactive control panel.

Requirements: Xcode Command Line Tools (`xcode-select --install`),
Python ≥ 3.10 and libsodium (`brew install python@3.12 libsodium`).

## Commands

Run `klipperformac` with no arguments for the TUI; these are the CLI equivalents.

| command | what it does |
|---|---|
| `setup` | clone pinned Klipper/Moonraker, fetch Fluidd + Mainsail, build venv, verify everything compiles |
| `up` / `down` / `restart` | start/stop the stack (with `caffeinate`, so sleep can't kill a print) |
| `status` | service state, pinned versions, available updates |
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
Timing is comfortable: Klipper allows hundreds of milliseconds of host latency,
and macOS delivers sub-millisecond timer precision. The one rule: **the Mac
must stay awake while printing** — `up` runs `caffeinate` for you; keep it on AC.

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
