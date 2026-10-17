"""Firmware flashing for boards attached to this Mac: `klipperformac flash`.

Why this module exists at all (research spec "klipperformac flash - design +
feasibility"): upstream Klipper ships no firmware binaries, and upstream's own
flasher is Linux-only - `scripts/flash_usb.py` resolves a target through
`/sys` and `/dev/serial/by-path` (`translate_serial_to_tty`,
`translate_serial_to_usb_path`), neither of which exists here. So the macOS
path uses the primitives the boards themselves expose:

  * RP2040/RP2350: BOOTSEL exposes a USB mass-storage volume, and writing a
    valid .uf2 to that volume IS the flash (`/Volumes/<name>` + `diskutil
    eject` as the fallback).
  * STM32 in DFU mode: brew `dfu-util`, with the exact flag sets upstream
    passes itself (scripts/flash_usb.py: `call_dfuutil`, `flash_stm32f1`,
    `flash_stm32f4`).
  * STM32 in the 3.3V serial bootloader: brew `stm32flash`, which is what
    upstream's own `make serialflash` runs (`src/stm32/Makefile:109-111`,
    `docs/Bootloaders.md:307`), or upstream's flasher script executed as a
    subprocess with the stack's venv python (pyserial is that venv's own
    dependency).

Upstream is sacred (AGENTS.md rule 1). Nothing here writes to a checkout: an
upstream script is *executed*, never edited, and where it cannot work on macOS
that is reported to the user instead of patched. Firmware is always
user-supplied (`--bin FILE`) - we never download anything, and we validate what
the user handed us against the magic constants upstream itself uses
(`lib/pico-sdk/boot/uf2.h:19-33`, `lib/elf2uf2/main.cpp:295-301`,
`lib/rp2040_flash/main.c:52-59`) before writing it.

Two more invariants:

  * It never opens a serial port. klippy holds `/dev/cu.*` with a flock (the
    multi-MCU spec's section 0, upstream `serialposix.py:310`); a foreign
    opener corrupts a live printer. Enumeration only: `ioreg`, `/Volumes`, and
    pyserial's `list_ports` inside a throwaway subprocess via `detect`.
  * Every mutating flow refuses to run while the stack is up, and this module
    never stops the stack itself: the user decides when the printer goes down.
"""
import difflib
import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import time

from . import detect, paths, process

# ------------------------------------------------------------------ constants
# UF2 container, verbatim from the pristine checkout: lib/pico-sdk/boot/uf2.h
#   :19-21  magic_start0 / magic_start1 / magic_end
#   :24-25  UF2_FLAG_FILE_CONTAINER / UF2_FLAG_FAMILY_ID_PRESENT
#   :33-38  RP2040_FAMILY_ID and the RP2350 bootrom family ids
#   :48-61  struct uf2_block - eight uint32, data[476], trailing magic
_UF2_MAGIC_START0 = 0x0A324655
_UF2_MAGIC_START1 = 0x9E5D5157
_UF2_MAGIC_END = 0x0AB16F30
_UF2_FLAG_FILE_CONTAINER = 0x00001000
_UF2_FLAG_FAMILY_ID_PRESENT = 0x00002000
_RP2040_FAMILY_ID = 0xE48BFF56
_RP2350_FAMILY_IDS = (0xE48BFF59, 0xE48BFF5A, 0xE48BFF5B)
UF2_BLOCK_SIZE = 512
UF2_MAX_PAYLOAD = 476  # data[476]
_UF2_HEADER = struct.Struct("<8I")

# Sanity floor/ceiling for anything we would put in a chip (flash spec: "Reject
# any file > 4 MiB or < 4 KiB"). Klipper's RP2040 firmware is ~30-60 KiB; a
# 4 MiB .uf2 is a container or not Klipper at all.
MIN_ARTIFACT_BYTES = 4 * 1024
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024

# The volume names the boot ROMs use. RPI-RP2 is what an RP2040 (SKR Pico, Pi
# Pico) mounts; RP2350 uses its own. --volume accepts another name, with the
# same safety checks.
BOOTSEL_VOLUMES = ("RPI-RP2", "RP2350")
# Every UF2 bootloader volume publishes one of these at its root. Requiring it
# is what makes "cp the firmware to /Volumes/<name>" safe on a Mac whose only
# other mounted volume is the one it booted from.
UF2_VOLUME_MARKERS = ("info_uf2.txt", "info_rt.txt", "index.htm")
BOOT_VOLUME_NAMES = ("Macintosh HD", "Macintosh HD - Data", "Data", "System")

# USB ids of chips sitting in a bootloader - upstream's own numbers:
# scripts/flash_usb.py flash_rp2040 (`rawdev = "2e8a:0003"`, rp2350 "2e8a:000f"),
# STM32F1_HELP ("1eaf:0003"), STM32F4_HELP ("0483:df11").
UF2_BOOTSEL_VIDPID = {"2e8a:0003": "rp2040", "2e8a:000f": "rp2350"}
DFU_VIDPID = {"0483:df11": "stm32 DFU (ST AN2606 ROM bootloader)",
              "1eaf:0003": "stm32duino bootloader",
              "1d50:60c6": "lpc176x (dfu)", "1c57:021d": "lpc176x (dfu)"}

# dfu-util flags, mirroring upstream: flash_stm32f1 -> ["-R", "-a", "2"] with
# -d 1eaf:0003; flash_stm32f4 (and f042/f070/f072/g0b1/f7/h7/l4/g4 per MCUTYPES)
# -> ["-R", "-a", "0", "-s", "<app address>:leave"] with -d 0483:df11. The
# default application address is upstream's own value for
# STM32_FLASH_START_4000 (src/stm32/Kconfig:323-329).
DFU_PRESETS = {
    "stm32f103": {"vidpid": "1eaf:0003", "alt": "2", "address": None,
                  "leave": False},
    "stm32f4": {"vidpid": "0483:df11", "alt": "0", "address": "0x8004000",
                "leave": True},
    "stm32f407": {"vidpid": "0483:df11", "alt": "0", "address": "0x8004000",
                  "leave": True},
    "stm32f072": {"vidpid": "0483:df11", "alt": "0", "address": "0x8004000",
                  "leave": True},
    "stm32f042": {"vidpid": "0483:df11", "alt": "0", "address": "0x8004000",
                  "leave": True},
}
# Chips that are not flashed over DFU, and the transport they do have.
_NOT_DFU_HINTS = {"rp2040": "uf2", "rp2350": "uf2",
                  "sam3x": "bossa (not implemented yet)",
                  "sam4s": "bossa (not implemented yet)"}

# ARM Cortex-M vector-table windows (initial MSP must land in SRAM, the reset
# vector in flash) - upstream's linker scripts guarantee both.
CHIP_RANGES = {
    "rp2040": (0x20000000, 0x20042000, 0x10000000, 0x11000000),
    "rp2350": (0x20000000, 0x20080000, 0x10000000, 0x11000000),
    "stm32f103": (0x20000000, 0x20010000, 0x08000000, 0x08200000),
    "stm32f4": (0x20000000, 0x20020000, 0x08000000, 0x08200000),
    "stm32f407": (0x20000000, 0x20020000, 0x08000000, 0x08200000),
    "stm32f072": (0x20000000, 0x2000A000, 0x08000000, 0x08100000),
}
BUILD_CHIPS = ("rp2040", "rp2350", "stm32f103", "stm32f407", "stm32f072")

# Where built artifacts would be copied and where upstream's defconfigs live.
# The flash spec asked for paths.FIRMWARE_DIR / paths.BUILD_DIR; those
# constants do not exist in paths.py and editing paths.py is outside this
# round's file ownership, so they are named here instead.
FIRMWARE_DIR_NAME = "firmware"
KLIPPER_DEFCONFIG_DIR = "config"
KLIPPER_DEFCONFIG_SUFFIX = ".cfg"
# Candidate upstream flashers for the serial transport, best first. This
# checkout has no scripts/flash_stm32.py (it ships flash-{ar100,linux,pru,
# sdcard} and flash_usb.py), which is why the list is probed instead of a
# single hardcoded name.
UPSTREAM_SERIAL_FLASH_SCRIPTS = ("scripts/flash_stm32.py",
                                 "scripts/flash_usb.py")
DEFCONFIG_NAME_REASON = ("--defconfig {!r} is not a board-config name: one "
                         "filename inside klipper/config/, with no path "
                         "separators and no '..'.")

VENV_PROBE_TIMEOUT = 15
IOREG_TIMEOUT = 20
DISKUTIL_TIMEOUT = 60
FLASH_TOOL_TIMEOUT = 240   # a real dfu-util/stm32flash run (flash spec: 240 s)
VOLUME_TIMEOUT = 60        # how long `flash uf2` waits for BOOTSEL
VOLUME_POLL = 0.5
RESET_TIMEOUT = 15         # the RP2040 ROM unmounts the volume by itself
IDENTITY_TIMEOUT = 5
DEFAULT_JOBS = 4
PROBE_TIMEOUT = 20

_PROBE_RUNTIME_SRC = r"""
import json
import sys

sys.path.insert(0, sys.argv[1])

import clocksync
import reactor
import serialhdl


def main():
    device = sys.argv[2]
    baud = int(sys.argv[3])
    timeout = float(sys.argv[4])
    result = {"ok": False, "device": device, "baud": baud}
    sr = None
    r = reactor.Reactor()
    done = r.completion()

    def finish(payload):
        if not done.test():
            done.complete(payload)
        try:
            if sr is not None:
                sr.disconnect()
        except Exception:
            pass
        r.end()

    def task(eventtime):
        nonlocal sr
        endtime = eventtime + max(1.0, timeout)
        try:
            sr = serialhdl.SerialReader(r)
            clk = clocksync.ClockSync(r)
            while True:
                try:
                    sr.connect_uart(device, baud)
                    break
                except Exception:
                    curtime = r.monotonic()
                    if curtime > endtime:
                        raise
                    try:
                        sr.disconnect()
                    except Exception:
                        pass
                    r.pause(curtime + 0.5)
            clk.connect(sr)
            mp = sr.get_msgparser()
            version, build_versions = mp.get_version_info()
            finish({
                "ok": True,
                "device": device,
                "baud": baud,
                "mcu": mp.get_constant("MCU", ""),
                "clock_freq": mp.get_constant_int("CLOCK_FREQ", None),
                "version": version,
                "build_versions": build_versions,
            })
        except Exception as e:
            finish({
                "ok": False,
                "device": device,
                "baud": baud,
                "error": str(e),
                "error_type": type(e).__name__,
            })

    r.register_callback(task)
    try:
        r.run()
    finally:
        try:
            if sr is not None:
                sr.disconnect()
        except Exception:
            pass
        r.finalize()
    payload = done.wait() if done.test() else result
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
"""


def _run(argv, timeout, **kw):
    """argv list, captured output, hard timeout: (code, out, err), never raises.

    Same contract as shaper.py:51-58: an absent binary, a timeout or a
    permission error becomes (None, "", why), which the caller turns into one
    printed line rather than a traceback.
    """
    try:
        res = subprocess.run([str(a) for a in argv], capture_output=True,
                             timeout=timeout, **kw)
    except Exception as e:
        return None, "", str(e)
    return (res.returncode, res.stdout.decode(errors="replace"),
            res.stderr.decode(errors="replace"))


def _run_bytes(argv, timeout):
    """_run() that keeps stdout as bytes - plistlib wants the bytes back."""
    try:
        res = subprocess.run([str(a) for a in argv], capture_output=True,
                             timeout=timeout)
    except Exception as e:
        return None, b"", str(e).encode()
    return res.returncode, res.stdout, res.stderr


def find_tool(name):
    """Absolute path of a flasher, or None.

    shutil.which alone is not enough: the SwiftUI app and launchd hand us a
    minimal PATH, and Homebrew is /usr/local on Intel but /opt/homebrew on
    Apple Silicon, so both bin dirs are probed (shaper.py:24 keeps the same
    list for brew itself).
    """
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/usr/local/bin", "/opt/homebrew/bin",
                   "/usr/local/sbin", "/opt/homebrew/sbin"):
        cand = os.path.join(prefix, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def brew_path():
    """First Homebrew that exists, or None."""
    return find_tool("brew")


def volumes_dir():
    """Where macOS mounts volumes.

    Overridable so the offline tests can point the whole flow at a temp
    directory instead of the real /Volumes - no test can then write to, wait on
    or eject anything a user owns."""
    return os.environ.get("KLIPPERFORMAC_VOLUMES_DIR") or "/Volumes"


def mount_point(volume):
    return os.path.join(volumes_dir(), volume)


def list_volumes():
    """Mounted volume names, minus the macOS system volumes."""
    root = volumes_dir()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [n for n in names if n not in BOOT_VOLUME_NAMES
            and os.path.isdir(os.path.join(root, n))]


def bootloader_volume_p(mount):
    """True when `mount` is a UF2 BOOTSEL disk: it exists and carries the
    INFO_UF2.TXT (or INDEX.HTM) the boot ROM always publishes at its root."""
    try:
        entries = [e.lower() for e in os.listdir(mount)]
    except OSError:
        return False
    return any(m in entries for m in UF2_VOLUME_MARKERS)


def safe_volume_name(raw):
    """(ok, name_or_reason) - a volume name is one path component, nothing else.

    Refuses empty, '..', separators, control characters and the macOS system
    volume names, so `--volume` can never point the write at the disk the user
    booted from.
    """
    name = (raw or "").strip()
    if not name:
        return False, "No volume name given."
    if len(name) > 63:
        return False, "Volume name too long ({} characters).".format(len(name))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,62}", name):
        return False, ("Volume name {!r} is not usable: no path separators, no "
                       "'..', and only letters, digits, space, '.', '_' or '-'"
                       .format(raw))
    if name in BOOT_VOLUME_NAMES:
        return False, ("Refusing to write firmware to {!r}: that is a macOS "
                       "system volume, not a board bootloader.".format(name))
    return True, name


# ------------------------------------------------------------ artifact checks
def _size_of(path):
    try:
        return os.path.getsize(path), ""
    except OSError as e:
        return None, "Cannot read {}: {}".format(
            path, getattr(e, "strerror", None) or e)


def _size_reject(path, size):
    if size > MAX_ARTIFACT_BYTES:
        return ("{} is {:,} bytes; firmware over {:,} bytes is refused as a "
                "sanity ceiling.".format(path, size, MAX_ARTIFACT_BYTES))
    if size < MIN_ARTIFACT_BYTES:
        return ("{} is only {} bytes - under the {:,} byte floor for real "
                "firmware.".format(path, size, MIN_ARTIFACT_BYTES))
    return None


def check_bin_path(raw):
    """(ok, path_or_reason) for a user-supplied firmware file.

    Three guards, in order:
      * no control characters - a CR or NUL in a path forges a line in every
        log and in the command we print back;
      * no '..' component - the string we show the user is the string we open,
        so an invisible hop out of the folder they named is not allowed;
      * it must be an existing regular file - a directory, a FIFO, /dev/null or
        a dangling symlink is a mistake, not an image.
    """
    text = "" if raw is None else str(raw)
    if not text.strip():
        return False, "No firmware file given (use --bin PATH)."
    if re.search(r"[\x00-\x1f\x7f]", text):
        return False, ("Firmware path must not contain control characters "
                       "(newlines, tabs or NUL).")
    if ".." in text.split(os.sep):
        return False, ("Refusing the path '{}': '..' segments are not allowed, "
                       "so what is printed and what is written can never "
                       "disagree. Give the resolved path instead.".format(text))
    path = os.path.realpath(os.path.expanduser(text))
    if not os.path.exists(path):
        return False, ("No such firmware file: {} (a symlink that points "
                       "nowhere counts as 'no such file').".format(path))
    if not os.path.isfile(path):
        return False, "{} is not a regular file.".format(path)
    return True, path


def uf2_block(buf, offset=0):
    """Parse one 512-byte UF2 sector -> dict, or None when it is not one."""
    if len(buf) - offset < UF2_BLOCK_SIZE:
        return None
    fields = _UF2_HEADER.unpack_from(buf, offset)
    (m0, m1, flags, target, payload, block_no, num_blocks,
     family) = fields
    if m0 != _UF2_MAGIC_START0 or m1 != _UF2_MAGIC_START1:
        return None
    if struct.unpack_from("<I", buf, offset + UF2_BLOCK_SIZE - 4)[0] != \
            _UF2_MAGIC_END:
        return None
    if payload > UF2_MAX_PAYLOAD:
        return None
    return {"flags": flags, "target_addr": target, "payload_size": payload,
            "block_no": block_no, "num_blocks": num_blocks, "family_id": family}


def verify_uf2(path, chip=None):
    """(ok, reason) - every sector must be a real UF2 block, and for an RP2040
    the family id must be the one its boot ROM accepts.

    This is not an extension check. lib/rp2040_flash/main.c:52-59 skips any
    block whose magic_start0/magic_end/file_size do not match, so a file that
    fails here would be silently swallowed by the board after we already told
    the user to unplug their working firmware.
    """
    size, why = _size_of(path)
    if size is None:
        return False, why
    bad = _size_reject(path, size)
    if bad:
        return False, bad
    if size % UF2_BLOCK_SIZE:
        return False, ("{} is {} bytes, not a whole number of 512-byte UF2 "
                       "sectors - the file is truncated.".format(path, size))
    with open(path, "rb") as f:
        data = f.read(MAX_ARTIFACT_BYTES + 1)
    blocks = 0
    num_blocks = None
    payload_total = 0
    last = None
    seen_numbers = set()
    # Every data sector carries the family id (struct uf2_block's last header
    # word, "file_size" in a container): the boot ROM compares block-by-block
    # and skips a mismatch (lib/rp2040_flash/main.c:52-59), so a mixed image is
    # a half-flash, not a rejected one. Collect them all instead of trusting the
    # last one.
    families = set()
    for offset in range(0, len(data), UF2_BLOCK_SIZE):
        blk = uf2_block(data, offset)
        if blk is None:
            return False, ("sector {} (offset {}) is not a UF2 block: magic "
                           "0x{:08X} + 0x{:08X} at the start and 0x{:08X} at "
                           "the end of the sector are required "
                           "(pico-sdk/boot/uf2.h:19-21)."
                           .format(blocks + 1, offset, _UF2_MAGIC_START0,
                                   _UF2_MAGIC_START1, _UF2_MAGIC_END))
        if blk["target_addr"] % 4:
            return False, ("UF2 sector {} targets 0x{:08X}, which is not "
                           "4-byte aligned.".format(blocks + 1,
                                                    blk["target_addr"]))
        if num_blocks is None:
            num_blocks = blk["num_blocks"]
        elif blk["num_blocks"] != num_blocks:
            return False, ("UF2 sector {} disagrees about the block count ({} "
                           "vs {}): two different images in one file."
                           .format(blocks + 1, blk["num_blocks"], num_blocks))
        if blk["block_no"] >= max(num_blocks, 1):
            return False, ("UF2 sector {} has block_no {} out of {} blocks."
                           .format(blocks + 1, blk["block_no"], num_blocks))
        if blk["block_no"] in seen_numbers:
            return False, ("UF2 sector {} repeats block_no {} of an earlier "
                           "sector: two images interleaved.".format(
                               blocks + 1, blk["block_no"]))
        seen_numbers.add(blk["block_no"])
        if not blk["flags"] & _UF2_FLAG_FILE_CONTAINER:
            payload_total += blk["payload_size"]
            if not blk["flags"] & _UF2_FLAG_FAMILY_ID_PRESENT:
                return False, ("UF2 sector {} has no family-id flag set "
                               "(flags 0x{:08x}); a boot ROM that checks the "
                               "family would skip this block and half-flash the "
                               "board.".format(blocks + 1, blk["flags"]))
            families.add(blk["family_id"])
        last = blk
        blocks += 1
    if not blocks:
        return False, "The .uf2 holds no sectors at all."
    if blocks != num_blocks:
        return False, ("{} UF2 sectors present but the header promised {} - "
                       "the image is incomplete.".format(blocks, num_blocks))
    family = last["family_id"]
    if len(families) > 1:
        return False, ("its data sectors carry different family ids ({}): one "
                       ".uf2 cannot be firmware for two chips, and a boot ROM "
                       "writes only the blocks it recognises."
                       .format(", ".join("0x{:08x}".format(f)
                                         for f in sorted(families))))
    if chip in ("rp2040", "rp2350"):
        want = ({_RP2040_FAMILY_ID} if chip == "rp2040"
                else set(_RP2350_FAMILY_IDS))
        if len(families) != 1 or not families <= want:
            return False, ("this .uf2 is not {} firmware: its data sectors "
                           "carry family id(s) {}, and the {} boot ROM only "
                           "accepts {}."
                           .format(chip,
                                   ", ".join("0x{:08x}".format(f)
                                             for f in sorted(families))
                                   or "none",
                                   chip,
                                   ", ".join("0x{:08x}".format(f)
                                             for f in sorted(want))))
    return True, ("{} UF2 sector(s), {:,} bytes of payload, family id "
                  "0x{:08x}".format(blocks, payload_total, family))


def verify_bin(path, chip=None):
    """(ok, reason) for a raw .bin: the ARM Cortex-M vector table must be sane.

    Word 0 is the initial stack pointer (must point into that chip's SRAM),
    word 1 the reset vector (must point into its flash)."""
    size, why = _size_of(path)
    if size is None:
        return False, why
    bad = _size_reject(path, size)
    if bad:
        return False, bad
    with open(path, "rb") as f:
        head = f.read(8)
    if len(head) < 8:
        return False, "Too short to hold an ARM vector table (8 bytes)."
    msp, reset = struct.unpack("<2I", head)
    chips = [chip] if chip in CHIP_RANGES else (
        sorted(CHIP_RANGES) if chip in (None, "auto") else None)
    if chips is None:
        return False, "Unknown chip {!r}; known: {}".format(
            chip, ", ".join(sorted(CHIP_RANGES)))
    for name in chips:
        ram_lo, ram_hi, flash_lo, flash_hi = CHIP_RANGES[name]
        if ram_lo <= msp <= ram_hi and flash_lo <= reset < flash_hi:
            return True, ("{}: stack pointer 0x{:08x} in SRAM, reset vector "
                          "0x{:08x} in flash".format(name, msp, reset))
    return False, ("vector table is not in any known chip's memory map: initial "
                   "MSP 0x{:08x}, reset vector 0x{:08x}. Wrong chip, or this "
                   ".bin is not firmware.".format(msp, reset))


def verify_artifact(path, kind="uf2", chip=None):
    """The safety gate every mutating flow passes through (flash spec:
    verify_artifact)."""
    if kind == "uf2":
        return verify_uf2(path, chip=chip)
    if kind == "bin":
        return verify_bin(path, chip=chip)
    return False, "Unknown firmware kind {!r} (expected 'uf2' or 'bin')".format(
        kind)


def artifact_kind(path):
    ext = os.path.splitext(str(path))[1].lower()
    return ext[1:] if ext in (".uf2", ".bin") else None


# ------------------------------------------------------------ stack collision
def services_up():
    """Tracked services that would collide with a flash.

    Only klipper (klippy holds /dev/cu.* with a flock) and moonraker (it would
    restart klippy on top of a half-flashed board) are real collisions; the web
    UIs and caffeinate are not, so `flash` does not ask you to stop them.
    """
    state = process.running()
    if not state:
        return []
    return sorted(n for n, live in state.items()
                  if live and n in ("klipper", "moonraker"))


def refuse_while_running():
    """(ok, lines) - the gate in front of every mutating flow.

    The flash spec borrows cmd_lan's reasoning: "if the stack is running, the
    state you see is not the state you'll get". Flashing is the worst case -
    klippy keeps the port, so the board is by definition not in its bootloader.
    """
    up = services_up()
    if not up:
        return True, []
    return False, [
        "Stack is running ({}). Flashing would collide with klippy on the "
        "serial port, and a board klippy is talking to is by definition not in "
        "its bootloader.".format(", ".join(up)),
        "  Stop it first:   klipperformac down",
        "Nothing to flash.",
    ]


# ----------------------------------------------------------------- UF2 volume
def find_bootsel_volume():
    """A BOOTSEL volume with the right shape, else None."""
    for name in BOOTSEL_VOLUMES:
        mount = mount_point(name)
        if os.path.isdir(mount) and bootloader_volume_p(mount):
            return name
    for name in list_volumes():
        if bootloader_volume_p(mount_point(name)):
            return name
    return None


def _nap(sleep):
    """The poll delay, looked up when the wait runs.

    `sleep=None` means time.sleep *at call time*, not at import time: a test (or
    a future async caller) can hand in its own clock, and replacing flash's
    `time` module still takes effect down the whole chain."""
    return sleep if callable(sleep) else time.sleep


def _wait_volume(name, timeout, sleep=None):
    """Poll for the volume to appear; Ctrl-C is a clean cancel, not a traceback."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            if bootloader_volume_p(mount_point(name)):
                return True, ["  mounted."]
            if time.monotonic() >= deadline:
                return False, ["  timed out after {} s.".format(timeout)]
            _nap(sleep)(VOLUME_POLL)
        except KeyboardInterrupt:
            return False, ["  cancelled - nothing was written to the board."]


def _wait_volume_gone(mount, timeout, sleep=None):
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if not os.path.isdir(mount):
            return True
        try:
            _nap(sleep)(VOLUME_POLL)
        except KeyboardInterrupt:
            break
    return not os.path.isdir(mount)


def _bootloader_instructions():
    return [
        "To put an RP2040/RP2350 board into its bootloader:",
        "  1. Unplug the board's USB.",
        "  2. Hold the BOOTSEL button.",
        "  3. Replug USB. Release BOOTSEL.",
        "  4. Rerun: klipperformac flash uf2 --bin <file>",
    ]


def _volume_listing():
    names = list_volumes()
    if not names:
        return ["  No volumes are mounted under {}.".format(volumes_dir())]
    return ["  Mounted volumes now: " + ", ".join(names) +
            " (was one named differently? pass --volume NAME)"]


def _wait_any_volume(timeout, sleep=None):
    """Wait for ANY BOOTSEL-shaped volume to appear; returns (name|None, lines).

    The wizard form of the flow: the user is told the physical step, then the
    tool watches /Volumes while they hold BOOTSEL and replug. The shape check is
    the same one the write uses, so a USB stick appearing mid-wait is not
    mistaken for a board."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            name = find_bootsel_volume()
            if name:
                return name, ["  /Volumes/{} appeared.".format(name)]
            if time.monotonic() >= deadline:
                return None, ["  timed out after {} s. Nothing was "
                              "written.".format(timeout)]
            _nap(sleep)(VOLUME_POLL)
        except KeyboardInterrupt:
            return None, ["  cancelled - nothing was written to the board."]


def flash_uf2(bin_path, volume=None, timeout=VOLUME_TIMEOUT, dry_run=False,
              sleep=None):
    """Write a .uf2 to an RP2040/RP2350 BOOTSEL volume - the SKR Pico path.

    The physical step stays physical: press BOOTSEL, plug USB in, release
    BOOTSEL. That is upstream's own instruction (scripts/flash_usb.py
    RP2040_HELP: "entering bootloader mode (hold bootsel button during
    powerup), mount the device as a usb drive, and copy klipper.uf2 to the
    device"). Once the volume is there everything else is automatic: verify,
    write, wait for the ROM to reset the chip and unmount (that is the success
    signal), fall back to `diskutil eject` for macOS's delayed writes.
    """
    lines = []
    name = volume or find_bootsel_volume()
    if name is None and not dry_run:
        lines += (["No UF2 bootloader volume is mounted right now."]
                  + _bootloader_instructions() + _volume_listing())
        lines.append("Waiting up to {} s for it to appear...".format(timeout))
        name, wait_lines = _wait_any_volume(timeout, sleep=sleep)
        lines += wait_lines
        if name is None:
            return False, lines
    name = name or BOOTSEL_VOLUMES[0]
    ok, why = safe_volume_name(name)
    if not ok:
        return False, lines + [why]
    name = why
    mount = mount_point(name)
    chip = "rp2040" if name == "RPI-RP2" else None
    ok, why = verify_uf2(bin_path, chip=chip)
    if not ok:
        return False, lines + ["Refusing to write: " + why]
    size, _ = _size_of(bin_path)
    if dry_run:
        return True, lines + [
            "Dry run - nothing was written and no board was touched.",
            "  volume           /Volumes/{}".format(name),
            "  would copy       {} ({:,} bytes) -> {}/new.uf2".format(
                bin_path, size or 0, mount),
            "  then wait        up to {} s for the board to reset itself and "
            "unmount".format(RESET_TIMEOUT),
            "  fallback         diskutil eject {}".format(mount)]
    if os.path.isdir(mount) and not bootloader_volume_p(mount):
        return False, lines + [
            "/Volumes/{} is mounted but is not a UF2 bootloader volume (no "
            "INFO_UF2.TXT at its root), so nothing was written to it."
            .format(name)] + _bootloader_instructions()
    if not bootloader_volume_p(mount):
        lines.append("Waiting for /Volumes/{} (up to {} s)...".format(
            name, timeout))
        ok, wait_lines = _wait_volume(name, timeout, sleep=sleep)
        lines += wait_lines
        if not ok:
            return False, lines + _bootloader_instructions() \
                + _volume_listing()
    lines.append("Writing {} ({:,} bytes) to /Volumes/{}...".format(
        os.path.basename(bin_path), size or 0, name))
    try:
        _copy_to_volume(bin_path, mount)
    except (OSError, shutil.Error) as e:
        return False, lines + ["Could not write to /Volumes/{}: {}".format(
            name, getattr(e, "strerror", None) or e)]
    lines.append("Copied. The RP2040 ROM consumes the file and resets the "
                 "chip; the volume disappears when that works.")
    if _wait_volume_gone(mount, RESET_TIMEOUT, sleep=sleep):
        lines.append("Board reset into Klipper: /Volumes/{} is gone.".format(
            name))
        lines.append("Start the stack:  klipperformac up")
        return True, lines
    lines.append("  /Volumes/{} is still mounted; ejecting so the buffered "
                 "writes reach the chip.".format(name))
    code, out, err = _run(["diskutil", "eject", mount], DISKUTIL_TIMEOUT)
    if code is None:
        return False, lines + ["diskutil could not run: " + (err or "missing")]
    if code != 0:
        return False, lines + ["diskutil eject failed (exit {}):".format(code)] \
            + _tail(out, err) + ["  Eject /Volumes/{} by hand and check the "
                                 "board.".format(name)]
    lines += _tail(out, err, count=3)
    if _wait_volume_gone(mount, RESET_TIMEOUT, sleep=sleep):
        lines.append("Board reset into Klipper: /Volumes/{} is gone.".format(
            name))
        lines.append("Start the stack:  klipperformac up")
        return True, lines
    return False, lines + [
        "[!] /Volumes/{} is still there after the eject, so the firmware did "
        "not take: unplug the board, hold BOOTSEL, replug and retry."
        .format(name)]


def _copy_to_volume(bin_path, mount):
    """Copy + flush. The flush is not decoration: macOS buffers writes to a USB
    mass-storage device and the boot ROM reads the file as soon as the host
    closes it."""
    target = os.path.join(mount, "new.uf2")
    shutil.copyfile(bin_path, target)
    with open(target, "rb") as fh:
        os.fsync(fh.fileno())


# ------------------------------------------------------------------ DFU/serial
def _tail(out, err, count=10):
    """Last few non-empty lines of a flasher's output (cam.py:211-214 does the
    same for the camera helper): dfu-util and stm32flash say the important part
    at the end, and it is the user's only clue when a flash fails."""
    lines = (out + err).splitlines()
    return ["  " + raw.strip()[:200] for raw in lines if raw.strip()][-count:]


def dfu_argv(bin_path, vidpid, alt=None, address=None, leave=False,
             dfu_util="dfu-util"):
    """dfu-util argv, mirroring upstream `call_dfuutil` (scripts/flash_usb.py):
    ["dfu-util"] + flags + ["-D", binfile], flags built as
    ["-d", "," + vidpid] + extra, extra being ["-R", "-a", alt] for F1 and
    ["-R", "-a", alt, "-s", "<app address>:leave"] for F4/F0."""
    flags = ["-d", "," + vidpid]
    if alt is not None:
        flags += ["-a", str(alt)]
    if address:
        flags += ["-s", address + (":leave" if leave else "")]
    flags.append("-R")
    return [dfu_util] + flags + ["-D", bin_path]


def dfu_plan(bin_path, chip=None, vidpid=None, alt=None, address=None,
             leave=None):
    """(ok, argv_or_reason) from a chip preset or explicit flags."""
    preset = DFU_PRESETS.get(chip) if chip else None
    if chip and preset is None:
        if chip in _NOT_DFU_HINTS:
            return False, ("{} is not flashed over DFU - use `klipperformac "
                           "flash {}`.".format(chip, _NOT_DFU_HINTS[chip]))
        return False, "Unknown --chip {!r}; known: {}".format(
            chip, ", ".join(sorted(DFU_PRESETS)))
    if vidpid is None:
        vidpid = (preset or {}).get("vidpid")
    if alt is None:
        alt = (preset or {}).get("alt")
    if address is None:
        address = (preset or {}).get("address")
    if leave is None:
        leave = bool((preset or {}).get("leave"))
    if not vidpid:
        return False, ("Nothing to target: pass --vidpid NNNN:MMMM, or a chip "
                       "that implies one (" + ", ".join(sorted(DFU_PRESETS)) +
                       ").")
    if not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", vidpid):
        return False, ("--vidpid {!r} must look like 0483:df11 - four hex "
                       "digits, colon, four hex digits, the same shape "
                       "upstream's flash_usb.py matches before calling "
                       "dfu-util.".format(vidpid))
    if address is not None and not re.fullmatch(r"0[xX][0-9a-fA-F]{1,8}",
                                                address):
        return False, ("--address {!r} must be a hex address like 0x8004000."
                       .format(address))
    return True, dfu_argv(bin_path, vidpid.lower(), alt=alt,
                          address=address.lower() if address else None,
                          leave=leave)


def flash_dfu(bin_path, chip=None, vidpid=None, alt=None, address=None,
              leave=None, dry_run=False):
    """Flash a .bin to a board in DFU mode (STM32F407/F072, stm32duino F103)."""
    ok, why = verify_bin(bin_path, chip=chip if chip in CHIP_RANGES else None)
    if not ok:
        return False, ["Refusing to write: " + why]
    ok, argv = dfu_plan(bin_path, chip=chip, vidpid=vidpid, alt=alt,
                        address=address, leave=leave)
    if not ok:
        return False, [argv]
    tool = find_tool("dfu-util")
    if tool is None:
        return False, [
            "dfu-util is not installed, so nothing was run and nothing was "
            "written.",
            "  brew install dfu-util",
            "(this command never installs anything - run that yourself, then "
            "rerun.)",
            "The command it would have run: " + " ".join(
                ["dfu-util"] + argv[1:])]
    argv[0] = tool
    return run_flasher(argv, dry_run)


def upstream_flash_script():
    """Path of an upstream flasher script that really exists, or None.

    Read-only existence check - it proves the pristine checkout is where we say
    it is before we execute anything. The name the task brief used,
    scripts/flash_stm32.py, is not in this checkout, which is exactly why the
    candidates are probed instead of assumed.
    """
    for rel in UPSTREAM_SERIAL_FLASH_SCRIPTS:
        cand = paths.KLIPPER / rel
        if cand.is_file():
            return cand
    return None


def serial_plan(bin_path, port, chip="stm32f407", tool="auto"):
    """(ok, argv_or_reason) for the STM32 3.3V serial (boot0) bootloader.

    `auto` prefers upstream's own script run through the stack venv python
    (pyserial is that venv's dependency, so nothing new is installed and no
    upstream file is edited - it is executed the way its Makefile executes it),
    and otherwise falls back to brew `stm32flash`, upstream's documented
    command for the serial bootloader (src/stm32/Makefile:109-111,
    docs/Bootloaders.md:307)."""
    problems = []
    if tool in ("auto", "upstream"):
        script = upstream_flash_script()
        if script is None:
            problems.append("no upstream flasher script under {} (this "
                            "checkout ships {})".format(
                                paths.KLIPPER,
                                ", ".join(UPSTREAM_SERIAL_FLASH_SCRIPTS)))
        elif not paths.VENV_PY.is_file():
            problems.append("the stack venv python is missing: {} (run: "
                            "klipperformac setup)".format(paths.VENV_PY))
        else:
            argv = [str(paths.VENV_PY), str(script), "-t", chip, "-d", port,
                    bin_path]
            if tool == "upstream":
                return True, argv
            # flash_usb.py turns a /dev/cu.* port into a /sys path before it
            # does anything, so on macOS it cannot drive a serial bootloader -
            # it is kept behind --tool upstream for a board addressed by
            # vidpid, and auto does not choose it for a serial port.
            problems.append("{} resolves the device through /sys "
                            "(Linux-only)".format(
                                _rel(script)))
    if tool in ("auto", "stm32flash"):
        stm = find_tool("stm32flash")
        if stm is None:
            problems.append("stm32flash is not installed")
        else:
            return True, [stm, "-w", bin_path, "-v", "-g", "0", port]
    return False, "; ".join(problems)


def _rel(path):
    try:
        return str(path.relative_to(paths.KLIPPER))
    except ValueError:
        return str(path)


def run_flasher(argv, dry_run, lines=None):
    """Print-then-executes for any external flasher."""
    base = list(lines or [])
    if dry_run:
        return True, base + ["Dry run - would run:", "  " + " ".join(argv),
                             "(no board was touched)"]
    code, out, err = _run(argv, FLASH_TOOL_TIMEOUT)
    if code is None:
        return False, base + ["Could not run {}: {}".format(argv[0], err)]
    if code != 0:
        return False, base + ["{} failed (exit {}):".format(
            os.path.basename(argv[0]), code)] + _tail(out, err)
    return True, base + ["{} finished OK.".format(os.path.basename(argv[0]))] \
        + _tail(out, err, count=3)


KEY_PREFIXES = ("usb:", "loc:", "vidpid:")


def resolve_port(port):
    """(path_or_None, note) for --port.

    A board's /dev/cu.* name changes on every replug - that rename is the whole
    reason detect's identity layer exists - so --port accepts the stable binding
    key `flash detect --json` prints ("usb:1d50:614e:5044506110A45B1C") as well
    as the literal path. Keys are resolved through detect.find_by_identity()
    when that symbol is available, then by comparing the key against the rows
    the identity layer hands back; a plain path is used as given.
    """
    text = (port or "").strip()
    if not text:
        return None, "no port was given"
    if not text.startswith(KEY_PREFIXES):
        return text, ""
    found = find_board_by_identity(text)
    if isinstance(found, str) and found:
        return found, ""
    if isinstance(found, list) and len(found) == 1:
        return str(found[0]), ""
    for row in identity_rows() or []:
        if row.get("key") == text and row.get("path"):
            return str(row["path"]), ""
    return None, text


def port_present(path):
    """Is that serial node there right now? (existence only - never opened)"""
    return os.path.exists(path)


def flash_serial(bin_path, port, chip="stm32f407", tool="auto", dry_run=False):
    """Flash a .bin through the STM32 serial (boot0) bootloader."""
    resolved, note = resolve_port(port)
    if resolved is None:
        return False, ["No board now matches {} - replug it and check "
                       "`klipperformac flash detect`.".format(note or port)]
    port = resolved
    if not re.fullmatch(r"/dev/[\w./-]{1,64}", port):
        return False, ["--port {!r} is not a /dev/... serial port path, and "
                       "it is not an identity key either (usb:.../loc:... from "
                       "`flash detect --json`).".format(port)]
    if not port_present(port):
        return False, ["{} does not exist. Serial ports right now: {}".format(
            port, ", ".join(p["path"] for p in detect.scan()) or "(none)")]
    ok, why = verify_bin(bin_path, chip=chip if chip in CHIP_RANGES else None)
    if not ok:
        return False, ["Refusing to write: " + why]
    ok, argv = serial_plan(bin_path, port, chip=chip, tool=tool)
    if not ok:
        return False, [
            "No usable STM32 serial flasher: " + argv + ".",
            "  install one:   brew install stm32flash",
            "  or flash over DFU instead: klipperformac flash dfu --bin {} "
            "--chip {}".format(bin_path, chip),
            "[i] upstream is not modified and not required: its "
            "scripts/flash_usb.py drives this same bootloader through Linux "
            "/sys paths, which macOS does not have.",
        ]
    lines = ["The STM32 only answers on this port with BOOT0=1 and a reset "
             "(docs/Bootloaders.md:300-317). Nothing is written until it does."]
    return run_flasher(argv, dry_run, lines=lines)


def probe_runtime(port, baud=250000, timeout=PROBE_TIMEOUT):
    """Read-only handshake with a Klipper MCU over its serial port.

    Explicitly separate from `flash detect`: detection never opens a port, while
    this command does. It is for the "stack is down, is this really a living
    Klipper MCU?" question that identity alone cannot answer.
    """
    resolved, note = resolve_port(port)
    if resolved is None:
        return False, ["No board now matches {} - replug it and check "
                       "`klipperformac flash detect`.".format(note or port)]
    port = resolved
    if not re.fullmatch(r"/dev/[\w./-]{1,64}", port):
        return False, ["--port {!r} is not a /dev/... serial port path, and "
                       "it is not an identity key either (usb:.../loc:... from "
                       "`flash detect --json`).".format(port)]
    if not port_present(port):
        return False, ["{} does not exist. Serial ports right now: {}".format(
            port, ", ".join(p["path"] for p in detect.scan()) or "(none)")]
    venv_py = str(paths.VENV_PY)
    if not (os.path.isfile(venv_py) and os.access(venv_py, os.X_OK)):
        return False, ["No stack venv python at {} (run: klipperformac setup)"
                       .format(venv_py)]
    klippy_dir = paths.KLIPPER / "klippy"
    if not klippy_dir.is_dir():
        return False, ["No klippy sources at {} (run: klipperformac setup)"
                       .format(klippy_dir)]
    code, out, err = _run([venv_py, "-c", _PROBE_RUNTIME_SRC,
                           str(klippy_dir), port, str(baud), str(timeout)],
                          timeout)
    if code is None:
        text = " ".join(str(err).split())
        if "timed out" in text.lower():
            return False, [
                "MCU probe failed: {} did not complete a Klipper handshake."
                .format(port),
                "  error: timed out after {}s".format(timeout),
                "  The USB device is present, but the firmware never answered "
                "identify_response on this link.",
            ]
        return False, ["Could not run the MCU probe: {}".format(text or err)]
    try:
        payload = json.loads((out or "").strip())
    except Exception:
        return False, ["MCU probe did not return JSON.",
                       "stdout: {}".format(" ".join((out or "").split()) or "(empty)"),
                       "stderr: {}".format(" ".join((err or "").split()) or "(empty)")]
    if payload.get("ok"):
        lines = [
            "MCU probe OK: {} answered directly.".format(port),
            "  MCU:        {}".format(payload.get("mcu") or "(unknown)"),
            "  CLOCK_FREQ: {}".format(payload.get("clock_freq")
                                      if payload.get("clock_freq") is not None
                                      else "(unknown)"),
        ]
        version = payload.get("version") or ""
        if version:
            lines.append("  Version:    {}".format(version))
        builds = payload.get("build_versions") or ""
        if builds:
            lines.append("  Build:      {}".format(builds))
        return True, lines
    return False, [
        "MCU probe failed: {} did not complete a Klipper handshake.".format(port),
        "  error: {}".format(payload.get("error") or payload.get("error_type")
                             or "unknown error"),
        "  This usually means the board is not running a compatible Klipper "
        "serial image, or the USB link resets before identify_response.",
    ]


# ---------------------------------------------------------------- enumeration
def ioreg_usb_devices(timeout=IOREG_TIMEOUT):
    """Attached USB devices: [{vidpid, vid, pid, product, serial, name}].

    `ioreg -p IOUSB` is the whole USB plane (~135 KB, ~0.1 s here) and -x makes
    it an XML plist, so there is no text format to mis-parse. Read-only: ioreg
    prints the registry, it never talks to the device.
    """
    code, raw, _err = _run_bytes(["ioreg", "-a", "-x", "-l", "-p", "IOUSB"],
                                 timeout)
    if code != 0:
        return []
    try:
        tree = plistlib.loads(raw)
    except Exception:
        return []
    rows = []
    _walk_usb(tree, rows)
    return rows


def _walk_usb(node, rows):
    if isinstance(node, dict):
        vid, pid = node.get("idVendor"), node.get("idProduct")
        if isinstance(vid, int) and isinstance(pid, int):
            location = node.get("locationID")
            rows.append({"vid": "%04x" % vid, "pid": "%04x" % pid,
                         "vidpid": "%04x:%04x" % (vid, pid),
                         "product": node.get("kUSBProductString")
                         or node.get("USB Product Name") or "",
                         "vendor": node.get("kUSBVendorString")
                         or node.get("USB Vendor Name") or "",
                         "serial": node.get("USB Serial Number")
                         or node.get("kUSBSerialNumberString") or "",
                         "location": ("%08x" % (location & 0xFFFFFFFF))
                         if isinstance(location, int) else "",
                         "name": node.get("IORegistryEntryName") or ""})
        for value in node.values():
            if isinstance(value, (dict, list)):
                _walk_usb(value, rows)
    elif isinstance(node, list):
        for value in node:
            _walk_usb(value, rows)


def identity_rows():
    """Every USB serial node as identity dicts, through detect's identity
    layer - or None when that layer is not usable here.

    The multi-MCU work landed `detect.all_identities()` and `detect.identity()`
    with rows of {path, vid_pid, serial, name, vendor, location, key} and one
    rule this module depends on: no port is ever opened (klippy holds it with a
    flock). Both symbols are looked up defensively - in another working tree
    they may not exist yet - and the answer is `None`, meaning "fall back to
    path-only enumeration", never an exception.
    """
    fn = getattr(detect, "all_identities", None)
    if callable(fn):
        try:
            rows = fn(timeout=IDENTITY_TIMEOUT)
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)] or None
        except Exception:
            pass
    one = getattr(detect, "identity", None)
    if not callable(one):
        return None
    rows = []
    for port in detect.scan():
        try:
            row = one(port.get("path"))
        except Exception:
            continue
        if isinstance(row, dict) and row.get("path"):
            rows.append(row)
    return rows or None


def _norm_vidpid(row):
    """Lower-case 'vvvv:pppp' from an identity row, however that row spells it.

    detect.identity() returns 'vid_pid' as '1D50:614E' (uppercase, the lsusb
    shape); the bootloader tables here are lower-case, so everything is folded
    before comparison.
    """
    for key in ("vid_pid", "vidpid"):
        value = row.get(key)
        if isinstance(value, str) and ":" in value:
            return value.strip().lower()
    vid, pid = _hex4(row.get("vid")), _hex4(row.get("pid"))
    return "{}:{}".format(vid, pid) if vid and pid else ""


def find_board_by_identity(key):
    """detect.find_by_identity() for one binding key, or None.

    Called with the shape that function actually documents -
    `find_by_identity(vid_pid, serial=None)` -> a /dev path or None - see
    `resolve_port()` for who calls this and why.

    The three key shapes detect.identity() produces are not equally resolvable:
      * `usb:1d50:614e:<SER>`  -> by-id, the case this answers.
      * `vidpid:1d50:614e`     -> by-id with no serial, which find_by_identity()
                                  itself refuses to guess between two identical
                                  boards; its answer (or None) is passed on.
      * `loc:<locationID>`     -> by PHYSICAL path. There is no VID:PID to hand
                                  over, so this returns None and `resolve_port()`
                                  answers it from the identity rows instead -
                                  which is the honest order of events, since a
                                  location is only stable while the cable stays
                                  in that port.
    """
    fn = getattr(detect, "find_by_identity", None)
    if not callable(fn):
        return None
    parts = str(key or "").split(":")
    prefix = parts[0].strip().lower()
    if prefix == "loc":
        return None
    if len(parts) < 3:
        return None
    if prefix in ("usb", "vidpid"):
        vid_pid = ":".join(parts[1:3])
    else:
        vid_pid = ":".join(parts[-2:])
    serial = ":".join(parts[3:]) if prefix == "usb" and len(parts) > 3 else None
    try:
        return fn(vid_pid, serial)
    except Exception:
        return None


def _describe(path):
    fn = getattr(detect, "_describe", None)
    if not callable(fn):
        return ""
    try:
        return str(fn(path))
    except Exception:
        return ""


def _hex4(value):
    """'1d50' from 0x1d50, 7504 or '1d50'; '' when there is nothing usable."""
    if value in (None, "", "-"):
        return ""
    try:
        return "%04x" % int(str(value), 16) if isinstance(value, str) \
            else "%04x" % int(value)
    except (TypeError, ValueError):
        return ""


def configured_serials(cfg_path=None):
    """[(section, serial)] from printer.cfg, [mcu] first.

    Uses detect.mcu_sections()/serial_lines() when the multi-MCU work has
    landed them, otherwise the same two patterns locally: a flash report must
    not depend on that module, and `flash` never writes config anyway.
    """
    path = cfg_path or paths.PRINTER_CFG
    try:
        text = paths.read_config_text(path)
    except (OSError, ValueError):
        return []
    sections_fn = getattr(detect, "mcu_sections", None)
    lines_fn = getattr(detect, "serial_lines", None)
    if callable(sections_fn) and callable(lines_fn):
        try:
            rows = [(name, value) for name, value, _line, _dup
                    in lines_fn(text)]
            return rows
        except (TypeError, ValueError, IndexError):
            pass
    rows = []
    section = None
    for raw in text.splitlines():
        stripped = raw.strip()
        head = re.fullmatch(r"\[mcu(?:[ \t]+([^\s#:;]+))?\]", stripped)
        if head:
            section = head.group(1) or "mcu"
            continue
        if stripped.startswith("["):
            section = None
            continue
        if section:
            value = re.match(r"serial[ \t]*:[ \t]*(\S+)", stripped)
            if value:
                rows.append((section, value.group(1)))
    return rows


def claimed_by_section(sections=None):
    """{device_path: "[mcu]"} for the sections in printer.cfg."""
    out = {}
    for name, serial in (sections if sections is not None
                         else configured_serials()):
        out.setdefault(serial, "[mcu]" if name == "mcu"
                       else "[mcu {}]".format(name))
    return out


def classify_targets(ports, usb, volumes, claimed=None):
    """Pure classifier behind `flash detect`: what is attached, in which state.

    Takes its three enumerations as arguments on purpose - the offline tests
    feed it saved ioreg samples and a fake /Volumes listing, so no test can
    depend on (or touch) what is plugged into this Mac.
    """
    claimed = dict(claimed or {})
    rows = []
    for volume in volumes or []:
        mount = mount_point(volume)
        if bootloader_volume_p(mount):
            rows.append({"kind": "uf2-volume", "name": volume, "mount": mount,
                         "vidpid": "", "serial": "",
                         "product": "UF2 bootloader volume",
                         "state": "uf2-bootloader", "action": "flash uf2",
                         "hint": "board is in BOOTSEL: safe to write a .uf2"})
    for dev in usb or []:
        vidpid = dev.get("vidpid") or ""
        if vidpid in UF2_BOOTSEL_VIDPID:
            rows.append({"kind": "usb", "name": "", "mount": "",
                         "vidpid": vidpid, "serial": dev.get("serial", ""),
                         "location": dev.get("location", ""),
                         "product": dev.get("product", ""),
                         "state": "uf2-bootloader", "action": "flash uf2",
                         "hint": "{} in BOOTSEL (mass storage)".format(
                             UF2_BOOTSEL_VIDPID[vidpid])})
        elif vidpid in DFU_VIDPID:
            rows.append({"kind": "usb", "name": "", "mount": "",
                         "vidpid": vidpid, "serial": dev.get("serial", ""),
                         "location": dev.get("location", ""),
                         "product": dev.get("product", ""), "state": "dfu",
                         "action": "flash dfu", "hint": DFU_VIDPID[vidpid]})
    for port in ports or []:
        row = port if isinstance(port, dict) else {"path": str(port)}
        path = str(row.get("path") or "")
        vidpid = _norm_vidpid(row)
        state, action, hint = "app", "flash uf2 (BOOTSEL) or flash serial", \
            "running firmware (Klipper USB-CDC class)"
        if not vidpid:
            # No USB identity behind the node: a Bluetooth RFCOMM port, an
            # Apple IR stack, a virtual serial. Claiming "running firmware"
            # would be a guess, and a guess here is a refused flash later.
            state, action = "unknown", "not a board unless you know better"
            hint = "no USB identity for this node (Bluetooth or virtual serial)"
        elif vidpid in UF2_BOOTSEL_VIDPID:
            state, action, hint = ("uf2-bootloader", "flash uf2",
                                   "board is in its bootloader, not in Klipper")
        elif vidpid in DFU_VIDPID:
            state, action, hint = ("dfu", "flash dfu",
                                   "board is in its bootloader, not in Klipper")
        rows.append({"kind": "serial", "name": "", "mount": "", "path": path,
                     "vidpid": vidpid,
                     "serial": row.get("serial") or row.get("usb_serial") or "",
                     "product": row.get("name") or row.get("product")
                     or _describe(path),
                     "state": state, "action": action, "hint": hint,
                     "key": row.get("key") or "",
                     "location": row.get("location") or "",
                     "mcu": claimed.get(path, "")})
    return rows


def detect_boards():
    """Everything a flash could be pointed at, plus whether it may be pointed.

    Order of enumeration cost: /Volumes is free, ioreg ~0.1 s, the identity
    layer 0.05-1.2 s - and it is the only step that can spawn a subprocess.
    """
    ident = identity_rows()
    ports = ident if ident else detect.scan()
    volumes = list_volumes()
    usb = ioreg_usb_devices()
    return {"ports": ports, "identity": bool(ident), "usb": usb,
            "volumes": volumes,
            "targets": classify_targets(ports, usb, volumes,
                                        claimed=claimed_by_section()),
            "stack_up": services_up(),
            "bootloader_volume": find_bootsel_volume()}


def detect_lines(report, as_json=False):
    if as_json:
        return [json.dumps(report, indent=2, sort_keys=True)]
    lines = []
    if not report.get("targets"):
        lines.append("No board found: no USB-CDC serial node, no DFU device "
                     "and no UF2 bootloader volume under {}."
                     .format(volumes_dir()))
        lines += _bootloader_instructions()
    for row in report.get("targets", []):
        where = (row.get("path") or row.get("mount") or row.get("vidpid")
                 or "?")
        tail = []
        if row.get("vidpid"):
            tail.append("VID:PID=" + row["vidpid"])
        if row.get("serial"):
            tail.append("SER=" + row["serial"])
        if row.get("location"):
            tail.append("LOC=" + row["location"])
        if row.get("mcu"):
            tail.append("-> " + row["mcu"])
        lines.append("{:<22} {:<16} {:<26} {}".format(
            where[:22], row.get("state", "?"), (row.get("product") or "")[:26],
            "  ".join(tail)))
        lines.append("{:<22} {:<16} {}{}".format(
            "", "", row.get("hint", ""),
            ("  key=" + row["key"]) if row.get("key") else ""))
    if not report.get("identity"):
        lines.append("[i] identity layer unavailable (detect.all_identities() "
                     "missing or IOKit unreadable): ports are listed by path "
                     "only, with no VID:PID/SER/key.")
    if report.get("stack_up"):
        lines.append("[!] stack is running ({}) - every flash subcommand "
                     "refuses until `klipperformac down`."
                     .format(", ".join(report["stack_up"])))
    elif report.get("ports"):
        lines.append("[i] stack is down: flashing is allowed.")
    return lines


# ------------------------------------------------------------------ toolchain
def toolchain_status():
    """What exists for building/flashing, and what is missing. Never installs.

    `ready` means the transports a user can flash with today: a board in
    BOOTSEL needs nothing at all, DFU needs dfu-util, serial needs
    stm32flash. The cross compiler only matters to `flash build`, which this
    round prints instead of running.
    """
    notes = []
    missing = []
    formulas = {"dfu-util": "dfu-util", "stm32flash": "stm32flash",
                "bossac": "bossa", "arm-none-eabi-gcc": "arm-none-eabi-gcc",
                "arm-none-eabi-objcopy": "arm-none-eabi-gcc"}
    found = {}
    for name, formula in formulas.items():
        where = find_tool(name)
        found[name] = where
        if where is None and formula not in missing:
            missing.append(formula)
    if brew_path() is None:
        notes.append("Homebrew not found: no flasher can be installed until it "
                     "is (https://brew.sh).")
    if not paths.KLIPPER.is_dir():
        notes.append("No upstream klipper checkout at {} (run: klipperformac "
                     "setup)".format(paths.KLIPPER))
    uf2_h = (paths.KLIPPER / "lib" / "pico-sdk" / "boot" / "uf2.h").is_file()
    if not uf2_h:
        notes.append("klipper's lib/pico-sdk/boot/uf2.h is absent: the UF2 "
                     "magic checks still run (the constants are built in) but "
                     "they cannot be re-verified against this checkout.")
    script = upstream_flash_script()
    volume_flow = bool(find_tool("diskutil")) and os.path.isdir(volumes_dir())
    return {"ready": bool(found["dfu-util"] or found["stm32flash"]
                         or volume_flow),
            "flashable": bool(volume_flow),
            "missing": missing, "found": found, "brew": brew_path(),
            "uf2_header": uf2_h,
            "elf2uf2_source": (paths.KLIPPER / "lib" / "elf2uf2" /
                               "main.cpp").is_file(),
            "upstream_flash_script": str(script) if script else None,
            "venv_py": paths.VENV_PY.is_file(), "volume_flow": volume_flow,
            "firmware_dir": str(paths.APP_HOME / FIRMWARE_DIR_NAME),
            "notes": notes}


def venv_pyserial(timeout=VENV_PROBE_TIMEOUT):
    """Version of pyserial inside the STACK venv, or None.

    pyserial is not our dependency to add - installer.py installs upstream's
    scripts/klippy-requirements.txt into that venv and pyserial is in it, which
    is what makes `flash serial --tool upstream` free."""
    if not paths.VENV_PY.is_file():
        return None
    code, out, _err = _run([str(paths.VENV_PY), "-c",
                            "import serial; print(serial.__version__)"],
                           timeout)
    return out.strip() if code == 0 else None


def _doctor_row(mark, label, text):
    """One aligned doctor line (cli.py's cmd_doctor uses the same [ok]/[!]
    shape; the label column keeps the report readable at a glance)."""
    return "[{}] {:<19} {}".format(mark, label, text)


def doctor_lines():
    """[ok]/[!]/[i] lines for `klipperformac flash doctor` (`flash status` is the
    same report - the spec's other name for it)."""
    st = toolchain_status()
    pyserial = venv_pyserial()
    lines = []
    if st["volume_flow"]:
        lines.append(_doctor_row("ok", "UF2 volume flow:",
                                 "/Volumes + diskutil - the RP2040/RP2350 "
                                 "path needs no tool at all"))
    else:
        lines.append(_doctor_row("!", "UF2 volume flow:",
                                 "unavailable - is {} a directory, and is "
                                 "diskutil installed?".format(volumes_dir())))
    for name in ("dfu-util", "stm32flash", "bossac"):
        where = st["found"].get(name)
        if where:
            lines.append(_doctor_row("ok", name + ":", where))
        elif name == "bossac":
            lines.append(_doctor_row(
                "!", "bossac:",
                "missing - SAM3X/SAM4S boards only, and `flash bossa` is not "
                "implemented yet (brew install bossa)"))
        else:
            lines.append(_doctor_row("!", name + ":",
                                     "missing - brew install {}".format(name)))
    gcc = st["found"].get("arm-none-eabi-gcc")
    lines.append(_doctor_row(
        "ok" if gcc else "i", "cross compiler:",
        gcc or "missing - only `flash build` would need it, and build is "
               "--dry-run this round"))
    lines.append(_doctor_row("ok" if st["brew"] else "!", "Homebrew:",
                             st["brew"] or "not found"))
    lines.append(_doctor_row(
        "ok" if st["venv_py"] else "!", "stack venv python:",
        "{}{}".format(paths.VENV_PY, "" if st["venv_py"]
                     else " (run: klipperformac setup)")))
    if pyserial:
        lines.append(_doctor_row("ok", "pyserial (venv):",
                                 "{} - upstream's own flasher can be run "
                                 "through this venv".format(pyserial)))
    elif st["venv_py"]:
        lines.append(_doctor_row("!", "pyserial (venv):",
                                 "not importable in {}: `flash serial --tool "
                                 "upstream` will not work".format(paths.VENV_PY)))
    else:
        lines.append(_doctor_row("i", "pyserial (venv):", "not probed (no venv)"))
    lines.append(_doctor_row(
        "ok" if st["upstream_flash_script"] else "!", "upstream flasher:",
        st["upstream_flash_script"] or "none under {} (this checkout's "
        "flash_usb.py needs Linux /sys paths)".format(paths.KLIPPER)))
    lines.append(_doctor_row(
        "ok" if st["uf2_header"] else "!", "klipper uf2.h:",
        paths.KLIPPER / "lib" / "pico-sdk" / "boot" / "uf2.h"))
    for note in st["notes"]:
        lines.append("[!] " + note)
    lines.append(_doctor_row("i", "firmware folder:", st["firmware_dir"]))
    lines.append(_doctor_row(
        "i", "", "nothing here installs anything, builds anything, or writes "
        "to a board. `klipperformac flash detect` says what is attached; "
        "`doctor` exits {} when the UF2 transport is usable."
        .format(0 if st["flashable"] else 1)))
    return lines


# ---------------------------------------------------------------------- build
def defconfig_name(raw):
    """(ok, filename_or_reason) for `--defconfig`.

    Upstream's board configs all live in klipper/config/ with a `.cfg` suffix -
    228 of them in the pinned checkout, and no `.defconfig` files at all (the
    word "defconfig" is what Klipper calls the file, not its extension). So a
    name typed without the suffix is completed rather than refused, and a name
    with a separator or a '..' in it is refused: this becomes a filename inside
    the pristine checkout, and a preview that reads /etc/passwd as a board
    config is not a preview.
    """
    text = str(raw or "").strip()
    if not text:
        return False, "No --defconfig given. The board configs are in {} " \
                      "(e.g. generic-bigtreetech-skr-pico-v1.0.cfg for the " \
                      "SKR Pico).".format(paths.KLIPPER /
                                          KLIPPER_DEFCONFIG_DIR)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", text):
        return False, DEFCONFIG_NAME_REASON.format(raw)
    if not text.endswith(KLIPPER_DEFCONFIG_SUFFIX):
        text += KLIPPER_DEFCONFIG_SUFFIX
    return True, text


def defconfig_suggestions(cfg_dir, wanted, limit=6):
    """Names in klipper/config/ close to `wanted` - the list a typo needs."""
    stem = os.path.splitext(wanted)[0]
    try:
        names = sorted(p.name for p in cfg_dir.iterdir()
                       if p.suffix == KLIPPER_DEFCONFIG_SUFFIX and p.is_file())
    except OSError:
        return []
    close = difflib.get_close_matches(stem, [os.path.splitext(n)[0]
                                             for n in names], n=limit)
    if not close and "-" in stem:
        tokens = [t for t in stem.split("-") if len(t) > 3]
        close = [n for n in names
                 if all(t in os.path.splitext(n)[0] for t in tokens)][:limit]
    return [n + KLIPPER_DEFCONFIG_SUFFIX if not n.endswith(
        KLIPPER_DEFCONFIG_SUFFIX) else n for n in close]


def build_plan(defconfig=None, chip="rp2040", jobs=DEFAULT_JOBS, overrides=()):
    """The exact commands `flash build` would run - printed, never executed.

    The shape comes from the flash spec's build function plus upstream's own
    Makefile: copy the pristine defconfig to <klipper>/.config (gitignored -
    klipper/.gitignore lists `.config` and `out`, which is why a build keeps
    upstream pristine), expand it with the vendored kconfiglib
    (`make olddefconfig`, Makefile:114-115), then `make -jN out/klipper.uf2`.
    Real toolchain invocation is out of scope this round, so this is a preview;
    the one thing it does enforce is that the defconfig exists upstream.
    """
    if not paths.KLIPPER.is_dir():
        return False, ["No upstream klipper checkout at {} (run: klipperformac "
                       "setup)".format(paths.KLIPPER)]
    if chip not in BUILD_CHIPS:
        return False, ["--chip {!r}: this round previews builds for {}."
                       .format(chip, ", ".join(BUILD_CHIPS))]
    ok, name = defconfig_name(defconfig)
    if not ok:
        return False, [name]
    artifact = "klipper." + ("uf2" if chip.startswith("rp") else "bin")
    cfg = paths.KLIPPER / KLIPPER_DEFCONFIG_DIR / name
    if not cfg.is_file():
        cfg_dir = paths.KLIPPER / KLIPPER_DEFCONFIG_DIR
        hints = defconfig_suggestions(cfg_dir, name)
        return False, ["{} is not a board config in {}.".format(
            name, cfg_dir)] + (["  did you mean: " + ", ".join(hints)]
                               if hints else [])
    st = toolchain_status()
    lines = [
        "Would build {} - NOT run this round:".format(
            paths.KLIPPER / "out" / artifact),
        "  cp {} {}".format(cfg, paths.KLIPPER / ".config"),
    ]
    for key, value in overrides:
        lines.append("  rewrite .config: {}={}".format(key, value))
    lines += [
        "  make -C {} olddefconfig     # lib/kconfiglib/olddefconfig.py, "
        "Makefile:114-115".format(paths.KLIPPER),
        "  make -C {} -j{} out/{}".format(paths.KLIPPER, jobs, artifact),
        "      env PATH=<brew prefix>/bin:$PATH PYTHON={} "
        "CROSS_PREFIX=arm-none-eabi-".format(sys.executable),
        "      (CPATH is deliberately NOT inherited: firmware compiles against "
        "real arm-none-eabi headers, not our macOS shims)",
        "      then verify_artifact() the result and keep a copy in {}/"
        "klipper-{}-<utc>.{}".format(st["firmware_dir"], chip,
                                      artifact.rsplit(".", 1)[-1]),
    ]
    missing = sorted(set(st["missing"]))
    if missing:
        lines.append("[!] missing toolchain pieces (this command installs "
                     "nothing): " + ", ".join(missing))
        lines.append("  brew install " + " ".join(missing))
    lines.append("[!] `flash build` will not compile yet. The flow writes "
                 "only files upstream itself gitignores (.config and out/), so "
                 "the checkout stays pristine - but it needs a real run, a "
                 "verified arm-none-eabi-gcc (libc_nano.a for cortex-m0plus) "
                 "and a doctor pass on upstream's git status before it is "
                 "shipped. Until then bring the firmware: --bin from your "
                 "board vendor.")
    return True, lines


# ------------------------------------------------------------------- CLI glue
def artifact_arg(args):
    """--bin PATH, or the positional PATH the spec uses. Exactly one."""
    bin_path = getattr(args, "bin", None)
    positional = getattr(args, "path", None)
    if bin_path and positional:
        return False, "Give the firmware file once: --bin or the positional."
    return True, str(bin_path or positional or "")


def _overrides(args):
    out = []
    for item in getattr(args, "set", None) or []:
        if "=" in item:
            key, value = item.split("=", 1)
            if re.fullmatch(r"CONFIG_[A-Za-z0-9_]+", key):
                out.append((key, value))
    return out


def _print(payload):
    for line in ([payload] if isinstance(payload, str) else payload):
        print(line)


def guarded(flow):
    """The stack gate in front of every mutating flow, plus the two ways a human
    can leave: Ctrl-C mid-wait, and anything unexpected inside the flow."""
    ok, lines = refuse_while_running()
    if not ok:
        return ok, lines
    try:
        return flow()
    except KeyboardInterrupt:
        return False, ["Cancelled. Nothing further was written - if the .uf2 "
                       "had already been copied the board either reset by "
                       "itself or needs a power cycle."]
    except Exception as e:  # the spec's contract: never raise at the user
        return False, ["Unexpected error: {}".format(e)]


def dispatch(args):
    """cli.cmd_flash() hands the parsed namespace here; the subparser owns the
    flags, this owns what they mean (the backup.py:1162 pattern)."""
    action = getattr(args, "action", None)
    if action == "detect":
        _print(detect_lines(detect_boards(),
                            as_json=bool(getattr(args, "json", False))))
        return 0
    if action in ("doctor", "status"):
        lines = doctor_lines()
        _print(lines)
        return 0 if toolchain_status()["flashable"] else 1
    if action == "probe":
        ok, lines = refuse_while_running()
        if not ok:
            _print(lines)
            return 1
        ok, lines = probe_runtime(getattr(args, "port", None),
                                  baud=getattr(args, "baud", None) or 250000,
                                  timeout=getattr(args, "timeout", None)
                                  or PROBE_TIMEOUT)
        _print(lines)
        return 0 if ok else 1
    if action == "build":
        ok, lines = build_plan(getattr(args, "defconfig", None),
                               chip=getattr(args, "chip", "rp2040"),
                               jobs=getattr(args, "jobs", None) or DEFAULT_JOBS,
                               overrides=_overrides(args))
        _print(lines)
        return 0 if ok else 1
    if action not in ("uf2", "dfu", "serial"):
        _print("Unknown flash action: {!r} (detect, probe, uf2, dfu, serial, "
               "doctor, status, build)".format(action))
        return 2
    ok, spec = artifact_arg(args)
    if not ok:
        _print(spec)
        return 1
    ok, path_or_reason = check_bin_path(spec)
    if not ok:
        _print([path_or_reason])
        return 1
    bin_path = path_or_reason
    if action == "uf2":
        ok, lines = guarded(lambda: flash_uf2(
            bin_path, volume=getattr(args, "volume", None),
            timeout=getattr(args, "timeout", None) or VOLUME_TIMEOUT,
            dry_run=bool(getattr(args, "dry_run", False))))
    elif action == "dfu":
        ok, lines = guarded(lambda: flash_dfu(
            bin_path, chip=getattr(args, "chip", None),
            vidpid=getattr(args, "vidpid", None),
            alt=getattr(args, "alt", None),
            address=getattr(args, "address", None),
            leave=getattr(args, "leave", None),
            dry_run=bool(getattr(args, "dry_run", False))))
    elif action == "serial":
        port = getattr(args, "port", None)
        if not port:
            _print(["--port is required: the STM32 serial bootloader needs to "
                    "know which port it lives on (`klipperformac flash detect` "
                    "lists them)."])
            return 1
        ok, lines = guarded(lambda: flash_serial(
            bin_path, port, chip=getattr(args, "chip", "stm32f407"),
            tool=getattr(args, "tool", "auto"),
            dry_run=bool(getattr(args, "dry_run", False))))
    else:  # uf2/dfu/serial were the only options the check above allowed
        _print("Unknown flash action: {!r}".format(action))
        return 2
    _print(lines)
    return 0 if ok else 1


def add_parser(sub):
    """The `flash` subcommand. Kept whole in here so cli.py's main() only gains
    one call and one handler entry."""
    p = sub.add_parser(
        "flash", help="write firmware to a board: UF2 (BOOTSEL volume), DFU "
                      "(dfu-util) or STM32 serial (boot0)")
    fa = p.add_subparsers(dest="action")
    fa.required = True

    d = fa.add_parser("detect", help="what is attached and in which state "
                                     "(read-only, nothing is opened)")
    d.add_argument("--json", action="store_true",
                   help="one machine-readable object (the GUI reads this)")

    pr = fa.add_parser("probe", help="read-only direct handshake with a board "
                                      "while the stack is down")
    pr.add_argument("--port", metavar="PATH", required=True,
                    help="/dev/cu.* port, or an identity key from "
                         "`flash detect --json`")
    pr.add_argument("--baud", type=int, default=250000,
                    help="UART baud to try for the handshake (default 250000)")
    pr.add_argument("--timeout", type=int, default=PROBE_TIMEOUT,
                    help="seconds to wait for the probe (default {})"
                         .format(PROBE_TIMEOUT))

    for name in ("doctor", "status"):
        fa.add_parser(name, help="which flashers and toolchains exist "
                                 "(reports only; never installs)")

    b = fa.add_parser("build", help="print the upstream make commands for a "
                                    "defconfig (does NOT compile yet)")
    b.add_argument("--defconfig", metavar="NAME",
                   help="board config in klipper/config/, e.g. "
                        "generic-bigtreetech-skr-pico-v1.0.cfg (.cfg added if "
                        "omitted)")
    b.add_argument("--chip", default="rp2040", choices=BUILD_CHIPS)
    b.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    b.add_argument("--set", action="append", metavar="CONFIG_X=value",
                   help="override a Kconfig symbol (repeatable)")
    b.add_argument("--dry-run", action="store_true",
                   help="accepted for symmetry: build never runs")

    u = fa.add_parser("uf2", help="write a .uf2 to an RP2040/RP2350 BOOTSEL "
                                  "volume (press BOOTSEL, plug in)")
    u.add_argument("path", nargs="?")
    u.add_argument("--bin", metavar="FILE", help="the .uf2 to write")
    u.add_argument("--volume", metavar="NAME",
                   help="BOOTSEL volume name (default: auto-detect, normally "
                        "RPI-RP2)")
    u.add_argument("--timeout", type=int, metavar="SEC", default=VOLUME_TIMEOUT,
                   help="seconds to wait for the volume (default {})"
                        .format(VOLUME_TIMEOUT))
    u.add_argument("--dry-run", action="store_true",
                   help="print what would be written; touch nothing")

    f = fa.add_parser("dfu", help="write a .bin with dfu-util (STM32 in DFU "
                                  "mode)")
    f.add_argument("path", nargs="?")
    f.add_argument("--bin", metavar="FILE")
    f.add_argument("--chip", choices=tuple(sorted(DFU_PRESETS)),
                   help="sets -d/-a/-s the way upstream's flash_usb.py does")
    f.add_argument("--vidpid", metavar="NNNN:MMMM",
                   help="explicit target, e.g. 0483:df11")
    f.add_argument("--alt", metavar="N", help="dfu-util alternate setting")
    f.add_argument("--address", metavar="0xADDR",
                   help="application address for -s")
    f.add_argument("--leave", action="store_true", default=None,
                   help="pass -s <addr>:leave (what upstream does for F4/F0)")
    f.add_argument("--dry-run", action="store_true")

    s = fa.add_parser("serial", help="write a .bin through the STM32 serial "
                                     "(boot0) bootloader")
    s.add_argument("path", nargs="?")
    s.add_argument("--bin", metavar="FILE")
    s.add_argument("--port", metavar="PATH", required=True,
                   help="/dev/cu.* port, or an identity key from "
                        "`flash detect --json`")
    s.add_argument("--chip", default="stm32f407",
                   help="MCU type passed to upstream's -t (default stm32f407)")
    s.add_argument("--tool", choices=("auto", "upstream", "stm32flash"),
                   default="auto",
                   help="auto prefers upstream's own script through the stack "
                        "venv python, then brew stm32flash")
    s.add_argument("--dry-run", action="store_true")
    return p
