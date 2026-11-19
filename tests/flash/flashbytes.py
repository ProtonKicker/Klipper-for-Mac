"""Byte-level factories for `klipperformac flash` tests.

Standalone module (not conftest) so it can never collide with another
conftest.py in the suite, and loaded by path from tests/flash/conftest.py.

The constants are re-declared here instead of imported from flash.py on purpose:
a test that reads the magic numbers out of the module under test cannot catch a
typo in them. Values are copied from the pristine upstream checkout -
klipper `lib/pico-sdk/boot/uf2.h:19-33` - which is exactly what upstream's own
converter writes (`lib/elf2uf2/main.cpp:295-301`) and what the boot ROM
compares against (`lib/rp2040_flash/main.c:52-59`).
"""
import os
import plistlib
import struct

UF2_MAGIC_START0 = 0x0A324655
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
UF2_FLAG_FILE_CONTAINER = 0x00001000
UF2_FLAG_FAMILY_ID_PRESENT = 0x00002000
RP2040_FAMILY_ID = 0xE48BFF56
RP2350_FAMILY_ID = 0xE48BFF59
UF2_BLOCK_SIZE = 512
UF2_PAYLOAD_AREA = 476

# This Mac's bench board, captured read-only with `ioreg -a -x -l -p IOUSB` on
# 2026-10-16 (SKR Pico running Klipper's rp2040 CDC firmware): idVendor 7504
# (0x1d50), idProduct 24910 (0x614e), "USB Serial Number" 5044506110A45B1C,
# locationID 336592896 (0x14100000).
SKR_PICO_NODE = {
    "IORegistryEntryName": "rp2040",
    "IOObjectClass": "IOUSBHostDevice",
    "idVendor": 7504,
    "idProduct": 24910,
    "USB Product Name": "rp2040",
    "kUSBProductString": "rp2040",
    "USB Vendor Name": "Klipper",
    "USB Serial Number": "5044506110A45B1C",
    "locationID": 336592896,
    "sessionID": 192920582145205,
}
# The same RP2040 in BOOTSEL: the ROM enumerates 2e8a:0003 (upstream's own
# `rawdev` in scripts/flash_usb.py flash_rp2040) and macOS mounts RPI-RP2.
RP2040_BOOTSEL_NODE = {
    "IORegistryEntryName": "UFS2",
    "IOObjectClass": "IOUSBHostDevice",
    "idVendor": 0x2E8A,
    "idProduct": 0x0003,
    "USB Product Name": "UFS2",
    "kUSBProductString": "UFS2",
    "USB Vendor Name": "Microchip Technology",
    "locationID": 336592896,
}
# An STM32F407 in DFU mode: 0483:df11 (upstream STM32F4_HELP). A Blue Pill with
# the stm32duino bootloader: 1eaf:0003 (STM32F1_HELP).
STM32_DFU_NODE = {
    "IORegistryEntryName": "STM32  Bootstrap",
    "IOObjectClass": "IOUSBHostDevice",
    "idVendor": 0x0483,
    "idProduct": 0xDF11,
    "USB Product Name": "STM32  Bootstrap",
    "kUSBProductString": "STM32  Bootstrap",
    "locationID": 335643648,
}
STM32_DUINO_NODE = {
    "IORegistryEntryName": "STM32 Maple",
    "IOObjectClass": "IOUSBHostDevice",
    "idVendor": 0x1EAF,
    "idProduct": 0x0003,
    "USB Product Name": "STM32 Maple",
    "kUSBProductString": "STM32 Maple",
    "locationID": 335643648,
}
# A Bluetooth port: no USB node behind it, must never be mistaken for a board.
BLUETOOTH_NODE = {
    "IORegistryEntryName": "Bluetooth Control",
    "IOObjectClass": "IOUserSerial",
    "IOCalloutDevice": "/dev/cu.Bluetooth-Incoming-Port",
}


def ioreg_plist(nodes):
    """Bytes shaped like `ioreg -a -x -l -p IOUSB`: a root dict whose
    IORegistryEntryChildren is the device list, each node carrying its
    properties inline (verified against this Mac's real output)."""
    return plistlib.dumps({"IOObjectClass": "IORegistryEntry",
                           "IORegistryEntryName": "Root",
                           "IORegistryEntryChildren": list(nodes)})


def uf2_sector(payload=b"", block_no=0, num_blocks=1, target=0x10000000,
               flags=UF2_FLAG_FAMILY_ID_PRESENT, family=RP2040_FAMILY_ID,
               magic0=UF2_MAGIC_START0, magic1=UF2_MAGIC_START1,
               magic_end=UF2_MAGIC_END, payload_size=None):
    """One 512-byte UF2 block: 32-byte header, data[476], trailing magic.

    Every field is a keyword so a test can break exactly one of them.
    `payload_size` may be set past the 476-byte area on purpose - that is a
    corrupt header, not a bug in this builder.
    """
    if len(payload) > UF2_PAYLOAD_AREA:
        raise ValueError("a UF2 payload cannot exceed %d bytes"
                         % UF2_PAYLOAD_AREA)
    size = len(payload) if payload_size is None else payload_size
    header = struct.pack("<8I", magic0, magic1, flags, target, size, block_no,
                         num_blocks, family)
    data = payload + b"\x00" * (UF2_PAYLOAD_AREA - len(payload))
    return header + data + struct.pack("<I", magic_end)


def uf2_image(blocks=8, payload=b"K" * 16, **kw):
    """`blocks` coherent sectors (block_no counts up, num_blocks is the total).

    The smallest valid image is 8 sectors = 4096 bytes, which is exactly the
    size floor `flash` enforces, so every "structurally fine" fixture uses it.
    """
    return b"".join(uf2_sector(payload=payload, block_no=i, num_blocks=blocks,
                               **kw) for i in range(blocks))


def replace_sector(blob, index, **kw):
    """`blob` with sector `index` rebuilt from `kw` (the block_no/num_blocks
    defaults stay sane unless overridden)."""
    sectors = [blob[i * UF2_BLOCK_SIZE:(i + 1) * UF2_BLOCK_SIZE]
               for i in range(len(blob) // UF2_BLOCK_SIZE)]
    total = len(sectors)
    fields = {"payload": b"X" * 16, "block_no": index, "num_blocks": total}
    fields.update(kw)
    sectors[index] = uf2_sector(**fields)
    return b"".join(sectors)


def bin_image(msp=0x20010000, reset=0x08008101, size=8192):
    """A raw .bin whose first two words are a Cortex-M vector table (default:
    STM32F407 SRAM + flash), padded past the size floor."""
    body = struct.pack("<2I", msp, reset)
    return body + b"\x00" * max(0, size - len(body))


class Clock:
    """A deterministic clock for the bounded waits.

    flash_uf2's polls use time.monotonic() plus a `sleep` callable; handing it
    both from here makes "timed out after 5 s" happen in zero wall-clock
    seconds, and lets a test decide on which poll the volume shows up."""

    def __init__(self, step=0.5):
        self.now = 0.0
        self.step = step
        self.slept = 0
        self.on_sleep = None

    def monotonic(self):
        return self.now

    def sleep(self, _seconds=None):
        self.slept += 1
        self.now += self.step
        if self.on_sleep is not None:
            self.on_sleep(self.slept)


class Recorder:
    """Stands in for flash._run / flash._run_bytes: records argv, replies.

    `replies` maps a command basename to (code, out, err); a callable gets the
    argv and returns that tuple - and may mutate the fake /Volumes, which is how
    a test simulates the RP2040 ROM resetting the board and unmounting itself."""

    def __init__(self, replies=None, as_bytes=False):
        self.calls = []
        self.replies = dict(replies or {})
        self.as_bytes = as_bytes

    def __call__(self, argv, timeout, **kw):
        argv = [str(a) for a in argv]
        self.calls.append((argv, timeout))
        reply = self.replies.get(os.path.basename(argv[0]), (0, "", ""))
        if callable(reply):
            reply = reply(argv)
        if len(reply) == 2:  # (code, out) is the shape a test writes when it
            reply = (reply[0], reply[1], "")  # only cares about stdout
        code, out, err = reply
        if self.as_bytes:
            return code, _as_bytes(out), _as_bytes(err)
        return code, _as_text(out), _as_text(err)

    def commands(self):
        return [c[0] for c in self.calls]

    def ran(self, name):
        """True when a command whose argv[0] basename is `name` was run."""
        return any(os.path.basename(c[0][0]) == name for c in self.calls)

    def argv_for(self, name):
        return [c[0] for c in self.calls if os.path.basename(c[0][0]) == name]


def _as_bytes(value):
    return value if isinstance(value, bytes) else str(value).encode()


def _as_text(value):
    return value if isinstance(value, str) else value.decode(errors="replace")
