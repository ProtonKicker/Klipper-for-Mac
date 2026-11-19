"""Fixtures for the multi-MCU printer.cfg reader/writer (klipperformac/mcus.py).

All offline and side-effect free: mcus.py is pure text in / text out (it only
touches a file when a test hands it a path inside tmp_path), so nothing here
shells out, opens a port, resets the attached bench board or reads ~/.klipperformac.

The config texts are deliberately the shapes that bite on real printer.cfgs:
vendor files with CRLF, comments after headers, indented pin lists, a
SAVE_CONFIG block at the bottom, and the hostile section names a human types
when the pin identity is already wrong.
"""
import pytest

from klipperformac import mcus

# ----------------------------------------------------------------- sample cfgs --
#: A normal two-MCU printer.cfg: the timing master plus a toolboard, with the
#: comments and indentation a vendor file actually has.
TWO_MCU = (
    "# Klipper for Mac - bench printer\n"
    "[mcu]\n"
    "serial: /dev/cu.usbmodem14101   # SKR Pico, left port\n"
    "baud: 250000\n"
    "\n"
    "[stepper_x]\n"
    "step_pin: PA4\n"
    "dir_pin: PA3\n"
    "\n"
    "[mcu tool]\n"
    "serial: /dev/cu.usbmodem14103\n"
    "\n"
    "[extruder]\n"
    "heater_pin: tool:PB3\n"
)

#: One master, one secondary, one CAN secondary and a SAVE_CONFIG block below
#: the marker klipper generates (klippy/configfile.py:233-237).
WITH_AUTOSAVE = (
    "[mcu]\n"
    "serial: /dev/cu.usbmodem14101\n"
    "\n"
    "[mcu tool]\n"
    "serial: /dev/cu.usbmodem14103\n"
    "\n"
    "[mcu rpi]\n"
    "canbus_uuid: 112233445566\n"
    "\n"
    "#*# <---------------------- SAVE_CONFIG ---------------------->\n"
    "#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.\n"
    "#*#\n"
    "#*# [extruder]\n"
    "#*# control = PID\n"
    "#*#   Kp = 21.3\n"
)

#: A CRLF vendor file, the shape Windows-side tooling leaves behind.
CRLF_CFG = (
    "[mcu]\r\n"
    "serial: /dev/cu.usbmodem14101\r\n"
    "baud: 250000\r\n"
    "\r\n"
    "[mcu tool]\r\n"
    "serial: /dev/cu.usbmodem14103\r\n"
)

#: Every hostile section shape the brief names, as (label, text) pairs. Each one
#: must come back as entries carrying conflicts, never as a traceback and never
#: as an empty answer that hides the bug.
HOSTILE = (
    ("two-space name", "[mcu  a]\nserial: /dev/x\n"),
    ("empty suffix", "[mcu ]\nserial: /dev/x\n"),
    ("mcu only bracket", "[mcu]\n"),
    ("uppercase", "[MCU]\nserial: /dev/x\n"),
    ("mixed case suffix", "[Mcu tool]\nserial: /dev/x\n"),
    ("two words", "[mcu a b]\nserial: /dev/x\n"),
    ("trailing space in name", "[mcu a ]\nserial: /dev/x\n"),
    ("colon in name", "[mcu a:b]\nserial: /dev/x\n"),
    ("no mcu at all", "[fan]\npin: PA1\n"),
    ("nine sections", "\n".join(
        "[mcu m%d]\nserial: /dev/%d" % (i, i) for i in range(9))),
    ("duplicate port", "[mcu]\nserial: /dev/x\n[mcu tool]\nserial: /dev/x\n"),
    ("duplicate serial option", "[mcu]\nserial: /dev/x\nserial: /dev/y\n"),
    ("duplicate header", "[mcu]\n[mcu]\nserial: /dev/x\n"),
    ("crlf", "[mcu]\r\nserial: /dev/x\r\n"),
    ("comment after header", "[mcu tool] ; the toolboard\nserial: /dev/x\n"),
    ("hash after header", "[mcu] # master\nserial: /dev/x\n"),
    ("unterminated header", "[mcu tool\nserial: /dev/x\n"),
    ("two headers one line", "[mcu] [mcu tool]\nserial: /dev/x\n"),
    ("option before section", "serial: /dev/x\n[mcu]\n"),
    ("empty file", ""),
    ("binary junk", "[mcu]\nserial: \x00\x0b/dev/x\n\x0c\n"),
    ("continuation serial", "[mcu]\nserial: /dev/x\n  /dev/y\n"),
    ("serial with hash", "[mcu]\nserial: /dev/x#y\n"),
    ("serial with semi", "[mcu]\nserial: /dev/x ; old cable\n"),
    ("baud junk", "[mcu]\nserial: /dev/x\nbaud: fast\n"),
    ("baud too low", "[mcu]\nserial: /dev/x\nbaud: 1200\n"),
)


@pytest.fixture
def two_mcu():
    """The plain two-MCU printer.cfg text."""
    return TWO_MCU


@pytest.fixture
def autosave_cfg():
    """A config with a CAN secondary and a SAVE_CONFIG block at the bottom."""
    return WITH_AUTOSAVE


@pytest.fixture
def crlf_cfg():
    """A CRLF printer.cfg."""
    return CRLF_CFG


@pytest.fixture
def hostile():
    """(label, text) pairs for every malformed/hostile config shape."""
    return HOSTILE


@pytest.fixture
def codes():
    """The issue codes for a config: text, a path or list_mcus() output.

    validate() takes all three, so a test never has to spell out
    `mcus.validate(mcus.list_mcus(text))` to ask "what is wrong here".
    """
    return lambda cfg: [issue["code"] for issue in mcus.validate(cfg)]


@pytest.fixture
def by_name():
    """{entry["name"]: entry} for a list_mcus() result."""
    return lambda entries: {entry["name"]: entry for entry in entries}


@pytest.fixture
def target_diff():
    """Assert a write changed ONE line (or inserted one) and nothing else.

    The byte-level guard the brief asks for: a writer that "also tidied up" a
    comment, a blank line or a line ending anywhere else in the file fails here,
    because every other line is compared exactly, '\\r' included.
    """
    def _diff(before_text, after_text):
        before = before_text.split("\n")
        after = after_text.split("\n")
        if len(before) == len(after):
            changed = [(index, old, new)
                       for index, (old, new) in enumerate(
                           zip(before, after, strict=True)) if old != new]
            return {"op": "replace", "changed": changed}
        if len(after) == len(before) + 1:
            for index, old in enumerate(before):
                if old != after[index]:
                    return {"op": "insert", "at": index,
                            "added": after[index],
                            "untouched_before": before[:index] == after[:index],
                            "untouched_after": before[index:]
                            == after[index + 1:]}
            return {"op": "append", "added": after[-1],
                    "untouched_before": before == after[:-1]}
        raise AssertionError(
            "a write changed the line count by %d (only one serial: line may be "
            "added or replaced)" % (len(after) - len(before)))
    return _diff


@pytest.fixture
def entry_for():
    """The single entry for an mcu name in a config text."""
    def _one(cfg_text, name):
        hits = [entry for entry in mcus.list_mcus(cfg_text)
                if entry["name"] == name]
        assert len(hits) == 1, "expected one %r entry, got %r" % (name, hits)
        return hits[0]
    return _one
