"""USB serial device discovery on macOS (stdlib only, no pyserial)."""
import glob
import re
import subprocess

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


# An MCU section name this tool is willing to write: one word, printable ASCII,
# no whitespace/brackets/colon/semicolon/hash (all of which Klipper would read
# as something other than what the user typed). See mcu_header().
_MCU_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,31}\Z")


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


def mcu_header(name="mcu"):
    """The printer.cfg section header for an MCU name: '[mcu]' / '[mcu tool]'.

    None when `name` cannot safely become one. Klipper reads a header as
    `pruned_line[1:-1].strip()` after stripping `#`/`;` comments
    (klippy/configfile.py), so a secondary MCU's object name is the section
    text - '[mcu tool]' is the object 'mcu tool' (klippy/mcu.py:1246-1253).
    Anything this tool cannot write back verbatim (whitespace, brackets,
    a colon, a control character) is refused here rather than half-written,
    because a section klippy reads as a DIFFERENT object than the name the
    user typed is a silently broken printer.
    """
    if name is None:
        name = "mcu"
    if not isinstance(name, str):
        return None
    name = name.strip()
    if name == "mcu":
        return "[mcu]"
    if not name or not _MCU_NAME_RE.match(name):
        return None
    return "[mcu {}]".format(name)


def set_serial_text(text, device_path, header="[mcu]"):
    """printer.cfg text -> the same text with ONE section's serial: set.

    None when the section is not there or the path could inject config lines,
    so a caller can tell "nothing changed" from "changed to nothing". Every
    byte outside that one line is kept exactly as it was: other sections,
    comments, blank lines, CRLF endings, indentation. This is the kernel
    `write_serial` has always used, split out because mcus.set_serial() is a
    pure text-in/text-out writer for EVERY [mcu*] section and must not
    re-implement the line rules (one implementation, one behavior).
    """
    if not isinstance(text, str) or not isinstance(header, str):
        return None
    if not isinstance(device_path, str):
        return None
    if re.search(r"[\r\n\0]", device_path):
        return None
    lines = text.splitlines(True)
    in_mcu = False
    has_mcu = False
    replaced = False
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_mcu = stripped == header
            has_mcu = has_mcu or in_mcu
        if in_mcu and re.match(r"serial\s*:", stripped):
            eol = "\r\n" if line.endswith("\r\n") else "\n"
            line = "serial: {}{}".format(device_path, eol)
            in_mcu = False
            replaced = True
        out.append(line)
    if not replaced:
        if not has_mcu:
            return None
        out2 = []
        inserted = False
        for line in out:
            if not inserted and line.strip() == header and \
                    not line.endswith(("\n", "\r")):
                line = line + "\n"
            out2.append(line)
            if not inserted and line.strip() == header:
                out2.append("serial: {}\n".format(device_path))
                inserted = True
        out = out2
    return "".join(out)


def write_serial(cfg_path, device_path, mcu_name="mcu"):
    """Set `serial:` inside ONE [mcu...] section and nowhere else.

    `mcu_name` defaults to "mcu" (the timing master), which is exactly the old
    signature and the old behavior: only `[mcu]` changes, `[mcu tool]` is left
    untouched. Name a section to point that one at a port ('tool' -> '[mcu
    tool]'). Adds the line when the section exists without it; refuses
    (returns False) when the section does not exist, when the name cannot be
    a section header (see mcu_header), or when the device path could inject
    config lines.

    NEVER creates a section: adding `[mcu <name>]` to a config is
    mcus.add_mcu_section()'s job (and a user decision), not a side effect of
    setting a path.

    Reads/writes byte-safe: a vendor printer.cfg saved in cp1252 is rewritten
    with its own encoding (see paths.read_config), and through a temp file, so
    a crash cannot leave a half printer.cfg where Klipper expects a whole one."""
    header = mcu_header(mcu_name)
    if header is None or not cfg_path.exists():
        return False
    try:
        text, encoding = paths.read_config(cfg_path)
    except OSError:
        return False
    updated = set_serial_text(text, device_path, header=header)
    if updated is None:
        return False
    paths.write_config(cfg_path, updated, encoding)
    return True


# ===========================================================================
# USB board identity -- the macOS answer to Linux' /dev/serial/by-id.
#
# WHY THIS EXISTS
# macOS renumbers /dev/cu.usbmodemNNNNN on every replug (and often across
# sleep/wake), so a `serial:` written once is wrong the second time the printer
# is plugged in. On Linux the stable name is the /dev/serial/by-id symlink; on
# macOS the stable facts (idVendor / idProduct / "USB Serial Number" /
# locationID) live only in the IOKit registry, which `ioreg` prints.
#
# NEVER A PORT OPEN
# Everything here is registry enumeration through `ioreg`. Nothing opens a
# /dev/cu.* node: klippy holds its port with flock (klippy/serialposix.py), so
# a second opener corrupts a running printer. That is what makes this safe to
# call from the GUI serial picker, from `doctor`, and from the flash wizard
# while a print is live.
#
# WHO CALLS IT
#   * Multi-MCU bindings (wave 3): persist "usb:1d50:614e:<SER>" in
#     settings.json, then re-resolve it to whatever /dev/cu.* exists today.
#   * `klipperformac flash detect` (S2): same idea for a board being flashed.
#     CAVEAT for that caller: a board sitting in UF2 bootloader mode is a mass
#     storage volume with NO serial node, so it never appears here - pair this
#     API with `diskutil list`. In runtime mode the RP2040 shows 1D50:614E, in
#     bootloader mode 2E8A:0003.
#
# PUBLIC API (four functions, dead simple, and not one of them ever raises):
#   identity(dev)                         -> dict | None
#   all_identities()                      -> [dict, ...]
#   find_by_identity(vid_pid, serial)     -> "/dev/cu.X" | None
#   find_all_by_identity(vid_pid, serial) -> ["/dev/cu.X", ...]
#
# `scan()` above is untouched on purpose: the Swift GUI decodes its exact
# [{path, description}] shape. If scan() is ever extended, ADD keys only.
# ===========================================================================

# `ioreg -p IOService -l -w 0`: the whole service plane with properties, as a
# text tree, no line wrapping. Measured on the bench Mac (macOS 13/x86_64):
# 3.4 MB, ~0.45 s wall clock, ~0.05 s to parse. The IOUSB plane is NOT enough:
# it has the device but none of its IOSerialBSDClient children, i.e. no
# /dev/cu.* mapping, which is the whole point.
_IOREG_ARGV = ("ioreg", "-p", "IOService", "-l", "-w", "0")

# Hard ceiling for the subprocess call. Callers may ask for LESS, never more:
# this runs on the GUI's serial picker, and a wedged ioreg must not wedge the
# app. Total failure (no ioreg, timeout, unparsable, garbage) -> no identity.
IOREG_TIMEOUT_CAP = 3.0

# `  | +-o <name>  <class <Class>, id 0x..., registered, ...>`. Verified on this
# Mac: ioreg introduces EVERY child with `+-o ` (no `` `-o `` last-child glyph
# here), other builds do use it, and a few write `+--o ` -- hence the character
# class plus the optional extra dash.
_IOREG_ENTRY_RE = re.compile(
    r"[+`]--?o (?P<name>.*?)\s+<(?:class|protocol|service)\s+"
    r"(?P<cls>[A-Za-z0-9_]+),")

# `    | |   |   "key" = value` -- the tree prefix is spaces plus bars, then a
# quoted key. Every key we care about is a single-line scalar.
_IOREG_PROP_RE = re.compile(
    r'^\s*(?:\|\s*)*"(?P<key>(?:[^"\\]|\\.)*)"\s*=\s*(?P<val>.*?)\s*$')

_IOREG_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}

# One USB vendor/product id as everybody writes it: 1-4 hex digits.
_HEX_ID_RE = re.compile(r"^[0-9a-f]{1,4}$")


def _ioreg_value(text):
    r"""`"abc"` -> abc with ioreg's backslash escapes resolved; else stripped.

    Non-string values (`24910`, `Yes`, `<001122>`, `{"a"="b"}`) come back as
    text and are handled by _ioreg_int() / ignored downstream.
    """
    text = text.strip()
    if not (len(text) >= 2 and text[0] == '"' and text[-1] == '"'):
        return text
    text = text[1:-1]
    if "\\" not in text:
        return text
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append(_IOREG_ESCAPES.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _ioreg_int(text):
    """7504 -> 7504, "0x1d50" -> 7504, '"7504"' -> 7504, garbage -> None.

    This macOS prints idVendor/idProduct as DECIMAL (verified: 7504 / 24910 for
    1d50:614e). Other builds/flags print hex, and a quoted form is possible, so
    accept all three instead of guessing.
    """
    text = _ioreg_value(text).strip()
    if not text:
        return None
    try:
        return int(text, 0) if text.lower().startswith("0x") else int(text, 10)
    except (TypeError, ValueError):
        return None


def _fmt_vid_pid(vid, pid):
    """(7504, 24910) -> '1D50:614E' (4 hex digits, uppercase, colon)."""
    if vid is None or pid is None:
        return None
    return "%04X:%04X" % (vid & 0xFFFF, pid & 0xFFFF)


def _parse_vid_pid(vid_pid):
    """'1d50:614e' / '1D50:614E' / '0x1d50:0x614e' -> (7504, 24910) | None.

    ALWAYS HEX here, never decimal: that is the lsusb / system_profiler /
    `identity()` convention, and it is what callers round-trip back into this
    function. ioreg's own property output is decimal, which _ioreg_int() deals
    with on the way in -- do not confuse the two. '7504:24910' therefore means
    0x7504:0x24910, not the ioreg decimal of the bench board.
    """
    if not isinstance(vid_pid, str):
        return None
    parts = [p for p in vid_pid.strip().replace(" ", ":").split(":") if p]
    if len(parts) != 2:
        return None
    vid = _parse_hex_id(parts[0])
    pid = _parse_hex_id(parts[1])
    if vid is None or pid is None:
        return None
    return (vid, pid)


def _parse_hex_id(text):
    """'1D50' / '0x1d50' -> 7504; anything else -> None."""
    text = text.strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not text or len(text) > 4 or _HEX_ID_RE.match(text) is None:
        return None
    return int(text, 16)


def _norm_serial(serial):
    """Case/whitespace-insensitive compare key for a USB serial string."""
    if serial is None:
        return ""
    return " ".join(str(serial).split()).casefold()


def _identity_key(vid_pid, serial, location):
    """Stable key: by-id when the board exposes a serial, else by physical path.

    Klipper's CDC firmware always sets a serial (the RP2040's unique device id,
    verified on the bench: 5044506110A45B1C), so real printer boards get `usb:`
    and survive any port rename or cable swap. Cheap CH340/FTDI bridges expose
    none and fall back to `loc:<locationID>`, which is only stable while the
    cable stays in the same physical port - exactly the by-id vs by-path split
    Klipper's own docs make on Linux.
    """
    if not vid_pid:
        return None
    if serial:
        return "usb:{}:{}".format(vid_pid.lower(), " ".join(str(serial).split()))
    if location:
        return "loc:{}".format(location)
    return "vidpid:{}".format(vid_pid.lower())


def _parse_ioreg(text):
    """ioreg text tree -> list of node dicts {name, cls, props, parent, path}.

    `parent` is the index of the enclosing node (or -1) and `path` the registry
    path built from node names, e.g.
    "Root/MacBookPro15,4/.../rp2040@14100000/IOUSBHostInterface@1/"
    "AppleUSBACMData/IOSerialBSDClient".
    Indentation depth is the column of the `+-o` marker, which is how ioreg
    encodes nesting in this format (the marker is always the first one on the
    line, so a product name containing the text cannot fool it). Never raises:
    lines that do not parse are ignored, so a truncated or garbage dump simply
    yields fewer nodes.

    KNOWN LIMITATION, accepted deliberately: a property whose VALUE spans
    several lines (`<....>` data blobs, a string containing a raw newline) has
    its continuation lines skipped, so such a property is read as truncated. It
    does not affect this module -- idVendor, idProduct, "USB Serial Number",
    "USB Product Name", locationID and IOCalloutDevice are all single-line
    scalars here and on any macOS that ships ioreg. `-w 0` is what keeps the
    tree itself from wrapping.
    """
    nodes = []
    stack = []  # [(depth, index)]
    for raw in text.splitlines():
        entry = _IOREG_ENTRY_RE.search(raw)
        if entry:
            depth = raw.find("+-o ")
            if depth < 0:
                depth = raw.find("`-o ")
            if depth < 0:
                depth = 0
            while stack and stack[-1][0] >= depth:
                stack.pop()
            parent = stack[-1][1] if stack else -1
            name = entry.group("name").strip()
            path = name if parent < 0 else nodes[parent]["path"] + "/" + name
            stack.append((depth, len(nodes)))
            nodes.append({"name": name, "cls": entry.group("cls"),
                          "props": {}, "parent": parent, "path": path})
            continue
        prop = _IOREG_PROP_RE.match(raw)
        if prop and nodes:
            # Property blocks always follow their own entry line, so they belong
            # to the most recently seen node.
            nodes[-1]["props"][prop.group("key")] = _ioreg_value(prop.group("val"))
    return nodes


def _chain(nodes, index):
    """[index, parent, ... root] as node dicts, bounded so a cyclic parse can
    never spin forever."""
    out = []
    guard = 0
    while index is not None and index >= 0 and guard < 256:
        out.append(nodes[index])
        index = nodes[index]["parent"]
        guard += 1
    return out


def _usb_props(chain):
    """The USB *device* node above a port (inclusive) -> props dict | None.

    IOKit copies idVendor/idProduct onto interface and driver children, so the
    nearest node with those fields is often still not the board. The other trap
    is a USB hub: walking outward until "the first node with ids" returns the
    hub, not the RP2040 under it. The rule here is therefore:

      1. prefer the NEAREST USB *device* class carrying ids;
      2. if no such class is visible, fall back to the nearest node with ids.

    That keeps the board's own vendor/product/serial/location while still
    surviving odd driver stacks. A chain with no idVendor anywhere is not a USB
    board -> None.
    """
    for node in chain:
        props = node["props"]
        if ("idVendor" in props and "idProduct" in props
                and node["cls"] in ("IOUSBHostDevice", "IOUSBDevice")):
            return props
    for node in chain:
        props = node["props"]
        if "idVendor" in props and "idProduct" in props:
            return props
    return None


def _is_bluetooth(chain):
    """True when any node above the port belongs to the Bluetooth stack.

    /dev/cu.Air4SE and friends are RFCOMM tunnels to headphones and phones, not
    printers: they must never be offered as a `serial:` candidate even on a
    machine whose BT controller happens to be USB-attached (it is, here).
    """
    for node in chain:
        blob = (node["cls"] + "/" + node["name"]).lower()
        if "bluetooth" in blob:
            return True
    return False


def _identity_fields(nodes, index):
    """Build the identity dict for node `index` (a serial-port node). None if
    the node is not a /dev/cu.* USB serial port."""
    props = nodes[index]["props"]
    callout = props.get("IOCalloutDevice") or ""
    if not callout.startswith("/dev/cu."):
        return None
    chain = _chain(nodes, index)
    if _is_bluetooth(chain):
        return None
    usb = _usb_props(chain)
    if not usb:
        # Not USB-backed: the IR stack, a virtual/loopback node. No stable board
        # identity to offer, so it is not our printer.
        return None
    vid = _ioreg_int(usb.get("idVendor", ""))
    pid = _ioreg_int(usb.get("idProduct", ""))
    vid_pid = _fmt_vid_pid(vid, pid)
    if not vid_pid:
        return None
    serial = (usb.get("USB Serial Number")
              or usb.get("kUSBSerialNumberString") or "").strip()
    name = (usb.get("USB Product Name") or usb.get("kUSBProductString")
            or nodes[index]["name"]).strip()
    location_id = _ioreg_int(usb.get("locationID", ""))
    location = "%08x" % location_id if location_id is not None else None
    return {
        "vid_pid": vid_pid,
        "serial": serial,
        "name": name,
        "ioservice_path": nodes[index]["path"],
        "path": callout,
        "vendor": (usb.get("USB Vendor Name")
                   or usb.get("kUSBVendorString") or "").strip(),
        "location": location,
        "key": _identity_key(vid_pid, serial, location),
    }


def _usb_ports(timeout=None):
    """Every USB serial node IOKit knows about, as identity dicts. [] on failure.

    Single source of truth for identity()/all_identities()/find_by_identity().
    The two `except Exception` blocks below are not laziness: they are the
    "never raises" half of the contract, kept separate from the *expected*
    subprocess failures so a timeout reads as a timeout in the code and in a
    traceback. A parser bug, a str instead of bytes from a test double, a
    MemoryError on a 300-device machine: all of them degrade to "no idea".
    """
    try:
        limit = IOREG_TIMEOUT_CAP if timeout is None else min(
            float(timeout), IOREG_TIMEOUT_CAP)
    except (TypeError, ValueError):
        return []
    if limit <= 0:
        return []
    try:
        done = subprocess.run(list(_IOREG_ARGV), capture_output=True,
                              timeout=limit, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        # No ioreg on PATH, timeout (SubprocessError family), bad limit.
        return []
    except Exception:  # pragma: no cover - never raise out of this module
        # Anything else a subprocess call can be talked into by a wrapper, a
        # sandbox or a test double. "I don't know" is still the right answer.
        return []
    try:
        raw = done.stdout or b""
        text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        if not text:
            return []
        nodes = _parse_ioreg(text)
        out = []
        for i in range(len(nodes)):
            ident = _identity_fields(nodes, i)
            if ident is not None:
                out.append(ident)
    except Exception:  # pragma: no cover - unreachable unless the parser is wrong
        return []
    return out


def _dev_aliases(dev):
    """Comparable names for a device path: '/dev/cu.usbmodem14101',
    'usbmodem14101', 'cu.usbmodem14101' and the tty twin all match."""
    if not isinstance(dev, str):
        return frozenset()
    name = dev.strip().rstrip("/")
    if not name:
        return frozenset()
    base = name.rsplit("/", 1)[-1]
    out = {base}
    for prefix in ("cu.", "tty."):
        if base.startswith(prefix):
            out.add(base[len(prefix):])
    return {a.casefold() for a in out if a}


def identity(dev, timeout=None):
    """USB board identity behind one /dev/cu.* node. dict | None. Never raises.

    Returns (bench SKR Pico example, values verified with ioreg on this Mac):

        {'vid_pid': '1D50:614E',           # uppercase 4-hex idVendor:idProduct
         'serial': '5044506110A45B1C',     # "USB Serial Number", '' if absent
         'name': 'rp2040',                 # "USB Product Name"
         'ioservice_path': 'Root/.../IOSerialBSDClient',
         'path': '/dev/cu.usbmodem14101',  # the node you asked for (canonical)
         'vendor': 'Klipper',              # "USB Vendor Name"
         'location': '14100000',           # locationID, 8 hex digits
         'key': 'usb:1d50:614e:5044506110A45B1C'}  # stable binding key

    The first four keys are the contract; the rest are additive (never remove
    one, and extend by adding keys only).

    `dev` may be '/dev/cu.usbmodem14101', 'cu.usbmodem14101' or 'usbmodem14101'
    (the /dev/tty twin resolves to the same board). None means "no idea": the
    node is gone, it is not USB (Bluetooth ports), ioreg is missing/unreadable,
    or the dump timed out -- `timeout` is clamped to IOREG_TIMEOUT_CAP (3 s).
    """
    wanted = _dev_aliases(dev)
    if not wanted:
        return None
    for port in _usb_ports(timeout):
        if wanted & _dev_aliases(port["path"]):
            return port
    return None


def all_identities(timeout=None):
    """Every USB serial node present right now, as identity() dicts, sorted by
    path. [] when ioreg cannot be read. Never raises.

    This is the list form used by `serial --list-json` / the GUI picker: one
    call, then answer questions from memory instead of running ioreg again.
    """
    return sorted(_usb_ports(timeout), key=lambda p: p.get("path", ""))


def _matching_ports(vid_pid, serial, timeout):
    """(paths, identities) whose USB ids match, sorted by path.

    Malformed vid_pid -> ([], []).
    """
    ids = _parse_vid_pid(vid_pid)
    if ids is None:
        return ([], [])
    want_vid, want_pid = ids
    want_serial = _norm_serial(serial) if serial is not None else None
    hits = []
    for port in _usb_ports(timeout):
        if _parse_vid_pid(port.get("vid_pid")) != (want_vid, want_pid):
            continue
        if want_serial is not None and want_serial != _norm_serial(
                port.get("serial")):
            continue
        hits.append(port)
    hits.sort(key=lambda p: p.get("path", ""))
    return ([p["path"] for p in hits], hits)


def find_by_identity(vid_pid, serial=None, timeout=None):
    """The /dev/cu.* node a board is attached to RIGHT NOW. str | None.

    Give it what `identity()` returned -- '1D50:614E' + the serial -- and it
    survives a cable rename: the board that was /dev/cu.usbmodem14101 comes
    back as /dev/cu.usbmodem14102 (or under another driver's name entirely)
    and still resolves. This is the primitive multi-MCU bindings (wave 3) and
    `flash detect` (S2) call; there is no /dev/serial/by-id on macOS, so this
    IS the by-id layer.

    Rules (all deliberate, all silent -- it never raises):
      * `vid_pid` accepts '1d50:614e', '1D50:614E', '0x1d50:0x614e'. Anything
        unparseable matches nothing -> None.
      * `serial` compares case/whitespace-insensitively, so '5044506110a45b1c'
        matches the board's '5044506110A45B1C'. Omit it to match on vid:pid
        alone.
      * Several nodes for ONE board (a CDC device enumerating a control plus a
        data interface, or the cu/tty pair) share serial + locationID, so they
        are one group and the first path in sorted order is returned,
        deterministically.
      * More than one group with no serial to pin them down -- two identical
        SKRs, a fleet of serial-less CH340 dongles -- is NOT guessed: writing
        firmware or a `serial:` line to the wrong board is exactly the damage
        this layer exists to prevent, so this returns None. Use
        find_all_by_identity() to get the pick-list and ask the user.
    """
    paths, hits = _matching_ports(vid_pid, serial, timeout)
    if not paths:
        return None
    if len(paths) == 1:
        return paths[0]
    groups = {(p.get("serial") or "", p.get("location") or "") for p in hits}
    if len(groups) > 1 and serial is None:
        return None
    return paths[0]


def find_all_by_identity(vid_pid, serial=None, timeout=None):
    """All currently present /dev/cu.* nodes matching, sorted. [] when none.

    The listing `flash detect` shows and the disambiguation `serial --auto`
    needs when find_by_identity() refuses to pick. Same matching rules.
    """
    paths, _hits = _matching_ports(vid_pid, serial, timeout)
    return paths
