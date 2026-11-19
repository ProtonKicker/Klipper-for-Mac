"""The cli.py seam: `klipperformac serial` prints identity, and agrees with
`klipperformac flash detect` about who claims a port.

detect.identity()/all_identities() and flash.detect_boards() are each tested in
their own tiers; what no other file proves is the wiring - that cmd_serial
(a) renders the VID:PID/SER/LOC columns and the `-> [mcu]` claim, (b) degrades
to the pre-identity line shape when IOKit cannot be read or a node has no USB
board behind it, and (c) stays read-only: one `ioreg`, and never a path handed
to a child process (klippy holds its port with a flock; a second opener corrupts
a live printer).

The fake ioreg trees come from tests/identity/conftest.py, which reproduces this
bench Mac's real `ioreg -p IOService -l -w 0` output byte-for-byte in shape.
"""
import pytest

PORTS = [{"path": "/dev/cu.usbmodem14101", "description": "USB-CDC (STM32 / SKR"
                                                           " / Pi Pico class boards)"},
         {"path": "/dev/cu.URT0", "description": "WCH CH340/CH341"}]

CFG = """[mcu]
serial: /dev/cu.usbmodem14101
baud: 250000

[stepper_x]
step_pin: P2.1
"""


def _cli(fstack, monkeypatch, ports=PORTS):
    """Point the scanner at these nodes (a test cannot create /dev entries)."""
    monkeypatch.setattr(fstack.detect, "scan", lambda: list(ports))
    return fstack


@pytest.fixture
def fstack(stack):
    """`stack` plus the lazily-imported flash module, so cmd_serial's
    `from . import flash` has a module to talk to."""
    import importlib
    stack.flash = importlib.reload(
        importlib.import_module("klipperformac.flash"))
    return stack


def _cfg(fstack, text=CFG):
    fstack.paths.PRINTER_CFG.write_text(text)
    return text


def test_serial_listing_carries_the_identity_columns_and_the_claim(
        fstack, iokit, fake_run, monkeypatch, capsys):
    """The row a user needs after a replug: same board, new node, still [mcu]."""
    fake_run(text=iokit.tree_of(
        iokit.usb_board(),
        iokit.usb_board(path="/dev/cu.URT0", vid=6790, pid=29987,
                        product="USB Serial", vendor="wCH"),
    ))
    _cfg(fstack)
    _cli(fstack, monkeypatch)
    fstack.cli.main(["serial"])
    lines = capsys.readouterr().out.splitlines()
    pico = [row for row in lines if row.startswith("/dev/cu.usbmodem14101")]
    assert len(pico) == 1, lines
    assert "VID:PID=1d50:614e" in pico[0]
    assert "SER=5044506110A45B1C" in pico[0]
    assert "LOC=" in pico[0]
    assert "-> [mcu]" in pico[0], "printer.cfg's claim belongs on the row"
    ch340 = [row for row in lines if row.startswith("/dev/cu.URT0")]
    assert ch340 and "VID:PID=1a86:7523" in ch340[0]
    assert "-> " not in ch340[0], "an unclaimed port says nothing about [mcu]"


def test_a_node_with_no_usb_board_behind_it_prints_the_old_plain_line(
        fstack, iokit, fake_run, monkeypatch, capsys):
    """Bluetooth RFCOMM and virtual ports have no identity, and inventing one is
    how a printer gets pointed at a pair of headphones."""
    fake_run(text=iokit.one_board())
    ports = [{"path": "/dev/cu.Air4SE", "description": ""}]
    _cli(fstack, monkeypatch, ports)
    fstack.cli.main(["serial"])
    out = capsys.readouterr().out
    assert "/dev/cu.Air4SE" in out
    assert "VID:PID" not in out


def test_unreadable_iokit_degrades_to_the_pre_identity_listing(
        fstack, fake_run, monkeypatch, capsys):
    """No ioreg, a timeout, a sandbox: the listing must still work, because
    `serial` is how a user finds their port at all."""
    fake_run(exc=OSError("ioreg: not running"))
    _cfg(fstack)
    _cli(fstack, monkeypatch)
    fstack.cli.main(["serial"])
    out = capsys.readouterr().out
    assert "/dev/cu.usbmodem14101" in out
    assert "VID:PID" not in out
    assert "-> [mcu]" in out, "the claim comes from printer.cfg, not from ioreg"


def test_the_listing_opens_nothing_and_runs_no_child_but_ioreg(
        fstack, iokit, fake_run, monkeypatch):
    """The read-only promise one level up: the CLI's identity pass is
    enumeration, never a port handed to a subprocess."""
    rec = fake_run(text=iokit.one_board())
    _cfg(fstack)
    _cli(fstack, monkeypatch)
    fstack.cli.main(["serial"])
    assert rec.calls, "identity came from ioreg, not from a guess"
    for call in rec.calls:
        argv = [str(a) for a in call["argv"]]
        assert argv[0] == "ioreg", argv
        assert not any("/dev/cu" in a for a in argv), argv


def test_a_configured_port_that_is_not_attached_says_so_and_offers_the_fix(
        fstack, iokit, fake_run, monkeypatch, capsys):
    """macOS renumbered the node: this is the whole reason identity exists, so
    the listing has to name the problem and the command that fixes it."""
    fake_run(text=iokit.one_board(path="/dev/cu.usbmodem14102"))
    _cfg(fstack)
    _cli(fstack, monkeypatch,
         [{"path": "/dev/cu.usbmodem14102", "description": "USB-CDC"}])
    fstack.cli.main(["serial"])
    out = capsys.readouterr().out
    assert "[mcu] points at /dev/cu.usbmodem14101" in out
    assert "not attached right now" in out
    assert "klipperformac serial --set /dev/cu.usbmodem14102" in out


def test_two_sections_on_one_node_is_called_out(fstack, iokit, fake_run,
                                                monkeypatch, capsys):
    """Not a warning to ignore: the second klippy reader retries for 90 s and
    then shuts the printer down, so the listing says it in plain words."""
    fake_run(text=iokit.one_board())
    _cfg(fstack, CFG + "\n[mcu tool]\nserial: /dev/cu.usbmodem14101\n")
    _cli(fstack, monkeypatch)
    fstack.cli.main(["serial"])
    out = capsys.readouterr().out
    assert "both point at /dev/cu.usbmodem14101" in out
    assert "[mcu tool]" in out


def test_serial_and_flash_detect_agree_about_the_claim(fstack, iokit, fake_run,
                                                       monkeypatch, capsys):
    """One fact, one owner: cmd_serial reads the same section map
    flash.classify_targets() does, so a user cannot get two answers."""
    fake_run(text=iokit.one_board())
    _cfg(fstack)
    _cli(fstack, monkeypatch)
    fstack.cli.main(["serial"])
    out = capsys.readouterr().out
    assert fstack.flash.claimed_by_section() == \
        {"/dev/cu.usbmodem14101": "[mcu]"}
    assert "-> [mcu]" in out


def test_no_ports_no_children(fstack, fake_run, monkeypatch, capsys):
    """Empty scanner: identity is not even asked, so a machine with no board
    pays no ioreg cost and gets the same message as before."""
    rec = fake_run(text="")
    _cli(fstack, monkeypatch, [])
    fstack.cli.main(["serial"])
    assert "No USB serial devices found" in capsys.readouterr().out
    assert rec.calls == []
