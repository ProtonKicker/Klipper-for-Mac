"""detect.write_serial: only [mcu] may change, hostile paths must not inject."""
import configparser

import pytest

pytestmark = pytest.mark.usefixtures("stack")


def parse(path):
    cp = configparser.ConfigParser(strict=False)
    cp.read(path)
    return cp


def test_replaces_serial_in_mcu_only(stack, tmp_path):
    cfg = tmp_path / "printer.cfg"
    cfg.write_text(
        "[mcu]\nserial: /dev/OLD\nbaud: 250000\n\n"
        "[mcu extra]\nserial: /dev/OTHER\n")
    assert stack.detect.write_serial(cfg, "/dev/NEW") is True
    cp = parse(cfg)
    assert cp["mcu"]["serial"] == "/dev/NEW"
    assert cp["mcu extra"]["serial"] == "/dev/OTHER"


def test_adds_serial_when_mcu_lacks_it(stack, tmp_path):
    cfg = tmp_path / "printer.cfg"
    cfg.write_text("[mcu]\nbaud: 250000\n")
    assert stack.detect.write_serial(cfg, "/dev/NEW") is True
    assert parse(cfg)["mcu"]["serial"] == "/dev/NEW"


def test_refuses_without_mcu(stack, tmp_path):
    cfg = tmp_path / "printer.cfg"
    cfg.write_text("[printer]\nspeed: 50\n")
    assert stack.detect.write_serial(cfg, "/dev/NEW") is False
    assert cfg.read_text() == "[printer]\nspeed: 50\n"


def test_missing_file(stack, tmp_path):
    assert stack.detect.write_serial(tmp_path / "nope.cfg", "/dev/x") is False


def test_mcu_last_line_without_trailing_newline(stack, tmp_path):
    cfg = tmp_path / "printer.cfg"
    cfg.write_text("[fan]\npin: P1\n\n[mcu]")
    assert stack.detect.write_serial(cfg, "/dev/NEW") is True
    cp = parse(cfg)
    assert cp["mcu"]["serial"] == "/dev/NEW"


def test_multiline_device_rejected(stack, tmp_path):
    cfg = tmp_path / "printer.cfg"
    cfg.write_text("[mcu]\nserial: /dev/OLD\n")
    evil = "/dev/x\n[mcu]\nserial: /dev/EVIL"
    try:
        stack.detect.write_serial(cfg, evil)
    except ValueError:
        return
    assert cfg.read_text().count("[mcu]") == 1
    assert "EVIL" not in cfg.read_text()
