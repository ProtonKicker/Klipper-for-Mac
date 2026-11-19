"""`serial` for humans: the port rows keep their shape, the sections get a table.

The port listing is older than multi-MCU and is parsed by scripts and by the
SwiftUI picker: one port per line, the path FIRST, then the driver hint, then
VID:PID/SER/LOC and the `-> [mcu]` claim. The MCU table is additive - every line
of it starts with spaces, so nothing that reads the old shape can be handed a
new kind of line.
"""


def test_the_port_rows_are_unchanged_and_the_table_is_additive(
        cstack, attached, printer_cfg, facts, capsys):
    printer_cfg(facts.CFG_MULTI_SET)
    attached([facts.MASTER, facts.TOOL_NODE],
             [facts.identity_row(facts.MASTER),
              facts.identity_row(facts.TOOL_NODE, vid_pid="1A86:7523",
                                 serial="00A1B2C3")])
    cstack.cli.cmd_serial(facts.serial_ns())
    lines = capsys.readouterr().out.splitlines()
    masters = [row for row in lines if row.startswith(facts.MASTER)]
    assert len(masters) == 1, "one port, one row, path first"
    assert "VID:PID=1d50:614e" in masters[0] and "-> [mcu]" in masters[0]
    tool_rows = [row for row in lines if row.startswith(facts.TOOL_NODE)]
    assert len(tool_rows) == 1 and "-> [mcu tool]" in tool_rows[0]
    table = [row for row in lines if row.startswith("  [mcu")]
    assert len(table) == 2, lines
    assert "attached" in table[0]
    assert "[mcu tool]" in table[1] and "attached" in table[1]


def row_for(out, header):
    """The table line for one section header ('  [mcu tool]  ...'), or "".

    The table indents two spaces and pads the header to a column, so a line
    belongs to `header` when it starts with it and the next character is a
    space - '[mcu tool]' can never be mistaken for '[mcu]'.
    """
    prefix = "  " + header
    for line in out.splitlines():
        if line.startswith(prefix) and \
                line[len(prefix):len(prefix) + 1] in ("", " "):
            return line
    return ""


def test_the_table_names_the_verdict_a_section_deserves(
        cstack, attached, printer_cfg, facts, capsys):
    """Four states a user can act on: attached, gone, never set, shared."""
    printer_cfg("[mcu]\nserial: /dev/cu.gone\n\n"
                "[mcu tool]\nserial: " + facts.MASTER + "\n\n"
                "[mcu exp]\nserial: " + facts.MASTER + "\n\n"
                "[mcu idle]\n")
    attached([facts.MASTER], [facts.identity_row(facts.MASTER)])
    cstack.cli.cmd_serial(facts.serial_ns())
    out = capsys.readouterr().out
    assert "port not attached" in row_for(out, "[mcu]")
    assert "2 sections on one port" in row_for(out, "[mcu tool]")
    assert "2 sections on one port" in row_for(out, "[mcu exp]")
    assert "no serial set" in row_for(out, "[mcu idle]")
    assert "bad section name" not in out


def test_the_table_is_there_without_any_board(no_mcus, attached, printer_cfg,
                                             facts, capsys):
    """The section table comes from printer.cfg, not from IOKit: unplugged is
    exactly when a user needs to see which section wants which port."""
    printer_cfg(facts.CFG_MULTI)
    attached([], [])
    no_mcus.cli.cmd_serial(facts.serial_ns())
    out = capsys.readouterr().out
    assert "No USB serial devices found" in out
    assert "MCU sections in" in out
    assert "[mcu tool]" in out and "no serial set" in out


def test_no_sections_no_table_and_the_old_text_shape(cstack, attached,
                                                    printer_cfg, facts, capsys):
    """A config with no [mcu] at all prints what it always printed."""
    printer_cfg("[printer]\nspeed: 50\n")
    attached([], [])
    cstack.cli.cmd_serial(facts.serial_ns())
    out = capsys.readouterr().out
    assert "MCU sections in" not in out
    assert "No USB serial devices found" in out


def test_the_json_hint_is_offered_once(cstack, attached, printer_cfg, facts,
                                       capsys):
    printer_cfg(facts.CFG_SINGLE)
    attached([facts.MASTER], [facts.identity_row(facts.MASTER)])
    cstack.cli.cmd_serial(facts.serial_ns())
    out = capsys.readouterr().out
    assert out.count("klipperformac serial --json") == 1
