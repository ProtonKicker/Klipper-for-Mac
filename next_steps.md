# Next Steps — Klipper-for-Mac reliability & Pi-parity roadmap

Ground truth: this app is a 3D-printer host. Reliability > features. Every item
below is either (a) proven by repro, (b) a measured perf/robustness gap vs a
Pi image, or (c) a tooling gate that makes future bugs impossible to ship.

## State of the union (verified, not vibes)

- Suite: ~3,130 offline tests + live/hardware tiers; CI green on python 3.12;
  production venv is 3.11; local dev 3.14 — three-version matrix already
  caught a real pathlib-raising bug in production code.
- Gates: pre-commit (fast) + pre-push (full) hooks; ruff; shellcheck (advisory
  until toolchain repair); swiftc -parse + direct swift logic harness; GitHub
  Actions ci/release/nightly.
- Shipped parity: USB multi-MCU (two Picos proven, one instance), firmware
  flash/build (`flash uf2|rp2040|probe|build-rp2040` — real UF2s flashed),
  backups, launchd login + supervise, batteryguard, power-loss journal + live
  kill suite, camera recording with mic, doctor/selftest per-MCU + firmware
  consistency, USB identity binding surviving renames, RT telemetry (0 timer
  errors with dual boards).

## P1 — Known product tickets (small, do next)

1. **flash uf2 success criterion**: "volume disappeared" is not proof the app
   booted; wait for the runtime device (VID:PID 1d50:614e, same USB LOC) to
   reappear within N seconds; on bounce-back-to-BOOTSEL, say so honestly with
   the INFO_UF2.TXT readback. Repro exists: this session's first flashes.
2. **GUI dark-mode verification round**: FlatIcons palette contrast in dark
   (audit measured 1.16:1 selected icons), MessageBanner material backdrop,
   red destructive button 2.17:1. Fix contrast, screenshot-prove both
   appearances, keep light look byte-identical.
3. **Perf re-measure round** (audit baseline exists): idle CPU%, requests per
   minute, per-tick `ps` spawn (StackReader), status now cached — verify GUI
   poll path got the same speedup; CLIRunner deadlines; proxy HTTP/1.1
   keep-alive decision (deliberate 1.0 today — measure, then decide).
4. **Dual-homing caveat doc + guard**: mcus.validate should WARN when an axis
   endstop and its steppers sit on different MCUs (25 ms trsync budget,
   docs/Multi_MCU_Homing.md) — the user's 4+4 scenario is exactly this trap.

## P2 — Parse-the-app deep review (in flight)

Adversarial per-module review + fuzz-style edge hunting on surfaces the first
hunts never attacked: proxy websocket relay state machine (post-rewrite), cam
recorder lifecycle (SIGTERM/TCC-denied/disk-full), supervise/launchd restart
semantics under `down`, backup restore vs running config + symlink games,
identity/ioreg parser hostile fixtures, **TUI (never attacked at all)**,
install.sh cold-run in fresh HOME (never run end-to-end offline), uninstall
round-trips, configgen vs hand-mangled multi-MCU configs.

## P3 — Tooling upgrades (the "like Rust won't let it compile" ask)

- Add **bandit** (security lint) and **pyright basic** (type check) as gate
  tiers with committed baselines; ruff rule-set expansion (C90 complexity,
  RUF, SIM) as advisory-then-blocking.
- swiftlint baseline tier when the Apple toolchain is repaired.
- Mutation testing (mutmut) on the two pure-criticality kernels only:
  mcus.py and configgen path-sync — proves the tests actually bite.
- CI matrix: add python 3.11 (production interpreter) alongside 3.12.
- Fix this Mac: `sudo rm -rf /Library/Developer/CommandLineTools &&
  xcode-select --install` (unblocks swift test/build + brew + shellcheck
  blocking) — user action, everything else degrades gracefully without it.

## P4 — Pi-better features (only after P1–P3 are green)

- One-command "import from Pi": tarball of ~/printer_data/config + gcodes ->
  presets + path rewrite + diff report (migration path is what actually
  beats a Pi image).
- Print-session evidence bundle: klippy.log tail + timing telemetry + power
  state + config hash -> single zip for bug reports ("attach log" button).
- Doctor history: last N doctor runs kept (data/logs/doctor-history.jsonl) so
  "it was fine yesterday" becomes checkable.
- Auto-reconnect with identity: on serial loss during print, re-find the
  board by USB serial (not path) and firmware_restart — the identity layer
  already exists; this is the trust payoff.
- Deferred, keep on radar: moonraker-timelapse integration (feasibility
  proven), YOLO spaghetti detection (mac NPU > Pi), multi-instance
  (explicitly parked by user).

## Non-negotiables (every PR)

upstream pristine (doctor proves), every bug ships with its repro test,
qa-fast at commit / qa-full at push / CI green, taskboard.md updated per run.
