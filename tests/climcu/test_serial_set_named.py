"""`serial --set PATH [MCUNAME]`: one section per write, and never a lie.

The name defaults to the timing master (`mcu`), an unknown name is refused with
the list of what IS there, and the exit-1-on-write-failure truthfulness the
command has always had survives the multi-MCU rework: an exit 0 that wrote
nothing is a lie CI, scripts and the GUI all believe (AGENTS.md 4).
"""
import json

import pytest


def test_set_names_the_default_master_and_leaves_the_secondary(
        cstack, attached, printer_cfg, facts, capsys):
    nodes = attached([facts.MASTER], [], existing=[facts.MASTER])
    printer_cfg(facts.CFG_MULTI)
    capsys.readouterr()
    cstack.cli.cmd_serial(facts.serial_ns(set=nodes[facts.MASTER]))
    out = capsys.readouterr().out
    text = cstack.paths.PRINTER_CFG.read_text()
    assert "Wrote serial" in out and "for [mcu]" in out
    assert "serial: {}".format(nodes[facts.MASTER]) in text
    assert "[mcu tool]" in text
    assert "baud: 250000" in text, "every other byte of the file is untouched"


def test_set_writes_the_named_secondary_only(cstack, attached, printer_cfg,
                                            facts, capsys):
    """[mcu] keeps its port: the whole point of naming a section."""
    nodes = attached([facts.MASTER, facts.TOOL_NODE], [],
                     existing=[facts.MASTER, facts.TOOL_NODE])
    printer_cfg(facts.CFG_MULTI)
    capsys.readouterr()
    cstack.cli.cmd_serial(facts.serial_ns(set=nodes[facts.TOOL_NODE],
                                          mcu="tool"))
    text = cstack.paths.PRINTER_CFG.read_text()
    master_part, tool_part = text.split("[mcu tool]")
    assert "serial: {}".format(nodes[facts.TOOL_NODE]) in tool_part
    assert "serial: " + facts.MASTER in master_part


def test_unknown_name_is_refused_with_the_available_list(
        cstack, attached, printer_cfg, facts, capsys):
    nodes = attached([], [], existing=[facts.TOOL_NODE])
    printer_cfg(facts.CFG_MULTI)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        cstack.cli.cmd_serial(facts.serial_ns(set=nodes[facts.TOOL_NODE],
                                              mcu="nosuch"))
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "nosuch" in err and "[mcu tool]" in err
    assert cstack.paths.PRINTER_CFG.read_text() == facts.CFG_MULTI, \
        "a refusal writes nothing"


def test_a_port_already_claimed_by_another_section_is_refused(
        cstack, attached, printer_cfg, facts, capsys):
    """Two klippy readers on one node is a printer that cannot start: this is
    the write the tool must refuse even though a human typed it."""
    nodes = attached([], [], existing=[facts.MASTER])
    printer_cfg("[mcu]\nserial: /dev/cu.KEEPME\n\n[mcu tool]\nserial: {}\n"
                .format(nodes[facts.MASTER]))
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        cstack.cli.cmd_serial(facts.serial_ns(set=nodes[facts.MASTER]))
    assert exc.value.code == 1
    assert "already claimed" in capsys.readouterr().err
    assert "/dev/cu.KEEPME" in cstack.paths.PRINTER_CFG.read_text()


def test_a_missing_device_path_exits_one_and_writes_nothing(
        cstack, attached, printer_cfg, facts, capsys):
    attached([], [])
    printer_cfg(facts.CFG_MULTI)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        cstack.cli.cmd_serial(facts.serial_ns(set="/dev/cu.usbmodemNOPE",
                                              mcu="tool"))
    assert exc.value.code == 1
    assert "not a device that exists" in capsys.readouterr().err
    assert cstack.paths.PRINTER_CFG.read_text() == facts.CFG_MULTI


def test_a_header_the_writer_cannot_target_is_exit_one(
        cstack, attached, printer_cfg, facts, capsys):
    """'[mcu  tool]' (two spaces) is a section klipper reads as a different
    chip: naming it 'tool' must fail loudly, not write into a section that is
    not the one the user meant."""
    nodes = attached([], [], existing=[facts.TOOL_NODE])
    printer_cfg("[mcu]\nserial: {}\n\n[mcu  tool]\nserial: {}\n".format(
        facts.TOOL_NODE, "/dev/cu.OTHER"))
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        cstack.cli.cmd_serial(facts.serial_ns(set=nodes[facts.TOOL_NODE],
                                              mcu="tool"))
    assert exc.value.code == 1
    assert "nothing was written" in capsys.readouterr().err


def test_set_still_works_when_mcus_py_is_absent(no_mcus, attached, printer_cfg,
                                               facts, capsys):
    """No wave 3, no problem: detect.write_serial(cfg, path, name) is the
    fallback writer and the answer is still read back from the file."""
    nodes = attached([], [], existing=[facts.TOOL_NODE])
    printer_cfg(facts.CFG_MULTI)
    capsys.readouterr()
    no_mcus.cli.cmd_serial(facts.serial_ns(set=nodes[facts.TOOL_NODE],
                                           mcu="tool"))
    assert "Wrote serial" in capsys.readouterr().out
    assert "serial: {}".format(nodes[facts.TOOL_NODE]) in \
        no_mcus.paths.PRINTER_CFG.read_text().split("[mcu tool]")[1]


def test_the_write_is_routed_through_mcus_set_serial(
        cstack, attached, printer_cfg, facts, capsys, stub_mcus):
    """Proves the seam: when mcus.set_serial exists the CLI asks IT for the new
    text and owns only the atomic file write."""
    seen = {}

    def set_serial(cfg_text, mcu_name, device):
        seen["args"] = (mcu_name, device)
        return cfg_text.replace("serial: " + facts.MASTER,
                                "serial: {}".format(device))

    stub_mcus(set_serial=set_serial)
    nodes = attached([], [], existing=[facts.MASTER])
    printer_cfg(facts.CFG_SINGLE)
    capsys.readouterr()
    cstack.cli.cmd_serial(facts.serial_ns(set=nodes[facts.MASTER]))
    assert seen["args"][0] == "mcu"
    assert seen["args"][1] == nodes[facts.MASTER]
    assert "serial: {}".format(nodes[facts.MASTER]) in \
        cstack.paths.PRINTER_CFG.read_text()


def test_a_refusal_from_mcus_set_serial_is_relayed_not_swallowed(
        cstack, attached, printer_cfg, facts, capsys, stub_mcus):
    def refuse(cfg_text, mcu_name, device):
        raise RuntimeError("Refusing to write a port into [mcu tool] - "
                           "klipper reads a different chip name")

    stub_mcus(set_serial=refuse)
    nodes = attached([], [], existing=[facts.TOOL_NODE])
    printer_cfg(facts.CFG_MULTI)
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        cstack.cli.cmd_serial(facts.serial_ns(set=nodes[facts.TOOL_NODE],
                                              mcu="tool"))
    assert exc.value.code == 1
    assert "different chip name" in capsys.readouterr().err
    assert cstack.paths.PRINTER_CFG.read_text() == facts.CFG_MULTI


def test_json_mode_after_a_write_is_the_contract_alone(cstack, attached,
                                                      printer_cfg, facts,
                                                      capsys):
    """stdout stays parseable: the human line moves out, the payload is in."""
    nodes = attached([facts.MASTER], [facts.identity_row(facts.MASTER)],
                     existing=[facts.MASTER])
    printer_cfg(facts.CFG_MULTI)
    capsys.readouterr()
    cstack.cli.cmd_serial(facts.serial_ns(set=nodes[facts.MASTER], json=True))
    data = json.loads(capsys.readouterr().out)
    assert data["mcus"][0]["serial"] == nodes[facts.MASTER]
    assert data["single"] is False
