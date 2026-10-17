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
- `make qa-full` now passes at `3090 passed, 25 deselected`

## Live Tier Status

Latest `KLIPPERFORMAC_ALLOW_LIVE=1 make test-live` result:

- 16 passed
- 6 failed
- 3 skipped

Remaining live failures all share one bench-state problem:

- `klippy` remains in `startup`
- `/printer/info` never reaches `ready`
- `/printer/objects/query?mcu` returns `{}` for `mcu`
- `FIRMWARE_RESTART` does not recover the board to ready
- powerloss live tests fail because they require a ready stack first
- a direct port-level handshake now fails the same way even with the stack down
- a host-driven RP2040 bootloader request also times out, so no automated flash
  can proceed until the board is put into BOOTSEL physically

## Suspected Live Blocker

Current best read:

- the app-side USB identity bug is fixed
- the board is now identified correctly
- the remaining issue is likely bench firmware / live MCU readiness, not the
  serial identity layer

Evidence:

- `./bin/klipperformac doctor` still reports `klipper state: error`
- `./bin/klipperformac status` shows `klippy startup`
- `klippy.log` shows repeated `mcu 'mcu': Unable to connect`
- `./bin/klipperformac flash probe --port /dev/cu.usbmodem142101` times out
  waiting for `identify_response` even with klippy stopped
- `./bin/klipperformac flash rp2040 --bin ... --port /dev/cu.usbmodem142101`
  times out waiting for the bootloader, so the board does not honor the
  upstream 1200-baud handoff on this bench

## Next Suggested Moves

- [ ] Read current live `klippy.log` and confirm whether the SKR Pico is flashed with the expected Klipper firmware
- [ ] Run read-only flash detection (`klipperformac flash detect`) and capture the board's current runtime/bootloader posture
- [ ] Put the board into BOOTSEL physically, then run:
      - `klipperformac flash uf2 --bin ~/.klipperformac/firmware/klipper-rp2040-20261017T131241Z.uf2`
      - or `klipperformac flash rp2040 --bin ... --port ...` if a future board honors the handoff
- [ ] If the board becomes ready, re-run:
      - `./bin/klipperformac doctor`
      - `KLIPPERFORMAC_ALLOW_LIVE=1 make test-live`
- [ ] If more GUI robustness work is wanted, add a real Swift test target or UITest target for the multi-MCU pane and CLI timeout behavior

## Ground Rules

- never edit upstream checkouts
- keep this file updated after each substantial run
- record exact commands and outcomes when the live bench state changes
