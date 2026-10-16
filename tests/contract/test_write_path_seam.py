"""The write-path seam: which per-section `serial --set` the CLI refuses, and
what the GUI is therefore not allowed to offer.

The contract's read side is pinned elsewhere in this directory. The write side is
where a wave-boundary bug actually damages a printer, because the GUI, the TUI and
a human in a terminal all reach the same section through `serial --set PATH NAME`
and each used to have its own idea of which names are writable:

  * mcus.py refuses a header klipper would strip into a different pin chip;
  * cli.py refuses a name `detect.mcu_header()` cannot spell back verbatim, a
    section that connects over CAN, and a port another section is holding;
  * Swift's `McuRow.isAddressable` decides which rows get a port picker at all.

Three guards, one rule. The runtime proof of the Swift half is a standalone
harness this suite deliberately does not shell out to (a pytest that compiles
Swift is a 5 s toolchain dependency, not a unit test):

    xcrun swiftc -o /tmp/mcucheck /tmp/main.swift \
        KlipperMac/Sources/KlipperMac/Core/McuSerial.swift
    /tmp/mcucheck tests/contract/serial_json.golden

which prints the golden's bytes, decodes them, and lists each row's addressability
next to the conflict badge the producer sent.
"""
import json
import re

import pytest

# A master attached with a real port, then one section of each kind that has to
# be answered differently: a USB board with no port written yet, a CAN board, and
# a header klipper strips into a name no pin can address.
SEAM_CFG = """[mcu]
serial: /dev/cu.usbmodem14101
baud: 250000

[mcu tool]
baud: 250000

[mcu rpi]
canbus_uuid: 112233445566

[mcu  board]
serial: /dev/cu.usbmodem14198
"""


def rows(payload_out):
    """The `mcus` list of a captured --json payload, keyed by name."""
    return {row["mcu"]: row for row in json.loads(payload_out)["mcus"]}


# ------------------------------------------------------------- CAN sections --
@pytest.mark.parametrize("use_mcus", [True, False], ids=["mcus.py", "degraded"])
def test_a_can_section_is_never_given_a_serial_line(run_serial_set,
                                                   device_node,
                                                   printer_cfg_text, use_mcus):
    """klippy/mcu.py:780-791 - canbus_uuid WINS and serial: is never read.

    Writing one anyway is the exact thing AGENTS.md rule 2 forbids: a config
    change that silently means nothing while looking like a fix. Both backends
    refuse, because the degrade path is what an un-restarted install runs.
    """
    node = device_node("cu.can-target")
    result = run_serial_set(node, mcu="rpi", text=SEAM_CFG)
    assert result["exit"] == 1, result
    assert "CAN" in result["err"] and "canbus_uuid" in result["err"], result
    assert printer_cfg_text() == SEAM_CFG, "a refusal changed the file"


def test_a_can_section_keeps_its_row_and_its_clean_verdict(run_serial_json):
    """Refusing the write must not turn the row into an error: a CAN MCU is
    configured, claims no node, and has no conflict."""
    out, _ = run_serial_json(text=SEAM_CFG)
    assert rows(out)["rpi"] == {"mcu": "rpi", "section": "[mcu rpi]",
                                "serial": "112233445566", "configured": True,
                                "claimed": False, "identity": None,
                                "conflict": None}


# ---------------------------------------------------- names klipper mangles --
@pytest.mark.parametrize("mcu_arg,expect", [
    # An empty MCUNAME means the section klipper names '' - it must never be
    # read as "no argument given", because the default is the timing master.
    ("", "Invalid MCU name"),
    # '[mcu  board]' registers the chip ' board' (mcu.py:1151-1158 strips four
    # characters). Typing the trimmed half is not a section that exists.
    ("board", "No [mcu board] section"),
    (" board", "No [mcu board] section"),
], ids=["empty name", "trimmed guess", "klipper's own name"])
def test_a_name_that_is_not_a_section_this_tool_writes_never_moves_the_master(
        run_serial_set, device_node, printer_cfg_text, mcu_arg, expect):
    """Each of these, if it defaulted, would rewrite the MASTER's port.

    The master is the timing source: pointing it at a toolboard's cable is how a
    heater ends up driven by a board it has no wires to, so "no argument" and "an
    argument that cannot be honoured" are deliberately different paths.
    """
    node = device_node("cu.refuse-target")
    result = run_serial_set(node, mcu=mcu_arg, text=SEAM_CFG)
    assert result["exit"] == 1, result
    assert expect in result["err"], result["err"]
    assert node not in printer_cfg_text(), "a refused write reached the file"


def test_the_bad_header_is_a_conflict_the_row_shows_and_the_button_lacks(
        run_serial_json, swift_contract_source):
    """`bad-section-name` is the row's badge; Swift's isAddressable is the button.

    The Swift half is pinned by shape here: all three conditions of the guard
    must still be in the body, because each mirrors a refusal the CLI makes for
    real, and a picker that offers a write the CLI will reject is a dead end with
    an error toast instead of a fix.
    """
    out, _ = run_serial_json(text=SEAM_CFG)
    board = rows(out)[" board"]
    assert board["conflict"] == "bad-section-name", board
    body = re.search(r"var isAddressable: Bool \{(.*?)\n    \}",
                     swift_contract_source, re.S)
    assert body, "McuRow.isAddressable moved or became an expression body"
    guard = body.group(1)
    assert "isSafeArgument" in guard, guard          # empty / control chars
    assert "trimmingCharacters" in guard, guard      # ' board' is not 'board'
    assert "badSectionName" in guard, guard          # the producer's verdict


# --------------------------------------------------------------- real writes --
def test_a_named_section_write_lands_in_that_section_and_nowhere_else(
        run_serial_set, run_serial_json, device_node, printer_cfg_text):
    """The positive case, through mcus.set_serial and back out of the file.

    `serial --set PATH tool` adds the line under [mcu tool] and leaves the
    master's port, the baud lines, the CAN section and the bad header exactly as
    they were: that byte-preservation is the whole point of the per-section
    surface, and it is verified by re-reading the file, not by the return value.
    """
    node = device_node("cu.toolboard")
    result = run_serial_set(node, mcu="tool", text=SEAM_CFG)
    assert result["exit"] is None, result
    written = printer_cfg_text()
    assert written != SEAM_CFG
    tool_block = written.split("[mcu tool]")[1].split("[mcu rpi]")[0]
    assert "serial: " + node in tool_block, tool_block
    assert written.split("[mcu tool]")[0] == SEAM_CFG.split("[mcu tool]")[0], \
        "everything before the target section moved"
    out, err = run_serial_json()
    data = rows(out)
    assert data["tool"]["serial"] == node, data["tool"]
    assert data["tool"]["claimed"] is True, data["tool"]
    # ... and still 'missing-port', correctly: `claimed` is what printer.cfg
    # holds, `missing-port` is what the bus shows, and the faked machine here
    # has only ever attached the master and the 1a86 board. A GUI that conflated
    # the two would tell the user the write failed when it succeeded.
    assert data["tool"]["conflict"] == "missing-port", data["tool"]
    assert data["mcu"]["serial"] == "/dev/cu.usbmodem14101", data["mcu"]
    assert err == "", err


def test_two_sections_on_one_node_are_refused_not_overwritten(
        run_serial_set, device_node):
    """klippy holds a serial port exclusively: the second board never connects.

    This is the failure a user gets by copy-pasting [mcu] to add a toolboard, and
    the CLI is the one place both the GUI and the terminal can be stopped.
    """
    node = device_node("cu.shared")
    assert run_serial_set(node, mcu="tool", text=SEAM_CFG)["exit"] is None
    result = run_serial_set(node, mcu="mcu", text=None)
    assert result["exit"] == 1, result
    assert "already claimed" in result["err"], result["err"]


@pytest.mark.parametrize("use_mcus", [True, False], ids=["mcus.py", "degraded"])
def test_the_json_after_a_write_is_the_contract_not_a_second_format(
        run_serial_set, device_node, use_mcus):
    """--json keeps its promise even on the write path (the GUI re-reads after a
    picker choice): one document, seven keys, nothing else on stdout."""
    node = device_node("cu.json-target")
    result = run_serial_set(node, mcu="tool", text=SEAM_CFG, as_json=True,
                            use_mcus=use_mcus)
    assert result["exit"] is None, result
    data = json.loads(result["out"])
    assert set(data) == {"mcus", "single"}
    for row in data["mcus"]:
        assert set(row) == {"mcu", "section", "serial", "configured", "claimed",
                            "identity", "conflict"}, row
    assert data["single"] is False
    assert rows(result["out"])["tool"]["serial"] == node
