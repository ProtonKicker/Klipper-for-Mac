"""detect.identity(): the USB board identity behind one /dev/cu.* node.

All offline: `subprocess.run` is monkeypatched with an ioreg-shaped string (see
tests/identity/conftest.py, whose renderer reproduces the structure of the bench
Mac's real `ioreg -p IOService -l -w 0` output -- decimal ids, `| ` tree bars,
escaped strings). Nothing here shells out, opens a port, resets a board or reads
the live registry; the attached-Pico case lives in test_identity_live_probe.py,
marked live+hardware.

The contract under test:
  identity(dev) -> {'vid_pid', 'serial', 'name', 'ioservice_path', ...} parsed
  from ioreg, bounded runtime, TOTAL FAILURE -> None. It never raises, because
  its callers are a GUI picker and a flash wizard that must degrade to "I don't
  know" instead of crashing on a machine with an odd kernel extension.
"""
import subprocess

import pytest

from klipperformac import detect

CONTRACT_KEYS = {"vid_pid", "serial", "name", "ioservice_path"}


# ---------------------------------------------------------------- happy path --
def test_identity_normal(ioreg_says, pico_tree):
    ioreg_says(pico_tree)
    ident = detect.identity("/dev/cu.usbmodem14101")
    assert ident is not None
    # The four contract keys, with the values read off the real bench board.
    assert ident["vid_pid"] == "1D50:614E"
    assert ident["serial"] == "5044506110A45B1C"
    assert ident["name"] == "rp2040"
    assert ident["ioservice_path"].endswith(
        "rp2040@14100000/IOUSBHostInterface@1/AppleUSBACMData/IOSerialBSDClient")
    assert CONTRACT_KEYS <= set(ident)


def test_identity_additive_keys_are_json_safe(ioreg_says, pico_tree, iokit):
    """Additive keys the bindings layer needs. Adding keys is allowed; these
    must never disappear, and the dict must stay JSON-serialisable because the
    GUI decodes `serial --list-json`."""
    import json
    ioreg_says(pico_tree)
    ident = detect.identity("/dev/cu.usbmodem14101")
    assert ident["path"] == iokit.PICO_PATH
    assert ident["vendor"] == "Klipper"
    assert ident["location"] == "14100000"          # locationID, 8 hex digits
    assert ident["key"] == "usb:1d50:614e:5044506110A45B1C"
    assert iokit.IDENTITY_KEYS == set(ident)
    assert json.loads(json.dumps(ident)) == ident


def test_identity_accepts_bare_and_tty_names(ioreg_says, pico_tree):
    """One board, five ways to spell it: /dev/cu, /dev/tty, bare, 'cu.'-only."""
    ioreg_says(pico_tree)
    for dev in ("/dev/cu.usbmodem14101", "cu.usbmodem14101",
                "usbmodem14101", "/dev/tty.usbmodem14101",
                "/dev/cu.usbmodem14101/"):
        ident = detect.identity(dev)
        assert ident is not None, dev
        assert ident["path"] == "/dev/cu.usbmodem14101", dev


def test_identity_is_built_from_decimal_ioreg_ids(ioreg_says, pico_tree):
    """ioreg prints idVendor/idProduct in DECIMAL (7504 / 24910 here, verified
    on the bench). identity() hands back the hex form everybody else uses."""
    ioreg_says(pico_tree)
    assert detect.identity("/dev/cu.usbmodem14101")["vid_pid"] == "1D50:614E"


def test_identity_prefers_the_board_over_a_parent_usb_hub(ioreg_says, iokit):
    """A board hanging off a hub must keep the board's ids, not the hub's.

    The live bench now has exactly this shape: a usbmodem node under an RP2040
    below a USB2.1 hub. Walking "outward until ids appear" picked the hub's
    05E3:0610 instead of the board's 1D50:614E.
    """
    board = iokit.usb_board(path="/dev/cu.usbmodem142101", location_id=337772544)
    hub = iokit.node("USB2.1 Hub@14200000", "IOUSBHostDevice", [
        ("USB Vendor Name", iokit.quoted("GenesysLogic")),
        ("USB Product Name", iokit.quoted("USB2.1 Hub")),
        ("idVendor", str(1507)),
        ("idProduct", str(1552)),
        ("locationID", str(337641472)),
    ], [board])
    ioreg_says(iokit.render(iokit.root_tree([hub], bluetooth=False)))
    ident = detect.identity("/dev/cu.usbmodem142101")
    assert ident is not None
    assert ident["vid_pid"] == "1D50:614E", ident
    assert ident["serial"] == iokit.PICO_SERIAL, ident
    assert ident["name"] == "rp2040", ident


# ------------------------------------------------------------- serial strings --
def test_identity_without_serial_field(ioreg_says, iokit):
    """CH340/FTDI bridges publish no serial: serial is '' and the key degrades
    to the physical path (loc:), which is the honest answer -- stable only while
    the cable stays in the same port."""
    ioreg_says(iokit.one_board(path="/dev/cu.USR2", product="usbserial",
                               vid=6790, pid=29987, with_serial=False,
                               location_id=50331648))
    ident = detect.identity("/dev/cu.USR2")
    assert ident is not None
    assert ident["serial"] == ""
    assert ident["vid_pid"] == "1A86:7523"
    assert ident["key"] == "loc:03000000"
    assert CONTRACT_KEYS <= set(ident)


def test_identity_serial_with_weird_chars(ioreg_says, iokit):
    """Vendor firmware with a hostile serial string (quote, backslash, tab,
    spaces, UTF-8). ioreg escapes it; we un-escape it and still never raise --
    the serial is data, never code."""
    weird = 'SP "1\\2\t3 4-Klipperün'
    ioreg_says(iokit.one_board(serial=weird))
    ident = detect.identity("/dev/cu.usbmodem14101")
    assert ident is not None
    assert ident["serial"] == weird
    # The binding key collapses whitespace so it can never smuggle a line into
    # settings.json or a printed table.
    assert ident["key"].startswith("usb:1d50:614e:")
    assert "\n" not in ident["key"] and "\t" not in ident["key"]


def test_identity_serial_with_newline_survives(ioreg_says, iokit):
    """A \\n inside a serial comes back as a real newline (the value is only
    ever compared or echoed, never written to config) and lookups still work."""
    ioreg_says(iokit.one_board(serial="AB\nCD"))
    ident = detect.identity("/dev/cu.usbmodem14101")
    assert ident["serial"] == "AB\nCD"
    assert detect.find_by_identity("1D50:614E", "ab cd") == "/dev/cu.usbmodem14101"


# ----------------------------------------------------------------- vanished --
def test_identity_device_vanished(ioreg_says, pico_tree, iokit):
    """Unplug between calls: the node simply is not in the registry, so the
    answer is None -- not a stale identity, not an exception."""
    ioreg_says(pico_tree)
    assert detect.identity("/dev/cu.usbmodem14101") is not None
    ioreg_says(iokit.one_board(path="/dev/cu.usbmodem99999"))
    assert detect.identity("/dev/cu.usbmodem14101") is None
    assert [p["path"] for p in detect.all_identities()] == ["/dev/cu.usbmodem99999"]


def test_identity_other_port(ioreg_says, pico_tree):
    """A port that exists but is not the board asked for."""
    ioreg_says(pico_tree)
    assert detect.identity("/dev/cu.usbserial-110") is None


@pytest.mark.parametrize("dev", [None, "", "   ", 1234, [], {}, "/dev/", "/"])
def test_identity_junk_argument(ioreg_says, pico_tree, dev):
    """Callers pass whatever the GUI or printer.cfg happened to contain."""
    ioreg_says(pico_tree)
    assert detect.identity(dev) is None


# ----------------------------------------------------- malformed ioreg output --
@pytest.mark.parametrize("garbage", [
    b"\x00\x01\x02\xff\xfe garbage \xb0\xa0",       # raw non-UTF-8 bytes
    b"",                                            # ioreg printed nothing
    b"+-o ",                                        # a truncated tree line
    b"not ioreg at all\njust some text\n",
    b'{"IOCalloutDevice" = }{[',                    # brace soup
    b'"idVendor" = 7504\n"x" = \xc3\x28\n',         # broken UTF-8 mid-stream
    b"+-o Root  <class IORegistryEntry, id 0x1>\n"  # ids that are not numbers
    b'  | {\n  |   "idVendor" = not_a_number\n'
    b'  |   "idProduct" = \n'
    b'  |   "IOCalloutDevice" = "/dev/cu.usbmodem14101"\n  | }\n',
])
def test_identity_garbage_output_is_none(fake_run, garbage):
    """A kernel extension that confuses ioreg must not confuse us into a crash
    OR into inventing a board."""
    fake_run(stdout=garbage)
    assert detect.identity("/dev/cu.usbmodem14101") is None
    assert detect.all_identities() == []


def test_identity_nonzero_exit(fake_run):
    """ioreg failed (unknown plane, EPERM): a partial tree with no usable port
    still answers None."""
    fake_run(stdout=b"+-o Root  <class IORegistryEntry, id 0x1>\n", returncode=1)
    assert detect.identity("/dev/cu.usbmodem14101") is None


def test_identity_no_ioreg_binary(fake_run):
    """A machine without /usr/sbin/ioreg (or a stripped CI container)."""
    fake_run(exc=FileNotFoundError("ioreg"))
    assert detect.identity("/dev/cu.usbmodem14101") is None
    assert detect.all_identities() == []


def test_identity_timeout_returns_none(fake_run):
    """The registry hung: subprocess raises TimeoutExpired, identity() answers
    None and does NOT propagate -- a wedged ioreg may never wedge the GUI."""
    fake_run(exc=subprocess.TimeoutExpired(cmd="ioreg", timeout=3))
    assert detect.identity("/dev/cu.usbmodem14101") is None
    assert detect.find_by_identity("1d50:614e") is None
    assert detect.find_all_by_identity("1d50:614e") == []
    assert detect.all_identities() == []


def test_identity_other_subprocess_error(fake_run):
    fake_run(exc=subprocess.SubprocessError("boom"))
    assert detect.identity("/dev/cu.usbmodem14101") is None


def test_identity_survives_an_absurd_subprocess_result(fake_run):
    """The never-raises half of the contract, taken literally: whatever shape
    the subprocess result comes back in -- stdout as text instead of bytes, no
    result at all -- the caller gets None, not a traceback, because it is a
    serial picker on the main thread of a GUI."""
    fake_run(stdout="+-o Root  <class IORegistryEntry, id 0x1>\n")  # str, not bytes
    assert detect.identity("/dev/cu.usbmodem14101") is None
    fake_run(exc=RuntimeError("a test double nobody expected"))
    assert detect.identity("/dev/cu.usbmodem14101") is None
    assert detect.all_identities() == []


# ------------------------------------------------------------ runtime bounds --
def test_command_is_enumeration_only(fake_run, iokit):
    """`ioreg -p IOService -l -w 0` and nothing else: no port path is ever
    opened (klippy holds it with flock, so a second opener corrupts a live
    printer) and no board is reset."""
    rec = fake_run(text=iokit.one_board())
    assert detect.identity("/dev/cu.usbmodem14101") is not None
    call = rec.calls[-1]
    assert call["argv"] == ["ioreg", "-p", "IOService", "-l", "-w", "0"]
    assert "usbmodem" not in " ".join(call["argv"])
    assert call["kwargs"]["stdin"] is subprocess.DEVNULL


def test_timeout_is_bounded_and_clamped(fake_run, iokit):
    """Callers may ask for less than 3 s, never more: the cap is what keeps a
    GUI tick responsive."""
    rec = fake_run(text=iokit.one_board())
    detect.identity("/dev/cu.usbmodem14101", timeout=10)
    assert rec.calls[-1]["kwargs"]["timeout"] == detect.IOREG_TIMEOUT_CAP
    assert rec.calls[-1]["kwargs"]["timeout"] <= 3.0
    detect.identity("/dev/cu.usbmodem14101", timeout=0.5)
    assert rec.calls[-1]["kwargs"]["timeout"] == pytest.approx(0.5)
    # A non-positive or nonsense timeout means "don't even try", not "forever".
    assert detect.identity("/dev/cu.usbmodem14101", timeout=0) is None
    assert detect.identity("/dev/cu.usbmodem14101", timeout=-2) is None
    assert detect.identity("/dev/cu.usbmodem14101", timeout="soon") is None
    assert len(rec.calls) == 2


def test_single_ioreg_call_per_query(fake_run, iokit):
    """One call per public query: cheap enough for the GUI, and no cache that
    could report a stale port name right after a replug."""
    rec = fake_run(text=iokit.one_board())
    detect.identity("/dev/cu.usbmodem14101")
    assert len(rec.calls) == 1


def test_identity_retries_once_when_the_first_ioreg_pass_is_transiently_empty(
        monkeypatch, iokit):
    """The live board can reappear in /dev before ioreg repopulates the USB
    ancestry detect.identity() needs. One bounded retry should recover."""
    calls = []
    answers = [
        subprocess.CompletedProcess(list(detect._IOREG_ARGV), 0,
                                    iokit.render(iokit.root_tree([])).encode(),
                                    b""),
        subprocess.CompletedProcess(list(detect._IOREG_ARGV), 0,
                                    iokit.one_board().encode(), b""),
    ]

    def fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        return answers[min(len(calls) - 1, len(answers) - 1)]

    sleeps = []
    monkeypatch.setattr(detect.subprocess, "run", fake_run)
    monkeypatch.setattr(detect.time, "sleep", lambda secs: sleeps.append(secs))
    ident = detect.identity("/dev/cu.usbmodem14101")
    assert ident is not None
    assert ident["path"] == iokit.PICO_PATH
    assert len(calls) == 2
    assert sleeps and sleeps[0] <= detect._IOREG_EMPTY_RETRY_DELAY


# --------------------------------------------------------------- non-printers --
def test_identity_ignores_bluetooth_port(fake_run, iokit):
    """/dev/cu.Air4SE is an RFCOMM tunnel to headphones. It has a /dev/cu path
    AND a USB controller above it carrying idVendor/idProduct, so the Bluetooth
    check -- not luck -- is what keeps it out of the serial picker."""
    fake_run(text=iokit.render(iokit.root_tree(
        [iokit.usb_board(), iokit.bluetooth_port()])))
    assert detect.identity("/dev/cu.Air4SE") is None
    assert [p["path"] for p in detect.all_identities()] == [iokit.PICO_PATH]


def test_identity_ignores_non_usb_serial_stack(fake_run, iokit):
    """A serial node with no USB device anywhere above it."""
    fake_run(text=iokit.render([iokit.node(
        "SomePlatformExpert", "IOPlatformExpertDevice", [], [
            iokit.node("virtual", "IOSerialBSDClient", [],
                       [iokit.serial_client("/dev/cu.watson")])])]))
    assert detect.identity("/dev/cu.watson") is None
    assert detect.all_identities() == []


def test_usb_device_without_a_port_is_not_listed(fake_run, iokit):
    """A board sitting in UF2 bootloader mode is mass storage: no
    IOCalloutDevice, so no port. `flash detect` (S2) must pair this API with
    `diskutil list` -- pinned here so the behaviour is never mistaken for a bug
    later."""
    fake_run(text=iokit.one_board(product="USB Boot", vid=11914, pid=3,
                                  with_callout=False))
    assert detect.all_identities() == []
    assert detect.find_by_identity("2E8A:0003") is None
