"""USB serial device discovery on macOS (stdlib only, no pyserial)."""
import glob
import plistlib
import re
import subprocess

PATTERNS = (
    "/dev/cu.usbserial*",
    "/dev/cu.usbmodem*",
    "/dev/cu.wchusbserial*",
    "/dev/cu.SLAB_USBtoUART*",
    "/dev/cuuart*",
)


def _ioreg_usb():
    try:
        out = subprocess.run(
            ["ioreg", "-p", "IOUSB", "-a"], capture_output=True, timeout=10
        ).stdout
        return plistlib.loads(out)
    except Exception:
        return None


def _walk(tree, found):
    if isinstance(tree, dict):
        props = tree
        name = props.get("IORegistryEntryName") or ""
        if props.get("kUSBSerialNumberString") or props.get("USB Serial Number"):
            vid = props.get("idVendor")
            pid = props.get("idProduct")
            serial = (
                props.get("kUSBSerialNumberString")
                or props.get("USB Serial Number")
                or ""
            )
            vendor = props.get("kUSBVendorString") or props.get("USB Vendor Name") or ""
            product = props.get("kUSBProductString") or props.get("USB Product Name") or ""
            if vid and pid:
                found.append(
                    {
                        "vid": int(vid),
                        "pid": int(pid),
                        "serial": str(serial),
                        "vendor": str(vendor),
                        "product": str(product),
                    }
                )
        for v in props.values():
            _walk(v, found)
    elif isinstance(tree, list):
        for v in tree:
            _walk(v, found)


def _describe(path, usb_devices):
    base = path.split("/")[-1]
    for d in usb_devices:
        tag = "{:04x}_{:04x}".format(d["vid"], d["pid"])
        if tag in base.lower():
            bits = [d["vendor"], d["product"], d["serial"]]
            return " ".join(b for b in bits if b)
    return ""


def scan():
    """Returns [{'path':..., 'description':...}] candidate printer ports."""
    ports = []
    for pat in PATTERNS:
        ports.extend(glob.glob(pat))
    usb = []
    tree = _ioreg_usb()
    if tree is not None:
        _walk(tree, usb)
    return [
        {"path": p, "description": _describe(p, usb)}
        for p in sorted(set(ports))
    ]


def stable_id(path):
    """A by-id-like stable identifier: cu name + (vid:pid) + usb serial."""
    m = re.search(r"(\d{4}_\d{4})", path)
    return m.group(1) if m else path.split("/")[-1]


def write_serial(cfg_path, device_path):
    """Set [mcu] serial: line, same semantics as instances/detect.py."""
    if not cfg_path.exists():
        return False
    lines = cfg_path.read_text().splitlines(True)
    found_mcu = False
    replaced = False
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[mcu]"):
            found_mcu = True
        elif found_mcu and stripped.startswith("serial:"):
            line = "serial: {}\n".format(device_path)
            found_mcu = False
            replaced = True
        out.append(line)
    if replaced:
        cfg_path.write_text("".join(out))
    return replaced
