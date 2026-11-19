"""GROUP integrator / cross-file repair for F3's needsFixInF1 (finding [5]).

`klipperformac presets --save NAME` and `--import-name NAME` put NAME straight
into a filename (PRESETS/<name>.cfg, plus <name>.cfg.tmp while copying). The
separator check in cli._safe_name() bounded *where* a name could go but never
*what* it was, so two things were broken:

  * a name longer than the filesystem's 255-byte component cap raised
    OSError(ENAMETOOLONG) out of shutil.copy2 - a raw interpreter traceback on a
    typo'd preset name (400 characters was enough);
  * a name containing a newline or a tab was accepted, wrote a real file, and
    then printed as extra rows in `klipperformac presets` - and in the GUI/TUI
    list that parses it - so one saved preset could claim to be several.

Both are fixed in klipperformac/cli.py: _safe_name() now rejects control
characters and over-long names (in characters AND in encoded bytes, because an
emoji name hits the cap long before it hits 100 characters), and _copy_atomic()
turns any remaining OSError - disk full, read-only folder, source that vanished
mid-command - into one clean line on stderr with exit 1.

This is the half of finding [5] that lives in cli.py; the installer half is
pinned by tests/regress/test_v_04_name_length_guards.py.
"""
import argparse
import errno
import shutil
import types

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

HS = dict(deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.function_scoped_fixture,
                                 HealthCheck.too_slow])

LONG = "A" * 400                  # enough for ENAMETOOLONG on APFS/HFS+
HUGE = "A" * 100000               # the reporter's 100 KB name


def preset_args(**kw):
    base = dict(library=False, use_library=None, history=False, restore=None,
                snapshot=None, save=None, use=None, import_=None,
                import_name=None, auto_serial=False)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def printer(stack):
    """A throwaway install with a real printer.cfg to save."""
    stack.paths.PRINTER_CFG.write_text("[mcu]\nserial: /dev/x\n")
    return stack


def fail_copy(errno_code, message):
    """A shutil.copy2 that dies the way a real full/read-only disk dies."""
    def boom(src, dst, *a, **kw):
        raise OSError(errno_code, message)
    return boom


def test_a_400_char_name_is_one_clean_line_not_a_traceback(printer, capsys):
    """The hunt repro: OSError [Errno 63] File name too long, dumped raw."""
    with pytest.raises(SystemExit) as caught:
        printer.cli.cmd_presets(preset_args(save=LONG))
    assert caught.value.code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err and "Errno 63" not in err, err
    assert "too long" in err
    assert list(printer.paths.PRESETS.iterdir()) == [], \
        "a refusal writes nothing"


def test_a_100k_char_name_is_refused_too(printer, capsys):
    with pytest.raises(SystemExit):
        printer.cli.cmd_presets(preset_args(save=HUGE))
    assert "Traceback" not in capsys.readouterr().err


def test_an_over_long_import_name_is_refused_before_the_copy(printer, tmp_path,
                                                             capsys):
    src = tmp_path / "generic-rambo.cfg"
    src.write_text("[mcu]\nserial: /dev/REPLACE\n")
    with pytest.raises(SystemExit) as caught:
        printer.cli.cmd_presets(preset_args(import_=str(src), import_name=LONG))
    assert caught.value.code == 1
    assert "Traceback" not in capsys.readouterr().err
    assert list(printer.paths.PRESETS.iterdir()) == []


def test_an_over_long_source_filename_is_refused_too(printer, tmp_path, capsys):
    """Without --import-name the name comes from the file itself, which is
    exactly how a 200-character download became an interpreter dump. (macOS
    cannot even create a 300-character component, so the reachable window is
    101..255 bytes: the guard closes it.)"""
    src = tmp_path / ("D" * 200 + ".cfg")
    src.write_text("[mcu]\n")
    with pytest.raises(SystemExit) as caught:
        printer.cli.cmd_presets(preset_args(import_=str(src)))
    assert caught.value.code == 1
    assert "Traceback" not in capsys.readouterr().err
    assert list(printer.paths.PRESETS.iterdir()) == []


@pytest.mark.parametrize("name", [
    pytest.param("a\nb", id="newline"),
    pytest.param("a\tb", id="tab"),
    pytest.param("a\x00b", id="nul"),
    pytest.param("ok\x1b[31m", id="ansi-escape"),
])
def test_a_control_character_name_is_refused(printer, capsys, name):
    """Before: `presets --save $'a\\nb'` printed "Saved preset 'a<newline>b'"
    and the listing then showed two rows for one file."""
    with pytest.raises(SystemExit) as caught:
        printer.cli.cmd_presets(preset_args(save=name))
    assert caught.value.code == 1
    err = capsys.readouterr().err
    assert "control characters" in err, err
    assert list(printer.paths.PRESETS.iterdir()) == []


def test_a_saved_listing_cannot_be_forged_by_one_file(printer, capsys):
    """The row-counting consequence, end to end: after refusing the newline
    name, `presets` lists exactly the presets that really exist."""
    printer.cli.cmd_presets(preset_args(save="real-one"))
    with pytest.raises(SystemExit):
        printer.cli.cmd_presets(preset_args(save="fake\ntwo\nthree"))
    capsys.readouterr()
    printer.cli.cmd_presets(preset_args())
    lines = [ln for ln in capsys.readouterr().out.splitlines()
             if ln.startswith("  ")]
    assert [ln.strip() for ln in lines] == ["* real-one"], lines


def test_a_multibyte_name_is_bounded_by_bytes_not_characters(printer, capsys):
    """100 snake emojis are 400 BYTES: still ENAMETOOLONG. The bound has to be
    the encoded length, which is what the filesystem measures."""
    with pytest.raises(SystemExit):
        printer.cli.cmd_presets(preset_args(save="\U0001F40D" * 81))   # 324 B
    assert "Traceback" not in capsys.readouterr().err
    short = "\U0001F40D" * 40          # 160 bytes, 40 characters: legal
    printer.cli.cmd_presets(preset_args(save=short))
    assert (printer.paths.PRESETS / (short + ".cfg")).is_file()


def test_names_up_to_the_limit_still_save(printer):
    """The guard must not eat legitimate names: spaces, dots, hyphens, and a
    name exactly at the character limit all still produce a preset."""
    limit = printer.cli.PRESET_NAME_MAX_CHARS
    for name in ("Voron 2.4", "my-printer_v2", "B" * limit):
        printer.cli.cmd_presets(preset_args(save=name))
        assert (printer.paths.PRESETS / (name + ".cfg")).is_file(), name
    assert printer.cli.PRESET_NAME_MAX_BYTES + len(".cfg.tmp") <= 255, \
        "the bound must leave room for the temp name _copy_atomic writes"


def test_the_traversal_wording_is_unchanged(printer, capsys):
    """CLI prose is a user-facing (and GUI-parsed) surface: the pre-existing
    refusal message must stay byte-for-byte what it was."""
    with pytest.raises(SystemExit):
        printer.cli.cmd_presets(preset_args(save="../../evil"))
    assert capsys.readouterr().err == (
        "Invalid preset name '../../evil': no path separators, '..', or "
        "leading dot allowed\n")


def test_a_full_disk_on_save_is_a_clean_refusal(printer, monkeypatch, capsys):
    monkeypatch.setattr(printer.cli, "shutil", types.SimpleNamespace(
        copy2=fail_copy(errno.ENOSPC, "No space left on device")))
    with pytest.raises(SystemExit) as caught:
        printer.cli.cmd_presets(preset_args(save="full-disk"))
    assert caught.value.code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err, err
    assert err.startswith("Cannot save the current config as preset "
                          "'full-disk':"), err
    assert "No space left on device" in err and "(nothing was written)" in err
    assert list(printer.paths.PRESETS.glob("*")) == [], "no half-written preset"


def test_a_read_only_folder_on_import_is_a_clean_refusal(printer, tmp_path,
                                                         monkeypatch, capsys):
    src = tmp_path / "a.cfg"
    src.write_text("[mcu]\n")
    monkeypatch.setattr(printer.cli, "shutil", types.SimpleNamespace(
        copy2=fail_copy(errno.EACCES, "Permission denied")))
    with pytest.raises(SystemExit) as caught:
        printer.cli.cmd_presets(preset_args(import_=str(src),
                                            import_name="denied"))
    assert caught.value.code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err, err
    assert "Cannot import" in err and "Permission denied" in err
    assert list(printer.paths.PRESETS.glob("*")) == []


def test_applying_a_preset_that_cannot_be_written_leaves_the_config_alone(
        printer, monkeypatch, capsys):
    """The --use direction rewrites printer.cfg, the one file Klipper cannot
    start without: a failed copy must exit cleanly and not truncate it.

    Only the printer.cfg write fails (the printer.cfg.bak staging copy is
    allowed through), so this pins the site that matters."""
    preset = printer.paths.PRESETS / "broken.cfg"
    preset.write_text("[mcu]\nserial: /dev/NEW\n")
    before = printer.paths.PRINTER_CFG.read_text()

    def copy2(src, dst, *a, **kw):
        if str(dst).endswith("printer.cfg.tmp"):
            raise OSError(errno.EROFS, "Read-only file system")
        return shutil.copy2(src, dst, *a, **kw)

    monkeypatch.setattr(printer.cli, "shutil",
                        types.SimpleNamespace(copy2=copy2))
    with pytest.raises(SystemExit) as caught:
        printer.cli.cmd_presets(preset_args(use="broken"))
    assert caught.value.code == 1
    err = capsys.readouterr().err
    assert "Cannot apply preset 'broken'" in err, err
    assert "Traceback" not in err, err
    assert printer.paths.PRINTER_CFG.read_text() == before
    assert not list(printer.paths.CONFIG.glob("*.tmp"))


@settings(max_examples=250, **HS)
@given(name=st.text(min_size=1, max_size=400))
@example(name=LONG)
@example(name="a\nb")
@example(name="\U0001F40D" * 100)
def test_no_name_ever_leaks_an_exception_other_than_a_clean_exit(printer, name):
    """Property version of the same rule: whatever the user types, the only way
    out of `presets --save NAME` is a written preset, a refused name, or a
    refused copy - never an OSError with a traceback.

    (min_size=1 because an empty --save is argparse-absent: cmd_presets falls
    through to the listing, which is the pre-existing contract.)"""
    try:
        printer.cli.cmd_presets(preset_args(save=name))
    except SystemExit as e:
        assert e.code == 1, e.code
    except OSError as e:
        pytest.fail("raw OSError for name %r: %s" % (name[:40], e))
    else:
        assert (printer.paths.PRESETS / (name + ".cfg")).is_file(), repr(name)


def test_a_refusal_prints_no_success_line(printer, capsys):
    """`presets --save` used to be able to say nothing at all and exit 1 on a
    traceback; a refusal must be stderr-only so the GUI never reads 'Saved'."""
    with pytest.raises(SystemExit):
        printer.cli.cmd_presets(preset_args(save=LONG))
    assert capsys.readouterr().out == ""
