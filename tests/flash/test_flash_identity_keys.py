"""The flash+identity seam: an identity KEY is a usable `--port`.

`flash detect --json` prints `key=usb:1d50:614e:<SER>` next to every board, and
`flash serial --port <that key>` is the promise that makes the key worth having:
macOS renames /dev/cu.usbmodemNNNNN on every replug, so a user who copy-pasted a
path an hour ago has a stale one. cli.py wires the two commands together; this
file pins the three key shapes detect.identity() produces, the order they are
tried in, and - above all - that an unresolvable key stops before any flasher
runs.

detect's own tier tests the IOKit parsing; this one only tests the seam, so the
identity layer is stubbed at its public functions rather than faked through ioreg
text.
"""
import argparse

PICO_KEY = "usb:1d50:614e:5044506110A45B1C"
PICO_PATH = "/dev/cu.usbmodem14101"

PICO_ROW = {"path": PICO_PATH, "vid_pid": "1D50:614E",
            "serial": "5044506110A45B1C", "name": "rp2040",
            "vendor": "Klipper", "location": "14100000", "key": PICO_KEY}
CH340_ROW = {"path": "/dev/cu.URT0", "vid_pid": "1A86:7523", "serial": "",
             "name": "USB Serial", "vendor": "wch", "location": "301989888",
             "key": "loc:301989888"}


def _identity(fstack, monkeypatch, rows=(PICO_ROW, CH340_ROW), spy=None,
              found=None):
    """Stub the identity layer's two public answers."""
    monkeypatch.setattr(fstack.detect, "all_identities",
                        lambda timeout=None: list(rows))
    if spy is not None:
        monkeypatch.setattr(fstack.detect, "find_by_identity", spy)
    elif found is not None:
        monkeypatch.setattr(fstack.detect, "find_by_identity",
                            lambda vid_pid, serial=None, timeout=None: found)
    return fstack


def _finder(seen, found):
    """A find_by_identity() that records what it was asked and answers `found`."""
    def spy(vid_pid, serial=None, timeout=None):
        seen.append((vid_pid, serial))
        return found
    return spy


def test_a_usb_key_resolves_through_detect_by_id(fstack, monkeypatch):
    """The keys and values handed over are exactly the by-id pair."""
    seen = []
    _identity(fstack, monkeypatch, spy=_finder(seen, PICO_PATH))
    assert fstack.flash.resolve_port(PICO_KEY) == (PICO_PATH, "")
    assert seen == [("1d50:614e", "5044506110A45B1C")]


def test_a_location_key_is_answered_from_the_rows_not_by_vid_pid(fstack,
                                                                 monkeypatch):
    """`loc:` has no VID:PID to hand over: asking by-id would match a different
    board on the same chip, so the row whose key equals the location wins."""
    def never(*a, **kw):
        raise AssertionError("find_by_identity must not be called for loc:")

    _identity(fstack, monkeypatch, spy=never)
    assert fstack.flash.resolve_port("loc:301989888") == ("/dev/cu.URT0", "")
    assert fstack.flash.find_board_by_identity("loc:301989888") is None


def test_a_vidpid_key_without_a_serial_asks_by_id_only(fstack, monkeypatch):
    seen = []
    _identity(fstack, monkeypatch, spy=_finder(seen, None))
    # Nothing matched by-id, and no row carries that key either.
    assert fstack.flash.resolve_port("vidpid:1d50:ffff") == (
        None, "vidpid:1d50:ffff")
    assert seen == [("1d50:ffff", None)]


def test_a_serial_containing_a_colon_survives_the_key(fstack, monkeypatch):
    """detect builds `usb:<vid>:<pid>:<serial>`, so a serial with a ':' in it
    must be re-joined, not split again - the alternative silently matches
    nothing and the user gets "no board found" for a board that is plugged in."""
    key = "usb:1d50:614e:AA:BB"
    seen = []
    _identity(fstack, monkeypatch, rows=[dict(PICO_ROW, key=key,
                                              serial="AA:BB")],
              spy=_finder(seen, PICO_PATH))
    assert fstack.flash.resolve_port(key) == (PICO_PATH, "")
    assert seen == [("1d50:614e", "AA:BB")]


def test_a_plain_path_is_used_as_given_and_asks_no_one(fstack, monkeypatch):
    def never(*a, **kw):
        raise AssertionError("a literal path must not trigger identity lookup")

    _identity(fstack, monkeypatch, spy=never)
    assert fstack.flash.resolve_port(PICO_PATH) == (PICO_PATH, "")


def test_an_unknown_key_resolves_to_nothing_and_says_which(fstack,
                                                           monkeypatch):
    """The note is the key itself, so the refusal can name what it could not
    find instead of blaming the port the user typed."""
    _identity(fstack, monkeypatch, found=None)
    assert fstack.flash.resolve_port(PICO_KEY.replace("5044", "9999")) == (
        None, PICO_KEY.replace("5044", "9999"))


def test_flash_serial_refuses_an_unresolvable_key_before_any_flasher(fstack,
                                                                     monkeypatch,
                                                                     uf2bytes):
    """The order matters: key resolution happens first, so a stale identity can
    never reach stm32flash - nothing runs, and the message names the key. The
    firmware file here is deliberately valid, so the refusal can only be about
    the port."""
    fstack.down()
    unknown = PICO_KEY.replace("5044", "9999")
    rec = fstack.use_recorder()
    _identity(fstack, monkeypatch, found=None)
    monkeypatch.setattr(fstack.flash, "port_present", lambda p: True)
    bin_path = fstack.artifacts(name="klipper.bin",
                               blob=uf2bytes.bin_image())
    ok, lines = fstack.flash.flash_serial(bin_path, unknown, dry_run=True)
    assert not ok
    assert "No board now matches " + unknown in "\n".join(lines)
    assert rec.commands() == []


def test_flash_serial_writes_to_the_node_the_board_has_today(fstack, monkeypatch,
                                                             uf2bytes):
    """End-to-end at the module seam: the plan names the resolved /dev node, and
    a dry run still starts no child."""
    fstack.down()
    asked = []
    monkeypatch.setattr(fstack.flash, "port_present",
                        lambda p: asked.append(p) or True)
    stack_tools = fstack.tools("stm32flash")
    assert stack_tools == {"stm32flash"}
    rec = fstack.use_recorder()
    bin_path = fstack.artifacts(name="klipper.bin",
                                blob=uf2bytes.bin_image())
    _identity(fstack, monkeypatch, found=PICO_PATH)
    ok, lines = fstack.flash.flash_serial(bin_path, PICO_KEY,
                                          chip="stm32f407", tool="stm32flash",
                                          dry_run=True)
    assert ok, lines
    assert asked == [PICO_PATH]
    text = "\n".join(lines)
    assert PICO_PATH in text, text
    assert "usb:1d50" not in text, "the key must not be passed to a flasher"
    assert rec.commands() == [], rec.commands()


def test_the_key_is_printed_where_a_user_would_copy_it(fstack, monkeypatch):
    """`flash detect` hands the key over; `--port` takes it back. One round
    trip, asserted on both sides so a rename of either breaks a test."""
    fstack.down()
    _identity(fstack, monkeypatch, found=PICO_PATH)
    monkeypatch.setattr(fstack.flash, "ioreg_usb_devices",
                        lambda *a, **kw: [])
    monkeypatch.setattr(fstack.flash, "list_volumes", lambda: [])
    monkeypatch.setattr(fstack.flash, "claimed_by_section",
                        lambda sections=None: {PICO_PATH: "[mcu]"})
    text = "\n".join(fstack.flash.detect_lines(fstack.flash.detect_boards()))
    assert "key=" + PICO_KEY in text, text
    assert "-> [mcu]" in text, text
    assert fstack.flash.resolve_port(PICO_KEY) == (PICO_PATH, "")


def test_dispatch_serial_accepts_a_key_as_its_port(fstack, monkeypatch, capsys,
                                                   uf2bytes):
    """The argparse seam: `flash serial --port usb:...` resolves, plans, and -
    because this is a dry run - writes nothing."""
    fstack.down()
    fstack.tools("stm32flash")
    rec = fstack.use_recorder()
    _identity(fstack, monkeypatch, found=PICO_PATH)
    monkeypatch.setattr(fstack.flash, "port_present", lambda p: True)
    bin_path = fstack.artifacts(name="klipper.bin",
                                blob=uf2bytes.bin_image())
    code = fstack.flash.dispatch(argparse.Namespace(
        action="serial", bin=bin_path, path=None, port=PICO_KEY,
        chip="stm32f407", tool="stm32flash", dry_run=True))
    assert code == 0, code
    out = capsys.readouterr().out
    assert PICO_PATH in out, out
    assert "Dry run" in out
    assert rec.commands() == []
