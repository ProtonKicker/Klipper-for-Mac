"""mcus.validate(): the section names and port claims klipper would refuse.

Each expectation cites the upstream rule that makes it a failure rather than a
style preference:

  * klippy/mcu.py:1151-1158 - four characters are stripped, so ``[mcu  a]`` is
    the mcu ``' a'``;
  * klippy/mcu.py:1003 + klippy/pins.py:126-131 - that name is registered as the
    pin chip, and ``register_chip`` does ``chip_name.strip()`` then raises
    "Duplicate chip name";
  * klippy/pins.py:71 - ``re_pin`` matches a pin name as ``[^ ]*``, so a name
    with a space inside cannot be addressed;
  * klippy/pins.py:78-83 - a pin prefix splits at the FIRST ':', so a name with
    a colon is unreachable;
  * klippy/mcu.py:1242-1249 + klippy/configfile.py:430-441 - ``[MCU]`` builds no
    MCU and is then rejected as an unknown section;
  * klippy/mcu.py:788 + klippy/configfile.py:31-37 - ``serial`` has no default,
    so a section without it stops klipper at connect.
"""
import pytest

from klipperformac import mcus
from tests.mcus.conftest import HOSTILE, TWO_MCU, WITH_AUTOSAVE


def test_clean_config_has_no_issues(codes):
    """The point of the whole module: a sane multi-MCU config says nothing."""
    assert codes(TWO_MCU) == []
    assert mcus.conflicts_by_mcus(mcus.list_mcus(TWO_MCU)) == \
        {"mcu": None, "tool": None}


def test_canbus_config_has_no_issues(codes):
    """A CAN secondary is complete without a serial line (mcu.py:780-786)."""
    assert codes(WITH_AUTOSAVE) == []


def test_accepts_entries_text_or_path(by_name):
    """validate(mcus) is the documented input; text/path are conveniences."""
    entries = mcus.list_mcus("[mcu  a]\nserial: /dev/x\n")
    from_entries = mcus.validate(entries)
    assert mcus.validate("[mcu  a]\nserial: /dev/x\n") == from_entries
    assert [issue["code"] for issue in from_entries] == \
        ["bad-section-name", "missing-master"]


def test_upstream_name_rules(codes):
    """Every way a header can stop matching the name the user meant."""
    cases = [
        ("[mcu  a]", ["bad-section-name", "missing-master"]),
        ("[mcu ]", ["bad-section-name", "missing-master"]),
        ("[mcu a ]", ["bad-section-name", "missing-master"]),
        ("[mcu a b]", ["bad-section-name", "missing-master"]),
        ("[mcu a:b]", ["bad-section-name", "missing-master"]),
        ("[mcu a#b]", ["bad-section-name", "missing-master"]),
        ("[MCU]", ["bad-section-name", "missing-master"]),
        ("[Mcu tool]", ["bad-section-name", "missing-master"]),
    ]
    for header, expected in cases:
        found = codes(header + "\nserial: /dev/x\n")
        assert found == expected, header


def test_master_needs_no_name_check(codes):
    """'[mcu]' is the master by definition (mcu.py:1246), so it is never bad."""
    assert codes("[mcu]\nserial: /dev/x\n") == []


def test_a_second_master_named_mcu_collides_with_the_master(codes):
    """'[mcu mcu]' would register the chip 'mcu' - the master's own name."""
    found = codes("[mcu]\nserial: /dev/a\n[mcu mcu]\nserial: /dev/b\n")
    assert "bad-section-name" in found
    assert "duplicate-chip-name" in found


def test_duplicate_port_claims(codes):
    """Two sections, one node: klipper holds a serial port exclusively."""
    entries = mcus.list_mcus(
        "[mcu]\nserial: /dev/shared\n[mcu tool]\nserial: /dev/shared\n")
    found = codes(entries)
    assert found.count("duplicate-port") == 2
    conflicts = mcus.conflicts_by_mcus(entries)
    assert conflicts == {"mcu": "duplicate-port", "tool": "duplicate-port"}


def test_same_port_claimed_three_times(codes):
    entries = mcus.list_mcus(
        "[mcu]\nserial: /dev/x\n[mcu a1]\nserial: /dev/x\n[mcu a2]\n"
        "serial: /dev/x\n")
    assert codes(entries).count("duplicate-port") == 3


def test_missing_port_is_reported_per_section(codes):
    """mcu.py:788: config.get('serial') with no default is a required option."""
    entries = mcus.list_mcus("[mcu]\nserial: /dev/a\n[mcu tool]\nbaud: 250000\n")
    found = mcus.validate(entries)
    assert [issue["code"] for issue in found] == ["missing-port"]
    assert found[0]["mcu"] == "tool"
    assert mcus.conflicts_by_mcus(entries) == {"mcu": None, "tool": "missing-port"}


def test_empty_serial_value_counts_as_missing(codes):
    """'serial:' with nothing after it cannot be opened either."""
    entries = mcus.list_mcus("[mcu]\nserial:\n")
    assert entries[0]["serial"] is None
    assert codes(entries) == ["missing-port"]


def test_chip_name_collision_after_the_strip(codes):
    """'[mcu a]' + '[mcu  a]' - klipper dies with "Duplicate chip name 'a'".

    This is the sharpest illustration of why the four-character strip is an
    identity rule and not cosmetics: the two sections look different in the file
    and become the same pin chip in pins.py:126-131.
    """
    entries = mcus.list_mcus(
        "[mcu]\nserial: /dev/master\n[mcu a]\nserial: /dev/one\n"
        "[mcu  a]\nserial: /dev/two\n")
    found = codes(entries)
    assert found.count("duplicate-chip-name") == 2
    assert "bad-section-name" in found           # the mangled one is named first


def test_mcu_count_sanity(codes):
    """Eight is our advisory ceiling; klipper itself has none."""
    text = "[mcu]\nserial: /dev/master\n" + "".join(
        "[mcu b%d]\nserial: /dev/%d\n" % (i, i) for i in range(7))
    assert "too-many-mcus" not in codes(text)
    text = "[mcu]\nserial: /dev/master\n" + "".join(
        "[mcu b%d]\nserial: /dev/%d\n" % (i, i) for i in range(8))
    assert "too-many-mcus" in codes(text)
    assert mcus.MAX_MCU_SECTIONS == 8


def test_missing_master_is_a_file_level_issue(codes):
    """mcu.py:1246 always builds the master, even when only '[mcu tool]' exists."""
    entries = mcus.list_mcus("[mcu tool]\nserial: /dev/x\n")
    found = mcus.validate(entries)
    assert codes(entries) == ["missing-master"]
    # A file-level finding belongs to no section.
    assert found[0]["mcu"] is None and found[0]["section"] is None
    assert entries[0]["is_master"] is False


def test_no_mcu_at_all_is_reported(codes):
    """Empty list + missing-master: a config with no board says so plainly."""
    entries = mcus.list_mcus("[fan]\npin: PA1\n")
    assert entries == []
    assert codes(entries) == ["missing-master"]


def test_duplicate_section_and_duplicate_option_are_said_out_loud(codes):
    """strict=False makes both invisible in klipper (configfile.py:172)."""
    entries = mcus.list_mcus(
        "[mcu]\nserial: /dev/a\nserial: /dev/b\n\n[mcu]\nbaud: 115200\n")
    found = codes(entries)
    assert "duplicate-section" in found
    assert "duplicate-serial-option" in found


def test_issue_shape_is_stable():
    """Every issue carries the same five keys and a message a human can act on."""
    for _label, text in HOSTILE:
        for issue in mcus.validate(text):
            assert set(issue) == set(mcus.ISSUE_KEYS)
            assert issue["code"] in mcus.ISSUE_CODES
            assert isinstance(issue["message"], str)
            assert len(issue["message"]) > 20
            # Every message says who complains (klipper) and why.
            assert "klipper" in issue["message"].lower()
            assert issue["mcu"] is None or isinstance(issue["mcu"], str)
            assert issue["line_no"] is None or isinstance(issue["line_no"], int)


def test_messages_cite_upstream_where_the_rule_lives():
    """A conflict a user cannot act on is worse than no conflict at all.

    The messages name the upstream file so `doctor` output and a bug report point
    at the same four-character strip rather than at a mystery.
    """
    joined = " ".join(issue["message"]
                      for issue in mcus.validate("[mcu  a]\nserial: /dev/x\n"))
    assert "klippy/mcu.py" in joined
    collisions = " ".join(issue["message"] for issue in mcus.validate(
        "[mcu a]\nserial: /dev/x\n[mcu  a]\nserial: /dev/y\n"))
    assert "Duplicate chip name" in collisions


def test_conflicts_never_invent_a_code(codes):
    """The JSON contract's conflict field only ever holds a contract value."""
    for _label, text in HOSTILE:
        for conflict in mcus.conflicts_by_mcus(mcus.list_mcus(text)).values():
            assert conflict is None or conflict in mcus.CONFLICT_CODES


def test_name_conflict_outranks_the_missing_port_it_causes(codes):
    """'[mcu ]' with no serial reports the cause (the name), not the symptom."""
    entries = mcus.list_mcus("[mcu ]\n")
    assert codes(entries) == ["bad-section-name", "missing-port",
                              "missing-master"]
    assert mcus.conflicts_by_mcus(entries)[""] == "bad-section-name"


def test_issues_for_one_name():
    """The per-section view the CLI needs when it prints one board's row."""
    entries = mcus.list_mcus("[mcu]\nserial: /dev/x\n[mcu tool]\nserial: /dev/x\n")
    found = mcus.issues_for(entries, "tool")
    assert [issue["code"] for issue in found] == ["duplicate-port"]
    assert mcus.issues_for(entries, "mcu") != found


def test_validate_on_a_non_list_non_config_is_a_programming_error():
    with pytest.raises(TypeError):
        mcus.validate(7)
