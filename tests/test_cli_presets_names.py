"""Preset/history name handling must not escape its directory.

xfail(strict=True) marks encode the EXPECTED robust behavior: hostile names
are rejected with a clean error (SystemExit from _die), not silently joined
into paths outside the presets/history dirs. Delete the marker when fixed.
"""
import argparse

import pytest


def preset_args(**kw):
    base = dict(library=False, use_library=None, history=False, restore=None,
                snapshot=None, save=None, use=None, import_=None,
                import_name=None, auto_serial=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_valid_names_work(stack):
    stack.paths.PRINTER_CFG.write_text("[mcu]\nserial: /dev/x\n")
    stack.cli.cmd_presets(preset_args(save="my-printer_v2"))
    assert (stack.paths.PRESETS / "my-printer_v2.cfg").exists()


@pytest.mark.xfail(strict=True, reason="BUG T1: presets --save does not "
                   "validate the name; '../evil' escapes the presets dir")
def test_save_dotdot_rejected(stack, tmp_path):
    stack.paths.PRINTER_CFG.write_text("[mcu]\n")
    with pytest.raises(SystemExit):
        stack.cli.cmd_presets(preset_args(save="../../evil"))
    escaped = (stack.paths.PRESETS / "../../evil.cfg").resolve()
    assert not escaped.exists()


@pytest.mark.xfail(strict=True, reason="BUG T1: Path(base) / absolute_name "
                   "silently overrides the base directory")
def test_save_absolute_rejected(stack, tmp_path):
    outside = tmp_path / "outside.cfg"
    stack.paths.PRINTER_CFG.write_text("[mcu]\n")
    with pytest.raises(SystemExit):
        stack.cli.cmd_presets(preset_args(save=str(outside / "evil")))
    assert not outside.exists()


@pytest.mark.xfail(strict=True, reason="BUG T1: presets --use with an "
                   "absolute path copies ANY file onto printer.cfg")
def test_use_absolute_rejected(stack, tmp_path):
    sneaky = tmp_path / "sneaky"          # extensionless: --use appends .cfg
    sneaky.with_suffix(".cfg").write_text("[mcu]\nserial: /dev/STOLEN\n")
    stack.paths.PRINTER_CFG.write_text("[mcu]\nserial: /dev/good\n")
    with pytest.raises(SystemExit):
        stack.cli.cmd_presets(preset_args(use=str(sneaky)))
    assert "STOLEN" not in stack.paths.PRINTER_CFG.read_text()


@pytest.mark.xfail(strict=True, reason="BUG T1: presets --restore with a "
                   "path-traversal name restores ANY file")
def test_restore_traversal_rejected(stack):
    marker = stack.home / "not-history.cfg"
    marker.write_text("[mcu]\nserial: /dev/FROM_OUTSIDE\n")
    stack.paths.PRINTER_CFG.write_text("[mcu]\nserial: /dev/good\n")
    rel = "../not-history"
    with pytest.raises(SystemExit):
        stack.cli.cmd_presets(preset_args(restore=rel + ".cfg"))
    assert "FROM_OUTSIDE" not in stack.paths.PRINTER_CFG.read_text()
