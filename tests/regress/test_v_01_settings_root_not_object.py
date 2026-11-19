"""Regression (hunt finding [7], group F3): a settings.json whose top-level JSON
value is not an object must never take a command down.

Filed as: `echo '[1,2,3]' > ~/.klipperformac/settings.json` then
`klipperformac lan on` / `klipperformac data --set PATH` died with

    TypeError: list indices must be integers or slices, not str

because load_settings() returned whatever json.loads() produced and the caller
subscripted it. settings.json is shared with the SwiftUI app, so a junk root is
a real-world state, not a hypothetical. Fixed in klipperformac/paths.py
(load_settings() now returns {} for any non-object root), which protects every
caller at once: cmd_lan, cmd_data/relocate_data, lan_enabled, web_url,
cam_settings, _resolve_data.
"""
import argparse
import json
import os

import pytest

JUNK_ROOTS = [
    pytest.param("[1,2,3]", id="json-list"),
    pytest.param("42", id="json-number"),
    pytest.param('"lan"', id="json-string"),
    pytest.param("null", id="json-null"),
    pytest.param("true", id="json-bool"),
    pytest.param('["{}", "{}"]', id="json-list-of-dicts"),
    pytest.param("[]", id="json-empty-list"),
]


def write_settings(stack, text):
    stack.home.mkdir(parents=True, exist_ok=True)
    stack.paths.SETTINGS.write_text(text)
    return stack.paths.SETTINGS


@pytest.mark.parametrize("payload", JUNK_ROOTS)
def test_load_settings_returns_a_dict_for_a_non_object_root(stack, payload):
    write_settings(stack, payload)
    loaded = stack.paths.load_settings()
    assert isinstance(loaded, dict), "load_settings() must hand back a dict"
    assert loaded == {}, "a junk root reads as 'no settings'"


def test_load_settings_still_reads_a_real_object(stack):
    write_settings(stack, '{"lan": true, "data": "/tmp/x"}')
    assert stack.paths.load_settings() == {"lan": True, "data": "/tmp/x"}


def test_load_settings_survives_a_settings_file_that_is_a_directory(stack):
    stack.home.mkdir(parents=True, exist_ok=True)
    stack.paths.SETTINGS.mkdir()
    assert stack.paths.load_settings() == {}


def test_lan_on_survives_a_list_settings_file(stack, capsys):
    """The exact repro: settings.json == [1,2,3], then `lan on`."""
    write_settings(stack, "[1,2,3]")
    stack.cli.cmd_lan(argparse.Namespace(state="on"))
    out = capsys.readouterr().out
    assert "LAN access OPEN" in out
    assert json.loads(stack.paths.SETTINGS.read_text()) == {"lan": True}
    assert stack.paths.lan_enabled() is True


def test_lan_off_survives_a_scalar_settings_file(stack):
    write_settings(stack, "42")
    stack.cli.cmd_lan(argparse.Namespace(state="off"))
    assert stack.paths.lan_enabled() is False


def test_data_set_survives_a_list_settings_file(stack, tmp_path, capsys):
    """The other repro: `data --set` used to die on settings["data"] = ..."""
    write_settings(stack, "[1,2,3]")
    target = tmp_path / "moved-data"
    target.mkdir()
    stack.cli.cmd_data(argparse.Namespace(set=str(target)))
    assert "Data folder" in capsys.readouterr().out
    saved = json.loads(stack.paths.SETTINGS.read_text())
    assert os.path.realpath(saved["data"]) == os.path.realpath(str(target))
    assert saved["data"].startswith("/")


def test_every_load_settings_helper_reads_a_junk_root_as_empty(stack):
    write_settings(stack, "[1,2,3]")
    assert stack.paths.cam_settings() == {}
    assert stack.paths.lan_enabled() is False
    assert stack.paths.web_url().startswith("http://localhost:")
    # and a junk *value* cannot forge a camera or a data folder either
    write_settings(stack, '{"cam": [1,2], "data": 7}')
    assert stack.paths.cam_settings() == {}
    assert stack.paths._usable_data(
        stack.paths.load_settings().get("data")) is None


def test_cam_save_and_lan_write_merge_onto_a_junk_root(stack):
    write_settings(stack, '"not a dict"')
    stack.paths.save_cam_settings(fps=20)
    assert stack.paths.cam_settings() == {"fps": 20}
    settings = stack.paths.load_settings()
    settings["lan"] = True
    stack.paths.save_settings(settings)
    assert stack.paths.lan_enabled() is True
