"""Property tests: the reader survives any bytes, the writers move at most one line.

Three promises mcus.py makes about text it has never seen, which are exactly the
ones examples miss:

  1. ``list_mcus()`` never raises about config content (so ``serial --json`` can
     always exit 0 with conflicts instead of tracebacks);
  2. a write changes only the target section's ``serial:`` line - every other
     line of the file, ``\\r`` included, is the line it was;
  3. ``add_mcu_section()`` then ``set_serial()`` round-trips: the section that was
     added is the section the reader finds, named the way klipper would name it
     (klippy/mcu.py:1151-1158), with the port the writer was given.
"""
import re

import pytest
from hypothesis import assume, given, settings, strategies as st

from klipperformac import mcus

# A valid mcu name: what `add_mcu_section` promises to accept and what klipper
# would register as a pin chip (one word, no whitespace, none of BAD_NAME_CHARS).
NAME_ALPHABET = st.sampled_from(
    ["tool", "m", "cam", "rpi", "a1", "board_x", "tool-head", "tool.v2",
     "Tool", "T3"])
# A device path as macOS actually prints one - never a newline, never a NUL.
DEVICE = st.builds(
    lambda family, digits: "/dev/cu.%s%s" % (family, digits),
    st.sampled_from(["usbmodem", "usbserial", "wchusbserial", "SLAB_USBtoUART"]),
    st.from_regex(r"[0-9]{1,6}", fullmatch=True))
EOL = st.sampled_from(["\n", "\r\n"])
# Junk lines a real printer.cfg contains: comments, options, other sections and
# indented continuations. Deliberately NO `serial:` line in here: a second one
# inside the section under test is a case the deterministic writer tests own
# (duplicate-serial-option must be refused, not rewritten), and mixing it into a
# byte-preservation property would only blur what the property says.
NOISE = st.sampled_from([
    "# a comment", "; another", "", "[printer]", "max_velocity: 300",
    "[stepper_x]", "step_pin: PA4", "\ty1:PB4", "[fan]", "pin: tool:PB3",
    "[mcu extra]", "baud: 250000", "restart_method: command",
    "#*# [extruder]", "[gcode_macro X]", "gcode:", "  G91", "  G1 Z1",
])


@settings(max_examples=200, deadline=None)
@given(st.text(max_size=400))
def test_reader_never_raises_on_arbitrary_text(text):
    """Whatever bytes a config file has picked up, the reader returns a list.

    The only exception allowed is the deliberate TypeError for an input that is
    not text at all (see test_mcus_reader).
    """
    entries = mcus.list_mcus(text)
    assert isinstance(entries, list)
    for entry in entries:
        assert set(mcus.MCU_ENTRY_KEYS) <= set(entry)
    issues = mcus.validate(entries)
    assert all(issue["code"] in mcus.ISSUE_CODES for issue in issues)
    conflicts = mcus.conflicts_by_mcus(entries)
    assert set(conflicts) == {entry["name"] for entry in entries}
    assert all(value in mcus.CONFLICT_CODES or value is None
               for value in conflicts.values())


@settings(max_examples=120, deadline=None)
@given(st.text(max_size=400))
def test_the_whole_pipeline_is_json_safe(text):
    """`serial --json` decodability cannot depend on the config being sane."""
    import json
    payload = {"mcus": mcus.list_mcus(text), "single": True}
    assert json.loads(json.dumps(payload))["mcus"] == payload["mcus"]


@settings(max_examples=120, deadline=None)
@given(name=NAME_ALPHABET, device=DEVICE, eol=EOL,
       noise=st.lists(NOISE, max_size=8))
def test_add_then_set_round_trips(name, device, eol, noise):
    """The CLI's flow: create the section, point it at a port, read it back.

    The noise sits inside a `[noise]` section on purpose: an indented line joins
    the option above it (configparser's continuation rule), so loose noise after
    the master's serial line would mangle the master - which the reader reports
    honestly as bad-serial-value, and this test is not about.
    """
    base = eol.join(["[mcu]", "serial: /dev/cu.usbmodem14101", "", "[noise]"]
                    + noise) + eol
    assume(name not in [entry["name"] for entry in mcus.list_mcus(base)])
    added = mcus.add_mcu_section(base, name)
    assert ("[" + mcus.SECONDARY_PREFIX + name + "]") in added
    written = mcus.set_serial(added, name, device)
    rows = {entry["name"]: entry for entry in mcus.list_mcus(written)}
    assert name in rows
    assert rows[name]["serial"] == device
    # The master is untouched by a write that named the secondary.
    assert rows["mcu"]["serial"] == "/dev/cu.usbmodem14101"
    # And the section the writer added is not itself a defect.
    assert [issue for issue in mcus.validate(written)
            if issue["mcu"] == name and issue["code"] in mcus.CONFLICT_CODES] == []


@settings(max_examples=120, deadline=None)
@given(name=NAME_ALPHABET, device=DEVICE, eol=EOL,
       noise=st.lists(NOISE, max_size=8))
def test_a_write_moves_at_most_one_line(name, device, eol, noise):
    """Byte-level: every line that is not the target's serial line is identical.

    This is the guard against a writer that "helpfully" reserialized the file -
    which would drop comments, reorder keys, normalise line endings and rewrite
    a hand-edited printer.cfg into something the vendor would not recognise. The
    target section goes LAST so none of the noise can sit inside it.
    """
    header = "[" + mcus.SECONDARY_PREFIX + name + "]"
    base = eol.join(["[mcu]", "serial: /dev/a"] + noise
                    + ["", header, "serial: /dev/b"]) + eol
    written = mcus.set_serial(base, name, device)
    before = base.split("\n")
    after = written.split("\n")
    assert len(before) == len(after), "a replace must not change the line count"
    changed = [(i, b, a) for i, (b, a) in enumerate(zip(before, after,
                                                        strict=True))
               if b != a]
    assert len(changed) == 1, changed
    _index, old, new = changed[0]
    assert old.startswith("serial:"), old
    assert new == "serial: " + device + ("\r" if old.endswith("\r") else "")


@settings(max_examples=80, deadline=None)
@given(name=NAME_ALPHABET, device=DEVICE, eol=EOL)
def test_writing_the_same_port_twice_is_idempotent(name, device, eol):
    base = eol.join(["[mcu]", "serial: /dev/a", "",
                     "[" + mcus.SECONDARY_PREFIX + name + "]",
                     "serial: /dev/b"]) + eol
    once = mcus.set_serial(base, name, device)
    twice = mcus.set_serial(once, name, device)
    assert once == twice


@settings(max_examples=80, deadline=None)
@given(header_tail=st.sampled_from(["", " ; note", " # note", "  "]))
def test_reader_and_writer_agree_or_refuse_together(header_tail):
    """Either the write lands where klipper reads it, or nothing is written.

    klipper accepts text after a header's ']' (klippy/configfile.py:211); the
    line writer matches the header verbatim. So a tail must produce a clean write
    or a refusal - never a section written twice, and never success with the old
    port still live.
    """
    base = "[mcu]%s\nserial: /dev/a\n" % header_tail
    try:
        written = mcus.set_serial(base, "mcu", "/dev/NEW")
    except mcus.McuConfigError:
        return                      # a clean refusal wrote nothing: also correct
    assert written is not None
    entries = mcus.list_mcus(written)
    assert len(entries) == 1
    assert entries[0]["serial"] == "/dev/NEW"
    assert entries[0]["serial_count"] == 1


@settings(max_examples=60, deadline=None)
@given(st.lists(st.tuples(NAME_ALPHABET, DEVICE), max_size=6, unique_by=tuple))
def test_n_boards_are_read_back_as_n_boards(pairs):
    """The whole point of the research verdict: N USB MCUs, not one."""
    assume(len({name for name, _ in pairs}) == len(pairs))
    # Different boards, different nodes: two names on one node is the
    # duplicate-port case the deterministic suite already pins.
    assume(len({device for _, device in pairs}) == len(pairs))
    text = "[mcu]\nserial: /dev/master\n"
    text += "".join("[mcu %s]\nserial: %s\n" % (name, device)
                    for name, device in pairs)
    entries = mcus.list_mcus(text)
    assert [entry["name"] for entry in entries] == \
        ["mcu"] + [name for name, _ in pairs]
    assert [entry["serial"] for entry in entries] == \
        ["/dev/master"] + [device for _, device in pairs]
    assert mcus.validate(entries) == []


@pytest.mark.slow
@settings(max_examples=60, deadline=None)
@given(name=NAME_ALPHABET, device=DEVICE)
def test_a_refusal_never_produces_text(name, device):
    """Every refusal path raises instead of returning the input unchanged: a
    caller that trusts the return value must not be handed a lie."""
    hostile = "[mcu  %s]\nserial: /dev/a\n" % name
    with pytest.raises(mcus.McuConfigError):
        mcus.set_serial(hostile, name, device)


@settings(max_examples=100, deadline=None)
@given(text=st.text(alphabet=st.characters(exclude_characters="\r\n\x00"),
                    max_size=60))
def test_one_line_never_becomes_two(text):
    """No single-line value can inject a config line, whatever it contains.

    ``serial:`` is written on the line the reader already found, so a device path
    built from arbitrary characters cannot add a section, a second serial or a
    newline - either the write lands on one line or it is refused.
    """
    assume(not re.search(r"[\r\n\0]", text))
    base = "[mcu]\nserial: /dev/a\n"
    try:
        written = mcus.set_serial(base, "mcu", text)
    except mcus.McuConfigError:
        return
    assert written.count("\n") == base.count("\n")
    assert mcus.list_mcus(written)[0]["serial_count"] == 1
