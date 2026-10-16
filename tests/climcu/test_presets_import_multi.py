"""`presets --import` of a multi-MCU cfg: warn on stderr, never block.

An imported config is a file for a printer that may not be plugged into this
Mac, so this is advice, not a gate: the preset lands, the exit code stays 0, and
the conflicts `mcus.validate()` sees (or, with no wave 3, the CLI's own parse)
are said out loud because "Printer is halted" on the second boot is far harder
to read than one line now.
"""
import pytest


def write_source(tmp_path, text, name="mine-printer.cfg"):
    src = tmp_path / name
    src.write_text(text)
    return src


def test_a_two_section_conflict_is_said_and_the_import_still_happens(
        cstack, attached, facts, tmp_path, capsys):
    src = write_source(tmp_path, "[mcu]\nserial: " + facts.MASTER +
                       "\n\n[mcu tool]\nserial: " + facts.MASTER + "\n")
    attached([], [])
    cstack.cli.cmd_presets(facts.preset_args(import_=str(src)))
    captured = capsys.readouterr()
    assert "Imported" in captured.out
    assert "duplicate-port" in captured.err or "both claim" in captured.err, \
        captured.err
    assert (cstack.paths.PRESETS / "mine-printer.cfg").is_file()
    assert captured.err, "a conflict must not go to stdout (scripts read it)"


def test_a_clean_single_mcu_import_says_nothing_on_stderr(
        cstack, attached, facts, tmp_path, capsys):
    src = write_source(tmp_path, facts.CFG_SINGLE)
    attached([facts.MASTER], [facts.identity_row(facts.MASTER)])
    cstack.cli.cmd_presets(facts.preset_args(import_=str(src)))
    captured = capsys.readouterr()
    assert "Imported" in captured.out
    assert captured.err == "", captured.err


def test_a_section_without_a_serial_is_warned_about(cstack, attached, facts,
                                                   tmp_path, capsys):
    """klipper's most common halt: 'Option serial in section mcu tool must be
    specified'. Better said at import than discovered by a cold bed."""
    src = write_source(tmp_path, "[mcu]\nserial: /dev/x\n\n[mcu tool]\n")
    attached([], [])
    cstack.cli.cmd_presets(facts.preset_args(import_=str(src)))
    err = capsys.readouterr().err
    assert "[mcu tool]" in err and "serial" in err


def test_a_broken_config_is_warned_about_and_still_imports(
        cstack, attached, facts, tmp_path, capsys):
    """No traceback and no refusal: '[mcu' with no ']' is a file a human can
    paste, and the import is the user's call, not ours."""
    src = write_source(tmp_path, "[mcu\nserial: /dev/x\n\n[mcu  a b]\n")
    attached([], [])
    cstack.cli.cmd_presets(facts.preset_args(import_=str(src)))
    captured = capsys.readouterr()
    assert "Imported" in captured.out
    assert captured.err.strip(), "a broken section must be named"
    assert "Traceback" not in captured.err


def test_the_warnings_survive_a_missing_mcus_module(no_mcus, attached, facts,
                                                   tmp_path, capsys):
    """Same advice with no wave 3: the CLI's own parse reports the conflicts it
    can see (duplicate-port, no serial) and the import still completes."""
    src = write_source(tmp_path, "[mcu]\nserial: " + facts.MASTER +
                       "\n\n[mcu tool]\nserial: " + facts.MASTER + "\n")
    attached([], [])
    no_mcus.cli.cmd_presets(facts.preset_args(import_=str(src)))
    captured = capsys.readouterr()
    assert "Imported" in captured.out
    assert "duplicate-port" in captured.err
    assert (no_mcus.paths.PRESETS / "mine-printer.cfg").is_file()


def test_the_import_check_never_raises(no_mcus, attached, facts, tmp_path,
                                      capsys, monkeypatch):
    """A helper that cannot run is one more line, not a dead command."""
    src = write_source(tmp_path, facts.CFG_SINGLE)
    attached([], [])
    monkeypatch.setattr(no_mcus.cli, "_mcu_conflicts",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")))
    no_mcus.cli.cmd_presets(facts.preset_args(import_=str(src)))
    captured = capsys.readouterr()
    assert "Imported" in captured.out
    assert "did not run" in captured.err


def test_a_binary_or_huge_import_still_refuses_before_any_warning(
        cstack, attached, facts, tmp_path, capsys):
    """The pre-existing guards run first: this test only pins that the new
    multi-MCU check did not jump ahead of them."""
    src = tmp_path / "keychain.cfg"
    src.write_bytes(b"\x00\x01not a config")
    attached([], [])
    with pytest.raises(SystemExit):
        cstack.cli.cmd_presets(facts.preset_args(import_=str(src)))
    captured = capsys.readouterr()
    assert "binary" in captured.err
    assert not (cstack.paths.PRESETS / "keychain.cfg").exists()
