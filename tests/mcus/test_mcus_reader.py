"""mcus.list_mcus(): reading N MCUs out of printer.cfg the way klipper reads them.

What is pinned here is upstream behaviour, not our taste. The two facts every
assertion leans on:

  * klippy/mcu.py:1242-1249 - klipper builds the timing master from the section
    named exactly ``mcu`` and every secondary from
    ``get_prefix_sections('mcu ')`` (that prefix carries a trailing space and is
    case sensitive), so ``[MCU]`` and ``[Mcu tool]`` are NOT MCUs;
  * klippy/mcu.py:1151-1158 - ``MCU.__init__`` strips EXACTLY the four characters
    ``'mcu '`` from the section name, so ``[mcu  a]`` is the mcu ``' a'`` and
    ``[mcu ]`` is the mcu ``''``. That name is the pin chip identity
    (klippy/mcu.py:1003, klippy/pins.py:126-131), which is why our `name` is the
    four-character strip and not a prettified one.

Everything is pure text: no files, no ports, no board, no live stack.
"""
import pytest

from klipperformac import mcus
from tests.mcus.conftest import HOSTILE, TWO_MCU


def test_entry_shape_is_the_pinned_superset():
    """The keys consumers may rely on are always there; extras stay additive."""
    entries = mcus.list_mcus(TWO_MCU)
    for entry in entries:
        for key in mcus.MCU_ENTRY_KEYS:
            assert key in entry
    assert set(mcus.MCU_ENTRY_KEYS) <= set(mcus.MCU_ENTRY_ALL_KEYS)
    assert set(entries[0]) == set(mcus.MCU_ENTRY_ALL_KEYS)


def test_master_first_then_file_order(by_name):
    """klipper loads 'mcu' first (mcu.py:1246); the rest stay in file order."""
    entries = mcus.list_mcus(TWO_MCU)
    assert [entry["name"] for entry in entries] == ["mcu", "tool"]
    rows = by_name(entries)
    assert rows["mcu"]["is_master"] is True
    assert rows["tool"]["is_master"] is False
    assert rows["mcu"]["line_no"] == 2
    assert rows["tool"]["line_no"] == 10


def test_names_and_headers_are_upstream_faithful(by_name):
    """name 'mcu'/'tool', section_header '[mcu]'/'[mcu tool]' - the JSON pair."""
    rows = by_name(mcus.list_mcus(TWO_MCU))
    assert rows["mcu"]["section_header"] == "[mcu]"
    assert rows["tool"]["section_header"] == "[mcu tool]"
    assert rows["tool"]["raw_name"] == "mcu tool"


def test_serial_baud_and_line_numbers(two_mcu, by_name):
    """serial/baud are what klipper reads, and the line numbers point at them."""
    rows = by_name(mcus.list_mcus(two_mcu))
    assert rows["mcu"]["serial"] == "/dev/cu.usbmodem14101"
    assert rows["mcu"]["baud"] == 250000
    assert rows["mcu"]["baud_configured"] is True
    assert rows["mcu"]["serial_line_no"] == 3
    assert rows["mcu"]["serial_count"] == 1
    assert rows["tool"]["serial"] == "/dev/cu.usbmodem14103"
    # No baud line: klipper's default, not our invention (mcu.py:791).
    assert rows["tool"]["baud"] is None
    assert rows["tool"]["baud_configured"] is False


def test_unconfigured_baud_is_none_not_a_default(by_name):
    """A reader does not invent a value klipper only applies at connect time."""
    rows = by_name(mcus.list_mcus("[mcu]\nserial: /dev/x\n"))
    assert rows["mcu"]["baud"] is None
    assert mcus.DEFAULT_BAUD == 250000      # klippy/mcu.py:791, for reference


def test_option_rules_match_configparser_rules():
    """Case-insensitive keys, ':' or '=' separators, ';' and '#' comment cuts.

    All four are configparser/klipper rules (klippy/configfile.py:161-164 and
    :170-176) and each one changes what klipper would open.
    """
    rows = {entry["name"]: entry for entry in mcus.list_mcus(
        "[mcu]\nSERIAL : /dev/upper\n"
        "[mcu eq]\nserial = /dev/equals\n"
        "[mcu semi]\nserial: /dev/semi ; old cable\n"
        "[mcu hash]\nserial: /dev/hash#truncated\n"
        "[mcu nobare]\nserial: /dev/bare;kept\n")}
    assert rows["mcu"]["serial"] == "/dev/upper"
    assert rows["eq"]["serial"] == "/dev/equals"
    assert rows["semi"]["serial"] == "/dev/semi"
    # '#' truncates for klipper too, so that IS the value it would open.
    assert rows["hash"]["serial"] == "/dev/hash"
    # A ';' with no space before it is part of the value (verified against
    # configparser with klipper's exact inline_comment_prefixes).
    assert rows["nobare"]["serial"] == "/dev/bare;kept"


def test_last_serial_wins_like_strict_false():
    """strict=False (configfile.py:172): a repeated option is not an error.

    klipper reads the LAST one, so the reader reports the last and keeps the
    count, instead of pretending the section has one value it does not have.
    """
    entry = mcus.list_mcus(
        "[mcu]\nserial: /dev/first\nserial: /dev/last\n")[0]
    assert entry["serial"] == "/dev/last"
    assert entry["serial_count"] == 2
    # serial_line_no is the line a write rewrites, i.e. the FIRST: the gap
    # between those two is exactly why validate() reports the duplicate.
    assert entry["serial_line_no"] == 2


def test_repeated_header_is_one_mcu_like_configparser():
    """Two '[mcu tool]' headers are ONE section upstream (merged), not two."""
    entries = mcus.list_mcus(
        "[mcu]\nserial: /dev/a\n\n[mcu tool]\nserial: /dev/one\n\n"
        "[mcu tool]\nserial: /dev/two\n")
    assert [entry["name"] for entry in entries] == ["mcu", "tool"]
    tool = entries[1]
    assert tool["serial"] == "/dev/two"
    assert tool["header_count"] == 2
    assert tool["line_no"] == 4           # where the section actually starts


def test_canbus_section_is_configured_without_a_port():
    """mcu.py:780-786: canbus_uuid takes precedence over serial, and is enough."""
    entries = mcus.list_mcus("[mcu]\nserial: /dev/a\n"
                             "[mcu rpi]\ncanbus_uuid: deadbeef\n")
    rows = {entry["name"]: entry for entry in entries}
    assert rows["rpi"]["canbus_uuid"] == "deadbeef"
    assert rows["rpi"]["serial"] is None
    # A CAN MCU is configured and complete, so it is never "missing a port".
    assert mcus.conflicts_by_mcus(entries) == {"mcu": None, "rpi": None}
    assert mcus.validate(entries) == []


def test_indentation_and_blank_lines_do_not_move_the_reader():
    """An indented mcu header is still a header (configparser strips the line)."""
    entries = mcus.list_mcus("   [mcu]\n\tserial: /dev/indented\n")
    assert entries[0]["serial"] == "/dev/indented"


def test_continuation_line_mangles_the_value_and_says_so():
    """An indented line joins the value (configparser), so it can be no port.

    The reader hands back None for `serial` (nothing can open a two-line path)
    and keeps the raw text in `serial_raw`, and validate() names the reason
    instead of just "no port configured".
    """
    entries = mcus.list_mcus("[mcu]\nserial: /dev/x\n  /dev/y\n")
    assert entries[0]["serial"] is None
    assert "\n" in entries[0]["serial_raw"]
    assert [issue["code"] for issue in mcus.validate(entries)] == \
        ["missing-port", "bad-serial-value"]


def test_hostile_text_never_raises(hostile):
    """The contract's core promise: malformed cfg -> entries, never a traceback.

    Every shape in the corpus must return a list of dict entries with the pinned
    keys, in a JSON-safe shape. Anything that raises here would take `serial
    --json` down with it instead of reporting the conflict.
    """
    for label, text in hostile:
        entries = mcus.list_mcus(text)
        assert isinstance(entries, list), label
        for entry in entries:
            assert set(mcus.MCU_ENTRY_KEYS) <= set(entry), label
            assert isinstance(entry["name"], str), label
            assert isinstance(entry["line_no"], int), label
        # And the whole pipeline stays quiet: validate + the conflict mapping.
        mcus.validate(entries)
        mcus.conflicts_by_mcus(entries)


def test_hostile_entries_are_visible_not_dropped():
    """A mcu-shaped section is always reported, even when klipper rejects it."""
    visible = dict(HOSTILE)
    for label in ("two-space name", "empty suffix", "uppercase", "two words",
                  "unterminated header", "two headers one line",
                  "mixed case suffix"):
        assert mcus.list_mcus(visible[label]), label
    # 'no mcu at all' and 'option before section' really have no MCU to report.
    assert mcus.list_mcus(visible["no mcu at all"]) == []


def test_is_mcu_separates_impostors_from_secondaries(by_name):
    """is_mcu is klipper's verdict, not a spelling opinion."""
    rows = by_name(mcus.list_mcus("[MCU]\nserial: /dev/x\n[mcu tool]\n"
                                  "serial: /dev/y\n[mcu  z]\nserial: /dev/z\n"))
    assert rows["MCU"]["is_mcu"] is False        # case sensitive prefix
    assert rows["tool"]["is_mcu"] is True
    assert rows[" z"]["is_mcu"] is True          # 'mcu ' + ' z'
    assert rows[" z"]["name"] == " z"


def test_baud_floor_is_klipper_value_not_ours():
    """mcu.py:791 - config.getint('baud', 250000, minval=2400)."""
    assert mcus.MIN_BAUD == 2400
    entries = mcus.list_mcus("[mcu]\nserial: /dev/x\nbaud: 9600\n")
    assert entries[0]["baud"] == 9600            # read truthfully
    codes = [issue["code"] for issue in mcus.validate(entries)]
    assert "bad-baud" not in codes
    entries = mcus.list_mcus("[mcu]\nserial: /dev/x\nbaud: 1200\n")
    assert entries[0]["baud"] == 1200
    assert "bad-baud" in [issue["code"] for issue in mcus.validate(entries)]


def test_line_numbers_are_one_based_and_point_at_the_header():
    entries = mcus.list_mcus("\n[mcu]\nserial: /dev/a\n")
    assert entries[0]["line_no"] == 2


def test_a_path_is_read_the_same_way_as_the_text(tmp_path):
    """TEXT OR PATH: a Path is the file, a str is the text - never a guess.

    ``mcus`` treats ``str`` as config text (so a path spelled as a plain string
    is just text that happens to contain no sections) and ``os.PathLike`` -
    what every caller in this codebase already holds - as the file to read.
    """
    cfg = tmp_path / "printer.cfg"
    cfg.write_text(TWO_MCU)
    assert mcus.list_mcus(cfg) == mcus.list_mcus(TWO_MCU)
    assert mcus.list_mcus(str(cfg)) == []


def test_non_string_non_path_input_is_a_programming_error():
    """Text or a path; a 7 is neither, and failing loudly beats guessing."""
    with pytest.raises(TypeError):
        mcus.list_mcus(7)
    with pytest.raises(TypeError):
        mcus.list_mcus(None)


def test_readers_never_write(tmp_path):
    """`serial --json` is read-only: passing a path must not touch the file."""
    cfg = tmp_path / "printer.cfg"
    cfg.write_text(TWO_MCU)
    before = cfg.read_bytes()
    mcus.list_mcus(cfg)
    mcus.validate(cfg)
    mcus.conflicts_by_mcus(cfg)
    assert cfg.read_bytes() == before
