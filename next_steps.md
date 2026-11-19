# Next Steps — Klipper-for-Mac (v2, incorporating user audit feedback)

This file is the prioritized task list after picking up from Qoder/Trae and a
deep read of every surface the earlier hunts flagged. Updated 2026-10-18 after
user responses to the v1 audit.

---

## Verification state (this session, all confirmed)

| Check | Result |
|---|---|
| `make qa-fast` (161 tests) | PASS, 3.2 s |
| `make qa-full` (full offline tree + shellcheck) | PASS |
| `ruff check klipperformac/` (E/F/B9/W) | PASS, zero findings |
| `bash -n scripts/install.sh` | parses |
| Swift (`swiftc -parse`) CameraView, ContentView, DashboardView | all clean |
| `klipperformac doctor` — upstream pristine | OK |
| Deleted `import socket` from cli.py | done |
| 3 F841 dead-code findings fixed (`url`, `lines`, `merged`) | done |
| `pyproject.toml` F401/F841 quarantines removed | done |

---

## P0 — Live bugs found this audit (already fixed in this branch)

### ~~P0-1: TUI zero tests~~ → deferred
Moved to P2. Not a live bug, and the test framework priority below matters first.

### P0-2: device picker reverts to "Select device…" after selection [FIXED]
The `syncChosen()` callout inside `McuSerialRow` ran on every refresh tick and
set `chosen = ""` whenever `serial --json` hadn't caught up with the write —
so the button showed the picked port for one frame, then fell back to the
placeholder. Fix: `syncChosen()` now preserves a still-unconfirmed user pick and
only updates `chosen` when a non-empty resolved port is available, or clears it
when the config has settled on a genuinely different serial.

### P0-3: picker lets user select a port already owned by another [mcu] [FIXED]
That round-trip ended in a yellow "already claimed by [mcu] X" banner. Fix:
mcuRows computes a `claimants` map (every configured /dev/cu → its section) and
passes it to each McuSerialRow; the picker disables and tags "used by [mcu X]"
any port held by a DIFFERENT section, so the CLI never sees a conflict.

### ~~P0-4: unused `import socket`~~ [DELETED]
Confirmed dead (`grep socket\\. cli.py` → nothing). Removed from cli.py:11 and
the `"klipperformac/cli.py" = ["F401"]` line from pyproject.toml.

### ~~P0-5: F841 dead variables~~ [FIXED]
`url` (cli.py:346, inside `_wait_ready`), `lines` (cli.py:2123, inside
`_auto_serial_single`), `merged` (cli.py:2470, `cam.save_prefs`). All three
confirmed as never-read after assignment. Removed; F841 quarantine removed from
pyproject.toml.

### Camera tab → Recording with mic on/off [DONE]
Tab label is now "Recording" (icons unchanged). Added a Recording section above
preview with start/stop and a "Record audio (mic)" toggle wired to
`cam config --audio on|off`. audio state loads/saves with the other camera
prefs.

---

## P1 — Pi parity + hardening (do this month)

### P1-1: `flash uf2` success criterion
Confirmed open from live bench. When flashing an RP2040/RP2350 board, "volume
disappeared" (ROM consumed + reset) is the first act; the board must come back
as a RUNTIME USB-CDC device (VID:PID 1d50:614e at the same LOC). One SKR Pico
bounced back to BOOTSEL after a successful copy, and the flash tool reported
success. Product gap: wait for the runtime device to reappear within N seconds;
on bounce-back-to-BOOTSEL, report via the INFO_UF2.TXT readback instead of
"flashed OK."

### P1-2: GUI dark-mode contrast
FlatIcons palette measured at ~1.16:1 in dark mode for selected icons (WCAG AA
= 3:1 for UI components). MessageBanner material backdrop and destructive
button at ~2.17:1. Fix in `FlatPalette`'s dark-mode colors; screenshot-prove
both appearances in CI. Keep light mode byte-identical. (Visually the dark
mode is readable but falls short of accessibility standards for low-vision
users — the fix is a few stop adjustments, not a palette redesign.)

### P1-3: Perf verify after status cache
`versions.cached_check` shipped (6 h TTL, corrupt-safe, network errors never
clobber known facts). Measured `status` 3.4 s → 1.9 s. Verify the GUI poll path
got the same speedup: measure `StackReader` overhead, CLIRunner deadlines, and
proxy HTTP keep-alive (deliberate HTTP/1.0 today — measure, then decide on
1.1+pipeline if Mainsail's ~50 concurrent XHR/poll warrant it).

### P1-4: Dual-homing trsync caveat
Real 4+4 trap: an axis endstop on one MCU and that axis's steppers on another
MCU at the default `trsync` budget (25 ms across two USB-CDC clocks). No crash
but can surface as print defects or a homing fault mid-print. `mcus.validate()`
still has no detector for it. Fix: document `docs/Multi_MCU_Homing.md`; add a
WARN in `validate()` when an endstop's `[mcu X]` differs from its steppers'
`[mcu Y]`.

### P1-5: adversarial config/configgen fixtures
`_upsert_section_key()` with regex-special section names (e.g. `[mcu a[b]`);
`sync_virtual_sdcard()` on a 0-byte printer.cfg; `mcus.list_mcus()` on a file
that is pure noise; `detect._describe()` on `/dev/cu.usbmodem*` with no ioreg
match; `relocate_data()` with symlink attacks in the old data folder.

### P1-6: CLI dark-mode / TUI tests
TUI (~510 lines of `curses` that handles start/stop/restart/kill-all/data-
relocate/preset-switch) still has the only large untested interactive surface.
Tests to add under `tests/tui/`: `App.do("start")` when already up / when it
fails; `App.do("kill_all")` when nothing is running; `App.do("data")` with an
invalid path; `key_name()` with path-trailing input; `App.draw()` on a 2x2
terminal; terminal encoding error in a log line. (Path-traversal in
`key_name()` is a one-line fix: strip `/` from the name before building the
output path.)

---

## P2 — Tooling upgrades (get into CI)

### P2-1: add pyright to the gate
Cheapest way to catch "undefined method on struct" before shipping. Install in
`.venv-test`, run `pyright klipperformac/` with a committed baseline of accepted
findings. Start advisory (warnings only); fail the gate only on NEW findings.

### P2-2: add bandit to the gate
`bandit -r klipperformac/` runs in seconds. Catches `subprocess.shell=True`,
`eval()` on input, predictable temp filenames. Baseline the same way: committed
known-findings file, fail only on new hits.

### P2-3: expand ruff rules
Today: E/F/B9/W. Expansion that rarely fires false positives: `C901`
(function complexity cap or document why), `SIM` (flake8-simplify, auto-fixes
most hits), `RUF` (ruff-specific perf/design), `TID` (banned-imports: no raw
`os.path` where pathlib works), `FLY` (f-string migration). Turn on one rule-set
at a time, baseline the offenses, shrink the quarantine list. Goal: delete
`per-file-ignores` from pyproject.toml entirely.

### P2-4: mutation testing on the critical kernels
`mutmut` on `klipperformac/mcus.py` and `klipperformac/configgen.py` — the
only path that, if wrong, silently corrupts printer.cfg or breaks multi-MCU
detection. 10 minutes of mutation work tells you whether the tests actually
bite.

---

## P3 — Pi-better features (only after P0–P1 and P2 are green)

### P3-1: one-command "import from Pi"
`tarball-of-printer_data/config+gcodes` → presets + path rewrite + diff report.
This is how to actually beat a Pi image: a user who has a Pi wants to land
here without hand-rewriting configs.

### P3-2: print-session evidence bundle
`klipperformac bundle-report` → one zip with: klippy.log tail + timing
telemetry + power state + config hash + last doctor/selftest outputs. "Attach
log" button in the GUI.

### P3-3: doctor history
`data/logs/doctor-history.jsonl` — each `doctor` run appends {timestamp,
pass/fail, notes}. "It was fine yesterday" becomes checkable without asking the
user to know where logs live.

### P3-4: auto-reconnect by USB identity during print
On serial loss, re-find the board by USB serial number (not `/dev/cu.*`) and
`firmware_restart`. On a Pi the `/dev/serial/by-path` hook does this
automatically; on macOS a user unplug / replug is ~10 s of print loss. This
turns it into < 2 s.

### P3-5: moonraker-timelapse integration
Feasibility proven. Frame capture is `cam snapshot` per layer-change gcode.
Klipper's `[timelapse]` calls `park_layer` and `snapshot_gcode` — wire them to
the existing camera path.

### P3-6: multi-device GUI polish
One picker per `[mcu]` section already works (shown when `serial --json`
answers non-single). When printer.cfg has N sections and >N USB devices, show
a clear "Add another [mcu] with this port" affordance. Wraps `serial --set
<brdpath> <mcu>` which already exists.

---

## Non-negotiable (every PR)

- upstream pristine (`doctor` proves)
- every bug ships with its repro test
- `qa-fast` at commit / `qa-full` at push / CI green
- `taskboard.md` updated per run
- never edit/monkey-patch upstream checkouts

---

## File map

| Surface | Verdict |
|---|---|
| `klipperformac/powerloss.py` | Ship-standard: atomic writes, session markers, fsync. Maintain. |
| `klipperformac/proxy.py` | Hardened: port/bind/docroot validation, body cap, header sanitization. |
| `klipperformac/configgen.py` | Sound regex section assumptions; add hostile-input fixtures (P1-5). |
| `klipperformac/batteryguard.py` | Bounded inputs, missing-battery = no-action. Maintain. |
| `klipperformac/process.py` | Rollback landed, orphan reaping, state-file semantics. Maintain. |
| `klipperformac/mcus.py` | Exhaustive multi-MCU contract. Add dual-homing tsync warning (P1-4). |
| `klipperformac/tui.py` | **Needs tests (P1-6); sanitize preset names.** |
| `scripts/install.sh` | Parses clean, reasonable. Can't cold-test offline (network needed) but logic-test the path handling, quoting, error paths. |
| `klipperformac/cli.py` | Fixed dead imports, F841 clean. Maintain. |
| `KlipperMac/Views/DashboardView.swift` | Picker bugfixes landed this session. |
| `KlipperMac/Views/CameraView.swift` | Renamed Recording, mic toggle, record start/stop. |
| `KlipperMac/Views/ContentView.swift` | Tab renamed to "Recording", icons kept. |
