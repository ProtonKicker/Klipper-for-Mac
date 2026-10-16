"""`flash detect`: what is attached, and in which state.

The fixtures are saved ioreg samples: the SKR Pico node is the property dict
this Mac really reports (captured read-only with `ioreg -a -x -l -p IOUSB` on
2026-10-16 - idVendor 7504 = 0x1d50, idProduct 24910 = 0x614e, "USB Serial
Number" 5044506110A45B1C, locationID 336592896 = 0x14100000), and the BOOTSEL /
DFU nodes are the ids upstream's own flasher names
(scripts/flash_usb.py: 2e8a:0003, 0483:df11, 1eaf:0003).

Detection is read-only by design and these tests enforce it: no port is opened
(the multi-MCU spec's section 0: klippy holds it with a flock), nothing is
ejected, and the only subprocess is `ioreg`, which prints the registry.
"""
import argparse
import json
import os

import pytest


def test_the_saved_ioreg_sample_parses_into_one_board(fstack, uf2bytes):
    fstack.use_recorder({"ioreg": (0, uf2bytes.ioreg_plist(
        [uf2bytes.SKR_PICO_NODE]))}, as_bytes=True)
    rows = fstack.flash.ioreg_usb_devices()
    assert len(rows) == 1
    row = rows[0]
    assert row["vidpid"] == "1d50:614e"
    assert row["product"] == "rp2040"
    assert row["vendor"] == "Klipper"
    assert row["serial"] == "5044506110A45B1C"
    assert row["location"] == "14100000"


def test_the_ioreg_command_is_the_plist_form_and_read_only(fstack):
    """`ioreg -a -x -l -p IOUSB` (archive, XML plist, properties, USB plane).
    A missing -x would make this a text parser nobody can trust, and a -w/-r
    flag would be a write."""
    rec = fstack.use_recorder({"ioreg": (0, b"<plist><dict/></plist>")},
                              as_bytes=True)
    fstack.flash.ioreg_usb_devices()
    assert rec.commands() == [["ioreg", "-a", "-x", "-l", "-p", "IOUSB"]]


def test_a_board_in_bootsel_is_recognised_by_its_usb_ids(fstack, uf2bytes):
    """2e8a:0003 is the RP2040 boot ROM's own id (upstream's `rawdev`), and a
    BOOTSEL board is the one thing this command exists to find."""
    fstack.use_recorder({"ioreg": (0, uf2bytes.ioreg_plist(
        [uf2bytes.RP2040_BOOTSEL_NODE]))}, as_bytes=True)
    rows = fstack.flash.ioreg_usb_devices()
    assert rows[0]["vidpid"] == "2e8a:0003"
    targets = fstack.flash.classify_targets([], rows, [])
    assert [t["state"] for t in targets] == ["uf2-bootloader"]
    assert targets[0]["action"] == "flash uf2"


def test_a_dfu_device_is_recognised_for_both_bootloaders(fstack, uf2bytes):
    fstack.use_recorder({"ioreg": (0, uf2bytes.ioreg_plist(
        [uf2bytes.STM32_DFU_NODE, uf2bytes.STM32_DUINO_NODE]))},
        as_bytes=True)
    rows = fstack.flash.ioreg_usb_devices()
    assert [r["vidpid"] for r in rows] == ["0483:df11", "1eaf:0003"]
    targets = fstack.flash.classify_targets([], rows, [])
    assert [t["state"] for t in targets] == ["dfu", "dfu"]
    assert [t["action"] for t in targets] == ["flash dfu", "flash dfu"]
    assert "AN2606" in targets[0]["hint"]
    assert "stm32duino" in targets[1]["hint"]


def test_a_bluetooth_node_has_no_usb_ids_and_is_not_a_board(fstack, uf2bytes):
    fstack.use_recorder({"ioreg": (0, uf2bytes.ioreg_plist(
        [uf2bytes.BLUETOOTH_NODE]))}, as_bytes=True)
    assert fstack.flash.ioreg_usb_devices() == []


@pytest.mark.parametrize("payload", [b"not a plist at all", b"", b"<xml/ >",
                                     b"\x00\x01\x02\x03"])
def test_an_unparseable_ioreg_answer_is_empty_not_a_crash(fstack, payload):
    """`flash detect` runs before anyone knows whether IOKit is cooperative.
    An empty answer is a report with no boards, never a traceback."""
    fstack.use_recorder({"ioreg": (0, payload)}, as_bytes=True)
    assert fstack.flash.ioreg_usb_devices() == []


def test_a_missing_ioreg_is_empty_not_a_crash(fstack):
    fstack.use_recorder({"ioreg": (None, "", "No such file or directory")},
                        as_bytes=True)
    assert fstack.flash.ioreg_usb_devices() == []


def test_a_board_running_klipper_is_reported_as_the_app(fstack, uf2bytes):
    """The common state (this Mac right now): the board is in Klipper, not in a
    bootloader, and flashing it is the thing that must be refused."""
    targets = fstack.flash.classify_targets(
        [{"path": "/dev/cu.usbmodem14101", "vid_pid": "1D50:614E",
          "serial": "5044506110A45B1C", "name": "rp2040",
          "key": "usb:1d50:614e:5044506110A45B1C", "location": "14100000"}],
        [], [])
    assert len(targets) == 1
    row = targets[0]
    assert row["state"] == "app"
    assert row["vidpid"] == "1d50:614e"
    assert row["product"] == "rp2040"
    assert row["key"].startswith("usb:")


def test_a_node_without_a_usb_identity_is_reported_as_unknown(fstack):
    """Path-only enumeration (identity layer unavailable) cannot tell a printer
    board from a Bluetooth port, so it says "unknown" instead of guessing."""
    targets = fstack.flash.classify_targets(
        [{"path": "/dev/cu.Bluetooth-Incoming-Port"}], [], [])
    assert targets[0]["state"] == "unknown"
    assert "no USB identity" in targets[0]["hint"]


def test_a_bootloader_behind_a_serial_node_is_still_a_bootloader(fstack):
    """An RP2040 in BOOTSEL also creates a /dev/cu node on some driver stacks;
    the ids win over the node kind, so the action is right either way."""
    targets = fstack.flash.classify_targets(
        [{"path": "/dev/cu.usbmodemBOOTS1", "vid_pid": "2E8A:0003"}], [], [])
    assert targets[0]["state"] == "uf2-bootloader"
    assert targets[0]["action"] == "flash uf2"


def test_a_mounted_bootloader_volume_becomes_a_target(fstack, uf2bytes):
    fstack.make_volume("RPI-RP2")
    targets = fstack.flash.classify_targets([], [], ["RPI-RP2"])
    assert targets[0]["kind"] == "uf2-volume"
    assert targets[0]["state"] == "uf2-bootloader"
    assert targets[0]["mount"].endswith("/RPI-RP2")


def test_anordinary_volume_is_not_a_target(fstack):
    """A USB stick is mounted, writable and not a board: the INFO_UF2.TXT shape
    is the proof, and the classifier uses the same function the writer uses."""
    fstack.make_volume("SANDISK", bootsel=False)
    assert fstack.flash.classify_targets([], [], ["SANDISK"]) == []


def test_printer_cfg_sections_are_named_next_to_their_port(fstack):
    """The multi-MCU contract: [mcu] is the primary board, [mcu tool] a
    secondary; the report has to say which section each node belongs to so a
    flash is never aimed at the wrong board's config entry."""
    cfg = fstack.paths.CONFIG / "printer.cfg"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("[mcu]\nserial: /dev/cu.usbmodem14101\n\n"
                   "[mcu tool]\nserial: /dev/cu.usbmodem14201\n\n"
                   "[stepper_x]\nstep_pin: gpio11\n")
    rows = fstack.flash.configured_serials(cfg)
    assert rows == [("mcu", "/dev/cu.usbmodem14101"),
                    ("tool", "/dev/cu.usbmodem14201")]
    claimed = fstack.flash.claimed_by_section(rows)
    assert claimed["/dev/cu.usbmodem14101"] == "[mcu]"
    assert claimed["/dev/cu.usbmodem14201"] == "[mcu tool]"
    targets = fstack.flash.classify_targets(
        [{"path": "/dev/cu.usbmodem14101", "vid_pid": "1D50:614E"}], [], [],
        claimed=claimed)
    assert targets[0]["mcu"] == "[mcu]"


def test_a_commented_or_missing_config_is_not_a_section(fstack):
    """Klipper treats '#' and ';' as comment prefixes and flash must not invent
    an [mcu] from somebody's commented-out example."""
    cfg = fstack.paths.CONFIG / "printer.cfg"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("# [mcu]\n# serial: /dev/cu.gone\n[printer]\n"
                   "kinematics: cartesian\n")
    assert fstack.flash.configured_serials(cfg) == []
    assert fstack.flash.claimed_by_section() == {}


def test_a_missing_printer_cfg_is_an_empty_answer(fstack):
    assert fstack.flash.configured_serials() == []


def test_the_identity_layer_is_used_when_detect_has_it(fstack, monkeypatch):
    """flash calls detect.all_identities() (the list form) and only falls back
    to path-only scanning when the symbol is missing or returns nothing."""
    monkeypatch.setattr(fstack.detect, "all_identities",
                        lambda timeout=None: [{"path": "/dev/cu.usbmodemX",
                                               "vid_pid": "1D50:614E"}])
    fstack.use_recorder({"ioreg": (0, b"<plist><dict/></plist>")},
                        as_bytes=True)
    report = fstack.flash.detect_boards()
    assert report["identity"] is True
    assert report["targets"][0]["vidpid"] == "1d50:614e"


def test_detect_falls_back_to_paths_when_identity_is_gone(fstack, monkeypatch):
    """S1's identity layer may be absent (or mid-edit). The report must still
    come out - with an explicit note that VID:PID/SER are missing - because
    `flash detect` is the first thing a stuck user runs."""
    monkeypatch.delattr(fstack.detect, "all_identities", raising=False)
    monkeypatch.delattr(fstack.detect, "identity", raising=False)
    monkeypatch.delattr(fstack.detect, "find_by_identity", raising=False)
    monkeypatch.setattr(fstack.detect, "scan",
                        lambda: [{"path": "/dev/cu.usbmodem14101",
                                  "description": "USB-CDC"}])
    fstack.use_recorder({"ioreg": (0, b"<plist><dict/></plist>")},
                        as_bytes=True)
    report = fstack.flash.detect_boards()
    assert report["identity"] is False
    assert report["ports"] == [{"path": "/dev/cu.usbmodem14101",
                                "description": "USB-CDC"}]
    lines = fstack.flash.detect_lines(report)
    assert any("identity layer unavailable" in line for line in lines)


def test_a_broken_identity_function_is_treated_as_absent(fstack, monkeypatch):
    """Every call into the identity layer is guarded: a helper that raises is
    worse than one that is missing, because it would take `flash detect` down
    with it."""
    def boom(timeout=None):
        raise RuntimeError("ioreg went sideways")

    monkeypatch.setattr(fstack.detect, "all_identities", boom)
    monkeypatch.setattr(fstack.detect, "identity", boom)
    monkeypatch.setattr(fstack.detect, "scan", lambda: [])
    fstack.use_recorder({"ioreg": (0, b"<plist><dict/></plist>")},
                        as_bytes=True)
    assert fstack.flash.identity_rows() is None
    assert fstack.flash.detect_boards()["identity"] is False


def test_detect_human_output_names_the_board_and_its_state(fstack,
                                                            monkeypatch,
                                                            capsys):
    monkeypatch.setattr(fstack.detect, "all_identities",
                        lambda timeout=None: [{
                            "path": "/dev/cu.usbmodem14101",
                            "vid_pid": "1D50:614E",
                            "serial": "5044506110A45B1C",
                            "name": "rp2040", "location": "14100000",
                            "key": "usb:1d50:614e:5044506110A45B1C"}])
    monkeypatch.setattr(fstack.process, "running",
                        lambda: {"klipper": True, "moonraker": True,
                                 "web_mainsail": False, "web_fluidd": False,
                                 "caffeinate": False})
    fstack.use_recorder({"ioreg": (0, b"<plist><dict/></plist>")},
                        as_bytes=True)
    code = fstack.flash.dispatch(argparse.Namespace(
        action="detect", json=False))
    assert code == 0
    out = capsys.readouterr().out
    assert "/dev/cu.usbmodem14101" in out
    assert "app" in out
    assert "VID:PID=1d50:614e" in out
    assert "SER=5044506110A45B1C" in out
    assert "stack is running" in out


def test_detect_json_is_machine_readable_for_the_gui(fstack, monkeypatch):
    """The SwiftUI side decodes this, so the shape is a contract: sorted keys,
    one object, and the same fields the human view prints."""
    monkeypatch.setattr(fstack.detect, "all_identities",
                        lambda timeout=None: [])
    rec = fstack.use_recorder({"ioreg": (0, b"<plist><dict/></plist>")},
                              as_bytes=True)
    line = fstack.flash.detect_lines(fstack.flash.detect_boards(),
                                    as_json=True)
    assert len(line) == 1
    data = json.loads(line[0])
    assert set(data) >= {"targets", "ports", "usb", "volumes", "stack_up",
                         "identity", "bootloader_volume"}
    assert data["identity"] in (True, False)
    assert isinstance(data["targets"], list)
    assert not rec.ran("diskutil")


def test_detect_never_opens_a_port_and_never_ejects_anything(fstack,
                                                             uf2bytes,
                                                             monkeypatch):
    """The two hard rules of enumeration, asserted by watching every subprocess
    argv: only `ioreg` is allowed, and the board keeps talking to klippy."""
    monkeypatch.setattr(fstack.detect, "all_identities",
                        lambda timeout=None: [])
    rec = fstack.use_recorder({"ioreg": (0, uf2bytes.ioreg_plist(
        [uf2bytes.SKR_PICO_NODE]))}, as_bytes=True)
    fstack.flash.detect_boards()
    for argv in rec.commands():
        assert os.path.basename(argv[0]) == "ioreg", argv
        assert not any(k in argv[0].lower()
                       for k in ("diskutil", "dfu-util", "stm32flash"))


def test_the_bootloader_instructions_survive_in_the_empty_report(fstack):
    """A user with nothing in BOOTSEL gets the same five lines every time -
    that text is the product."""
    monkeypatch_result = fstack.flash.classify_targets([], [], [])
    lines = fstack.flash.detect_lines({"targets": monkeypatch_result,
                                       "identity": True, "ports": [],
                                       "stack_up": []})
    text = "\n".join(lines)
    assert "No board found" in text
    assert "Hold the BOOTSEL button." in text

