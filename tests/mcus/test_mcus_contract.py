"""The shared JSON contract, pinned on the reader side.

B (``serial --json``) produces the payload, C (the SwiftUI picker / TUI) consumes
it; this file pins what mcus.py guarantees to that layer, so a rename here breaks
a test instead of silently shipping a JSON shape the GUI cannot decode::

    {"mcus": [{"mcu": "mcu"|"tool", "section": "[mcu tool]",
               "serial": "/dev/cu.usbmodem14101" | null,
               "configured": true, "claimed": true,
               "identity": {...} | null,
               "conflict": null | "duplicate-port" | "missing-port"
                         | "bad-section-name"}],
     "single": bool}

Exit 0 always in read-only mode, and a malformed printer.cfg shows up as a
`conflict` string on an entry - never as a traceback. The live facts
(`identity`, `claimed`, presence) come from detect/Moonraker and belong to the
CLI layer; what this module owes the contract is `mcu`, `section`, `serial` and
the conflict vocabulary.
"""
import json

import pytest

from klipperformac import mcus
from tests.mcus.conftest import CRLF_CFG, HOSTILE, TWO_MCU

# The contract's per-entry keys, restated here (not imported from cli.py) so this
# suite says what the GUI was promised rather than what one implementation does.
CONTRACT_ENTRY_KEYS = {"mcu", "section", "serial", "configured", "claimed",
                       "identity", "conflict"}
CONTRACT_TOP_KEYS = {"mcus", "single"}
CONTRACT_CONFLICTS = {None, "duplicate-port", "missing-port",
                      "bad-section-name"}


def project(cfg, identities=None):
    """The CLI's projection, rebuilt here from mcus entries only.

    ``identities`` stands in for detect.scan()/identity() (the live half of the
    contract), keyed by device node, so the whole payload can be assembled and
    decoded offline.
    """
    entries = mcus.list_mcus(cfg)
    conflicts = mcus.conflicts_by_mcus(entries)
    idents = identities or {}
    mcus_rows = []
    for entry in entries:
        serial = entry["serial"]
        identity = idents.get(serial)
        mcus_rows.append({
            "mcu": entry["name"],
            "section": entry["section_header"],
            "serial": serial,
            "configured": True,
            "claimed": serial is not None,
            "identity": identity,
            "conflict": conflicts.get(entry["name"]),
        })
    return {"mcus": mcus_rows, "single": len(mcus_rows) <= 1}


def test_conflict_vocabulary_is_exactly_the_contract():
    """Three codes, these spellings, nothing else may reach `conflict`."""
    assert set(mcus.CONFLICT_CODES) == (CONTRACT_CONFLICTS - {None})
    assert len(mcus.CONFLICT_CODES) == 3
    # validate() may know more than the contract carries; the extras stay in
    # prose (doctor notes) and can never leak into the JSON field.
    assert set(mcus.CONFLICT_CODES) <= set(mcus.ISSUE_CODES)


def test_entry_keys_are_a_stable_superset():
    """The CLI reads four keys by name; adding a fifth is allowed, renaming is not."""
    for entry in mcus.list_mcus(TWO_MCU):
        assert set(mcus.MCU_ENTRY_KEYS) <= set(entry)
        assert entry["name"] and entry["section_header"].startswith("[")
        assert isinstance(entry["line_no"], int)


def test_projection_matches_the_contract_for_a_two_mcu_printer():
    payload = project(TWO_MCU, identities={
        "/dev/cu.usbmodem14101": {"vid_pid": "1d50:614e",
                                  "serial": "5044506110A45B1C",
                                  "key": "usb:1d50:614e:5044506110A45B1C",
                                  "present": True}})
    assert set(payload) == CONTRACT_TOP_KEYS
    assert payload["single"] is False
    assert len(payload["mcus"]) == 2
    for row in payload["mcus"]:
        assert set(row) == CONTRACT_ENTRY_KEYS
        assert row["conflict"] in CONTRACT_CONFLICTS
    master, tool = payload["mcus"]
    assert (master["mcu"], master["section"]) == ("mcu", "[mcu]")
    assert master["identity"]["key"] == "usb:1d50:614e:5044506110A45B1C"
    assert master["claimed"] is True
    # The toolboard has no identity in this fixture: null, never a guess.
    assert (tool["mcu"], tool["section"]) == ("tool", "[mcu tool]")
    assert tool["identity"] is None
    assert json.loads(json.dumps(payload)) == payload


def test_single_is_true_for_one_mcu_and_for_none():
    assert project("[mcu]\nserial: /dev/a\n")["single"] is True
    assert project("[fan]\npin: PA1\n")["mcus"] == []
    # An empty list is not a multi-MCU config either; the GUI must not branch on
    # an empty list (the CLI's own contract note).
    assert project("[fan]\npin: PA1\n")["single"] is True


def test_malformed_configs_never_raise_out_of_the_projection(hostile):
    """Every hostile shape must produce a decodable payload, exit-0 style."""
    for label, text in hostile:
        payload = project(text)
        dumped = json.dumps(payload)
        assert json.loads(dumped) == payload, label
        for row in payload["mcus"]:
            assert row["conflict"] in CONTRACT_CONFLICTS, label


def test_hostile_shapes_carry_the_conflict_they_deserve():
    """Not just "a" conflict: the one that names the actual upstream failure."""
    cases = {
        "two-space name": "bad-section-name",
        "empty suffix": "bad-section-name",
        "uppercase": "bad-section-name",
        "mixed case suffix": "bad-section-name",
        "two words": "bad-section-name",
        "unterminated header": "bad-section-name",
        "duplicate port": "duplicate-port",
        "mcu only bracket": "missing-port",
    }
    for label, expected in cases.items():
        text = dict(HOSTILE)[label]
        conflicts = [row["conflict"] for row in project(text)["mcus"]]
        assert expected in conflicts, "%s -> %r" % (label, conflicts)


def test_missing_port_and_duplicate_port_are_distinct():
    """One is "nothing to connect", the other is "two boards, one cable"."""
    missing = project("[mcu]\nserial: /dev/a\n[mcu tool]\n")["mcus"]
    assert [row["conflict"] for row in missing] == [None, "missing-port"]
    shared = project("[mcu]\nserial: /dev/x\n[mcu tool]\nserial: /dev/x\n")
    assert [row["conflict"] for row in shared["mcus"]] == \
        ["duplicate-port", "duplicate-port"]


def test_a_canbus_section_is_configured_and_claims_no_node():
    """A CAN MCU is complete (`configured`) and holds no /dev/cu.* (`claimed`),
    so it must never be reported as missing a port."""
    payload = project("[mcu]\nserial: /dev/a\n[mcu rpi]\ncanbus_uuid: beef\n")
    rpi = [row for row in payload["mcus"] if row["mcu"] == "rpi"][0]
    assert rpi["configured"] is True
    assert rpi["claimed"] is False
    assert rpi["serial"] is None
    assert rpi["conflict"] is None


def test_section_header_is_the_klipper_object_spelling():
    """klipper names the object by the section (klippy/mcu.py:1247-1249), and the
    GUI shows `section` to a user who will type that text into printer.cfg."""
    rows = project(CRLF_CFG)["mcus"]
    assert [row["section"] for row in rows] == ["[mcu]", "[mcu tool]"]


def test_names_are_klipper_names_not_pretty_printed():
    """'[mcu  a]' must arrive as ' a', because that is the pin chip klipper
    registered; hiding the space would hide the bug the conflict names."""
    rows = project("[mcu  a]\nserial: /dev/x\n")["mcus"]
    assert rows[0]["mcu"] == " a"
    assert rows[0]["conflict"] == "bad-section-name"


def test_json_types_stay_decodable_by_swift():
    """str/int/bool/None only: a set or a Path in an entry would break the
    decoder on the other side of the contract."""
    allowed = (str, int, float, bool, type(None))
    for text in (TWO_MCU, CRLF_CFG, WITH_AUTOSAVE_TYPES):
        for entry in mcus.list_mcus(text):
            for key, value in entry.items():
                assert isinstance(value, allowed) or value is None, key


def test_conflicts_by_mcus_covers_every_entry_name():
    """A missing key would make the CLI print a null conflict for a broken
    section, which reads as "fine"."""
    for _label, text in HOSTILE:
        entries = mcus.list_mcus(text)
        conflicts = mcus.conflicts_by_mcus(entries)
        assert set(conflicts) == {entry["name"] for entry in entries}


def test_conflicts_of_a_clean_config_are_all_none():
    assert set(mcus.conflicts_by_mcus(mcus.list_mcus(TWO_MCU)).values()) == {None}


def test_the_module_never_reaches_for_the_live_stack():
    """Read-only by construction: this suite has no fixtures for a port, and
    nothing in mcus imports process/cam/installer."""
    for name in ("process", "cam", "installer", "cli"):
        assert not hasattr(mcus, name)
    assert "detect" in dir(mcus)          # the shared text kernel only
    with pytest.raises(AttributeError):
        mcus.no_such_function(TWO_MCU)


# A third sample with every section shape at once, for the type check above.
WITH_AUTOSAVE_TYPES = (
    "[mcu]\nserial: /dev/cu.usbmodem14101\nbaud: 250000\n"
    "[mcu tool]\nserial: /dev/cu.usbmodem14103   # toolboard\n"
    "[mcu rpi]\ncanbus_uuid: 112233445566\n"
    "#*# <---------------------- SAVE_CONFIG ---------------------->\n"
    "#*# [extruder]\n#*# control = PID\n"
)
