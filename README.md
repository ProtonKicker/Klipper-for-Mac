# MaKlipper — Klipper for Mac

Run [Klipper](https://www.klipper3d.org) on an Intel or Apple Silicon Mac like
it were the Raspberry Pi: your printer board plugs into USB, the Mac becomes
the host. No Linux box, no SD card, no hard fork.

**MaKlipper is not a Klipper fork.** Klipper, Moonraker, Mainsail and Fluidd are
downloaded pristine from upstream GitHub at setup time, at pinned refs
recorded in a lockfile. The only macOS-specific piece is two tiny
compatibility headers injected at compile time via `CPATH` — upstream
sources are never edited, and `maklipper doctor` verifies every checkout is
`git status` clean.

## Quick start

```sh
curl -fsSL https://raw.githubusercontent.com/YOURHANDLE/MaKlipper/main/scripts/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"   # once, if prompted

maklipper setup     # fetches upstream components + verifies they build on your Mac
maklipper serial    # plug in the printer board first; lists USB ports
maklipper serial --auto    # write the found port into printer.cfg
maklipper up        # starts klipper + moonraker + mainsail, keeps the Mac awake
```

Open **http://localhost:8080** for Mainsail (or **:8081** for Fluidd) — the full
printer dashboard, pre-connected to Moonraker. No setup wizard.

Or without the installer:

```sh
git clone https://github.com/YOURHANDLE/MaKlipper && cd MaKlipper
./bin/maklipper setup && ./bin/maklipper up
```

## Commands

| command | what it does |
|---|---|
| `setup` | clone pinned upstream Klipper/Moonraker, download Mainsail + Fluidd, build venv, verify the C engine compiles on this Mac |
| `up` / `down` / `restart` | start/stop the whole stack (also runs `caffeinate` so sleep can't kill a print) |
| `status` | which services are alive, pinned versions, available upstream updates |
| `serial` | list USB serial devices; `--set PATH` / `--auto` writes `[mcu] serial:` in printer.cfg |
| `presets` | list config presets (`*` = current); `--save NAME`, `--use NAME`, `--import PATH` (auto-backup as printer.cfg.bak) |
| `ui [mainsail\|fluidd]` | switch the default dashboard and open it (Mainsail :8080, Fluidd :8081) |
| `gcode SCRIPT...` / `gcode --status` | send G-code or query live printer state from the terminal |
| `logs [klipper\|moonraker] [-f]` | tail logs |
| `selftest` | verify Mainsail, Moonraker REST and the WebSocket link end-to-end |
| `update` | check upstream; `update --apply` re-fetches newest pinned tags and rebuilds |
| `update --pin moonraker=v0.11.0` | pin an exact upstream version (updates are always explicit, never automatic) |
| `doctor` | environment, build and power checks |

## Where things live

- `~/KlipperData/` — your files: configs, presets, web UIs, g-codes, logs.
  `config/printer.cfg` starts as a copy of the upstream `generic-rambo.cfg`
  example — replace it with your printer's real config. By default
  everything binds to localhost only.
- `~/.maklipper/` — app internals: pristine upstream checkouts, Python venv,
  Mainsail web files. Delete this directory to factory-reset; your data in
  `~/KlipperData` is untouched.

## Requirements

- Intel Mac (macOS 10.15+) or Apple Silicon Mac
- Xcode Command Line Tools: `xcode-select --install`
- Python ≥ 3.10: `brew install python@3.12` (or any `python3.1x` on PATH)
- `brew install libsodium` (used by Moonraker)

## How it works

Klipper's host software is nearly portable C + Python already: on macOS only
two system headers it expects (`<linux/can.h>` for CAN framing, `<sys/prctl.h>`
for thread naming) don't exist. MaKlipper ships two 20-line stub headers and
sets `CPATH` at build time so the compiler finds them first. Upstream sources
are bit-for-bit unchanged — `git status` in the checkout proves it.

Timing: Klipper's host needs responses within a few hundred milliseconds.
macOS delivers sub-millisecond timer precision while awake, better than a
stressed Raspberry Pi. The one real rule: **the Mac must stay awake during
printing** — `maklipper up` runs `caffeinate` for you. Closing the lid or
draining the battery can still interrupt a print; keep it on AC power.

## Limitations

- Running Klipper on macOS is **not supported by the Klipper project**
  (their stance: "possible but unsupported"). MaKlipper makes it "possible"
  conveniently — bugs in Klipper itself still go upstream.
- Shaper / input-shaping calibration (`SHAPER_CALIBRATE`) needs Python 3.14
  (macOS' multiprocessing default otherwise breaks it); use
  `MAKLIPPER_PYTHON=/path/to/python3.14 maklipper setup` when possible.
- `CAN bus`, host GPIO, KlipperScreen, and webcam integration are Linux-only
  or not wired up yet on v1.
- Notarized `.app` packaging is the planned v2; v1 is the CLI + Mainsail.

## License

MaKlipper code: MIT. Klipper: GPLv3, Moonraker: AGPLv3, Mainsail: GPLv3 —
all remain the property of their authors, downloaded unmodified from their
upstream repositories. Klipper is GPLv3, Moonraker GPLv3, Mainsail GPLv3,
Fluidd GPLv3 — see NOTICE. This project is not affiliated with or endorsed
by the Klipper project; "possible but unsupported" is their documented
stance for non-Linux hosts, so please reproduce any Klipper bug on Linux
before reporting it upstream.
