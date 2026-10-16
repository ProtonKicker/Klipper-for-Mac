"""THE contract: `klipperformac serial --json` -> {mcus:[...], single:bool}.

Pinned here (producer, B) and consumed by the GUI/TUI (C). Both sides must agree
on the seven entry keys, the four identity keys, the three conflict codes, and
the two promises that make this safe to call from a panel: read-only mode always
exits 0, and a malformed printer.cfg is a `conflict` string on a row, never a
traceback.

Every test feeds `detect.scan()` / `detect.all_identities()` fixed facts
(tests/climcu/conftest.py): nothing is opened, no ioreg runs, and the bench
SKR Pico is only ever talked about in strings.
"""
import argparse
import json

import pytest

CONTRACT_KEYS = {"mcu", "section", "serial", "configured", "claimed",
                 "identity", "conflict"}
IDENTITY_KEYS = {"vid_pid", "serial", "key", "present"}
CONFLICT_CODES = {None, "duplicate-port", "missing-port", "bad-section-name"}

MASTER = "/dev/cu.usbmodem14101"
TOOL_NODE = "/dev/cu.URT0"


def serial_ns(**kw):
    """The Namespace argparse builds for `serial ...`, so a test can call the
    handler directly instead of going through main()."""
    base = dict(set=None, auto=False, json=False, mcu=None)
    base.update(kw)
    return argparse.Namespace(**base)


def run_json(cstack, capsys, args=None):
    """Call the handler and parse its stdout as the contract. A traceback here
    is a test failure by construction: json.loads would raise."""
    capsys.readouterr()
    cstack.cli.cmd_serial(serial_ns(json=True, **(args or {})))
    return json.loads(capsys.readouterr().out)


def test_the_single_mcu_contract_is_exactly_this(cstack, attached, printer_cfg,
                                                 facts, capsys):
    """The golden row: the bench's own SKR Pico, one section, no conflicts."""
    printer_cfg(facts.CFG_SINGLE)
    attached([MASTER], [facts.identity_row(MASTER)])
    data = run_json(cstack, capsys)
    assert data == {
        "mcus": [{
            "mcu": "mcu",
            "section": "[mcu]",
            "serial": MASTER,
            "configured": True,
            "claimed": True,
            "identity": {"vid_pid": "1d50:614e",
                         "serial": "5044506110A45B1C",
                         "key": "usb:1d50:614e:5044506110A45B1C",
                         "present": True},
            "conflict": None,
        }],
        "single": True,
    }, data


def test_the_multi_mcu_contract_carries_both_sections(cstack, attached,
                                                     printer_cfg, facts,
                                                     capsys):
    """Master attached, tool bound by the identity marker and unplugged: the
    consumer must tell those two states apart from one payload."""
    printer_cfg(facts.CFG_MULTI)
    attached([MASTER], [facts.identity_row(MASTER)])
    data = run_json(cstack, capsys)
    assert data["single"] is False
    assert [e["mcu"] for e in data["mcus"]] == ["mcu", "tool"]
    assert data["mcus"][0]["conflict"] is None
    tool = data["mcus"][1]
    assert tool["section"] == "[mcu tool]"
    assert tool["serial"] is None
    assert tool["configured"] is True and tool["claimed"] is False
    assert tool["conflict"] == "missing-port"
    # the marker is an identity the consumer can show even when the board is
    # not there: parsed out of '# klipperformac:identity', present is a fact.
    assert tool["identity"] == {"vid_pid": "1a86:7523",
                                "serial": "00A1B2C3",
                                "key": facts.TOOL_KEY,
                                "present": False}


def test_every_row_is_the_contract_and_nothing_more(cstack, attached,
                                                   printer_cfg, facts, capsys):
    printer_cfg(facts.CFG_MULTI)
    attached([MASTER], [facts.identity_row(MASTER)])
    data = run_json(cstack, capsys)
    assert isinstance(data["single"], bool)
    assert set(data) == {"mcus", "single"}
    for entry in data["mcus"]:
        assert set(entry) == CONTRACT_KEYS, entry
        if entry["identity"] is not None:
            assert set(entry["identity"]) == IDENTITY_KEYS, entry
        assert entry["conflict"] in CONFLICT_CODES, entry


def test_two_sections_on_one_node_is_duplicate_port(cstack, attached,
                                                   printer_cfg, facts, capsys):
    """The dead-printer case, named in machine-readable form for the GUI."""
    printer_cfg(facts.CFG_SINGLE + "\n[mcu tool]\nserial: " + MASTER + "\n")
    attached([MASTER], [facts.identity_row(MASTER)])
    data = run_json(cstack, capsys)
    assert [e["conflict"] for e in data["mcus"]] == ["duplicate-port",
                                                     "duplicate-port"]


def test_a_configured_node_that_is_not_attached_is_missing_port(
        cstack, attached, printer_cfg, facts, capsys):
    printer_cfg(facts.CFG_SINGLE)
    attached([], [])                      # macOS renamed it, or it is off
    data = run_json(cstack, capsys)
    entry = data["mcus"][0]
    assert entry["conflict"] == "missing-port"
    assert entry["claimed"] is True        # the config still holds the node
    assert entry["identity"] is None


@pytest.mark.parametrize("broken", [
    "[mcu\nserial: /dev/cu.usbmodem14101\n",                  # no closing ]
    "[MCU]\nserial: /dev/x\n",                                # not a mcu section
    "[mcu  tool]\nserial: /dev/x\n",                          # double space
    "[mcu tool]\nserial: /dev/a\nserial: /dev/b\n",           # repeated option
    "[printer]\nspeed: 50\n",                                 # no MCU at all
    "[mcu]\nserial:\n",                                       # empty value
    "",                                                       # empty file
    "\x00 garbage \xff\n[mcu tool]\n",                        # not config
    "[mcu]\nbaud: not-a-number\nserial: /dev/x\n[mcu]\n",     # dup header
])
def test_a_malformed_config_answers_the_contract_not_a_traceback(
        cstack, attached, printer_cfg, facts, capsys, broken):
    """The exit-0/never-raise promise, against the shapes a human can actually
    produce in an editor at 2 am."""
    printer_cfg(broken)
    attached([MASTER], [facts.identity_row(MASTER)])
    data = run_json(cstack, capsys)
    assert isinstance(data["mcus"], list)
    assert isinstance(data["single"], bool)
    for entry in data["mcus"]:
        assert set(entry) == CONTRACT_KEYS
        assert entry["conflict"] in CONFLICT_CODES


def test_read_only_json_exits_zero_from_the_real_argparse(
        cstack, attached, printer_cfg, facts, capsys):
    """`main(["serial", "--json"])` must not raise SystemExit at all: that is
    the contract's other half, what lets a panel call it on a broken config."""
    printer_cfg(facts.CFG_MULTI)
    attached([MASTER], [facts.identity_row(MASTER)])
    assert cstack.cli.main(["serial", "--json"]) is None


def test_no_printer_cfg_is_an_empty_contract(cstack, attached, capsys):
    attached([MASTER], [])
    data = run_json(cstack, capsys)
    assert data == {"mcus": [], "single": True}


def test_a_canbus_section_is_configured_and_not_missing(
        cstack, attached, printer_cfg, facts, capsys):
    """CAN has no /dev node on macOS; `--json` must not call it a missing port
    just because the USB scan cannot see a uuid."""
    printer_cfg("[mcu]\nserial: " + MASTER +
                "\n\n[mcu tool]\ncanbus_uuid: 112233445566\n")
    attached([MASTER], [facts.identity_row(MASTER)])
    data = run_json(cstack, capsys)
    tool = data["mcus"][1]
    assert tool["conflict"] is None
    assert tool["claimed"] is False
    assert tool["serial"] == "112233445566"


def test_the_contract_survives_a_missing_mcus_module(no_mcus, attached,
                                                    printer_cfg, facts, capsys):
    """Wave 3 not landed (or broken): the same seven keys, from the CLI's own
    parse. A consumer must never be able to tell which backend answered."""
    printer_cfg(facts.CFG_MULTI)
    attached([MASTER], [facts.identity_row(MASTER)])
    data = run_json(no_mcus, capsys)
    assert [set(e) for e in data["mcus"]] == [CONTRACT_KEYS, CONTRACT_KEYS]
    assert [e["mcu"] for e in data["mcus"]] == ["mcu", "tool"]
    assert data["mcus"][1]["conflict"] == "missing-port"
    assert data["single"] is False


def test_the_contract_survives_an_mcus_module_that_raises(
        cstack, attached, printer_cfg, facts, capsys, stub_mcus):
    def boom(*args, **kwargs):
        raise RuntimeError("mcus.py is having a day")

    stub_mcus(list_mcus=boom, validate=boom)
    printer_cfg(facts.CFG_MULTI)
    attached([MASTER], [facts.identity_row(MASTER)])
    data = run_json(cstack, capsys)
    assert [e["mcu"] for e in data["mcus"]] == ["mcu", "tool"]
    for entry in data["mcus"]:
        assert entry["conflict"] in CONFLICT_CODES


def test_master_is_listed_first_whatever_the_file_order(
        cstack, attached, printer_cfg, facts, capsys):
    printer_cfg("[mcu tool]\nserial: " + TOOL_NODE +
                "\n\n[mcu]\nserial: " + MASTER + "\n")
    attached([MASTER, TOOL_NODE],
             [facts.identity_row(MASTER),
              facts.identity_row(TOOL_NODE, vid_pid="1A86:7523",
                                 serial="00A1B2C3")])
    data = run_json(cstack, capsys)
    assert [e["mcu"] for e in data["mcus"]] == ["mcu", "tool"]
    assert [e["conflict"] for e in data["mcus"]] == [None, None]
    assert data["mcus"][1]["identity"]["present"] is True
