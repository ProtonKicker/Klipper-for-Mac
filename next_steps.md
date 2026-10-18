# Next Steps — Klipper-for-Mac: Pi-parity, reliability, edge cases

Ground truth: this is a **3D-printer host**. A host that loses a print, corrupts
a config, or orphans a process at 3 a.m. is worse than no host at all. Every
item below is either (a) a confirmed gap from this audit, (b) measured against a
Pi image, or (c) a tooling gate that stops a future bug from shipping.

This file replaces the previous `next_steps.md`. It is deliberately specific:
every line is something you can start today.

---

## Current state (verified this audit)

- Suite: ~3,130+ offline tests pass. 29 live tests pass on the bench. Fast QA
  161 tests / 3.2 s.
- Gates: `ruff` clean (E/F/B9/W), `bash -n install.sh` clean, 27 Swift files
  parse, direct Swift logic checks pass, future SwiftPM tests parse.
- Limitations confirmed real by code review: no `uninstall.sh` in `scripts/`,
  no TUI tests at all, no pyright/bandit gates.

---

## P0 — These are bugs / real holes (do next week)

### P0-1: no TUI tests (HIGH risk)

`klipperformac/tui.py` is ~510 lines of interactive `curses` that handles
start/stop/restart/kill-all/data-relocate/preset-switching. It has **zero** test
coverage. A stack start from the TUI is how most users run the app.

```
klipperformac/tui.py — NO test file exists
```

Tests needed:
- `App.do("start")` when the stack is already up -> "already running"
- `App.do("start")` when it fails -> error message shown, no crash
- `App.do("kill_all")` when nothing is running -> "nothing to kill"
- `App.do("data")` with a path that fails relocate -> error, not crash
- `App.do("serial")` with no USB devices -> "no USB serial devices found"
- `App.do("apply_updates")` while the stack is running -> "stop stack before updating"
- `key_data()` with empty input -> "no path typed — cancelled"
- `key_data()` with a valid path -> calls relocate_data
- `App.draw()` on a tiny terminal (2x2) -> no crash from safe()
- Terminal encoding error in log line -> `errors="replace"` or no crash

### P0-2: TUI preset name path traversal (MEDIUM risk)

`tui.py:408` — `key_name()` builds a preset name with only `.strip()` and
`.replace(" ", "-")`. Type `../../tmp/evil` and it becomes `../../tmp/evil`,
which `paths.PRESETS / ("../../tmp/evil" + ".cfg")` lets escape the presets
folder. On POSIX `pathlib` this resolves outside the presets dir.

```python
# tui.py:408 — the fragile line
name = self.save_name.strip().replace(" ", "-")
# fix: add — name = re.sub(r"[^A-Za-z0-9_\-]", "_", name.lstrip("./"))
```

### P0-3: unused `import socket` in cli.py (debt)

`cli.py:11` — `import socket` is dead. `grep -n socket\\. klipperformac/cli.py`
returns nothing. Delete it, then remove the `"klipperformac/cli.py" = ["F401"]`
line from `pyproject.toml` so the gate can never regress on F401 again.

### P0-4: no `uninstall.sh` (project structure gap)

`AGENTS.md` documents `uninstall.sh` under "Project structure", but it does not
exist. `cmd_uninstall` in `cli.py` works as a subcommand, but there is no
stand-alone script for `curl | sh`-style install users to run. Either create a
thin `scripts/uninstall.sh` wrapper that execs `klipperformac uninstall "$@"`,
or remove the reference from `AGENTS.md`.

### P0-5: `install.sh` never cold-tested offline

The curl-install flow (`curl -fsSL … | scripts/install.sh | sh`) has no
hermetic test. The edge cases worth pinning:

- git not on PATH -> the precheck aborts, but does it exit non-zero cleanly?
- `SRC_DIR` already exists, dirty checkout -> `pull --ff-only` fails, WARNING printed
- `BIN_DIR` not on PATH -> the NOTE is a hint, not an error; correct?
- /dev/tty not available for the setup-prompt -> the `: <>/dev/tty` trick works?
- `$HOME` contains spaces -> every path here must stay quoted

### P0-6: F841 dead variables in cli.py (possible bugs)

The baseline has `cli.py:264 url`, `cli.py:735 lines`, `cli.py:1035 merged`
as "assigned and never read." Each is either dead code or a value that was
meant to be used. Two of the three `url` lines (129, 347) are local string
variables that shadow the concept `url` — harmless but confusing. Audit them;
where a value is genuinely unused, delete it and remove the quarantine line.

---

## P1 — Pi parity + hardening (do this month)

### P1-1: `flash uf2` success criterion (confirmed open from live bench)

When `flash uf2` copies a `.uf2` to the RPI-RP2 volume, "volume disappeared"
(ROM consumed the image, board reset) is only the first half. The board must
return as a **runtime** USB-CDC device (`VID:PID 1d50:614e` at the same LOC).

On the live bench this session: one SKR Pico bounced back to BOOTSEL after a
successful copy, and the flash tool reported success. Product gap: wait for the
runtime device to reappear within N seconds; on bounce-back-to-BOOTSEL, say so
with the INFO_UF2.TXT readback instead of "flashed OK."

### P1-2: GUI dark-mode accessibility (audit found real contrast gaps)

FlatIcons palette measured at 1.16:1 in dark mode for selected icons
(WCAG AA requires 3:1 for UI components, 4.5:1 for text). MessageBanner
material backdrop, destructive button at 2.17:1. Fix contrast, screenshot-
prove both appearances in CI, keep light mode byte-identical.

### P1-3: Perf round re-measure (status cache shipped, now verify)

`versions.cached_check` (6 h TTL, corrupt-safe, network errors never
clobber known facts) shipped per taskboard and measured status 3.4 s -> 1.9 s.
Verify the GUI poll path got the same speedup: measure `StackReader` overhead
(`ps` per tick), CLIRunner deadlines (is there a hung-CLI wedge?), proxy
keep-alive (deliberate HTTP/1.0 today — measure, then decide on 1.1+pipeline).

### P1-4: Dual-homing tsync caveat (real 4+4 trap)

`mcus.validate` still has no check for: an axis endstop and its steppers on
different MCUs at the default `trsync` budget (25 ms across two USB-CDC
clocks). The user's 4+4 scenario is exactly this. Document
`docs/Multi_MCU_Homing.md`, WARN in `validate()`.

### P1-5: Reliability against hostile inputs

These functions take raw config/user text and need adversarial fixtures:

- `_upsert_section_key()` with a section name containing regex specials
  (e.g., `[mcu a[b]`) — does `re.escape` protect the section lookup?
- `sync_virtual_sdcard()` when printer.cfg is empty (0 bytes)
- `mcus.list_mcus()` on a file where every line is noise (stress)
- `detect._describe()` on `/dev/cu.usbmodem` paths with no ioreg match
- `relocate_data()` symlink attacks in the old data folder

### P1-6: proxy HTTP keep-alive decision

The taskboard says HTTP/1.0 is deliberate. Mainsail opens 50+ concurrent XHR
to `/printer/objects/query`. Under HTTP/1.0 that is 50+ TCP connects/closes per
poll. Measure keep-alive; switch to HTTP/1.1 if the latency profile warrants.

---

## P2 — Tooling upgrades (get these into CI)

### P2-1: add pyright to the gate

`pyright` is the cheapest way to catch "undefined method on struct" before it
ships. Install it in `.venv-test`, run `pyright klipperformac/` with a
committed baseline of accepted findings. Start as advisory (warnings only),
fail the gate only on NEW findings.

### P2-2: add bandit to the gate

`bandit -r klipperformac/` runs in seconds. Catches `subprocess.shell=True`,
`eval()` on input, temp files with predictable names. Baseline it the same
way: ship a known-findings file, fail only on new hits.

### P2-3: expand ruff rules (rust-compiler-style)

Today: E/F/B9/W. An expansion that rarely fires false positives:
- `C901` function complexity (cap or document why)
- `SIM` flake8-simplify (auto-fixes most findings)
- `RUF` ruff-specific rules (perf/design)
- `TID` banned-imports (no raw `os.path` where pathlib works)
- `FLY` flynt (f-string migration)

Turn on one rule-set at a time, baseline the offenses, shrink the quarantine
list. **Goal: delete `per-file-ignores` in `pyproject.toml` entirely.**

### P2-4: mutation testing on the critical kernels

`mutmut` on `klipperformac/mcus.py` and `klipperformac/configgen.py` only —
the pure-criticality path that, if wrong, silently corrupts printer.cfg or
breaks multi-MCU detection. 10 minutes of mutation work tells you whether the
tests actually bite.

---

## P3 — Pi-better features (only after P0–P2 are green)

### P3-1: one-command "import from Pi"

`tarball-of-printer_data/config+gcodes -> presets + path rewrite + diff
report`. This is how you actually beat a Pi image: the user who has a Pi
today wants to land here without rewriting configs by hand.

### P3-2: print-session evidence bundle

`klipperformac bundle-report` -> one zip with: klippy.log tail + timing
telemetry + power state + config hash + the last doctor/selftest outputs.
"Attach log" button in the GUI. The difference between "it broke yesterday"
and "here is the full story."

### P3-3: doctor history

`data/logs/doctor-history.jsonl` — each `doctor` run appends a short line
(timestamp, pass/fail, notes on each check). "It was fine yesterday" becomes
checkable without asking the user to know where logs live.

### P3-4: auto-reconnect by USB identity during print

On serial loss, re-find the board by USB serial number (not `/dev/cu.*`) and
firmware_restart — the identity layer exists for exactly this. On a Pi the
`/dev/serial/by-path` hook does it automatically; on macOS a user unplug /
replug is ~10 s of print loss. This turns it into < 2 s.

### P3-5: moonraker-timelapse integration

Feasibility was proven. Frame capture is `cam snapshot` per layer-change gcode.
Klipper's `[timelapse]` calls `park_layer` and `snapshot_gcode` — wire them to
the existing camera path.

---

## P4 — Non-negotiable (every PR)

- upstream pristine (doctor proves)
- every bug ships with its repro test
- qa-fast at commit / qa-full at push / CI green
- taskboard.md updated per run
- never edit/monkey-patch upstream checkouts

---

## File map for this audit

| File | Verdict |
|---|---|
| `klipperformac/powerloss.py` | Ship-standard: atomic writes, session markers, fsync, sweep-on-start. Maintain as-is. |
| `klipperformac/proxy.py` | Extremely hardened: port/bind/docroot validation, concurrent-limit, body cap, forwarded-header sanitization. Maintain as-is. |
| `klipperformac/configgen.py` | Solid: regex section assumptions encode. Add hostile-input fixtures (P1-5). |
| `klipperformac/batteryguard.py` | Solid: bounded inputs, LCD-low streak + hysteresis, missing-battery = no-action. |
| `klipperformac/process.py` | Solid: rollback landed, orphan reaping, state-file semantics. |
| `klipperformac/mcus.py` | Excellent: exhaustive multi-MCU contract. Add the dual-homing tsync warning (P1-4). |
| `klipperformac/detect.py` | Solid after hub-nested fix and post-restart retry. |
| `klipperformac/flash.py` | Solid: flash probe + rp2040 + build-rp2040 all tested. Fix uf2 success criterion (P1-1). |
| `scripts/install.sh` | Parses clean, reasonable structure. Cold-test offline (P0-5). |
| `klipperformac/tui.py` | **Needs tests (P0-1); sanitize preset names (P0-2).** |
| `klipperformac/cam.py` | Solid: recording caps, helper flag probing, permission handling. |
| `.py` (∀ under `klipperformac/`) | ruff clean; pyright pending (P2-1); bandit pending (P2-2). |
