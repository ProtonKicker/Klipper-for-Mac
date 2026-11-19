"""auto_serial: bind per SECTION, by USB identity when the config says how.

`presets --use --auto-serial`, `presets --use-library` and the GUI's device
picker all land here. Two rules and one hard line: a section bound by
'# klipperformac:identity <key>' gets the board it names (which survives macOS
renumbering the node), anything else keeps the old "exactly one candidate"
guess - and one port is never handed to two sections, because klippy holds a
serial node exclusively and the second board retries for 90 s and then errors.
"""
import pytest


def test_a_marker_resolves_the_board_it_names(cstack, attached, printer_cfg,
                                              facts, capsys):
    """The renumbering case: [mcu tool] is bound to a USB identity, and that
    board is present under a node the config never mentions."""
    printer_cfg(facts.CFG_MULTI)
    attached([facts.MASTER, "/dev/cu.usbmodem99999"],
             [facts.identity_row(facts.MASTER),
              facts.identity_row("/dev/cu.usbmodem99999", vid_pid="1A86:7523",
                                 serial="00A1B2C3")])
    capsys.readouterr()
    lines, ok = cstack.cli.auto_serial(cstack.paths.PRINTER_CFG)
    text = cstack.paths.PRINTER_CFG.read_text()
    assert ok is True, lines
    assert "/dev/cu.usbmodem99999" in text.split("[mcu tool]")[1]
    assert any("matched by identity" in line for line in lines), lines
    assert "/dev/cu.usbmodem14101" in text.split("[mcu tool]")[0]


def test_one_port_is_never_claimed_twice(cstack, attached, printer_cfg, facts):
    """Both sections bound to the SAME identity: the second is told, not
    written - a duplicate serial: pair is a printer that cannot start."""
    printer_cfg("[mcu]\nserial: " + facts.MASTER +
                "\n\n[mcu tool]\n# klipperformac:identity " + facts.MASTER_KEY +
                "\n\n[mcu exp]\n# klipperformac:identity " + facts.MASTER_KEY +
                "\n")
    attached([facts.MASTER], [facts.identity_row(facts.MASTER)])
    lines, ok = cstack.cli.auto_serial(cstack.paths.PRINTER_CFG)
    text = cstack.paths.PRINTER_CFG.read_text()
    assert ok is False
    assert text.count("serial: " + facts.MASTER) == 1, text
    assert sum("already claimed" in line for line in lines) == 2, lines


def test_a_bound_section_is_never_given_the_leftover_cable(
        cstack, attached, printer_cfg, facts):
    """The board the marker names is not attached; the one that IS attached is
    a different board. Guessing here would drive the wrong MCU, so nothing is
    written and the line says exactly that."""
    printer_cfg("[mcu]\nserial: " + facts.MASTER +
                "\n\n[mcu tool]\n# klipperformac:identity " + facts.TOOL_KEY +
                "\n")
    attached([facts.MASTER, facts.TOOL_NODE],
             [facts.identity_row(facts.MASTER)])
    lines, ok = cstack.cli.auto_serial(cstack.paths.PRINTER_CFG)
    text = cstack.paths.PRINTER_CFG.read_text()
    assert ok is False
    assert "serial: " + facts.TOOL_NODE not in text
    assert any("not attached" in line and "nothing was guessed" in line
               for line in lines), lines


def test_without_markers_the_single_candidate_rule_still_writes(
        cstack, attached, printer_cfg, facts):
    """Back to the pre-multi-MCU behaviour for the configs that carry no
    binding: one board, one section, that is the only guess allowed."""
    printer_cfg("[mcu]\n# serial: /dev/serial/by-id/tbd\nbaud: 250000\n")
    attached([facts.TOOL_NODE], [])
    lines, ok = cstack.cli.auto_serial(cstack.paths.PRINTER_CFG)
    assert ok is True
    assert "serial: " + facts.TOOL_NODE in \
        cstack.paths.PRINTER_CFG.read_text()


def test_without_markers_two_candidates_are_still_a_refusal(
        cstack, attached, printer_cfg, facts):
    printer_cfg("[mcu]\nbaud: 250000\n")
    attached([facts.MASTER, facts.TOOL_NODE], [])
    lines, ok = cstack.cli.auto_serial(cstack.paths.PRINTER_CFG)
    assert ok is False
    assert "Multiple USB devices found" in lines[0]
    assert "serial --set PATH" in lines[0]
    assert "serial:" not in cstack.paths.PRINTER_CFG.read_text()


def test_two_sections_one_free_board_writes_the_free_one_only(
        cstack, attached, printer_cfg, facts):
    """[mcu] already has its board; the unbound second section may take the one
    unclaimed cable - and no other section can be given the same node."""
    printer_cfg("[mcu]\nserial: " + facts.MASTER +
                "\n\n[mcu tool]\nbaud: 250000\n")
    attached([facts.MASTER, facts.TOOL_NODE], [])
    lines, ok = cstack.cli.auto_serial(cstack.paths.PRINTER_CFG)
    assert ok is True, lines
    text = cstack.paths.PRINTER_CFG.read_text()
    assert "serial: " + facts.TOOL_NODE in text.split("[mcu tool]")[1]
    assert text.count("serial: " + facts.MASTER) == 1


def test_a_canbus_section_is_left_alone(cstack, attached, printer_cfg, facts):
    """CAN has no /dev node: auto_serial must not 'fix' a uuid with a USB port,
    and a uuid counts as a configured port for picked_ok."""
    printer_cfg("[mcu]\nserial: " + facts.MASTER +
                "\n\n[mcu tool]\ncanbus_uuid: 112233445566\n")
    attached([facts.MASTER, facts.TOOL_NODE], [])
    lines, ok = cstack.cli.auto_serial(cstack.paths.PRINTER_CFG)
    assert ok is True, lines
    text = cstack.paths.PRINTER_CFG.read_text()
    assert "canbus_uuid: 112233445566" in text
    assert facts.TOOL_NODE not in text


def test_every_section_already_set_says_so(cstack, attached, printer_cfg, facts):
    printer_cfg(facts.CFG_MULTI_SET)
    attached([facts.MASTER, facts.TOOL_NODE], [])
    lines, ok = cstack.cli.auto_serial(cstack.paths.PRINTER_CFG)
    assert ok is True
    assert lines == ["Serial port already set in printer.cfg."]


def test_no_board_attached_is_the_old_message(cstack, attached, printer_cfg,
                                              facts):
    printer_cfg(facts.CFG_MULTI)
    attached([], [])
    lines, ok = cstack.cli.auto_serial(cstack.paths.PRINTER_CFG)
    assert ok is False
    assert "No USB printer board detected" in lines[0]


def test_a_section_name_that_cannot_be_written_is_reported_not_written(
        cstack, attached, printer_cfg, facts):
    """'[mcu  a b]' is not a chip name klipper can address: say so, change
    nothing, and still serve the sections that are fine."""
    printer_cfg("[mcu]\nserial: " + facts.MASTER +
                "\n\n[mcu  a b]\n")
    attached([facts.MASTER], [])
    lines, ok = cstack.cli.auto_serial(cstack.paths.PRINTER_CFG)
    assert any("not a section name this tool writes" in line
               for line in lines), lines
    text = cstack.paths.PRINTER_CFG.read_text()
    assert text.count("[mcu  a b]") == 1
    assert "/dev/cu.nope" not in text


@pytest.mark.parametrize("name", ["tool", "EXP1BB", "ebb", "m1_2"])
def test_the_named_write_goes_to_the_right_section(cstack, attached,
                                                  printer_cfg, facts, name):
    """detect.write_serial(cfg, path, name) is the fallback writer and only
    ever touches the section it was named."""
    cfg = "[mcu]\nserial: /dev/cu.keep\n\n[mcu {}]\n\n[extruder]\n"
    printer_cfg(cfg.format(name))
    nodes = attached([], [], existing=[facts.TOOL_NODE])
    assert cstack.detect.write_serial(cstack.paths.PRINTER_CFG,
                                      nodes[facts.TOOL_NODE], name) is True
    text = cstack.paths.PRINTER_CFG.read_text()
    header = "[mcu {}]".format(name)
    assert "serial: /dev/cu.keep" in text.split(header)[0]
    assert nodes[facts.TOOL_NODE] in text.split(header)[1]
    assert "[extruder]" in text.split(header)[1], "the rest of the file stays"


def test_write_serial_default_name_is_byte_identical_to_the_old_behaviour(
        cstack, tmp_path):
    """The back-compat promise: the old call signature, the old output - a
    secondary section is never touched and the master's line keeps its CRLF."""
    cfg = tmp_path / "printer.cfg"
    cfg.write_bytes(b"[mcu]\nserial: /dev/OLD\r\nbaud: 250000\n\n"
                    b"[mcu tool]\nserial: /dev/OTHER\n")
    assert cstack.detect.write_serial(cfg, "/dev/NEW") is True
    text = cfg.read_bytes().decode()
    assert "serial: /dev/NEW\r\n" in text, "CRLF of the line it replaced"
    assert "/dev/OTHER" in text


def test_write_serial_refuses_a_name_it_cannot_header(cstack, tmp_path):
    cfg = tmp_path / "printer.cfg"
    cfg.write_text("[mcu]\nserial: /dev/OLD\n")
    for bad in ("", "a b", "a:b", "a]b", "tool\nserial: /dev/EVIL", "x" * 60):
        assert cfg.read_text() == "[mcu]\nserial: /dev/OLD\n"
        assert cstack.detect.write_serial(cfg, "/dev/NEW", bad) is False, bad
    assert "EVIL" not in cfg.read_text()


def test_write_serial_never_invents_a_section(cstack, tmp_path):
    cfg = tmp_path / "printer.cfg"
    cfg.write_text("[mcu]\nserial: /dev/OLD\n")
    assert cstack.detect.write_serial(cfg, "/dev/NEW", "ghost") is False
    assert "ghost" not in cfg.read_text()


def test_set_serial_text_is_the_shared_kernel(cstack):
    """detect.set_serial_text() is what mcus.set_serial() calls: text in, text
    out, None when the section is not there."""
    text = "[mcu tool]\nserial: /dev/OLD\n"
    assert cstack.detect.set_serial_text(text, "/dev/NEW",
                                        header="[mcu tool]") == \
        "[mcu tool]\nserial: /dev/NEW\n"
    assert cstack.detect.set_serial_text(text, "/dev/NEW",
                                         header="[mcu]") is None
    assert cstack.detect.set_serial_text(text, "/dev/a\nb",
                                         header="[mcu tool]") is None
