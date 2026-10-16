"""Fake `ioreg` trees for the detect.identity() tier.

detect.identity()/find_by_identity() get their facts from one subprocess call:
`ioreg -p IOService -l -w 0`. Every test here feeds that call a STRING -- the
shape below is what the bench Mac (macOS 13.6, x86_64) really prints, verified
with read-only `ioreg` runs and reproduced line for line (indentation, the
`| ` tree bars, DECIMAL idVendor/idProduct, quoted strings, `Yes`/`No`
booleans). No test shells out, opens a port, touches the board, or writes a
file: they monkeypatch subprocess.run inside the module under test.

The fixtures cover the shapes that actually bite:
  * a board with no serial string (CH340/FTDI)
  * a serial string with quotes/backslashes/spaces in it
  * a port that vanished (unplug between calls)
  * two boards sharing one VID:PID, different serials
  * garbage bytes on stdout, a non-zero exit, a timeout, no ioreg at all
"""
import subprocess
import types

import pytest

from klipperformac import detect


def quoted(text):
    r"""Render a Python string the way ioreg renders a C string property.

    ioreg escapes `"` and `\` and writes control characters as \n/\r/\t;
    everything else (including UTF-8) goes out as raw bytes.
    """
    out = ['"']
    for ch in text:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def node(name, cls, props=(), kids=(), entry_id="0x1000000ff"):
    """One IOKit registry node: name, class, [(key, rendered value)], children.

    `props` values are already rendered (a number is `7504`, a bool `Yes`, a
    string `quoted("x")`) because that is exactly the ambiguity the parser has
    to survive.
    """
    return {"name": name, "cls": cls, "props": list(props), "kids": list(kids),
            "id": entry_id}


def render(nodes):
    """Nodes -> the text `ioreg -p IOService -l -w 0` prints.

    Faithful to the bench Mac's real output: EVERY child is introduced by
    `+-o ` (this ioreg never uses the `` `-o `` last-child glyph, though
    detect's parser tolerates it), and the property block plus the children sit
    one 2-column level deeper -- prefixed `| ` while an ancestor still has
    siblings pending, `  ` once it does not. Those columns are the only nesting
    information there is: detect's parser reads the depth out of them.
    """
    lines = []

    def walk(group, ancestry):
        for i, n in enumerate(group):
            last = i == len(group) - 1
            deeper = ancestry + ("  " if last else "| ")
            marker = "+-o "
            lines.append(
                "{}{}{}  <class {}, id {}, registered, matched, active, "
                "busy 0 (1 ms), retain 5>".format(
                    ancestry, marker, n["name"], n["cls"], n["id"]))
            if n["props"]:
                lines.append(deeper + "{")
                for key, value in n["props"]:
                    lines.append("{}  {} = {}".format(
                        deeper, quoted(key), value))
                lines.append(deeper + "}")
            walk(n["kids"], deeper)

    walk(nodes, "")
    return "\n".join(lines) + "\n"


def serial_client(port, tty=None):
    """The IOSerialBSDClient leaf that publishes a /dev/cu.* node."""
    tty_name = tty or port.rsplit("/", 1)[-1].split(".", 1)[-1]
    return node("IOSerialBSDClient", "IOSerialBSDClient", [
        ("IOClass", quoted("IOSerialBSDClient")),
        ("CFBundleIdentifier", quoted("com.apple.iokit.IOSerialFamily")),
        ("IOProviderClass", quoted("IOSerialStreamSync")),
        ("IOTTYBaseName", quoted("usbmodem")),
        ("IOTTYDevice", quoted(tty_name)),
        ("IOCalloutDevice", quoted(port)),
        ("IODialinDevice", quoted(port.replace("/dev/cu.", "/dev/tty."))),
        ("IOPersonalityPublisher", quoted("com.apple.iokit.IOSerialFamily")),
        ("IOTTYSuffix", quoted("14101")),
    ])


def usb_board(path="/dev/cu.usbmodem14101", vid=7504, pid=24910,
              serial="5044506110A45B1C", product="rp2040", vendor="Klipper",
              location_id=336592896, entry_name=None, with_serial=True,
              with_callout=True):
    """A complete USB-CDC board: device -> interface -> ACMData -> serial node.

    Defaults are the bench SKR Pico's real values, in ioreg's real units:
    idVendor 7504 (= 0x1d50), idProduct 24910 (= 0x614e), serial
    5044506110A45B1C, locationID 336592896 (= 0x14100000).
      with_serial=False  -> the CH340/FTDI case: no serial property at all.
      with_callout=False -> a USB device with no /dev/cu node (a board in UF2
                            bootloader mode, a camera): never a port, so it
                            must not show up in the listing.
    `serial=None` is the same as with_serial=False, spelled differently for the
    tests that only care about the serial.
    """
    if serial is None:
        with_serial = False
        serial = ""
    entry_name = entry_name or "{}@{:08x}".format(product, location_id)
    dev_props = [
        ("bDeviceClass", "2"),
        ("iProduct", "2"),
        ("iSerialNumber", "3"),
        ("USB Vendor Name", quoted(vendor)),
        ("kUSBProductString", quoted(product)),
        ("USB Product Name", quoted(product)),
        ("idVendor", str(vid)),
        ("idProduct", str(pid)),
        ("sessionID", "192920582145205"),
        ("locationID", str(location_id)),
        ("Device Speed", "1"),
        ("built-in", "No"),
    ]
    if with_serial:
        # ioreg publishes the serial twice; both forms exist on real boards.
        dev_props.insert(0, ("kUSBSerialNumberString", quoted(serial)))
        dev_props.insert(9, ("USB Serial Number", quoted(serial)))
    iface_props = [
        ("idProduct", str(pid)),
        ("idVendor", str(vid)),
        ("USB Product Name", quoted(product)),
        ("locationID", str(location_id)),
        ("bInterfaceClass", "10"),
    ]
    acm_props = [
        ("IOClass", quoted("AppleUSBACMData")),
        ("CFBundleIdentifier", quoted("com.apple.driver.usb.cdc.acm")),
        ("IOProviderClass", quoted("IOUSBHostInterface")),
        ("IOTTYBaseName", quoted("usbmodem")),
        ("IOTTYSuffix", quoted("14101")),
    ]
    if with_callout:
        leaf = node("AppleUSBACMData", "AppleUSBACMData", acm_props,
                    [serial_client(path)])
    else:
        # A USB device with no /dev/cu node at all (UF2 bootloader, a camera).
        leaf = node("AppleUSBACMData", "AppleUSBACMData", acm_props)
    return node(entry_name, "IOUSBHostDevice", dev_props, [
        node("IOUSBHostInterface@1", "IOUSBHostInterface", iface_props, [leaf]),
    ])


def bluetooth_port(path="/dev/cu.Air4SE"):
    """An RFCOMM serial node hanging off a USB Bluetooth controller.

    Deliberately built the WORST way for a naive parser: there IS an idVendor
    above it (the BT controller really is a USB device on this Mac), so the
    Bluetooth-stack check is what keeps headphones out of the serial picker.
    """
    return node("BCM20702A1@12000000", "IOUSBHostDevice", [
        ("idVendor", "1452"),
        ("idProduct", "5285"),
        ("USB Product Name", quoted("BCM20702A1")),
        ("locationID", "301989888"),
    ], [
        node("IOBluetoothHCIController", "IOBluetoothHCIController", [
            ("IOClass", quoted("IOBluetoothHCIController")),
        ], [
            node("Air4SE", "IOBluetoothSerialSession", [
                ("IOClass", quoted("IOBluetoothSerialSession")),
            ], [
                serial_client(path, tty=path.rsplit("/", 1)[-1].split(".", 1)[-1]),
            ]),
        ]),
    ])


def root_tree(boards, bluetooth=True):
    """The full `ioreg -p IOService -l` shape down to the XHCI root hub."""
    hub_kids = list(boards)
    if bluetooth:
        hub_kids = hub_kids + [bluetooth_port()]
    return [
        node("Root", "IORegistryEntry", [
            ("IORegistryPlanes", '{"IOService"="IOService","IOUSB"="IOUSB"}'),
            ("IOConsoleLocked", "No"),
        ], [
            node("MacBookPro15,4", "IOPlatformExpertDevice", [
                ("IOClass", quoted("IOPlatformExpertDevice")),
                ("product-name", quoted("MacBookPro15,4")),
            ], [
                node("AppleACPIPlatformExpert", "AppleACPIPlatformExpert", [], [
                    node("PCI0@0", "IOPCIExpBridge", [], [
                        node("AppleACPIPCI", "AppleACPIPCI", [], [
                            node("XHC1@14", "AppleACPIPCI", [], [
                                node("XHC1@14000000", "AppleUSBXHCIPCI", [
                                    ("IOClass", quoted("AppleUSBXHCIPCI")),
                                ], [
                                    node("AppleUSBXHCIRootHub@14000000",
                                         "AppleUSBXHCIRootHub", [], hub_kids),
                                ]),
                            ]),
                        ]),
                    ]),
                ]),
            ]),
        ]),
    ]


def tree_of(*boards, **kwargs):
    """Render a tree containing exactly these boards."""
    return render(root_tree(list(boards), bluetooth=kwargs.get("bluetooth", True)))


def one_board(**kwargs):
    """Render a tree with one board; defaults are the bench SKR Pico.

    The rename tests are this with a different `path=` and nothing else.
    """
    return tree_of(usb_board(**kwargs))


# The bench board's facts, as one place to change if the bench changes.
PICO_PATH = "/dev/cu.usbmodem14101"
PICO_SERIAL = "5044506110A45B1C"
PICO_VID_PID = "1D50:614E"
IDENTITY_KEYS = {"vid_pid", "serial", "name", "ioservice_path",
                 "path", "vendor", "location", "key"}


class RunRecorder:
    """Stands in for subprocess.run; records the argv + timeout it was handed."""

    def __init__(self, stdout=b"", exc=None, returncode=0):
        self.stdout = stdout
        self.exc = exc
        self.returncode = returncode
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "kwargs": kwargs})
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(list(argv), self.returncode,
                                           self.stdout, b"")


@pytest.fixture
def fake_run(monkeypatch):
    """Replace subprocess.run for detect's own `subprocess` module reference.

    `fake_run(text=...)` answers ioreg; `fake_run(exc=...)` makes it fail.
    Returns the recorder so a test can assert the argv is still enumeration
    (never a port open) and the timeout stayed under the cap.
    """
    def _install(text=None, exc=None, stdout=None, returncode=0):
        if stdout is None:
            if isinstance(text, bytes):
                stdout = text
            else:
                stdout = (text or "").encode("utf-8")
        recorder = RunRecorder(stdout=stdout, exc=exc, returncode=returncode)
        monkeypatch.setattr(detect.subprocess, "run", recorder)
        return recorder
    return _install


@pytest.fixture
def ioreg_says(fake_run):
    """`ioreg` answers with this text."""
    return lambda text: fake_run(text=text)


@pytest.fixture
def pico_tree():
    """The bench SKR Pico on /dev/cu.usbmodem14101, plus the real
    /dev/cu.Air4SE Bluetooth port the same Mac always has."""
    return one_board()


@pytest.fixture
def iokit():
    """The builders above, as one namespace.

    Deliberately a fixture and not `from conftest import ...`: several feature
    dirs here (tests/, tests/backup/, tests/identity/) each own a conftest.py,
    and pytest keys them by module name, so importing one by name is ambiguous
    when the whole tree runs together.
    """
    return types.SimpleNamespace(
        quoted=quoted, node=node, render=render, serial_client=serial_client,
        usb_board=usb_board, bluetooth_port=bluetooth_port,
        root_tree=root_tree, tree_of=tree_of, one_board=one_board,
        PICO_PATH=PICO_PATH, PICO_SERIAL=PICO_SERIAL,
        PICO_VID_PID=PICO_VID_PID, IDENTITY_KEYS=IDENTITY_KEYS)
