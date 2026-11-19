# macOS hardware limits — verified verdicts

What macOS *cannot* do for Klipper, what it *can* do instead, and the evidence
for each claim. These verdicts come from a read-only research pass against the
pinned upstream checkouts on this machine; every factual line carries a
`file:line` or document citation.

Citation legend (checked 2026-10-16):
- `klipper/...` = the pristine Klipper checkout at `~/.klipperformac/klipper`,
  pinned HEAD `461c4e3722c3a897fba1c6b3f0780a5315043842`
  (`klipperformac/versions.py:21`).
- `moonraker/...` = the pristine Moonraker checkout at `~/.klipperformac/moonraker`.
- `docs/...` = Klipper's own upstream documentation in that checkout.
- Web UIs as shipped here: Mainsail v2.19.0 / Fluidd v1.37.6
  (`klipperformac/versions.py:23-24`), installed under `~/.klipperformac/data/web/`.

---

## 1. CAN bus

### 1a. Runtime `[mcu]` over CAN: structurally blocked on macOS

The chain that makes it impossible, link by link:

1. A `[mcu]` / `[mcu <name>]` section with `canbus_uuid:` selects the CAN
   transport and a `canbus_interface` that defaults to `can0`
   (klipper/klippy/mcu.py:780-783).
2. klippy then asks python-can for a bus with a **hardcoded**
   `bustype='socketcan'` (klipper/klippy/serialhdl.py:143-145).
3. SocketCAN is a Linux kernel protocol family. macOS/XNU has no AF_CAN/PF_CAN:
   on this machine's venv python, `socket.AF_CAN`, `socket.PF_CAN` and
   `socket.CAN_RAW` all do not exist, and `can.interface.Bus(bustype='socketcan')`
   raises `OSError [Errno 43] Protocol not supported` (EPROTONOSUPPORT) —
   verified live against the pinned `python-can==3.3.4`
   (klipper/scripts/klippy-requirements.txt:18).
4. Because errno 43 is caught as a retryable `os.error`
   (klipper/klippy/serialhdl.py:143-152), klippy does not crash: it spews
   "Unable to open CAN port" every 5 s and dies with "Unable to connect" after
   ~90 s (klipper/klippy/serialhdl.py:139-152).
5. Even if Python could fake a socket object, the C helper consumes the fd
   directly: klippy passes `serial_dev.fileno()` into the chelper
   (klipper/klippy/serialhdl.py:89) and klipper/chelper/serialqueue.c
   `read()`s/`write()`s raw `struct can_frame` records on it
   (klipper/klippy/chelper/serialqueue.c:331 and :398, `SQT_CAN` branch). The
   fd must be a genuine kernel CAN socket — there is no userspace way in.

Adding `PF_CAN`/`CAN_RAW` constants to our compile-time stub
(`klipperformac/compat/include/linux/can.h`) would do nothing: the gap is a
kernel protocol family plus the `can_frame`-fd contract above, not a missing
typedef. The stub itself must stay — it is `#include`d by serialqueue.c
(klipper/klippy/chelper/serialqueue.c:15) and compiled into `c_helper.so`,
which the working USB-serial path also needs.

USB-CAN adapters are no help: gs_usb/PEAK/Kvaser/Vector support in python-can
depends on Linux kernel drivers or host services (socketcand), and
upstream's "Klipper USB to CAN bus bridge" MCU (klipper/docs/CANBUS.md:86-94)
does not enumerate as a usable interface on macOS — there is no kernel CAN
family to attach to. A CANable flashed to **slcan** firmware does receive
messages through python-can's slcan backend, but that is diagnostics only: its
fd is a UART fd, not a `can_frame` fd, so it can never satisfy
klipper/chelper/serialqueue.c:331.

Keep CAN nodes on a separate Linux box if you must have CAN — but note Klipper
cannot span one `printer.cfg` across hosts, so that is a *different printer*,
not a remote MCU of this one.

### 1b. Flashing a CAN board from a Mac: yes — over USB-CDC, never over CAN

The common assumption "a CAN board can't be touched from a Mac" is wrong in one
important way.

- This Klipper HEAD has **no** `lib/canboot` and **no**
  `scripts/canboot/flash_can.py`; the CAN bootloader flasher is Katapult's
  vendored tool, klipper/lib/katapult/flashtool.py (invoked by
  klipper/scripts/flash_usb.py:100-101 as `lib/katapult/flashtool.py -d <tty>
  -f <bin>`). Anyone citing "flash_can.py" is citing an older tree.
- flashtool.py picks its transport on one condition: if `-d <device path>` is
  given it uses `SerialSocket` (klipper/lib/katapult/flashtool.py:1098-1103,
  class at :861); the CAN path (`-u <uuid>`) builds a `CanSocket`
  (class at :579) whose constructor needs `socket.PF_CAN`
  (klipper/lib/katapult/flashtool.py:596-597) — errno-43 dead end again.
- `SerialSocket` is pure pyserial over a tty (klipper/lib/katapult/flashtool.py:861-899),
  and pyserial works fine on macOS. So **any CAN board that exposes a USB-CDC
  bootloader port (Katapult/CanBoot with USB, RP2040 BOOTSEL, etc.) can be
  flashed from a Mac**: plug the board in by USB, flash over `/dev/cu.*`,
  unplug, and the board then runs its CAN firmware elsewhere. Success is
  reported as "Programming Complete" (klipper/lib/katapult/flashtool.py:1123).
- macOS gap to know about: flashtool's automatic bootloader re-entry (1200-baud
  DTR toggle, klipper/lib/katapult/flashtool.py:971-981) depends on Linux sysfs
  USB info; `get_usb_path()` returns None here, so it prints "Device path ...
  is not a usb device" and returns early (klipper/lib/katapult/flashtool.py:964-966).
  Enter the bootloader **manually** (BOOTSEL/reset button) before flashing.

### 1c. WebCAN / WebSerial flashing in the browser: not available here

- The Mainsail v2.19.0 and Fluidd v1.37.6 builds shipped by this project
  contain **zero** `navigator.serial` / `navigator.usb` / WebSerial / WebUSB /
  canboot / katapult code — verified by grep over both installed dists under
  `~/.klipperformac/data/web/` (empty result). Browser flashing is currently
  not a dashboard feature at all on this install, Mac or not.
- Even where such UIs exist upstream, WebSerial requires a Chromium-family
  browser (not Safari) and can only ever reach a board's *own* USB serial
  bootloader — never a USB-CAN adapter (there is no browser path to a CAN bus).
- Moonraker's CAN surfaces on macOS fail read-only, by the same PF_CAN gap:
  `GET /machine/peripherals/canbus` returns HTTP 500 `AttributeError: module
  'socket' has no attribute 'PF_CAN'` at moonraker/moonraker/utils/cansocket.py:69
  (verified live). It is triggered only on explicit user action and does not
  strand the UI. Per AGENTS.md rule 1 we do not patch Moonraker.

**Verdict:** CAN *runtime* is structurally blocked on macOS (XNU has no
PF_CAN; serialqueue.c consumes real CAN fds; python-can raises errno 43).
CAN board *flashing* works from a Mac over the board's USB-CDC bootloader
port. Browser WebSerial/WebCAN flashing is not present in shipped Mainsail/
Fluidd builds.

---

## 2. Host GPIO / I2C / SPI: unavailable — use a second USB MCU

- Klipper resolves every pin through registered MCU chips: a pin like
  `PA4` or `tool:PA4` maps to a chip name that must exist among the connected
  `[mcu...]` objects (klipper/klippy/pins.py:79-92). There is no host-GPIO chip
  on macOS.
- Upstream's host peripherals are Linux device services — `/dev/gpiochipN`
  (gpiochardev), `/dev/i2c-N`, `/dev/spidevX.Y`. This checkout carries none of
  that machinery at all (verified: no gpiochardev module and no `gpiochip`
  references anywhere under klipper/klippy/); the only host-bus code is
  BeagleBone-specific (klipper/klippy/extras/replicape.py, hardcoded
  `REPLICAPE_PCA9685_BUS = 2`). macOS exposes no equivalent character devices.
- The Linux host-MCU transport (`/tmp/klipper_host_mcu`, `/dev/rpmsg_*`) is
  likewise rejected here: klippy treats it as a special serial path
  (klipper/klippy/mcu.py:788-790) that requires a Linux IPC backend macOS does
  not provide.

**Workaround pattern:** attach the peripheral (LED strip, relay, I2C sensor or
LCD, SPI device) to a cheap second MCU — an Arduino/STM32/RP2040 flashed with
Klipper firmware — plugged in over USB as its own `[mcu <name>]` section, and
reference its pins as `<name>:PIN` (klipper/klippy/pins.py:79-92). Each board
just needs its own `/dev/cu.*` node.

Forward-looking: first-class multi-MCU tooling (per-board serial pickers,
bindings that survive macOS's `/dev/cu.*` renumbering on replug) is on the
Klipper-for-Mac **roadmap**; no date is promised. Until it lands, editing
`printer.cfg` by hand plus `klipperformac serial --set` works today. When you
split peripherals across boards for timing reasons, read §3 and
klipper/docs/Multi_MCU_Homing.md — keep each axis' endstop on the board that
drives that axis.

---

## 3. Real-time: this checkout contains NO host-side RT code, and does not need any

- Verified: the string `rtpriority` does not appear anywhere in this pinned
  Klipper (klippy/, config/, docs/Config_Reference.md — empty grep). There is
  no Linux RT-priority setup to port; adding it to `[printer]` would just be a
  hard config error for an unknown option (klipper/klippy/configfile.py:424-440).
- Step timing lives **on the MCU**, not the host: the host streams step-run
  descriptors (`interval`, `add`, `count` per run — klipper/src/stepper.c:30-36)
  and the micro-controller generates the actual step edges on its own clock.
- The host budget is generous by design: klippy needs scheduling only every
  ~0.200–0.450 s (BGFLUSH windows, klipper/klippy/extras/motion_queuing.py:9-12),
  keeps ~1 s of moves buffered (BUFFER_TIME_HIGH = 1.0,
  klipper/klippy/toolhead.py:195), requires just 0.1 s of lead time
  (MIN_SCHEDULE_TIME = 0.100, klipper/klippy/mcu.py:13), and the C helper
  pushes command batches 100 ms ahead with a 25 ms retransmit floor
  (MIN_REQTIME_DELTA = 0.100, klipper/klippy/chelper/serialqueue.c:110).
- Clock drift/jitter is absorbed by the sync layer: secondary MCUs are
  re-disciplined against the master with a 4-second correction horizon
  (SecondarySync.calibrate_clock, klipper/klippy/clocksync.py:175-231, horizon
  `sync1_print_time + 4.` at klipper/klippy/clocksync.py:217) and round-trip
  statistics. macOS scheduling jitter is
  therefore tolerated *by design*, not by luck.
- macOS specifics: there is no SCHED_FIFO and negative `nice` is root-only
  (measured on this machine: `os.setpriority(...,-N)` → EACCES, `os.nice(-N)`
  → EPERM), so Klipper-for-Mac cannot and does not try to claim real-time
  priority. Upstream's own bar for host scheduling is exactly this: Klipper
  "has some real-time scheduling requirements; if, during a print, the host
  computer also performs an intensive general-purpose computing task ... it may
  cause Klipper to report print errors" (klipper/docs/FAQ.md:126-132).
- Measured on this research Mac during normal operation: `srtt` ≤ 3 ms,
  minimum half-round-trip 64–84 µs, and zero "Timer too close" events across
  six klippy.log files. The one place milliseconds still matter is cross-board
  homing: the trsync expire window shrinks from 250 ms (single MCU) to 25 ms
  (cross-MCU) (TRSYNC_TIMEOUT / TRSYNC_SINGLE_MCU_TIMEOUT,
  klipper/klippy/mcu.py:279-280, applied in TriggerDispatch.start at
  klipper/klippy/mcu.py:311-320), and upstream requires consistently <10 ms
  round trip for it (klipper/docs/Multi_MCU_Homing.md:34-38).
- Telemetry: **shipped** — `klipperformac timing [--json]` reads the host budget
  (`srtt`, retransmits, `buffer_time`), the starvation errors in klippy.log
  (`Timer too close`, `Missed scheduling of next ...`, `Lost communication with
  MCU`), klippy's `ps` priority and the power/sleep state, all read-only and safe
  during a print; `klipperformac doctor` prints the short form and the GUI reads
  the JSON. The one mitigation macOS allows without root is also shipped: the
  generated `sitecustomize.py` asks for a USER_INTERACTIVE QoS class for the
  klippy child alone (`pthread_set_qos_class_self_np`, `sys/qos.h:51` — a request,
  not a guarantee, and *not* real-time priority). What is NOT possible remains
  the headline: no SCHED_FIFO, no negative `nice`, no `rtpriority` option.
  Keep the Mac on AC and awake while printing (`up` already runs
  `caffeinate` for you).

**Verdict:** no host-side real-time code exists in this checkout and none is
required — step edges are MCU-owned; macOS jitter is absorbed by Klipper's
queuing and clock-sync design; the residual risks are host sleep/power caps
and cross-board homing latency.

---

## 4. One-liners: hardware-blocked here, network-pluggable equivalents work

| Feature | Verdict | What to use instead |
|---|---|---|
| DS18B20 on a host 1-Wire bus (w1) | hardware-blocked on macOS — w1 GPIO is a Linux host service; klippy only speaks DS18B20 via MCU firmware commands (`config_ds18b20`, klipper/klippy/extras/ds18b20.py:37-42) | wire the DS18B20 to a spare MCU pin (see §2), or report temps from a network device |
| CANBLEC / CAN-env / CAN expansion & sensor boards | hardware-blocked at runtime (§1a) | I2C/SPI equivalents on a second USB MCU (§2), or ESP32-class boards reporting over the network |
| Host GPIO LEDs/relays/fans (§2) | unavailable | Tasmota / Shelly / HomeAssistant-controlled power and peripherals — Moonraker's standard "power devices" API already works from this stack, no upstream edits |
| KlipperScreen (local framebuffer) | no macOS equivalent | the shipped web dashboards (Mainsail/Fluidd) |

## 5. Maintaining this document

`tests/docs/test_hardware_limits_refs.py` asserts this file exists and keeps
the three verdict citations (CAN, host GPIO, real-time) present. If upstream
pinning moves, re-verify the `file:line` references before editing.
