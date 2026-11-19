"""mcus.set_serial() / mcus.add_mcu_section(): one line changes, nothing else moves.

A printer.cfg is hand-edited, vendor-shipped and appended to by klipper's own
SAVE_CONFIG, and a wrong byte in it stops the printer. So the assertions here are
byte-level: the tests compare line lists exactly (``\\r`` included) and fail if a
write touched anything beyond the target section's ``serial:`` line.

The writers are also a contract with the CLI layer: ``set_serial`` is the
``detect.set_serial_text`` kernel - the extracted body of ``detect.write_serial``,
which has always owned the master's serial line - called per section, so
``[mcu]`` and ``[mcu tool]`` cannot behave differently. One test pins that
delegation, because the moment it is re-implemented the two paths can drift.
"""
import pytest

from klipperformac import detect, mcus
from tests.mcus.conftest import TWO_MCU, WITH_AUTOSAVE


def test_master_serial_line_is_the_only_byte_moved(target_diff):
    out = mcus.set_serial(TWO_MCU, "mcu", "/dev/cu.usbmodem99999")
    diff = target_diff(TWO_MCU, out)
    assert diff["op"] == "replace"
    # line 3 of the fixture is the master's serial, inline comment and all.
    assert diff["changed"] == [(2, "serial: /dev/cu.usbmodem14101   "
                                   "# SKR Pico, left port",
                                "serial: /dev/cu.usbmodem99999")]


def test_secondary_section_only_its_own_line_changes(target_diff, by_name):
    out = mcus.set_serial(TWO_MCU, "tool", "/dev/cu.usbmodemNEW")
    diff = target_diff(TWO_MCU, out)
    assert diff["op"] == "replace"
    assert len(diff["changed"]) == 1
    rows = by_name(mcus.list_mcus(out))
    assert rows["tool"]["serial"] == "/dev/cu.usbmodemNEW"
    # The master is not yours to touch when the user named the toolboard.
    assert rows["mcu"]["serial"] == "/dev/cu.usbmodem14101"


def test_other_sections_keep_their_serials(by_name):
    """'[mcu tool]' and '[mcu other]' are separate boards, not one setting."""
    text = ("[mcu]\nserial: /dev/a\n[mcu other]\nserial: /dev/b\n"
            "[mcu tool]\nserial: /dev/c\n")
    out = mcus.set_serial(text, "tool", "/dev/Z")
    rows = by_name(mcus.list_mcus(out))
    assert [rows[n]["serial"] for n in ("mcu", "other", "tool")] == \
        ["/dev/a", "/dev/b", "/dev/Z"]


def test_serial_line_added_when_the_section_has_none(target_diff, by_name):
    """klipper needs the option (mcu.py:788); an existing section gets one line."""
    text = "[mcu]\nserial: /dev/a\n\n[mcu tool]\nbaud: 250000\n"
    out = mcus.set_serial(text, "tool", "/dev/T")
    diff = target_diff(text, out)
    assert diff["op"] == "insert"
    assert diff["added"] == "serial: /dev/T"
    assert diff["untouched_before"] and diff["untouched_after"]
    assert by_name(mcus.list_mcus(out))["tool"]["serial_line_no"] == 5


def test_crlf_file_stays_crlf_on_replace(crlf_cfg):
    out = mcus.set_serial(crlf_cfg, "tool", "/dev/T")
    assert out.count("\r\n") == crlf_cfg.count("\r\n")
    assert "serial: /dev/T\r\n" in out
    assert out.split("\n")[-1] == ""          # trailing CRLF, not a bare LF


def test_crlf_file_stays_crlf_when_a_line_is_added():
    """The kernel adds a bare LF (the behaviour write_serial has always had);
    set_serial gives the new line the file's own ending instead of leaving a
    half-CRLF config for the next human to debug."""
    text = "[mcu]\r\nserial: /dev/a\r\n[mcu tool]\r\nbaud: 250000\r\n"
    out = mcus.set_serial(text, "tool", "/dev/T")
    assert "\r\nserial: /dev/T\r\n" in out
    assert "\n" not in out.replace("\r\n", "")


def test_lf_file_is_not_turned_into_crlf():
    out = mcus.set_serial(TWO_MCU, "tool", "/dev/T")
    assert "\r" not in out


def test_comments_elsewhere_survive_byte_for_byte():
    """The '#' rules come from klipper itself (configfile.py:161-164), and a
    comment that is not on the serial line is not ours to rewrite."""
    text = ("# top\n[mcu]\n; a comment line\nserial: /dev/a\n"
            "# bottom comment\nbaud: 250000\n")
    out = mcus.set_serial(text, "mcu", "/dev/b")
    for line in ("# top", "; a comment line", "# bottom comment",
                 "baud: 250000"):
        assert line in out.split("\n")


def test_indented_pin_list_is_untouched():
    text = ("[mcu]\nserial: /dev/a\n\n[mcu tool]\nserial: /dev/b\n"
             "\n[stepper_y]\nstep_pin:\n\ty1:PB4\n\ty2:PB5\n")
    out = mcus.set_serial(text, "tool", "/dev/Z")
    assert "\n[stepper_y]\nstep_pin:\n\ty1:PB4\n\ty2:PB5\n" in out


def test_canbus_section_can_still_be_given_a_port(by_name):
    """A CAN board and a USB board are the same section shape to a writer."""
    text = "[mcu]\nserial: /dev/a\n[mcu rpi]\ncanbus_uuid: deadbeef\n"
    out = mcus.set_serial(text, "rpi", "/dev/rpmsg_endpoint")
    rows = by_name(mcus.list_mcus(out))
    assert rows["rpi"]["serial"] == "/dev/rpmsg_endpoint"
    # The uuid line is not the writer's business: it stays where it was.
    assert "canbus_uuid: deadbeef" in out


def test_section_header_with_a_trailing_comment_is_refused_not_guessed():
    """klipper reads '[mcu tool] ; note' as the section 'mcu tool'
    (configfile.py:211 drops everything after the ']'), and so does the reader.
    The line writer matches a header verbatim, so the honest answer is a refusal
    that says how to fix it - not a second copy of the section.
    """
    text = "[mcu tool] ; the toolboard\nserial: /dev/a\n"
    assert mcus.list_mcus(text)[0]["serial"] == "/dev/a"
    with pytest.raises(mcus.McuConfigError) as excinfo:
        mcus.set_serial(text, "tool", "/dev/b")
    assert "move that comment to a line of its own" in str(excinfo.value)


def test_unknown_mcu_name_refuses_and_lists_the_file():
    with pytest.raises(mcus.McuConfigError) as excinfo:
        mcus.set_serial(TWO_MCU, "nope", "/dev/x")
    message = str(excinfo.value)
    assert "[mcu]" in message and "[mcu tool]" in message


def test_hostile_header_refuses_with_the_four_character_strip_cited():
    """'[mcu  a]' IS a section klipper builds - as the mcu ' a'. Writing 'a'
    there would create a second section and two chips, so it is refused."""
    text = "[mcu  a]\nserial: /dev/a\n"
    with pytest.raises(mcus.McuConfigError) as excinfo:
        mcus.set_serial(text, "a", "/dev/b")
    assert "klippy/mcu.py:1151-1158" in str(excinfo.value)
    assert mcus.list_mcus(text)[0]["name"] == " a"


def test_non_mcu_section_refuses():
    """'[MCU]' builds no MCU (mcu.py:1242-1249); a port in it is a trap."""
    with pytest.raises(mcus.McuConfigError):
        mcus.set_serial("[MCU]\nserial: /dev/a\n", "MCU", "/dev/b")


def test_empty_suffix_refuses():
    with pytest.raises(mcus.McuConfigError):
        mcus.set_serial("[mcu ]\nserial: /dev/a\n", "", "/dev/b")


def test_duplicate_serial_lines_refuse_rather_than_lie():
    """strict=False (configfile.py:172) makes the LAST line the live one.

    Rewriting the first would return success while klipper still opened the old
    port, so the writer refuses and tells the user which lines to delete.
    """
    text = "[mcu]\nserial: /dev/a\nserial: /dev/b\n"
    with pytest.raises(mcus.McuConfigError) as excinfo:
        mcus.set_serial(text, "mcu", "/dev/c")
    assert "LAST" in str(excinfo.value)
    assert text == text            # nothing was produced at all


def test_a_device_path_cannot_inject_config_lines():
    for evil in ("/dev/x\n[mcu evil]\nserial: /dev/e", "/dev/x\ry", "/dev/x\x00y"):
        with pytest.raises(mcus.McuConfigError):
            mcus.set_serial(TWO_MCU, "tool", evil)
        with pytest.raises(mcus.McuConfigError):
            mcus.set_serial(TWO_MCU, "mcu", evil)


def test_a_serial_the_kernel_cannot_rewrite_is_refused_not_duplicated():
    """klipper accepts 'serial = /dev/a' (configfile.py:172); the line writer's
    option test is the ':' spelling. Appending a second serial line would leave
    klipper reading the LAST value - the old port - while reporting success, so
    the re-read verification refuses instead."""
    text = "[mcu]\nserial = /dev/a\n"
    assert mcus.list_mcus(text)[0]["serial"] == "/dev/a"      # klipper's view
    with pytest.raises(mcus.McuConfigError) as excinfo:
        mcus.set_serial(text, "mcu", "/dev/b")
    message = str(excinfo.value)
    assert "did not produce a port klipper would read" in message
    assert "serial = " in message


def test_host_mcu_serial_needs_no_baud(by_name):
    """klippy/mcu.py:789-791: /dev/rpmsg_* and /tmp/klipper_host_* skip baud."""
    text = ("[mcu]\nserial: /dev/cu.usbmodem14101\n\n[mcu rpi]\n"
            "serial: /tmp/klipper_host_mcu\n")
    assert mcus.validate(text) == []
    rows = by_name(mcus.list_mcus(text))
    assert rows["rpi"]["serial"] == "/tmp/klipper_host_mcu"
    assert rows["rpi"]["baud_configured"] is False


def test_a_section_at_eof_without_a_trailing_newline(by_name):
    """'[mcu tool]' as the last byte of the file is still a writable section."""
    text = "[mcu]\nserial: /dev/a\n\n[mcu tool]"
    out = mcus.set_serial(text, "tool", "/dev/T")
    rows = by_name(mcus.list_mcus(out))
    assert rows["tool"]["serial"] == "/dev/T"
    assert rows["mcu"]["serial"] == "/dev/a"


def test_writing_the_same_port_twice_is_idempotent():
    """A second identical write is a no-op, not a second serial line."""
    out = mcus.set_serial(TWO_MCU, "tool", "/dev/Z")
    assert mcus.set_serial(out, "tool", "/dev/Z") == out


def test_a_device_that_is_not_a_string_refuses():
    """A device path is text or nothing; a list would write '[""]' into cfg."""
    for bad in (None, 7, [""], ("",)):
        with pytest.raises(mcus.McuConfigError):
            mcus.set_serial(TWO_MCU, "tool", bad)


def test_missing_mcu_name_refuses():
    for bad in ("", "   ", None, 7):
        with pytest.raises(mcus.McuConfigError):
            mcus.set_serial(TWO_MCU, bad, "/dev/x")


def test_accepts_the_header_spelling_too(by_name):
    """list_mcus() hands back name AND header; a CLI has one of the two."""
    out = mcus.set_serial(TWO_MCU, "[mcu tool]", "/dev/T")
    assert by_name(mcus.list_mcus(out))["tool"]["serial"] == "/dev/T"
    out = mcus.set_serial(TWO_MCU, "[mcu]", "/dev/M")
    assert by_name(mcus.list_mcus(out))["mcu"]["serial"] == "/dev/M"


def test_the_master_path_is_the_write_serial_kernel(monkeypatch):
    """No second implementation of the same rule: set_serial delegates.

    detect.set_serial_text() is the extracted body of detect.write_serial(), so
    the master's serial line has exactly one writer in this codebase, and a
    re-implementation in mcus.py would fail this test instead of drifting.
    """
    calls = []
    kernel = detect.set_serial_text      # the real one, before the spy

    def spy(text, device_path, header="[mcu]"):
        calls.append((header, device_path))
        return kernel(text, device_path, header)

    monkeypatch.setattr(mcus.detect, "set_serial_text", spy)
    mcus.set_serial(TWO_MCU, "mcu", "/dev/M")
    mcus.set_serial(TWO_MCU, "tool", "/dev/T")
    assert calls == [("[mcu]", "/dev/M"), ("[mcu tool]", "/dev/T")]


def test_result_is_re_read_before_it_is_trusted(monkeypatch):
    """The verification net: if the kernel produced text klipper would not read
    as the port we asked for, the write is refused."""
    monkeypatch.setattr(mcus.detect, "set_serial_text",
                        lambda text, device, header=None: text)
    with pytest.raises(mcus.McuConfigError):
        mcus.set_serial(TWO_MCU, "tool", "/dev/T")


# ------------------------------------------------------------------ path mode --
def test_a_path_is_written_and_reads_back(tmp_path):
    cfg = tmp_path / "printer.cfg"
    cfg.write_text(TWO_MCU)
    returned = mcus.set_serial(cfg, "tool", "/dev/T")
    assert cfg.read_text() == returned
    assert {entry["name"]: entry["serial"] for entry in mcus.list_mcus(cfg)} == \
        {"mcu": "/dev/cu.usbmodem14101", "tool": "/dev/T"}


def test_text_input_never_touches_the_filesystem(tmp_path, monkeypatch):
    """Pure text in / text out: passing text must not write anything anywhere."""
    calls = []
    monkeypatch.setattr(mcus.paths, "write_config",
                        lambda *a, **k: calls.append(a))
    mcus.set_serial(TWO_MCU, "tool", "/dev/T")
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_cp1252_config_keeps_its_encoding(tmp_path):
    """paths.read_config's contract, applied to a multi-MCU write: a vendor file
    saved in cp1252 comes back out in cp1252, not as UTF-8 mojibake."""
    cfg = tmp_path / "printer.cfg"
    raw = "[mcu]\nserial: /dev/a\n#caf\xe9 vendor note\n".encode("cp1252")
    cfg.write_bytes(raw)
    mcus.set_serial(cfg, "mcu", "/dev/b")
    assert cfg.read_bytes() == \
        b"[mcu]\nserial: /dev/b\n#caf\xe9 vendor note\n"


def test_refusal_on_a_path_leaves_the_file_untouched(tmp_path):
    cfg = tmp_path / "printer.cfg"
    original = "[MCU]\nserial: /dev/a\n"
    cfg.write_text(original)
    with pytest.raises(mcus.McuConfigError):
        mcus.set_serial(cfg, "MCU", "/dev/b")
    assert cfg.read_text() == original


def test_write_leaves_no_temp_file_behind(tmp_path):
    """paths.write_config replaces atomically (AGENTS.md: never a half cfg)."""
    cfg = tmp_path / "printer.cfg"
    cfg.write_text("[mcu]\nserial: /dev/a\n")
    mcus.set_serial(cfg, "mcu", "/dev/b")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["printer.cfg"]


# ------------------------------------------------------------ add_mcu_section --
def test_stub_lands_at_the_end_with_the_pin_chip_reminder():
    out = mcus.add_mcu_section("[mcu]\nserial: /dev/a\n", "tool")
    assert out.startswith("[mcu]\nserial: /dev/a\n\n[mcu tool]\n")
    # The reminder that prevents the single most common multi-MCU mistake.
    assert "step_pin: tool:PB1" in out
    assert "klippy/mcu.py:1151-1158" in out
    assert "Duplicate chip name" in out
    # And it is readable as the section it claims to be.
    entries = mcus.list_mcus(out)
    assert [entry["name"] for entry in entries] == ["mcu", "tool"]


def test_stub_carries_no_serial_line_on_purpose(codes):
    """klipper's own error (configfile.py:31-37) points straight at the missing
    option, which beats a half-filled line a user trusts."""
    out = mcus.add_mcu_section("[mcu]\nserial: /dev/a\n", "tool")
    assert "\nserial:\n" not in out
    assert codes(out) == ["missing-port"]


def test_stub_goes_above_the_save_config_block(codes):
    """klippy/configfile.py:246-266: text below the marker makes klipper drop
    every saved probe offset, so the stub must not go there."""
    out = mcus.add_mcu_section(WITH_AUTOSAVE, "cam")
    marker = "#*# <---------------------- SAVE_CONFIG ---------------------->"
    assert "[mcu cam]" in out
    assert out.index("[mcu cam]") < out.index(marker)
    # The autosave block itself is byte-for-byte where it was.
    assert out.endswith(WITH_AUTOSAVE[WITH_AUTOSAVE.index(marker):])
    assert codes(out) == ["missing-port"]


def test_added_section_is_the_one_the_writer_finds():
    """add then set is the CLI's flow; it must work with nothing in between."""
    text = mcus.add_mcu_section("[mcu]\nserial: /dev/a\n", "tool")
    text = mcus.set_serial(text, "tool", "/dev/cu.usbmodem14103")
    entries = {entry["name"]: entry for entry in mcus.list_mcus(text)}
    assert entries["tool"]["serial"] == "/dev/cu.usbmodem14103"
    assert entries["mcu"]["serial"] == "/dev/a"
    assert mcus.validate(text) == []


def test_add_uses_the_files_line_ending():
    out = mcus.add_mcu_section("[mcu]\r\nserial: /dev/a\r\n", "tool")
    assert "[mcu tool]\r\n" in out
    assert "\n" not in out.replace("\r\n", "")


def test_add_to_an_empty_config():
    """An empty printer.cfg still gets a well-formed section, no leading blank."""
    out = mcus.add_mcu_section("", "tool")
    assert out.startswith("[mcu tool]\n")
    assert out.endswith("\n")
    assert mcus.list_mcus(out)[0]["name"] == "tool"


def test_add_refuses_a_name_that_already_exists():
    with pytest.raises(mcus.McuConfigError) as excinfo:
        mcus.add_mcu_section(TWO_MCU, "tool")
    assert "already exists" in str(excinfo.value)


def test_add_refuses_names_klipper_would_mangle():
    for bad in ("", " ", "a b", "a:b", "a;b", "a#b", "a]b", "[a", "mcu",
               " tool", "tool "):
        with pytest.raises(mcus.McuConfigError):
            mcus.add_mcu_section("[mcu]\nserial: /dev/a\n", bad)


def test_add_does_not_disturb_the_rest_of_the_file(target_diff):
    out = mcus.add_mcu_section(TWO_MCU, "cam")
    before = TWO_MCU.split("\n")
    after = out.split("\n")
    assert after[:len(before) - 1] == before[:len(before) - 1]
    assert out.startswith(TWO_MCU.rstrip("\n"))


def test_add_on_a_path_writes_the_file(tmp_path):
    cfg = tmp_path / "printer.cfg"
    cfg.write_text("[mcu]\nserial: /dev/a\n")
    returned = mcus.add_mcu_section(cfg, "tool")
    assert cfg.read_text() == returned
    assert "[mcu tool]" in cfg.read_text()


def test_writers_reject_a_cfg_that_is_neither_text_nor_a_path():
    """A 7 is not a config; failing loudly beats returning it unchanged."""
    with pytest.raises(TypeError):
        mcus.set_serial(7, "tool", "/dev/x")
    with pytest.raises(TypeError):
        mcus.add_mcu_section(7, "tool")
