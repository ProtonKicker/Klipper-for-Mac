# Full Klipper Functionality Roadmap

## Current v1 limitations
- SHAPER_CALIBRATE requires manual Python 3.14 switch
- No CAN bus
- No host GPIO (MCU-less pins)
- No KlipperScreen
- No webcam

## Goal
Achieve feature parity with a Raspberry Pi 4 running MainsailOS, using
macOS-native capabilities where possible.

---

## Phase 1: SHAPER_CALIBRATE — zero-friction Python 3.14

### Problem
Currently: `KLIPPERFORMAC_PYTHON=…3.14 klipperformac setup` is a manual env var.
Users don't know they need it until resonance calibration fails.

### Spec
- Detect when `[input_shaper]` or `SHAPER_CALIBRATE` is referenced in printer.cfg
- Auto-select Python 3.14 if available (brew install python@3.14)
- If 3.14 missing, offer `klipperformac setup --with-shaper` → auto-brew-install
- Document clearly: 3.14 venv is shaper-only; main stack stays on 3.12
- `doctor` should warn: "shaper will fail until python@3.14 is installed"

### Done when
- `klipperformac setup` auto-detects shaper need
- 3.14 venv created alongside 3.12 venv
- shaper scripts invoke correct python
- doctor reports shaper readiness
- no upstream edits

---

## Phase 2: Webcam via AVFoundation (Mac Camera)

### Problem
Klipper/Moonraker expect MJPEG over HTTP. macOS has no native MJPEG server.
But AVFoundation gives full camera access at 1080p/30fps easily.

### Architecture
Mac Camera → AVFoundation → MJPEG encoder → local HTTP server (:8082) → Moonraker/web

### Spec
- Swift helper: `KlipperCam` — reads camera frames via AVFoundation
- MJPEG HTTP server (embedded, port :8082)
- Configurable: resolution, FPS, quality
- `klipperformac cam on|off|status`
- Moonraker config: `[webcam]` pointing to `http://localhost:8082/?action=stream`
- Supports Continuity Camera (iPhone) via AVFoundation automatically
- Supports external USB UVC cameras (most webcams)
- Timelapse support: `klipperformac cam snapshot` → saves frame to g-code folder
- `printer.cfg` macro hooks: `START_TIMELAPSE`, `STOP_TIMELAPSE`

### Constraints
- Do NOT edit Moonraker source
- Use Moonraker's standard `[webcam]` config pointing at localhost
- Camera must NOT prevent sleep (only when actively printing)
- Respect macOS camera privacy (user must grant permission on first launch)
- .app GUI should show camera preview

### Done when
- `cam on` starts stream, `cam off` stops
- Mainsail/Fluidd show camera feed
- Timelapse frames saved per-layer (macro-triggered)
- `doctor` checks camera permission + port availability
- Works with Continuity Camera (iPhone)

---

## Phase 3: Host GPIO (via Arduino/MCU as GPIO expander)

### Problem
macOS has no `/dev/gpiochip`. True host GPIO is impossible.
But users want: relays, LEDs, filament runout sensors, door switches.

### Solution
Use a cheap Arduino/ESP32/STM32 as a "GPIO expander MCU":
- Flashed with Klipper firmware
- Connected via USB
- Acts as secondary `[mcu]` in printer.cfg

### Spec
- `klipperformac gpio-setup` wizard:
  - Detects Arduino/STM32 on USB
  - Guides through flashing Klipper firmware
  - Generates `[mcu expander]` + pin alias config
- Document: "for $5 you get 20+ GPIO pins"
- Include example configs for:
  - Filament runout (microswitch on expander pin)
  - Chamber LED (MOSFET on expander pin)
  - Exhaust fan relay
- `presets` includes GPIO expander templates

### Constraints
- No software GPIO on Mac itself (impossible, document why)
- Must use real Klipper MCU config, not custom code
- `doctor` checks expander MCU connectivity

### Done when
- Wizard flashes and configures expander
- Example configs in `presets/gpio-expander/`
- Docs explain the $5 Arduino solution clearly
- printer.cfg template with `[mcu expander]` section

---

## Phase 4: CAN bus (via USB-CAN adapter)

### Problem
macOS has no native CAN controller. But USB-CAN adapters exist (CANable,
USB2CAN, Waveshare). Klipper's CAN support is Linux-socketcan-specific.

### Spec
- Research: can usbcan interfaces work via user-space CAN (not socketcan)?
- If yes: write a socketcan-compatible shim translating to USB-CAN adapter
- If no: document clearly what's missing and why
- `klipperformac can-setup` checks for supported adapter
- At minimum: detect CANable in bootloader mode (stm32flash), guide user

### Constraints
- No kernel CAN support on macOS
- Must not claim "CAN works" if it doesn't
- Be honest in README about this being the hardest limitation

### Done when
- Research doc with verdict
- If possible: working CAN over USB adapter
- If not: clear docs on why + workaround (USB direct to toolhead)

---

## Phase 5: KlipperScreen equivalent (SwiftUI native)

### Problem
KlipperScreen is a Linux framebuffer touchscreen UI. macOS has no framebuffer.
But the .app GUI can be the equivalent.

### Spec
- Full SwiftUI dashboard:
  - Real-time temperature graph
  - Axis jogging (X/Y/Z/E)
  - Extrude/retract
  - Bed mesh visualization (parse bed_mesh/calibrate output)
  - Print progress + ETA + layer
  - Macro buttons (custom G-code macros from config)
  - Webcam preview (Phase 2)
  - Emergency stop (big red button, hardware interrupt if possible)
  - File browser + start print
- Connects to Moonraker API (:7125) — same as web UIs
- No VNC, no X11, no Linux
- `klipperformac gui` opens fullscreen/kiosk mode
- Touchscreen support (Macs with touch displays exist; iPad Sidecar?)

### Done when
- All major KlipperScreen panels implemented
- Real-time updates via Moonraker WebSocket
- Works as kiosk mode for garage/workshop Mac
- .app notarized + hardened runtime

---

## Phase 6: Integration & Polish

- Single `klipperformac upgrade --full` checks all phases
- `doctor` covers: Python 3.14, camera permission, GPIO expander, CAN adapter
- Installer detects hardware and suggests features
- README rewritten with "full Pi replacement" positioning
- Video walkthrough: Mac Mini as dedicated printer host

---

## Non-goals
- Replacing Klipper's real-time MCU code (already works)
- Linux VM (defeats the purpose)
- Windows support
- Android app (separate project)
