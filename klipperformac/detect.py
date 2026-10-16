"""USB serial device discovery on macOS (stdlib only, no pyserial)."""
import glob
import re

from . import paths

PATTERNS = (
    "/dev/cu.usbserial*",
    "/dev/cu.usbmodem*",
    "/dev/cu.wchusbserial*",
    "/dev/cu.SLAB_USBtoUART*",
    "/dev/cu.usbtrance*",
    # WCH CH340/CH341 boards with the vendor kext loaded name their ports
    # /dev/cu.URT<n> (and /dev/cu.USR2* for the UART-II chips). This is the
    # most common 3D-printer USB chip on the market, so leaving it out made a
    # plugged-in printer invisible to `serial`, `--auto` and the GUI picker.
    # Tradeoff, accepted deliberately: a CH340 that is NOT the printer (an
    # Arduino, a serial console on another box) now shows up as a candidate
    # too - which is what the listing is for. auto_serial()/`--auto` only ever
    # write a port when exactly ONE candidate exists, so multi-device users
    # still pick by hand (`serial --set PATH`).
    "/dev/cu.URT*",
    "/dev/cu.USR2*",
    "/dev/cu.WCHTL*",
)

# cu-name conventions on macOS: the driver family is embedded in the path.
_HINTS = (
    ("wchusbserial", "WCH CH340/CH341"),
    ("usbserial", "FTDI (or clone)"),
    ("usbmodem", "USB-CDC (STM32 / SKR / Pi Pico class boards)"),
    ("SLAB", "Silicon Labs CP210x"),
    ("usbtrance", "Teensy HID"),
    # Longest token first below: "urt" is also a substring of nothing else here.
    ("wchtl", "WCH CH340/CH341 (WCH-Link)"),
    ("usr2", "WCH CH340/CH341"),
    ("urt", "WCH CH340/CH341"),
)


def _describe(path):
    for token, label in _HINTS:
        if token.lower() in path.lower():
            return label
    return ""


def lan_ip():
    """This Mac's primary LAN IPv4 (UDP connect picks the routed interface;
    no packets are actually sent). None when offline."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def scan():
    """Returns [{'path':..., 'description':...}] candidate printer ports."""
    ports = []
    for pat in PATTERNS:
        ports.extend(glob.glob(pat))
    return [{"path": p, "description": _describe(p)} for p in sorted(set(ports))]


def write_serial(cfg_path, device_path):
    """Set `serial:` inside the [mcu] section only. Adds the line if [mcu]
    exists without it; refuses (returns False) if no [mcu] section exists or
    the device path could inject config lines.

    Reads/writes byte-safe: a vendor printer.cfg saved in cp1252 is rewritten
    with its own encoding (see paths.read_config), and through a temp file, so
    a crash cannot leave a half printer.cfg where Klipper expects a whole one."""
    if not cfg_path.exists():
        return False
    if re.search(r"[\r\n\0]", device_path):
        return False
    text, encoding = paths.read_config(cfg_path)
    lines = text.splitlines(True)
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
            eol = "\r\n" if line.endswith("\r\n") else "\n"
            line = "serial: {}{}".format(device_path, eol)
            in_mcu = False
            replaced = True
        out.append(line)
    if not replaced:
        if not has_mcu:
            return False
        out2 = []
        inserted = False
        for line in out:
            if not inserted and line.strip() == "[mcu]" and \
                    not line.endswith(("\n", "\r")):
                line = line + "\n"
            out2.append(line)
            if not inserted and line.strip() == "[mcu]":
                out2.append("serial: {}\n".format(device_path))
                inserted = True
        out = out2
    paths.write_config(cfg_path, "".join(out), encoding)
    return True
