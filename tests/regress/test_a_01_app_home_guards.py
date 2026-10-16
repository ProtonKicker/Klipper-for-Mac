"""GROUP F1 / findings [0] + [10]: the destructive paths of `uninstall` and
`data --set`.

Before this wave, two commands could delete folders they had no right to:

  * `KLIPPERFORMAC_HOME= klipperforac uninstall` resolved APP_HOME to '.'
    (the current directory) and `KLIPPERFORMAC_HOME=~` to the user's home
    folder, then ran shutil.rmtree(APP_HOME, ignore_errors=True) on it - so the
    CWD, or .zshrc + Documents + everything else, went away, and the command
    still printed "Uninstalled." with exit 0.
  * `data --set ~/Documents` accepted any directory at all, and the
    `uninstall --data` that followed rmtree'd whatever the data folder now
    pointed at.

The fix has three parts, and every test below fails without them:
paths.py refuses a blank/relative/home-root app home and a protected data
folder; `data --set` refuses destinations that are top-level user folders or
that already hold somebody else's files (and exits 1 on a refusal); and every
rmtree in cmd_uninstall goes through _deletable_tree(), which requires the
folder to carry the layout this tool actually installs.

Nothing here touches the real ~/.klipperformac: every case runs against a
throwaway home under tmp_path, and the signal paths are stubbed (a real
kill_all() scans machine-wide for `caffeinate -dis` and would kill this Mac's
live one).
"""
import argparse
import importlib
import json
import sys

import pytest

MODULES = ("paths", "configgen", "detect", "versions", "shaper", "process",
           "cam", "installer", "cli")


def _load(modules=MODULES):
    ns = {}
    for name in modules:
        mod = importlib.import_module("klipperformac." + name)
        ns[name] = importlib.reload(mod)
    return ns


def build_install(home, data, with_data_layout=True):
    """Create a folder shaped like what `klipperformac setup` leaves behind."""
    for part in ("venv/bin", "klipper/config", "moonraker", "src", "shims",
                 "refs.d"):
        (home / part).mkdir(parents=True, exist_ok=True)
    (home / "lockfile.json").write_text(json.dumps({"ui": "fluidd"}))
    py = home / "venv" / "bin" / "python"
    py.write_text("#!/bin/sh\nexit 0\n")
    py.chmod(0o755)
    if with_data_layout:
        for part in ("config/presets", "config/history", "run", "logs",
                     "gcodes", "web/mainsail", "web/fluidd"):
            (data / part).mkdir(parents=True, exist_ok=True)
        (data / "config" / "printer.cfg").write_text("[mcu]\nserial: /dev/x\n")
        (data / "config" / "moonraker.conf").write_text(
            "[server]\nhost: 127.0.0.1\nport: 7125\n")
    return home, data


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """A throwaway app home + data folder, with the modules reloaded onto them."""
    real_home = tmp_path / "userhome"
    real_home.mkdir()
    root = tmp_path / "install"
    home = root / "home"
    data = home / "data"
    build_install(home, data)
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(home))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(data))
    monkeypatch.setenv("KLIPPERFORMAC_MOONRAKER_PORT", "17125")
    monkeypatch.setenv("KLIPPERFORMAC_WEB_PORT", "18080")
    monkeypatch.setenv("KLIPPERFORMAC_FLUIDD_PORT", "18081")
    monkeypatch.setenv("KLIPPERFORMAC_CONSOLE", str(tmp_path / "nopty"))
    ns = _load()
    # Never signal anything: this Mac has a live stack.
    monkeypatch.setattr(ns["process"], "kill_all", lambda: [])
    monkeypatch.setattr(ns["cam"], "kill_orphans", lambda: [])
    ns["home"] = home
    ns["data"] = data
    ns["root"] = root
    ns["real_home"] = real_home
    ns["monkeypatch"] = monkeypatch
    return ns


# --------------------------------------------------------------- [0] paths
def test_blank_app_home_env_is_not_the_current_directory(tmp_path, monkeypatch):
    """`export KLIPPERFORMAC_HOME=` used to mean '.' (and `uninstall` then
    rmtree'd the CWD, with the decoy files in it)."""
    monkeypatch.setenv("KLIPPERFORMAC_HOME", "")
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(tmp_path / "data"))
    paths = _load(("paths",))["paths"]
    assert paths.APP_HOME.is_absolute()
    assert paths.APP_HOME != tmp_path
    assert str(paths.APP_HOME).endswith(".klipperformac")


def test_relative_app_home_is_refused_outright(monkeypatch):
    monkeypatch.setenv("KLIPPERFORMAC_HOME", "relative/home")
    with pytest.raises(SystemExit) as caught:
        _load(("paths",))
    assert "absolute path" in str(caught.value)


def test_app_home_equal_to_home_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KLIPPERFORMAC_HOME", "~")
    with pytest.raises(SystemExit) as caught:
        _load(("paths",))
    assert "home folder" in str(caught.value)


def test_relative_data_env_is_refused_and_blank_is_ignored(monkeypatch,
                                                           tmp_path):
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", "relative/data")
    with pytest.raises(SystemExit):
        _load(("paths",))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", "   ")
    paths = _load(("paths",))["paths"]
    assert paths.DATA == tmp_path / "home" / "data"


def test_a_junk_settings_value_for_data_is_ignored(monkeypatch, tmp_path):
    """A relative or protected "data" value in settings.json (a file the GUI
    also writes) must not decide where printer.cfg lives."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"data": "relative/stuff"}))
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(home))
    monkeypatch.delenv("KLIPPERFORMAC_DATA", raising=False)
    paths = _load(("paths",))["paths"]
    assert paths.DATA == home / "data"


def test_looks_like_checks_are_the_rmtree_gate(tmp_path):
    paths = _load(("paths",))["paths"]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert paths.looks_like_app_home(empty) is False
    assert paths.looks_like_data_dir(empty) is False
    app = tmp_path / "app"
    build_install(app, app / "data")
    assert paths.looks_like_app_home(app) is True
    assert paths.looks_like_data_dir(app / "data") is True
    # our own run/cli.lock side effect must not certify a folder as installed
    partial = tmp_path / "partial"
    (partial / "data" / "run").mkdir(parents=True)
    assert paths.looks_like_app_home(partial) is False


# --------------------------------------------------------- [0] uninstall
def test_uninstall_refuses_a_folder_that_is_not_an_install(fake, monkeypatch,
                                                          capsys):
    """The [0] repro: KLIPPERFORMAC_HOME pointing at somebody else's folder.
    Its files must survive and the command must not claim success."""
    stranger = fake["root"] / "stranger"
    stranger.mkdir()
    (stranger / "irreplaceable.txt").write_text("tax return")
    monkeypatch.setenv("KLIPPERFORMAC_HOME", str(stranger))
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(stranger / "data"))
    ns = _load()
    monkeypatch.setattr(ns["process"], "kill_all", lambda: [])
    monkeypatch.setattr(ns["cam"], "kill_orphans", lambda: [])
    with pytest.raises(SystemExit) as caught:
        ns["cli"].cmd_uninstall(argparse.Namespace(data=True, keep_data=False))
    out = capsys.readouterr()
    assert (stranger / "irreplaceable.txt").is_file()
    assert "Refusing to delete" in out.out + out.err
    assert caught.value.code == 1


def test_uninstall_removes_a_genuine_install(fake, capsys):
    """The guards must not break the actual job: a real-shaped install goes
    away, data included when --data was asked for.

    A success may either return (main() then exits 0) or exit(0) itself; what
    may NOT happen is the non-zero _die() the refusal path uses."""
    try:
        fake["cli"].cmd_uninstall(argparse.Namespace(data=True,
                                                     keep_data=False))
    except SystemExit as caught:
        assert (caught.code or 0) == 0, caught.code
    out = capsys.readouterr().out
    assert "Removed app folder: " + str(fake["home"]) in out
    assert not fake["home"].exists()
    assert "Removed data folder" in out


def test_uninstall_keeps_the_data_folder_without_the_flag(fake, capsys):
    try:
        fake["cli"].cmd_uninstall(argparse.Namespace(data=False,
                                                     keep_data=True))
    except SystemExit as caught:
        assert (caught.code or 0) == 0, caught.code
    out = capsys.readouterr().out
    assert "Kept data folder" in out
    assert fake["data"].exists() or not fake["home"].exists()


def test_uninstall_refuses_a_data_folder_in_a_top_level_user_folder(fake,
                                                                   monkeypatch,
                                                                   capsys):
    """The second half of [10]: KLIPPERFORMAC_DATA=~/Documents, then uninstall
    --data would rmtree the folder that holds somebody's thesis.

    paths.py answers this when the module graph is *imported*, which is earlier
    and stronger than the rmtree gate it was written to test: no command (this
    one included) can ever run with a protected data binding, so the delete
    below it is unreachable by env. Both halves are asserted - the refusal at
    load, and the files that survived it - and the rmtree gate itself is driven
    directly afterwards with the binding forced after import."""
    docs = fake["real_home"] / "Documents"
    (docs / "lifework").mkdir(parents=True)
    (docs / "lifework" / "thesis.tex").write_text("\\documentclass")
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(docs))
    with pytest.raises(SystemExit) as refused:
        _load()
    assert (docs / "lifework" / "thesis.tex").is_file(), "Documents was wiped"
    assert "too important a folder" in str(refused.value)

    # ...and the delete itself still refuses when something else (a hand
    # patched DATA, a future code path) hands it a protected folder anyway.
    monkeypatch.setenv("KLIPPERFORMAC_DATA", str(fake["data"]))
    ns = _load()
    monkeypatch.setattr(ns["process"], "kill_all", lambda: [])
    monkeypatch.setattr(ns["cam"], "kill_orphans", lambda: [])
    ns["paths"].DATA = docs
    with pytest.raises(SystemExit) as caught:
        ns["cli"].cmd_uninstall(argparse.Namespace(data=True, keep_data=False))
    out = capsys.readouterr()
    assert (docs / "lifework" / "thesis.tex").is_file(), "Documents was wiped"
    assert "Refusing to delete" in out.out + out.err
    assert caught.value.code == 1


# ------------------------------------------------------------- [10] data
def test_data_set_refuses_a_top_level_user_folder(fake, capsys):
    docs = fake["real_home"] / "Documents"
    docs.mkdir()
    with pytest.raises(SystemExit) as caught:
        fake["cli"].cmd_data(argparse.Namespace(set=str(docs)))
    out = capsys.readouterr().out
    assert "Refusing to use" in out and str(docs) in out
    assert caught.value.code == 1
    assert not docs.exists() or not (docs / "config").exists(), \
        "the refused folder was still turned into a data tree"


def test_data_set_refuses_a_foreign_non_empty_folder(fake, capsys):
    """[10] repro: a throwaway stand-in for ~/Documents holding one file."""
    foreign = fake["root"] / "fake-Documents"
    foreign.mkdir()
    (foreign / "irreplaceable.txt").write_text("tax return")
    with pytest.raises(SystemExit) as caught:
        fake["cli"].cmd_data(argparse.Namespace(set=str(foreign)))
    out = capsys.readouterr().out
    assert "does not look like a Klipper-for-Mac data folder" in out
    assert caught.value.code == 1
    assert (foreign / "irreplaceable.txt").is_file()
    assert sorted(p.name for p in foreign.iterdir()) == ["irreplaceable.txt"], \
        "the stack's folders were created inside the refused target"
    assert fake["data"].is_dir(), "the real data folder moved anyway"


def test_data_set_still_accepts_an_empty_folder_and_an_old_data_tree(fake,
                                                                    capsys):
    """The guards must not break relocation, which is the feature."""
    elsewhere = fake["root"] / "BigDrive" / "klipper-data"
    fake["cli"].cmd_data(argparse.Namespace(set=str(elsewhere)))
    out = capsys.readouterr().out
    assert "Data folder: " + str(elsewhere.resolve()) in out
    assert (elsewhere / "config" / "printer.cfg").is_file(), "printer.cfg moved"
    assert not (fake["data"] / "config" / "printer.cfg").exists()
    # and moving into another real data folder is allowed
    back = fake["root"] / "BigDrive" / "other"
    for part in ("config/presets", "run", "logs", "gcodes"):
        (back / part).mkdir(parents=True, exist_ok=True)
    (back / "config" / "printer.cfg").write_text("[mcu]\nserial: /dev/other\n")
    fake["cli"].cmd_data(argparse.Namespace(set=str(back)))
    assert "already holds a Klipper-for-Mac data folder" in \
        capsys.readouterr().out


def test_data_set_refuses_a_relative_path_with_a_nonzero_exit(fake, capsys):
    with pytest.raises(SystemExit) as caught:
        fake["cli"].cmd_data(argparse.Namespace(set="some/relative/path"))
    assert "Refusing to use the relative path" in capsys.readouterr().out
    assert caught.value.code == 1
