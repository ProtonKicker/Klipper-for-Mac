# Taskboard

Shared board for this robustness-testing quest. Keep it current so future
agent/tool passes can continue without re-discovering the same state.

## Current Goal

Continue the robustness-testing loop from the saved transcript:

- test CLI, GUI, and live stack behavior
- fix confirmed bugs
- add regression coverage and reusable test seams
- re-run verification after each fix
- keep upstream pristine

## Completed This Run

- [x] Recovered the previous plan from
      `2026-10-17-003455-can-u-run-a-contnue-agentic-loop-to-test-the-robustness-of-this-app-search-online-for-how-to-test-those-kidns-of-apps-and-stop-untill-u-exachuasted-all-possible-appraoches-after-you-foud-ntheos-problems-fix-them-and-then-rerun-the-te.txt`
- [x] Reviewed current repo state, existing tests, and in-progress Swift work
- [x] Ran `make qa-fast`
- [x] Ran `make qa-full`
- [x] Built GUI with `make build-gui`
- [x] Added rollback hardening for partial `process.start()` / `process.repair()` failures
- [x] Added GUI hardening for multi-MCU rows so non-USB/CAN rows do not offer a USB picker
- [x] Fixed USB identity parsing so a board behind a hub keeps the board's own VID:PID/serial
- [x] Added regression tests for the new process, Swift seam, and USB identity cases
- [x] Re-ran targeted tests and full offline QA after fixes
- [x] Ran live tier before/after fixes to measure remaining failures
- [x] Added `klipperformac flash probe` for an explicit read-only board handshake while the stack is down
- [x] Proved the current bench board times out even under a direct read-only Klipper handshake
- [x] Installed/unpacked a full ARM embedded toolchain locally and built a fresh RP2040 `klipper.uf2`
- [x] Added `klipperformac flash rp2040` for macOS-native runtime-to-bootloader flashing attempts
- [x] Proved the current bench board does not enter bootloader on the host-driven 1200-baud request
- [x] Productized the macOS RP2040 firmware build path as `klipperformac flash build-rp2040`
- [x] Verified `flash build-rp2040` succeeds end-to-end on this Mac and caches a fresh UF2
- [x] Added offline regression coverage for `up`/`down`/`restart` lifecycle edge branches and `_wait_ready()` timeout diagnostics
- [x] Hardened `data --set` relocation so post-move failures roll back files, settings, and the previously running stack
- [x] Hardened config path sync so hand-edited but valid `[virtual_sdcard]` and `[server]` sections still get repaired safely
- [x] Made `flash detect` stop overstating USB-CDC runtime nodes and point directly at `flash probe` for a real handshake check
- [x] Made `selftest` warn cleanly when the stack is stopped before the connection failures scroll by
- [x] Added the first native Swift package test target for the serial JSON decoder / legacy-CLI distinction
- [x] Added `flash probe --json` plus identity-key coverage so bench automation can consume direct MCU handshake evidence
- [x] Fixed the powerloss live-test harness/fixture path so the live tier can actually talk to Moonraker inside `tests/powerloss/`
- [x] Recovered the live bench from "startup" to `klippy ready`, then re-ran doctor, selftest, and the full live tier
- [x] Added a real opt-in hardware test for `flash probe` that stops the stack, handshakes the bench board, and restores the stack
- [x] Deduped the common BOOTSEL double-report so one RP2040 does not show up twice as both a UF2 volume and a USB bootloader device
- [x] Fixed the per-MCU live probe tests to reload the real install, removing the last artificial skips from `make test-live`
- [x] Added a direct `swiftc` logic harness so GUI parser/state checks run in QA even while `swift test` is blocked on this Mac
- [x] Fixed the Swift Moonraker parser to read print progress from `virtual_sdcard`, which is where the live API actually serves it

## Landed Fixes

1. `klipperformac/process.py`
   - roll back partially spawned children if `start()` fails mid-transaction
   - roll back partially repaired children if `repair()` fails mid-transaction
   - avoid leaving a stale placeholder state record behind on failure

2. `KlipperMac/Sources/KlipperMac/Core/McuSerial.swift`
   - preserve raw malformed MCU names instead of trimming them into apparently valid ones
   - add `offersPortPicker` so CAN/non-USB rows stay visible but read-only

3. `KlipperMac/Sources/KlipperMac/Model/AppModel.swift`
   - refuse GUI writes to rows that are not USB-pickable, even if the view regresses later

4. `KlipperMac/Sources/KlipperMac/Views/DashboardView.swift`
   - hide the USB picker for CAN/fixed-identity rows and explain why

5. `klipperformac/detect.py`
   - choose the nearest USB device in the IOKit chain instead of the parent hub

6. `klipperformac/flash.py`
   - add `flash probe --port ...` to test a direct, read-only Klipper serial handshake
   - bound the probe with a clean timeout message instead of a hanging subprocess
   - add `flash rp2040 --bin ... --port ...` to request RP2040 bootloader mode from a live USB runtime device on macOS
   - support both picoboot USB and BOOTSEL volume follow-up paths without relying on Linux `/dev/serial/by-path`
   - add `flash build-rp2040` to build and cache RP2040/RP2350 UF2 firmware on macOS
   - auto-unpack a cached `gcc-arm-embedded` pkg into `~/.klipperformac/toolchains/`
   - generate macOS wrapper tools for `g++`, `cpp`, and `readelf` so upstream's build works unchanged

7. `klipperformac/cli.py`
   - make `relocate_data()` roll back if settings write, config sync, or restart fails after files moved
   - restore the old data pointer and previous settings before surfacing the failure

8. `klipperformac/configgen.py`
   - add a section-scoped key upsert helper instead of assuming canonical config layout
   - keep `sync_virtual_sdcard()` working when `path:` is not the first key or was edited away
   - keep `sync_moonraker_conf()` scoped to `[server]` so unrelated `klippy_uds_address:` keys survive unchanged
   - restore missing `[server]` keys instead of silently leaving a half-pinned Moonraker config behind

9. `klipperformac/flash.py`
   - make `flash detect` describe USB-CDC runtime nodes as a runtime hint, not as proof of a living Klipper handshake
   - point the user at `klipperformac flash probe --port ...` when the stack is down and the next question is whether `identify_response` works

10. `klipperformac/cli.py`
   - print an early `selftest` advisory when the stack appears stopped so a wall of `Connection refused` lines is less opaque
   - flush that advisory before spawning the child so the hint arrives before the failures it explains

11. `KlipperMac/Sources/KlipperMac/Core/McuSerial.swift`
   - add a `looksLikeLegacyCLI(...)` helper so the app only caches "legacy CLI" on an actual `--json` parser rejection
   - keep transient garbage / mixed stderr from permanently hiding the multi-MCU pane

12. `KlipperMac/Sources/KlipperMac/Model/AppModel.swift`
   - stop treating every undecodable `serial --json` output as proof the CLI is too old
   - keep the last honest multi-MCU report on transient probe noise instead of downgrading to the single-board fallback

13. `KlipperMac/Package.swift`
   - add the first Swift package test target (`KlipperMacTests`) so app-side decoder/runtime seams can be exercised natively

14. `klipperformac/powerloss.py`
   - fix `LiveHarness.wait_ready()` to read Moonraker's nested `/printer/info -> result.state` shape instead of looking for a top-level `state`

15. `tests/powerloss/conftest.py`
   - keep the offline `no_network` guard for the directory, but let `@pytest.mark.live` tests use real sockets

16. `klipperformac/flash.py`
   - merge the common one-volume/one-USB BOOTSEL case into one richer `flash detect` target instead of double-reporting the same RP2040
   - keep ambiguous multi-volume/multi-device cases separate rather than guessing across boards

17. `tests/climcu/test_live_mcu_probe.py`
   - reload `paths` and `cli` against the real `~/.klipperformac` install before each live test
   - stop depending on whichever env/module state happened to be imported first in the full live suite

18. `KlipperMac/Sources/KlipperMac/Core/Moonraker.swift`
   - factor the parser into a pure `status(info:query:)` helper so it can be checked without SwiftPM
   - query `virtual_sdcard` and read progress from there instead of from `print_stats`, matching the live Moonraker payload

19. `scripts/qa/swift_logic.sh`
   - compile and run a small direct Swift logic harness with `swiftc`
   - give this Mac a runnable Swift-side gate even though `swift test` is blocked by the local Command Line Tools install

20. `scripts/qa/fast.sh`
   - run the new direct Swift logic checks as part of the existing Swift step

## Tests Added

- `tests/deep/test_process_start_stop.py`
  - start rollback on later spawn failure
  - start rollback on final state-write failure
  - repair rollback on later spawn failure

- `tests/contract/test_write_path_seam.py`
  - Swift keeps raw malformed MCU names
  - CAN rows stay visible but do not get a USB picker

- `tests/identity/test_identity_parsing.py`
  - nested-hub regression: board identity wins over parent hub identity

- `tests/flash/test_flash_probe.py`
  - probe refuses while the stack is running
  - probe success/failure/non-JSON/timeout paths
  - flash help lists the new probe action

- `tests/flash/test_flash_rp2040.py`
  - rp2040 dry-run
  - timeout and volume delegation behavior
  - picoboot path
  - dispatch requires `--port`

- `tests/flash/test_flash_build_doctor.py`
  - local embedded toolchain detection
  - `build-rp2040` dry-run plan
  - real build command orchestration and artifact caching

- `tests/launchd/test_cli_wiring.py`
  - `up` half-stopped core refusal
  - `up` UI-only repair path and already-running path
  - `restart` call ordering
  - `down` lingering-listener failure path
  - `_wait_ready()` timeout diagnostics for squatters and silent listeners

- `tests/deep/test_clmisc_data_ui.py`
  - rollback on settings write failure
  - rollback on config sync failure
  - rollback on restart failure

- `tests/test_configgen_paths.py`
  - hand-edited `[virtual_sdcard]` still rewrites to the active data folder
  - missing `path:` is restored inside `[virtual_sdcard]`
  - only `[server].klippy_uds_address` is rewritten
  - missing `[server]` keys are restored during sync

- `tests/flash/test_flash_detect.py`
  - runtime serial nodes now direct the user to `flash probe --port ...` instead of claiming a live handshake

- `tests/deep/test_clmisc_selftest_logs.py`
  - stopped-stack `selftest` prints a clean advisory before the child failure output

- `KlipperMac/Tests/KlipperMacTests/SerialReportDecoderTests.swift`
  - pinned fixture decode
  - JSON-with-preamble decode
  - legacy CLI rejection detection
  - transient garbage stays non-legacy

- `tests/flash/test_flash_probe.py`
  - `flash probe --json` machine-readable output
  - `probe` accepts `usb:` identity keys
  - `probe` accepts `loc:` identity keys

- `tests/powerloss/test_ready_precondition.py`
  - `LiveHarness.wait_ready()` follows Moonraker's nested `/printer/info` payload

- `tests/flash/test_flash_live_readonly.py`
  - live `flash probe` handshake against the real bench board while the stack is temporarily down
  - stack restore in `finally` so the rest of the live tier still runs

- `tests/flash/test_flash_detect.py`
  - dedupe a single BOOTSEL volume plus a single BOOTSEL USB device into one target
  - keep ambiguous multi-BOOTSEL cases unmerged

- `tests/climcu/test_live_mcu_probe.py`
  - reload the real install for live per-MCU probing instead of inheriting stale module state

- `KlipperMac/TestsLogic/SwiftLogicChecks.swift`
  - serial JSON fixture + legacy detection checks
  - Moonraker parser checks for `virtual_sdcard` progress and partial payloads
  - stack snapshot overall-state checks

## Verification Snapshot

- `make qa-full` -> PASS
- `make build-gui` -> PASS
- `swiftc -parse ...` on touched Swift files -> PASS
- `./bin/klipperformac serial` now reports the live bench board as:
  - `VID:PID=1d50:614e`
  - `SER=5044506110A45B1C`
- `./bin/klipperformac flash probe --port /dev/cu.usbmodem142101`
  now gives a bounded direct result instead of hanging:
  - `MCU probe failed ... timed out after 20s`
- built firmware artifact:
  - [x] `~/.klipperformac/firmware/klipper-rp2040-20261017T131241Z.uf2`
  - SHA256 `cf1693d82e82eb1ae88d44befba6040db0e36480c1eb76588eaae94d12cefedb`
- built through product command:
  - [x] `klipperformac flash build-rp2040`
  - [x] `~/.klipperformac/firmware/klipper-rp2040-20261017T134142Z.uf2`
- `make qa-full` now passes at `3119 passed, 26 deselected`
- focused config/path regressions:
  - `107 passed` across `tests/test_configgen_lan.py`, `tests/test_configgen_webcam.py`, `tests/test_configgen_paths.py`, and `tests/deep/test_clmisc_data_ui.py`
  - `9 passed` in `tests/regress/test_i_02_generated_xheaders_pin.py`
- focused flash/selftest regressions:
  - `62 passed` across `tests/flash/test_flash_detect.py`, `tests/deep/test_clmisc_selftest_logs.py`, `tests/climcu/test_mcu_runtime_report.py`, and `tests/flash/test_flash_probe.py`
  - `30 passed` across the selftest-related deep/runtime suites after the output-order flush tweak
- focused probe identity/CLI regressions:
  - `54 passed` across `tests/flash/test_flash_probe.py`, `tests/flash/test_flash_cli.py`, and `tests/flash/test_flash_identity_keys.py`
- focused powerloss regressions:
  - `54 passed` across `tests/powerloss/test_ready_precondition.py`, `tests/powerloss/test_journal.py`, and `tests/powerloss/test_kill_sequence_machine.py`
  - `2 passed` in `tests/powerloss/test_live_kill.py` with the live marker enabled
- live flash readonly tier:
  - `6 passed` in `tests/flash/test_flash_live_readonly.py`
- focused flash detect/probe/live regressions:
  - `38 passed, 6 deselected` across `tests/flash/test_flash_detect.py`, `tests/flash/test_flash_probe.py`, and `tests/flash/test_flash_live_readonly.py`
- live per-MCU probe tier:
  - `3 passed` in `tests/climcu/test_live_mcu_probe.py`
- direct Swift logic harness:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
- `make qa-fast` -> PASS after the latest Python + Swift edits
- `./bin/klipperformac flash detect` now says:
  - `USB-CDC runtime device; use \`klipperformac flash probe --port /dev/cu.usbmodem142101\` to confirm identify_response`
- `./bin/klipperformac flash probe --port /dev/cu.usbmodem142101 --json` now reports:
  - `ok: true`
  - `mcu: rp2040`
  - `clock_freq: 12000000`
  - `version: 461c4e3`
- live Moonraker payload check:
  - `/printer/objects/query?print_stats&extruder&heater_bed&gcode_move` does not include `progress` under `print_stats`
  - the Swift app now asks for `virtual_sdcard` and parses progress from there
- `./bin/klipperformac selftest` with the stack down now starts with:
  - `[i]  stack appears stopped: selftest will report connection failures until \`klipperformac up\`.`
- `klipperformac flash doctor` now reports:
  - embedded toolchain path
  - cached cask package path
  - `flash build-rp2040` as the macOS build path
- `./bin/klipperformac doctor` now reports:
  - `[ok] upstream checkouts pristine`
  - `[ok] c_helper builds with compat shims`
  - `[i] host scheduling posture: klippy not running`
- with the stack up again:
  - `./bin/klipperformac status` -> `klippy ready`
  - `./bin/klipperformac doctor` -> PASS with `[ok] klipper state: ready`
  - `./bin/klipperformac selftest` -> PASS
  - `KLIPPERFORMAC_ALLOW_LIVE=1 make test-live` -> `26 passed`
- `swiftc -parse` on the touched Swift files -> PASS
- `swift test` is currently blocked on this Mac by the local Command Line Tools install:
  - `xcrun --sdk macosx --show-sdk-platform-path` fails under `/Library/Developer/CommandLineTools`
- `make qa-fast` now runs direct Swift parser/state checks through `scripts/qa/swift_logic.sh`

## Live Tier Status

Latest `KLIPPERFORMAC_ALLOW_LIVE=1 make test-live` result:

- 26 passed

## Live Bench Notes

Current best read:

- the bench board now answers a direct Klipper handshake again
- the stack is back to `klippy ready`
- the remaining live work is now expansion/improvement work, not bench recovery

Evidence:

- `./bin/klipperformac flash probe --port /dev/cu.usbmodem142101 --json` now returns `ok: true`
- `./bin/klipperformac status` now shows `klippy ready`
- `./bin/klipperformac doctor` now passes with the stack up
- `KLIPPERFORMAC_ALLOW_LIVE=1 make test-live` now passes

## Next Suggested Moves

- [ ] Read current live `klippy.log` and confirm whether the SKR Pico is flashed with the expected Klipper firmware
- [ ] Fix the local Apple toolchain / install full Xcode so `swift test` can run `KlipperMacTests`
- [ ] Expand the direct Swift harness and future `swift test` coverage into more degraded-state cases (`klippy startup`, partial object trees, transient CLI noise)
- [ ] Fix the local Apple toolchain / install full Xcode so `swift test` can run the new `KlipperMacTests` target on this Mac
- [ ] Expand the new Swift tests into `StackSnapshot` / `Moonraker.status()` degraded-state cases (`klippy startup`, partial object trees, transient `serial --json` noise)

## Ground Rules

- never edit upstream checkouts
- keep this file updated after each substantial run
- record exact commands and outcomes when the live bench state changes
