"""USB serial device discovery on macOS (stdlib only, no pyserial)."""
import glob
import re

PATTERNS = (
    "/dev/cu.usbserial*",
    "/dev/cu.usbmodem*",
    "/dev/cu.wchusbserial*",
    "/dev/cu.SLAB_USBtoUART*",
    "/dev/cu.usbtrance*",
)

# cu-name conventions on macOS: the driver family is embedded in the path.
_HINTS = (
    ("wchusbserial", "WCH CH340/CH341"),
    ("usbserial", "FTDI (or clone)"),
    ("usbmodem", "USB-CDC (STM32 / SKR / Pi Pico class boards)"),
    ("SLAB", "Silicon Labs CP210x"),
    ("usbtrance", " Teensy HID"),
)


def _describe(path):
    for token, label in _HINTS:
        if token.lower() in path.lower():
            return label
    return ""


def scan():
    """Returns [{'path':..., 'description':...}] candidate printer ports."""
    ports = []
    for pat in PATTERNS:
        ports.extend(glob.glob(pat))
    return [{"path": p, "description": _describe(p)} for p in sorted(set(ports))]


def write_serial(cfg_path, device_path):
    """Set `serial:` inside the [mcu] section only. Adds the line if [mcu]
    exists without it; refuses (returns False) if no [mcu] section exists."""
    if not cfg_path.exists():
        return False
    lines = cfg_path.read_text().splitlines(True)
    in_mcu = False
    has_mcu = False
    replaced = False
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_mcu = stripped == "[mcu]"
            has_mcu = has_mcu or in_mcu
        if in_mcu and re.match(r"serial\s*:", stripped):
            line = "serial: {}\n".format(device_path)
            in_mcu = False
            replaced = True
        out.append(line)
    if not replaced:
        if not has_mcu:
            return False
        out2 = []
        inserted = False
        for line in out:
            out2.append(line)
            if not inserted and line.strip() == "[mcu]":
                out2.append("serial: {}\n".format(device_path))
                inserted = True
        out = out2
    cfg_path.write_text("".join(out))
    return True
