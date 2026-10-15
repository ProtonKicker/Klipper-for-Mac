"""apply_lan hostile inputs: only [server] host: and the managed CIDR list
under trusted_clients may change; everything else is byte-preserved."""
import configparser

import pytest


def parse(path):
    cp = configparser.ConfigParser(strict=False)
    cp.read(path)
    return cp


def test_toggle_roundtrip_on_generated_conf(stack, moonraker_generated):
    assert stack.configgen.apply_lan(True) is True
    assert stack.configgen.apply_lan(True) is False
    parse(stack.paths.MOONRAKER_CONF)  # still valid INI
    assert stack.configgen.apply_lan(False) is True
    assert stack.paths.MOONRAKER_CONF.read_text() == moonraker_generated


@pytest.mark.xfail(strict=True, reason="BUG T1: apply_lan rewrites the "
                   "FIRST ^host: line anywhere in the file, even inside an "
                   "unrelated section")
def test_only_server_host_rewritten(stack):
    stack.paths.MOONRAKER_CONF.write_text(
        "[webcam cam0]\nhost: printer-lan.local\n\n"
        "[server]\nhost: 127.0.0.1\nport: 7125\n"
        "klippy_uds_address: /x/klippy.sock\n\n"
        "[authorization]\ntrusted_clients:\n    127.0.0.1\n    ::1\n")
    stack.configgen.apply_lan(True)
    text = stack.paths.MOONRAKER_CONF.read_text()
    assert "printer-lan.local" in text, "unrelated host: was mangled"
    cp = parse(stack.paths.MOONRAKER_CONF)
    assert cp["server"]["host"] == "0.0.0.0"


def test_missing_server_section_tolerated(stack):
    stack.paths.MOONRAKER_CONF.write_text(
        "[authorization]\ntrusted_clients:\n    127.0.0.1\n")
    stack.configgen.apply_lan(True)
    parse(stack.paths.MOONRAKER_CONF)


def test_no_trusted_clients_still_parses(stack):
    stack.paths.MOONRAKER_CONF.write_text("[server]\nhost: 127.0.0.1\n")
    assert stack.configgen.apply_lan(True) is True
    cp = parse(stack.paths.MOONRAKER_CONF)
    assert cp["server"]["host"] == "0.0.0.0"


def test_crlf_file_survives_toggle(stack):
    stack.paths.MOONRAKER_CONF.write_text(
        "[server]\r\nhost: 127.0.0.1\r\n\r\n"
        "[authorization]\r\ntrusted_clients:\r\n    127.0.0.1\r\n")
    stack.configgen.apply_lan(True)
    stack.configgen.apply_lan(False)
    parse(stack.paths.MOONRAKER_CONF)
