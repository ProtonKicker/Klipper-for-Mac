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
- [x] Added a clearer degraded-state cue in the GUI when klippy is in `startup` / `error` / `shutdown` instead of just "running"
- [x] Added a stack-level printer warning so the top of the dashboard also reflects non-ready klippy states, not only the printer pane
- [x] Added a live firmware-version agreement test that compares `/printer/info` against a direct `flash probe` handshake and waits for Moonraker to return after probe-driven restarts
- [x] Added a doctor/selftest firmware-consistency check that compares the live MCU's reported firmware revision against the installed Klipper checkout revision
- [x] Hardened `detect.all_identities()` / `flash.identity_rows()` against the post-restart window where `/dev/cu.usbmodem*` is already back but the first `ioreg` pass still returns no usable USB identity rows
- [x] Expanded the direct Swift logic harness and future SwiftPM tests around degraded Moonraker payloads, shutdown/startup readiness notes, and transient serial CLI noise
- [x] Surfaced live firmware provenance from `klippy.log` in `doctor` / `selftest` and added a live check that the direct probe's build string matches klippy's latest `Loaded MCU` record
- [x] Extracted the app's serial-probe keep/legacy/report policy into a pure Swift helper and covered it in both the direct harness and future SwiftPM tests
- [x] Fixed Swift `StackReader` to match Python's `boot_time: "unknown"` semantics and added direct/future Swift coverage for tracked-versus-stale stack records
- [x] Extracted toast/message shaping into a pure Swift helper and hardened blank/whitespace camera feedback so success toasts do not disappear
- [x] Extracted banner severity/title and status-badge text/tone into a pure Swift presentation helper and covered them in the direct harness plus future SwiftPM tests
- [x] Extracted warning-row and progress-label presentation into the pure Swift feedback layer so blank warnings disappear and out-of-range progress labels stay aligned with the clamped progress bar
- [x] Extracted per-MCU row footer/picker presentation into the core MCU model so the dashboard's CAN/fixed, bad-section, and picker-help rules are testable without SwiftUI
- [x] Extracted per-MCU badge and identity presentation into the core row model so timing-master/conflict pills and presence text are testable without SwiftUI
- [x] Fixed a SwiftUI result-builder compatibility failure in `Components.swift` so `make build-gui` works again on this Mac
- [x] Extracted camera-state text, placeholder text, and device-list parsing into a pure Swift helper so camera guidance is testable without SwiftUI or AVFoundation
- [x] Hardened the camera list parser to accept only tabular `cam list` device rows and ignore mixed guidance/noise lines
- [x] Added a dedicated `swift_future_tests.sh` guard and wired it into `qa-fast` so future SwiftPM tests keep parsing cleanly even while `swift test` is blocked on this Mac
- [x] Expanded future SwiftPM coverage for generic `serial --json` usage noise, partial Moonraker object trees, and the `error` readiness state
- [x] Added a dedicated `swift_package_tests.sh` probe and wired it into `qa-full` so real `swift test` runs automatically when the Apple toolchain is ready and otherwise reports a precise blocked reason
- [x] Added a `make test-swiftpkg` entry point so the local Swift package-test/toolchain state can be checked directly
- [x] Added a shared `fast_pytest.sh` helper plus `fast_profile.sh` / `make test-fast-profile` so fast-tier timing drift can be profiled without rebuilding the selector by hand
- [x] Updated the fast-tier over-budget warning to point at the new profiling tool instead of a manual pytest recipe
- [x] Added a shared proxy test helper that starts throwaway socketservers with a short poll interval so fast-tier proxy regressions no longer spend ~0.5s each in idle shutdown waits
- [x] Fixed `update --pin` so invalid refs are rejected before they are written to lockfile.json, keeping junk pins from persisting into later setup/update runs
- [x] Made the proxy server explicitly quiet at the `socketserver` layer so dead upstreams and bad clients no longer dump background worker tracebacks into otherwise-green QA runs
- [x] Extracted the MCU row's read-only status help text and presence-dot presentation into the core row model so more of the remaining SwiftUI-only rendering surface is testable without the view
- [x] Extracted the camera pane's status-dot styling into `CameraFeedback` so the on/off/unknown indicator presentation is now model-driven end to end
- [x] Extracted the settings pane's component pin text formatting into a pure helper so the pinned-tag / resolved-sha display is testable without the view
- [x] Hardened `doctor`, `selftest`, and `timing` for the live-but-untracked case where Moonraker is still answering from our ports even though `process.running()` returns no tracked stack
- [x] Made `status` report `running (untracked)` instead of `stopped` when this install's Moonraker/UI ports are still answering without a tracked state record, and added a regression for that CLI surface
- [x] Fixed the remaining powerloss advisory seam so `status`, `doctor`, and `powerloss status` stop surfacing stale `LAST STOP WAS UNCLEAN` history while the current session is still live-but-untracked
- [x] Added the same untracked-live listener fallback to the Swift stack reader so the GUI no longer treats an answering Moonraker/UI stack as fully stopped just because `state.json` disappeared
- [x] Fixed the GUI serial-device control so it no longer snaps back to a blank `Select device...` placeholder after a successful write, shows the current/default device in the button label, and defaults a sole detected USB device without breaking multi-MCU row fan-out

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

21. `KlipperMac/Sources/KlipperMac/Views/DashboardView.swift`
   - show an explicit warning note when the printer pane sees `startup`, `error`, or other non-ready klippy states
   - stop making a merely-running stack read as a healthy printer

22. `KlipperMac/Sources/KlipperMac/Core/Moonraker.swift`
   - add a stack-level degraded summary (`stackWarning`) derived from the parsed klippy state
   - keep the app logic for "printer not ready" in one place instead of duplicating string rules in the view

23. `klipperformac/cli.py`
   - read the installed Klipper checkout's short git revision in a bounded, read-only way
   - compare each loaded MCU firmware version against that installed checkout in `doctor` and `selftest`
   - turn a live firmware/checkout mismatch into an explicit doctor failure instead of a manual log-reading task

24. `klipperformac/detect.py`
   - retry one empty `ioreg` identity pass within the original timeout budget instead of surfacing a transient `[]`
   - preserve the fast happy path and the 3 s cap while recovering the real post-`FIRMWARE_RESTART` race the bench showed

25. `KlipperMac/TestsLogic/SwiftLogicChecks.swift`
   - add direct logic checks for warning-preamble JSON decode, non-legacy generic usage noise, broken `/server/info`, malformed optional Moonraker fields, and `shutdown` readiness notes

26. `KlipperMac/Tests/KlipperMacTests/MoonrakerStateTests.swift`
   - mirror the direct harness coverage into future SwiftPM tests for Moonraker parsing, degraded printer-state notes, and `StackSnapshot.overall`

27. `klipperformac/cli.py`
   - parse the latest `Loaded MCU '...' (...)` lines out of `klippy.log`
   - show the loaded MCU build provenance in `doctor` and `selftest`
   - fail `doctor` if klippy's runtime MCU version disagrees with klippy.log's latest loaded-MCU record while the printer claims ready

28. `KlipperMac/Sources/KlipperMac/Core/McuSerial.swift`
   - extract `SerialProbePolicy` so the app's keep/legacy/report decision is testable without running the whole app model
   - keep `AppModel` behavior unchanged while making the policy explicit and reusable

29. `KlipperMac/Sources/KlipperMac/Core/StackReader.swift`
   - extract pure snapshot-building helpers so the tracked/stopped decision is testable without hitting `ps` or `sysctl`
   - treat `boot_time: "unknown"` the same way Python `process._load()` does, instead of silently reading a valid record as stopped

30. `KlipperMac/Sources/KlipperMac/Core/AppFeedback.swift`
   - extract banner/toast shaping into a pure helper so message behavior is testable without the whole app model
   - make blank snapshot output fall back to `Saved snapshot` instead of producing no visible toast

31. `KlipperMac/Sources/KlipperMac/Core/AppFeedback.swift`
   - extract banner title/severity and status-badge text/tone mapping into a pure helper so UI presentation decisions are testable without a SwiftUI harness

32. `KlipperMac/Sources/KlipperMac/Core/AppFeedback.swift`
   - extract inline warning-row and progress-label presentation so the dashboard can clamp percentage labels and suppress blank warning rows consistently

33. `KlipperMac/Sources/KlipperMac/Core/McuSerial.swift`
   - extract per-row picker label, picker help, and footer presentation so the dashboard can render MCU row guidance from model policy instead of hard-coded view strings

34. `KlipperMac/Sources/KlipperMac/Core/McuSerial.swift`
   - extract per-row badge presentation so timing-master and conflict pills come from pure model data instead of SwiftUI conditionals
   - extract identity presence presentation so the row model owns the text plus present/absent signal the dashboard renders

35. `KlipperMac/Sources/KlipperMac/Views/Components.swift`
   - move badge/banner presentation locals out of SwiftUI result-builder bodies so older local toolchains compile the GUI cleanly again

36. `KlipperMac/Sources/KlipperMac/Core/CameraFeedback.swift`
   - extract camera state and placeholder presentation so the camera pane's on/off/unknown guidance is testable without SwiftUI
   - parse `cam list` output strictly by the helper's indexed tabular contract instead of guessing by sentence punctuation
   - keep a saved-but-unplugged camera visible in the picker through a pure device-list presentation helper

37. `scripts/qa/swift_future_tests.sh`
   - parse every future SwiftPM test file together with the app sources so package-side tests still get exercised on machines where `swift test` cannot run

38. `scripts/qa/fast.sh`
   - run the new future-test parse guard as part of the normal Swift step so direct-harness and future-test coverage stay in lockstep

39. `scripts/qa/swift_package_tests.sh`
   - probe whether this Mac can really run `swift test --package-path KlipperMac`
   - print the active developer dir, the missing platform-path failure, and whether a full `Xcode*.app` exists under `/Applications`
   - exit with a distinct blocked status so QA can treat toolchain absence as an explicit advisory state instead of a silent omission

40. `scripts/qa/full.sh`
   - add a `swiftpkg` tier that runs real Swift package tests when possible
   - skip with a precise notice by default on CLT-only Macs, and fail instead when `QA_REQUIRE_SWIFTPM=1`

41. `Makefile`
   - add `make test-swiftpkg` as a thin wrapper around the new Swift package-test QA tier

42. `scripts/qa/fast_pytest.sh`
   - factor the fast offline pytest selector into a reusable helper so `qa-fast` and profiling tools cannot drift apart

43. `scripts/qa/fast_profile.sh`
   - add a dedicated profiler for the fast pytest tier with configurable `--durations` output

44. `scripts/qa/fast.sh`
   - run the shared fast pytest helper instead of duplicating the selector
   - point over-budget warnings at `make test-fast-profile` so slow-test diagnosis is a first-class workflow

45. `tests/proxy_testkit.py`
   - add a shared test-only server launcher that uses a short `serve_forever()` poll interval for throwaway proxy harnesses
   - keep the runtime proxy defaults unchanged while removing socketserver shutdown latency from the fast-tier regressions

46. `klipperformac/cli.py`
   - validate each `update --pin` value with `installer._validate_ref(...)` before saving pins, so a rejected ref never persists into lockfile.json

47. `klipperformac/proxy.py`
   - override `Server.handle_error(...)` to keep the proxy quiet when a background worker hits an unhandled socketserver-level error
   - match the product's existing "no access/error log spam" posture and the shipped runtime where the proxy stdio is on DEVNULL

48. `KlipperMac/Sources/KlipperMac/Core/McuSerial.swift`
   - extract read-only picker status presentation so the `CAN / fixed` label and its help text come from pure row policy instead of a hard-coded SwiftUI string
   - extract identity indicator presentation so the presence-dot visibility/tone/opacity are testable without the view layer

49. `KlipperMac/Sources/KlipperMac/Core/CameraFeedback.swift`
   - extend camera state presentation to include indicator tone/opacity so the Camera pane's dot styling is no longer view-owned

50. `KlipperMac/Sources/KlipperMac/Core/SettingsPresentation.swift`
   - extract component pin text formatting so the Settings pane's tag/sha display is no longer view-owned

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
  - live firmware-version agreement check between `/printer/info` and the direct serial `flash probe` handshake

- `tests/flash/test_flash_detect.py`
  - dedupe a single BOOTSEL volume plus a single BOOTSEL USB device into one target
  - keep ambiguous multi-BOOTSEL cases unmerged

- `tests/climcu/test_live_mcu_probe.py`
  - reload the real install for live per-MCU probing instead of inheriting stale module state
  - live master-MCU check that the board's firmware matches the installed Klipper checkout revision

- `tests/identity/test_identity_parsing.py`
  - retry-once regression for the post-restart empty-then-present `ioreg` identity shape

- `KlipperMac/Tests/KlipperMacTests/MoonrakerStateTests.swift`
  - future SwiftPM coverage for Moonraker parsing, malformed optional fields, degraded readiness notes, and stack-overall state

- `tests/flash/test_flash_live_readonly.py`
  - live build-provenance check: direct `flash probe` build string matches klippy.log's latest `Loaded MCU` record

- `KlipperMac/Tests/KlipperMacTests/SerialReportDecoderTests.swift`
  - future SwiftPM coverage for serial probe policy classification (`report`, `legacy`, `keep`)

- `KlipperMac/Tests/KlipperMacTests/MoonrakerStateTests.swift`
  - future SwiftPM coverage for `StackReader.snapshot(...)` on `boot_time: "unknown"` and foreign-boot records

- `KlipperMac/Tests/KlipperMacTests/AppFeedbackTests.swift`
  - future SwiftPM coverage for banner trimming/truncation, first meaningful line selection, and camera toast fallbacks
  - future SwiftPM coverage for banner title/severity and status-badge text/tone mapping
  - future SwiftPM coverage for inline warning trimming and clamped progress label presentation

- `KlipperMac/Tests/KlipperMacTests/SerialReportDecoderTests.swift`
  - future SwiftPM coverage for per-MCU row picker/footer presentation policy

- `KlipperMac/Tests/KlipperMacTests/SerialReportDecoderTests.swift`
  - future SwiftPM coverage for per-MCU master/conflict badge presentation and identity presence presentation

- `KlipperMac/Tests/KlipperMacTests/CameraFeedbackTests.swift`
  - future SwiftPM coverage for camera state presentation, placeholder guidance, strict device-list parsing, and saved-device picker preservation

- `KlipperMac/Tests/KlipperMacTests/SerialReportDecoderTests.swift`
  - future SwiftPM coverage for generic usage noise that should stay non-legacy when `--json` was not actually rejected

- `KlipperMac/Tests/KlipperMacTests/MoonrakerStateTests.swift`
  - future SwiftPM coverage for partial query payloads without optional objects and for the `error` readiness-note state

- `KlipperMac/TestsLogic/SwiftLogicChecks.swift`
  - serial JSON fixture + legacy detection checks
  - Moonraker parser checks for `virtual_sdcard` progress and partial payloads
  - stack snapshot overall-state checks
  - stack-level degraded summary checks for startup, error, and connected-but-not-ready states
  - printer degraded-state note checks for `startup`, `error`, and non-ready connected states

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
- focused MCU runtime/checkout regressions:
  - `12 passed` in `tests/climcu/test_mcu_runtime_report.py`
- focused identity regressions:
  - `37 passed, 8 deselected` across `tests/identity/test_identity_parsing.py`, `tests/identity/test_identity_live_probe.py`, and `tests/flash/test_flash_live_readonly.py`
- focused Swift logic verification:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - `swiftc -parse KlipperMac/Sources/KlipperMac/Core/Moonraker.swift KlipperMac/TestsLogic/SwiftLogicChecks.swift` -> PASS
  - `swiftc -parse KlipperMac/Tests/KlipperMacTests/MoonrakerStateTests.swift` -> PASS
- focused firmware provenance regressions:
  - `15 passed` in `tests/climcu/test_mcu_runtime_report.py`
  - `7 passed, 1 skipped` in `tests/flash/test_flash_live_readonly.py`
- focused app-model serial policy verification:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - `swiftc -parse KlipperMac/Sources/KlipperMac/Core/McuSerial.swift KlipperMac/Sources/KlipperMac/Model/AppModel.swift KlipperMac/TestsLogic/SwiftLogicChecks.swift` -> PASS
  - `swiftc -parse KlipperMac/Tests/KlipperMacTests/SerialReportDecoderTests.swift` -> PASS
- focused StackReader parity verification:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - `swiftc -parse KlipperMac/Sources/KlipperMac/Core/StackReader.swift KlipperMac/TestsLogic/SwiftLogicChecks.swift KlipperMac/Tests/KlipperMacTests/MoonrakerStateTests.swift` -> PASS
- focused app feedback verification:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - `swiftc -parse KlipperMac/Sources/KlipperMac/Core/AppFeedback.swift KlipperMac/Sources/KlipperMac/Model/AppModel.swift KlipperMac/TestsLogic/SwiftLogicChecks.swift KlipperMac/Tests/KlipperMacTests/AppFeedbackTests.swift` -> PASS
- focused banner/status presentation verification:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - `swiftc -parse KlipperMac/Sources/KlipperMac/Core/AppFeedback.swift KlipperMac/Sources/KlipperMac/Views/Components.swift KlipperMac/Sources/KlipperMac/Model/AppModel.swift KlipperMac/TestsLogic/SwiftLogicChecks.swift KlipperMac/Tests/KlipperMacTests/AppFeedbackTests.swift` -> PASS
- focused warning/progress presentation verification:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - `swiftc -parse KlipperMac/Sources/KlipperMac/Core/AppFeedback.swift KlipperMac/Sources/KlipperMac/Views/DashboardView.swift KlipperMac/Sources/KlipperMac/Views/Components.swift KlipperMac/TestsLogic/SwiftLogicChecks.swift KlipperMac/Tests/KlipperMacTests/AppFeedbackTests.swift` -> PASS
- focused MCU row presentation verification:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - `swiftc -parse KlipperMac/Sources/KlipperMac/Core/AppFeedback.swift KlipperMac/Sources/KlipperMac/Core/McuSerial.swift KlipperMac/Sources/KlipperMac/Views/DashboardView.swift KlipperMac/TestsLogic/SwiftLogicChecks.swift KlipperMac/Tests/KlipperMacTests/SerialReportDecoderTests.swift` -> PASS
- latest row-presentation + GUI compatibility rerun:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - `make build-gui` -> PASS
  - `make qa-fast` -> PASS (`161 passed`)
- latest camera feedback + parser hardening rerun:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - `make build-gui` -> PASS
  - `make qa-fast` -> PASS (`161 passed`)
  - `swiftc -parse KlipperMac/Sources/KlipperMac/Core/CameraFeedback.swift KlipperMac/Sources/KlipperMac/Model/AppModel.swift KlipperMac/Sources/KlipperMac/Views/CameraView.swift KlipperMac/Tests/KlipperMacTests/CameraFeedbackTests.swift` -> PASS
- latest future-test QA guard rerun:
  - `sh scripts/qa/swift_future_tests.sh` -> PASS
  - `make qa-fast` -> PASS (`161 passed`)
  - `qa-fast` Swift step now reports both `OK direct swift logic checks` and `OK future SwiftPM tests parse`
- latest Swift package-test probe rerun:
  - `sh scripts/qa/swift_package_tests.sh` -> blocked with:
    - active developer dir `/Library/Developer/CommandLineTools`
    - no `Xcode*.app` under `/Applications`
    - `xcrun --sdk macosx --show-sdk-platform-path` cannot resolve `PlatformPath`
  - `make qa-full` -> PASS
  - `qa-full` now includes a `swift package tests` tier and reports the blocked reason explicitly on this Mac
- latest fast-tier profiling rerun:
  - `make qa-fast` -> PASS
  - `make test-fast-profile` -> PASS
  - after the proxy harness speedup, the previous 0.5 s proxy teardowns dropped out of the slowest list
  - the current slowest fast-tier cost is now `tests/regress/test_v_02_update_pin_validation.py::test_junk_pin_leaves_that_component_exactly_as_it_was[shell-junk]` at about 2.0 s, followed by preset/config regressions
  - `make qa-full` -> PASS
- latest update-pin persistence fix rerun:
  - `tests/regress/test_v_02_update_pin_validation.py` -> PASS
  - `make qa-fast` -> PASS
  - `make qa-full` -> PASS
  - invalid `update --pin` values now leave lockfile.json on the previous good ref instead of persisting junk
- latest proxy quietness rerun:
  - targeted proxy regression checks -> PASS
  - `make qa-fast` -> PASS
  - `make qa-full` -> PASS
  - the full offline suite now stays green without the earlier background proxy traceback spray
  - `make qa-full` now passes at `3127 passed, 29 deselected`
- latest MCU row rendering-policy rerun:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - `make build-gui` -> PASS
  - `make qa-fast` -> PASS
  - the remaining row view now reads the indicator/help presentation from the model instead of owning those user-facing rules itself
- latest camera indicator presentation rerun:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - `make build-gui` -> PASS
  - `make qa-fast` -> PASS
  - the Camera pane now reads both status text and indicator styling from `CameraFeedback`
- latest settings presentation rerun:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - `make build-gui` -> PASS
  - `make qa-fast` -> PASS
  - the Settings pane now reads component pin text from `SettingsPresentation`
- focused powerloss regressions:
  - `54 passed` across `tests/powerloss/test_ready_precondition.py`, `tests/powerloss/test_journal.py`, and `tests/powerloss/test_kill_sequence_machine.py`
  - `2 passed` in `tests/powerloss/test_live_kill.py` with the live marker enabled
- live flash readonly tier:
  - `7 passed` in `tests/flash/test_flash_live_readonly.py`
- focused flash detect/probe/live regressions:
  - `39 passed, 6 deselected` across `tests/flash/test_flash_detect.py`, `tests/flash/test_flash_probe.py`, and `tests/flash/test_flash_live_readonly.py`
- live per-MCU probe tier:
  - `4 passed` in `tests/climcu/test_live_mcu_probe.py`
- direct Swift logic harness:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
- app-side degraded-state cue:
  - printer pane now shows a warning note for non-ready klippy states instead of only `connected/not connected (...)`
- stack-level degraded-state cue:
  - the stack section now warns when the printer is not actually ready, even if the services are technically running
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
  - `KLIPPERFORMAC_ALLOW_LIVE=1 make test-live` -> `27 passed`
- firmware consistency now surfaces directly in the CLI:
  - `./bin/klipperformac doctor` -> `[ok] mcu [mcu]: firmware matches installed klipper 461c4e3`
  - `./bin/klipperformac selftest` -> `[ok] mcu [mcu]: firmware matches installed klipper 461c4e3`
- latest broad reruns after the firmware-consistency change:
  - `make qa-full` -> `3122 passed, 28 deselected`
  - `KLIPPERFORMAC_ALLOW_LIVE=1 make test-live` -> `27 passed, 1 skipped`
  - skipped live case was traced to a real post-`FIRMWARE_RESTART` race:
    the first poll saw `scan()` still list `/dev/cu.usbmodem142101` while
    `detect.all_identities()` returned `[]`, and the next poll 0.5 s later
    returned the expected RP2040 identity row
- after the identity retry hardening:
  - `make qa-full` -> `3123 passed, 28 deselected`
  - `KLIPPERFORMAC_ALLOW_LIVE=1 make test-live` -> `28 passed`
  - `./bin/klipperformac doctor` -> PASS
  - `./bin/klipperformac selftest` -> PASS
- after the Swift degraded-state coverage expansion:
  - `make qa-fast` -> PASS
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - `swift test --package-path KlipperMac` is still blocked because this Mac only has Command Line Tools active and no `Xcode*.app` is installed under `/Applications`
- after the firmware provenance pass:
  - `./bin/klipperformac doctor` -> `[ok] mcu [mcu]: klippy.log last loaded 461c4e3 (gcc: Arm GNU Toolchain 14.2...)`
  - `./bin/klipperformac selftest` -> same loaded-MCU provenance line
  - `make qa-full` -> `3126 passed, 29 deselected`
  - `KLIPPERFORMAC_ALLOW_LIVE=1 make test-live` -> `29 passed`
- after the serial probe policy extraction:
  - `make qa-fast` -> PASS
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - future SwiftPM policy tests parse cleanly, but `swift test --package-path KlipperMac` remains blocked by the missing full Xcode install on this Mac
- after the StackReader parity fix:
  - `make qa-fast` -> PASS
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - future SwiftPM StackReader tests parse cleanly, but `swift test --package-path KlipperMac` is still blocked by the machine toolchain state
- after the app feedback extraction:
  - `make qa-fast` -> PASS
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - future SwiftPM AppFeedback tests parse cleanly, but `swift test --package-path KlipperMac` is still blocked by the machine toolchain state
- after the banner/status presentation extraction:
  - `make qa-fast` -> PASS
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - future SwiftPM presentation tests parse cleanly, but `swift test --package-path KlipperMac` is still blocked by the machine toolchain state
- after the warning/progress presentation extraction:
  - `make qa-fast` -> PASS
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - future SwiftPM warning/progress tests parse cleanly, but `swift test --package-path KlipperMac` is still blocked by the machine toolchain state
- after the MCU row presentation extraction:
  - `make qa-fast` -> PASS
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - future SwiftPM MCU row presentation tests parse cleanly, but `swift test --package-path KlipperMac` is still blocked by the machine toolchain state
- `swiftc -parse` on the touched Swift files -> PASS
- `swift test` is currently blocked on this Mac by the local Command Line Tools install:
  - `xcrun --sdk macosx --show-sdk-platform-path` fails under `/Library/Developer/CommandLineTools`
- `make qa-fast` now runs direct Swift parser/state checks through `scripts/qa/swift_logic.sh`
- after the untracked-stack detection pass:
  - `env PYTHONPATH=. .venv-test/bin/python -m pytest -q tests/rt/test_rt_timing_cli.py::test_doctor_reports_an_untracked_but_answering_stack` -> `1 passed`
  - `env PYTHONPATH=. .venv-test/bin/python -m pytest -q tests/deep/test_clmisc_selftest_logs.py::test_selftest_does_not_warn_when_moonraker_is_answering_untracked` -> `1 passed`
  - `env PYTHONPATH=. .venv-test/bin/python -m pytest -q tests/deep/test_clmisc_data_ui.py::test_status_with_nothing_installed_says_stopped tests/deep/test_clmisc_data_ui.py::test_status_reports_an_untracked_but_answering_stack` -> `2 passed`
  - `KLIPPERFORMAC_ALLOW_LIVE=1 env PYTHONPATH=. .venv-test/bin/python -m pytest -q tests/climcu/test_live_mcu_probe.py -m "live or hardware"` -> `5 passed`
  - `make qa-fast` -> PASS
  - push hook `scripts/qa/full.sh` -> `3130 passed, 30 deselected`
  - `./bin/klipperformac doctor` -> PASS with `[i]  stack is not tracked, but Moonraker is answering on port 7125`
  - `./bin/klipperformac selftest` -> `SELFTEST PASSED`
  - `./bin/klipperformac status` -> `running (untracked)`
- after the untracked-live powerloss advisory pass:
  - `env PYTHONPATH=. .venv-test/bin/python -m pytest -q tests/powerloss/test_cli_powerloss.py::test_status_hides_stale_unclean_history_when_untracked_ports_are_live tests/powerloss/test_cli_powerloss.py::test_doctor_hides_stale_unclean_history_when_moonraker_is_live_untracked tests/powerloss/test_cli_powerloss.py::test_powerloss_status_hides_stale_unclean_history_when_stack_is_live` -> `3 passed`
  - `env PYTHONPATH=. .venv-test/bin/python -m pytest -q tests/powerloss/test_cli_powerloss.py` -> `24 passed`
  - `make qa-fast` -> PASS
  - `./bin/klipperformac status` -> `running (untracked)` with no stale `LAST STOP WAS UNCLEAN`
  - `./bin/klipperformac doctor` -> PASS with `powerloss: this session is open and will be judged by the next \`up\`/\`status\``
  - `./bin/klipperformac powerloss status` -> no stale unclean-stop warning while the live untracked session is still up
- after the Swift listener-fallback parity pass:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - `sh scripts/qa/swift_future_tests.sh` -> PASS
  - `make build-gui` -> PASS
  - `make qa-fast` -> PASS
  - push hook `scripts/qa/full.sh` -> `3133 passed, 30 deselected`
  - the Swift stack reader now treats this install's own listening Moonraker/UI ports as running evidence, keeping GUI status/polling/update gating aligned with the CLI's untracked-live behavior
- after the GUI serial-picker/default-selection pass:
  - `sh scripts/qa/swift_logic.sh` -> `swift logic checks passed`
  - `sh scripts/qa/swift_future_tests.sh` -> PASS
  - `make build-gui` -> PASS
  - `make qa-fast` -> PASS (one run warned at 27s; immediate profile rerun below showed no persistent fast-tier regression)
  - `make test-fast-profile` -> `161 passed in 13.40s`
  - push hook `scripts/qa/full.sh` -> `3133 passed, 30 deselected`
  - the single-board and per-MCU serial controls now use action menus that keep the current/default device visible instead of resetting to an empty placeholder
  - per-MCU rows only default the sole detected USB device when there is only one pickable MCU row, so the GUI still respects multi-MCU fan-out instead of implying one cable can satisfy every row

## Live Tier Status

Latest `KLIPPERFORMAC_ALLOW_LIVE=1 make test-live` result:

- 29 passed

## Live Bench Notes

Current best read:

- the bench board now answers a direct Klipper handshake again
- the stack is back to `klippy ready`
- the remaining live work is now expansion/improvement work, not bench recovery
- the previously flaky post-restart USB identity gap is now covered by a bounded retry and no longer causes a live-tier skip
- the loaded-MCU version and build toolchain are now surfaced directly from `klippy.log`, not left as a manual checklist item

Evidence:

- `./bin/klipperformac flash probe --port /dev/cu.usbmodem142101 --json` now returns `ok: true`
- `./bin/klipperformac status` now shows `klippy ready`
- `./bin/klipperformac doctor` now passes with the stack up
- `KLIPPERFORMAC_ALLOW_LIVE=1 make test-live` now passes fully again
- `./bin/klipperformac doctor` and `selftest` now both print the latest `Loaded MCU` provenance line from `klippy.log`

## Next Suggested Moves

- [ ] Fix the local Apple toolchain / install full Xcode so `swift test` can run `KlipperMacTests`
- [ ] Expand the direct Swift harness and future `swift test` coverage into the remaining UI presentation case: layout-level rendering in SwiftUI (warning rows, pills, and badge spacing)
- [ ] Fix the local Apple toolchain / install full Xcode so `swift test` can run the new `KlipperMacTests` target on this Mac
- [ ] Expand the new Swift tests into `StackSnapshot` / `Moonraker.status()` degraded-state cases (`klippy startup`, partial object trees, transient `serial --json` noise)

## Ground Rules

- never edit upstream checkouts
- keep this file updated after each substantial run
- record exact commands and outcomes when the live bench state changes
